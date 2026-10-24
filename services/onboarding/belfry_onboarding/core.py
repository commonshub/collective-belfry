"""Core operations: activate, revoke, reactivate, reconcile, expire.

Every operation is idempotent and resumable: it re-reads state from Odoo, re-runs the external
steps (which are idempotent on their side: Authentik lookup before create, Keycast preload keyed
on ``vine_id``, status PUTs) and writes back to Odoo last. All state lives in Odoo.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import httpx

from .clients.authentik import AuthentikClient
from .clients.issuer import IssuerClient
from .clients.keycast import KeycastClient
from .clients.odoo import OdooApi, OdooError
from .clients.verifier import VerifierClient
from .nostr import hex_to_npub
from .relay import RelayPublisher
from .relay_whitelist import RelayWhitelist
from .settings import Settings

log = logging.getLogger(__name__)

PAID_STATES = ("paid", "in_payment")
TIER_DEFAULT_DAYS = {"day": 1, "week": 7, "full": 0}

PASS_FIELDS = [
    "partner_id",
    "tier",
    "product_id",
    "invoice_id",
    "transaction_id",
    "stripe_payment_intent",
    "date_start",
    "date_end",
    "state",
    "npub",
    "keycast_pubkey",
    "authentik_user_id",
    "stripe_dispute_id",
    "claim_token",
    "credential_status_index",
]


@dataclass
class Services:
    odoo: OdooApi
    authentik: AuthentikClient
    keycast: KeycastClient
    settings: Settings
    relay: RelayPublisher | None = None
    whitelist: RelayWhitelist | None = None
    # Phase 3
    issuer: IssuerClient | None = None
    verifier: VerifierClient | None = None
    issuer_jwk: dict[str, Any] = field(default_factory=dict)  # private JWK, signs status lists
    issuer_x5c: list[str] = field(default_factory=list)
    bridge_jwk: dict[str, Any] = field(default_factory=dict)  # private JWK, signs ID tokens


# -- small helpers ------------------------------------------------------------------------------


def m2o_id(value: Any) -> int | None:
    """Odoo returns many2one fields as ``[id, display_name]`` or ``False``."""
    if isinstance(value, list | tuple):
        return int(value[0]) if value else None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def odoo_date(value: Any) -> dt.date | None:
    """Parse an Odoo Date (``YYYY-MM-DD``) or Datetime (``YYYY-MM-DD HH:MM:SS``) string."""
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def now_utc_str() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")


def today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def member_number_fallback(partner_id: int) -> str:
    return f"OSV-{partner_id:04d}"


# -- activation ---------------------------------------------------------------------------------


def _find_paid_transaction(svc: Services, invoice: dict[str, Any]) -> dict[str, Any] | None:
    """The ``done`` non-refund transaction with a provider reference, or None."""
    tx_ids = [i for i in (invoice.get("transaction_ids") or []) if isinstance(i, int)]
    if not tx_ids:
        return None
    rows = svc.odoo.search_read(
        "payment.transaction",
        [
            ["id", "in", tx_ids],
            ["state", "=", "done"],
            ["operation", "!=", "refund"],
            ["provider_reference", "!=", False],
        ],
        ["provider_reference", "last_state_change", "operation", "state"],
        order="id desc",
    )
    rows = [r for r in rows if r.get("provider_reference")]
    return rows[0] if rows else None


def _tier_from_invoice(svc: Services, invoice_id: int) -> tuple[str | None, int | None, int | None]:
    """(tier, duration_days, product_id) from the first invoice line with a pass product."""
    lines = svc.odoo.search_read(
        "account.move.line",
        [["move_id", "=", invoice_id], ["product_id", "!=", False]],
        ["product_id"],
    )
    product_ids = [m2o_id(line.get("product_id")) for line in lines]
    product_ids = [p for p in product_ids if p]
    if not product_ids:
        return None, None, None
    products = svc.odoo.search_read(
        "product.product",
        [["id", "in", product_ids], ["belfry_tier", "!=", False]],
        ["belfry_tier", "belfry_duration_days"],
    )
    for product in products:
        if product.get("belfry_tier"):
            days = product.get("belfry_duration_days")
            return (
                product["belfry_tier"],
                (int(days) if days not in (None, False) else None),
                product["id"],
            )
    return None, None, None


def _steward_marked_paid(svc: Services, invoice_id: int) -> str | None:
    """The steward who registered this invoice's payment by hand on the steward pages
    (belfry.pass.manual_paid_by), or None. Such a payment counts like a Stripe transaction: it
    is attributed to a signed-in steward, unlike a manual payment made anywhere in Odoo."""
    rows = svc.odoo.search_read(
        "belfry.pass", [["invoice_id", "=", invoice_id]], ["manual_paid_by"], limit=1
    )
    return (rows[0].get("manual_paid_by") or None) if rows else None


def _find_or_create_pass(svc: Services, invoice: dict[str, Any]) -> dict[str, Any]:
    invoice_id = invoice["id"]
    rows = svc.odoo.search_read(
        "belfry.pass", [["invoice_id", "=", invoice_id]], PASS_FIELDS, limit=1
    )
    if rows:
        return rows[0]
    # Prefer the module's own method (it knows the product and invite); fall back for older modules.
    try:
        svc.odoo.call("belfry.pass", "belfry_find_or_create_for_invoice", [invoice_id])
    except OdooError as exc:
        if not exc.is_missing_method:
            raise
        log.warning("belfry.pass.belfry_find_or_create_for_invoice missing, creating pass locally")
        tier, _days, product_id = _tier_from_invoice(svc, invoice_id)
        if tier is None:
            raise RuntimeError(f"invoice {invoice_id} has no pass product line") from exc
        vals: dict[str, Any] = {
            "partner_id": m2o_id(invoice["partner_id"]),
            "tier": tier,
            "invoice_id": invoice_id,
            "state": "pending",
        }
        if product_id:
            vals["product_id"] = product_id
        try:
            svc.odoo.create("belfry.pass", vals)
        except OdooError as exc2:
            if not exc2.is_constraint_violation:
                raise
            log.info("belfry.pass for invoice %s was created concurrently", invoice_id)
    rows = svc.odoo.search_read(
        "belfry.pass", [["invoice_id", "=", invoice_id]], PASS_FIELDS, limit=1
    )
    if not rows:
        raise RuntimeError(f"belfry.pass for invoice {invoice_id} could not be created")
    return rows[0]


def _ensure_member_number(svc: Services, partner: dict[str, Any]) -> str:
    if partner.get("belfry_member_number"):
        return partner["belfry_member_number"]
    partner_id = partner["id"]
    try:
        svc.odoo.call("res.partner", "belfry_ensure_member_number", [[partner_id]])
    except OdooError as exc:
        if not exc.is_missing_method:
            raise
        log.warning("res.partner.belfry_ensure_member_number missing, using OSV-<partner id>")
        svc.odoo.write(
            "res.partner",
            [partner_id],
            {"belfry_member_number": member_number_fallback(partner_id)},
        )
    rows = svc.odoo.read("res.partner", [partner_id], ["belfry_member_number"])
    number = rows[0].get("belfry_member_number") if rows else None
    if not number:
        number = member_number_fallback(partner_id)
        svc.odoo.write("res.partner", [partner_id], {"belfry_member_number": number})
    return number


def _pass_dates(
    settings: Settings, tier: str, duration_days: int | None, payment_date: dt.date
) -> tuple[dt.date, dt.date]:
    date_start = max(payment_date, settings.event_start)
    days = duration_days if duration_days is not None else TIER_DEFAULT_DAYS.get(tier, 0)
    if tier == "full" or days <= 0:
        date_end = settings.event_end
    else:
        date_end = date_start + dt.timedelta(days=days)
    return date_start, date_end


def _ensure_authentik_user(svc: Services, partner: dict[str, Any], member_number: str) -> int:
    ak = svc.authentik
    email = partner.get("email") or None
    users = ak.find_users_by_attribute("belfry_member_number", member_number)
    if not users and email:
        users = ak.find_users_by_email(email)
    if users:
        user = users[0]
    else:
        user = ak.create_user(
            username=member_number.lower(),
            name=partner.get("name") or member_number,
            email=email,
            attributes={"belfry_member_number": member_number, "odoo_partner_id": partner["id"]},
            path=svc.settings.authentik_user_path,
        )
    user_pk = int(user["pk"])
    slug = svc.settings.authentik_source_slug
    # Pre-linking the pass-bridge source is best effort: authentik 2026.8.5 does not let the API
    # create UserOAuthSourceConnection rows (``user`` is read-only, see infra/README.md), so the
    # participant links the pass once while signed in. A failure here must not block activation.
    try:
        if not ak.find_source_connections(user_pk, slug):
            ak.create_source_connection(user_pk, slug, member_number)
    except httpx.HTTPStatusError as exc:
        log.warning(
            "authentik: could not pre-link source %s for user %s (%s); link must be made by the "
            "participant from user settings",
            slug,
            user_pk,
            exc.response.status_code,
        )
    return user_pk


def _set_participant(svc: Services, user_pk: int, member: bool) -> None:
    """Add the Authentik user to (or remove it from) the participants group, which gates the
    Keycast login (blueprint belfry-keycast-provider). Idempotent; raises if the group is
    missing, so a retry (webhook replay, reconcile) completes the job once the blueprint ran."""
    name = svc.settings.authentik_participants_group
    group = svc.authentik.find_group(name)
    if group is None:
        raise RuntimeError(f"authentik group {name!r} does not exist (blueprint not applied?)")
    if member:
        svc.authentik.add_user_to_group(group["pk"], user_pk)
    else:
        svc.authentik.remove_user_from_group(group["pk"], user_pk)


def sync_participants(svc: Services, strict: bool = False) -> dict[str, Any] | None:
    """Make the participants group equal to the Authentik users of all active passes.

    Run at startup and by reconcile, so passes activated before the group existed (or a missed
    group call) converge. Best effort unless ``strict``."""
    name = svc.settings.authentik_participants_group
    try:
        group = svc.authentik.find_group(name)
        if group is None:
            raise RuntimeError(f"authentik group {name!r} does not exist (blueprint not applied?)")
        rows = svc.odoo.search_read(
            "belfry.pass",
            [["state", "=", "active"], ["authentik_user_id", "!=", False]],
            ["authentik_user_id"],
        )
        wanted = {int(r["authentik_user_id"]) for r in rows if r.get("authentik_user_id")}
        current = svc.authentik.group_member_pks(name)
        for user_pk in sorted(wanted - current):
            svc.authentik.add_user_to_group(group["pk"], user_pk)
        for user_pk in sorted(current - wanted):
            svc.authentik.remove_user_from_group(group["pk"], user_pk)
        result = {
            "members": len(wanted),
            "added": sorted(wanted - current),
            "removed": sorted(current - wanted),
        }
        if result["added"] or result["removed"]:
            log.info("participants group %s synced: %s", name, result)
        return result
    except Exception as exc:
        if strict:
            raise
        log.warning("participants group not synced: %s", exc)
        return None


def _login_link(svc: Services, user_pk: int) -> tuple[str, str]:
    """A one-time sign-in link for the Authentik user, and its expiry (Odoo datetime).

    authentik plans the default brand's recovery flow (blueprint belfry-account-setup-flow) for
    the user and wraps it in a flow token; opening the link signs the browser in (participants
    have no password; later sign-ins use the login page's magic link or the pass). The link is
    built from the host the service called, so
    its origin is replaced with AUTHENTIK_PUBLIC_URL. A new link replaces the user's earlier
    token. Raises when the brand has no recovery flow (blueprint not applied yet), so the caller
    stays pending and a retry completes the job, like the participants group.
    """
    days = svc.settings.login_link_days
    link = svc.authentik.create_recovery_link(user_pk, f"days={days}")
    given = urllib.parse.urlsplit(link)
    public = urllib.parse.urlsplit(svc.settings.authentik_public_url.rstrip("/"))
    url = urllib.parse.urlunsplit(
        (public.scheme, public.netloc, public.path.rstrip("/") + given.path, given.query, "")
    )
    expires = dt.datetime.now(dt.UTC) + dt.timedelta(days=days)
    return url, expires.strftime("%Y-%m-%d %H:%M:%S")


def send_login_link(svc: Services, pass_id: int) -> str:
    """Mint a new one-time sign-in link for the holder of an active pass and store it on the pass.
    Odoo mails it: ``belfry.pass.write`` sends the activation template whenever the link changes
    on an active pass. Returns ``sent``, ``not_active``, ``no_user`` or ``not_found``."""
    pass_row = svc.odoo.read_one("belfry.pass", pass_id, PASS_FIELDS)
    if pass_row is None:
        return "not_found"
    if pass_row.get("state") != "active":
        log.info("send_login_link: pass %s is %s, no link", pass_id, pass_row.get("state"))
        return "not_active"
    user_pk = _authentik_user_for(svc, pass_row)
    if user_pk is None:
        log.warning("send_login_link: pass %s has no Authentik user", pass_id)
        return "no_user"
    url, expires = _login_link(svc, user_pk)
    svc.odoo.write(
        "belfry.pass", [pass_id], {"login_setup_url": url, "login_setup_expires_at": expires}
    )
    log.info("send_login_link: new link for pass %s (Authentik user %s)", pass_id, user_pk)
    return "sent"


def _ensure_keycast_user(
    svc: Services, partner: dict[str, Any], member_number: str
) -> tuple[str, str, str]:
    """Returns (pubkey hex, npub, user UCAN). The UCAN is used once for the profile, not stored."""
    result = svc.keycast.preload_user(
        vine_id=f"odoo-partner-{partner['id']}",
        username=member_number.lower(),
        display_name=partner.get("name") or member_number,
    )
    pubkey = result["pubkey"]
    svc.keycast.set_user_status(pubkey, "active")
    return pubkey, hex_to_npub(pubkey), result.get("token", "")


def active_pubkeys(svc: Services) -> list[str]:
    """Keycast pubkeys of every active pass: the relay whitelist, derived from Odoo."""
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "!=", False]],
        ["keycast_pubkey"],
    )
    return [r["keycast_pubkey"] for r in rows if r.get("keycast_pubkey")]


def sync_relay_whitelist(svc: Services, include: list[str] = (), strict: bool = False):
    """Rewrite the relay config with the active pubkeys plus ``include`` (a pass being activated
    whose Odoo state is still pending). Best effort unless ``strict``: a failure is logged and
    the next sync (any later activation, revoke, expiry or the cron) catches up.
    """
    if svc.whitelist is None:
        return None
    try:
        return svc.whitelist.sync([*active_pubkeys(svc), *include])
    except Exception as exc:
        if strict:
            raise
        log.warning("relay whitelist not updated: %s", exc)
        return None


def profile_content(name: str, member_number: str, nip05_domain: str) -> dict[str, str]:
    """What the directory may see without opt-in (decided 2026-10-22): name and member number."""
    return {
        "name": name,
        "display_name": name,
        "nip05": f"{member_number.lower()}@{nip05_domain}",
    }


PROFILE_KIND = 0
FOLLOWS_KIND = 3
RELAYS_KIND = 10002
DM_RELAYS_KIND = 10050


def starter_events(
    name: str, member_number: str, nip05_domain: str, relay_url: str
) -> dict[int, dict[str, Any]]:
    """The unsigned events a new participant starts with, by kind: the profile, an empty follow
    list, the relay list and the NIP-17 DM relay list, both naming the community relay. Clients
    such as Coracle wait for profile, relays and follows after sign-in and otherwise show "We're
    having a hard time finding your profile"; NIP-17 senders deliver to the 10050 relays."""
    now = int(time.time())
    content = profile_content(name, member_number, nip05_domain)
    return {
        PROFILE_KIND: {
            "kind": PROFILE_KIND,
            "created_at": now,
            "tags": [],
            "content": json.dumps(content, separators=(",", ":")),
        },
        FOLLOWS_KIND: {"kind": FOLLOWS_KIND, "created_at": now, "tags": [], "content": ""},
        RELAYS_KIND: {
            "kind": RELAYS_KIND,
            "created_at": now,
            "tags": [["r", relay_url]],
            "content": "",
        },
        DM_RELAYS_KIND: {
            "kind": DM_RELAYS_KIND,
            "created_at": now,
            "tags": [["relay", relay_url]],
            "content": "",
        },
    }


def publish_profile(
    svc: Services, token: str, pubkey: str, partner: dict[str, Any], member_number: str
) -> list[int]:
    """Publish the participant's starter events (``starter_events``) to the community relay,
    only the kinds the relay does not have yet for this key: a participant's own profile, follows
    or relay choices made in an app are never overwritten (reactivation, backfill). Best effort;
    returns the kinds published."""
    if svc.relay is None or not token:
        return []
    name = partner.get("name") or member_number
    relay_url = svc.settings.community_relay_public_url or svc.relay.url
    wanted = starter_events(name, member_number, svc.settings.nip05_domain, relay_url)

    def sign(unsigned: dict[str, Any]) -> dict[str, Any]:
        return svc.keycast.sign_event(token, {"pubkey": pubkey, **unsigned})

    published: list[int] = []
    try:
        existing = svc.relay.query([{"kinds": sorted(wanted), "authors": [pubkey]}], sign)
        have = {e.get("kind") for e in existing if e.get("pubkey") == pubkey}
        for kind, unsigned in wanted.items():
            if kind in have:
                continue
            svc.relay.publish(sign(unsigned), sign)
            published.append(kind)
    except Exception as exc:
        log.warning(
            "starter events for %s not (all) published to %s: %s", member_number, svc.relay.url, exc
        )
    return published


def publish_starter_events(svc: Services) -> dict[str, Any]:
    """Backfill ``starter_events`` for every active pass (participants activated before the relay
    lists existed). Keycast's preload is idempotent per partner and hands out a fresh UCAN."""
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "!=", False]],
        ["partner_id", "keycast_pubkey"],
    )
    done: dict[str, list[int]] = {}
    for row in rows:
        partner_id = m2o_id(row.get("partner_id"))
        if not partner_id:
            continue
        partner = svc.odoo.read_one("res.partner", partner_id, ["name", "belfry_member_number"])
        member_number = (partner or {}).get("belfry_member_number") or ""
        if not member_number:
            continue
        pubkey, _npub, token = _ensure_keycast_user(svc, partner, member_number)
        if pubkey != row["keycast_pubkey"]:
            log.warning("pass of %s: Keycast pubkey differs from Odoo's, skipped", member_number)
            continue
        done[member_number] = publish_profile(
            svc, token, pubkey, {"id": partner_id, **partner}, member_number
        )
    return {"published": done}


_activation_locks: dict[int, threading.Lock] = {}
_activation_locks_guard = threading.Lock()


def activate_pass(svc: Services, invoice_id: int) -> str:
    """Activate the pass of a paid invoice. Returns a short result code.

    Serialized per invoice: a steward's "Mark paid" and Odoo's invoice-paid webhook arrive at the
    same moment, and two concurrent runs would both see the pass pending (two sign-in links, two
    activation mails)."""
    with _activation_locks_guard:
        lock = _activation_locks.setdefault(invoice_id, threading.Lock())
    with lock:
        return _activate_pass(svc, invoice_id)


def _activate_pass(svc: Services, invoice_id: int) -> str:
    invoice = svc.odoo.read_one(
        "account.move",
        invoice_id,
        [
            "name",
            "move_type",
            "state",
            "payment_state",
            "partner_id",
            "invoice_date",
            "transaction_ids",
        ],
    )
    if invoice is None:
        log.warning("activate_pass: invoice %s not found", invoice_id)
        return "not_found"
    if (
        invoice.get("move_type") != "out_invoice"
        or invoice.get("state") != "posted"
        or invoice.get("payment_state") not in PAID_STATES
    ):
        log.info(
            "activate_pass: invoice %s not paid (type=%s state=%s payment_state=%s)",
            invoice_id,
            invoice.get("move_type"),
            invoice.get("state"),
            invoice.get("payment_state"),
        )
        return "not_paid"

    tx = _find_paid_transaction(svc, invoice)
    if tx is None:
        has_transactions = bool(invoice.get("transaction_ids"))
        steward = _steward_marked_paid(svc, invoice_id)
        if steward:
            log.info(
                "activate_pass: invoice %s paid by hand, marked by steward %s", invoice_id, steward
            )
        elif has_transactions or not svc.settings.allow_manual_payments:
            log.info(
                "activate_pass: invoice %s has no done transaction with a provider reference",
                invoice_id,
            )
            return "not_paid"
        else:
            log.info(
                "activate_pass: invoice %s paid without a transaction (manual payment accepted)",
                invoice_id,
            )

    pass_row = _find_or_create_pass(svc, invoice)
    state = pass_row.get("state")
    if state == "active":
        return "already_active"
    if state in ("revoked", "expired"):
        log.info("activate_pass: pass %s is %s, not reactivating", pass_row["id"], state)
        return f"already_{state}"

    partner_id = m2o_id(pass_row.get("partner_id")) or m2o_id(invoice.get("partner_id"))
    if partner_id is None:
        raise RuntimeError(f"pass {pass_row['id']} has no partner")
    partner = svc.odoo.read(
        "res.partner",
        [partner_id],
        ["name", "email", "belfry_member_number", "belfry_authentik_user_id", "belfry_npub"],
    )[0]
    member_number = _ensure_member_number(svc, partner)

    tier = pass_row.get("tier")
    duration_days: int | None = None
    if not tier:
        tier, duration_days, _product = _tier_from_invoice(svc, invoice_id)
        if not tier:
            raise RuntimeError(
                f"pass {pass_row['id']} has no tier and the invoice has no pass product"
            )
    else:
        product_id = m2o_id(pass_row.get("product_id"))
        if product_id:
            products = svc.odoo.read("product.product", [product_id], ["belfry_duration_days"])
            days = products[0].get("belfry_duration_days") if products else None
            duration_days = int(days) if days not in (None, False) else None

    # Payment date: the done transaction's last_state_change, else the invoice date, else today.
    payment_date = (
        (odoo_date(tx.get("last_state_change")) if tx else None)
        or odoo_date(invoice.get("invoice_date"))
        or today()
    )
    date_start, date_end = _pass_dates(svc.settings, tier, duration_days, payment_date)

    authentik_user_id = _ensure_authentik_user(svc, partner, member_number)
    pubkey, npub, user_token = _ensure_keycast_user(svc, partner, member_number)
    # Lets the participant sign in to Keycast through Authentik (the Keycast account exists now).
    _set_participant(svc, authentik_user_id, True)
    # Participants never get a password: Odoo mails this one-time sign-in link with the activation.
    login_url, login_expires = _login_link(svc, authentik_user_id)
    # The relay only accepts whitelisted pubkeys, so the whitelist must carry the new key before
    # the profile is published (the pass is still pending in Odoo at this point).
    sync_relay_whitelist(svc, include=[pubkey])
    publish_profile(svc, user_token, pubkey, partner, member_number)

    svc.odoo.write(
        "res.partner",
        [partner_id],
        {"belfry_authentik_user_id": authentik_user_id, "belfry_npub": npub},
    )
    vals: dict[str, Any] = {
        "npub": npub,
        "keycast_pubkey": pubkey,
        "authentik_user_id": authentik_user_id,
        "date_start": date_start.isoformat(),
        "date_end": date_end.isoformat(),
        "activated_at": now_utc_str(),
        "login_setup_url": login_url,
        "login_setup_expires_at": login_expires,
        # Phase 3: the claim page (/claim/<token>) mints the wallet offer; the status list bit
        # of this pass is its id (one list, one bit per pass).
        "claim_token": pass_row.get("claim_token") or secrets.token_urlsafe(24),
        "credential_status_index": pass_row["id"],
        "state": "active",
    }
    if not pass_row.get("tier"):
        vals["tier"] = tier
    if tx:
        vals["transaction_id"] = tx["id"]
        vals["stripe_payment_intent"] = tx["provider_reference"]
    svc.odoo.write("belfry.pass", [pass_row["id"]], vals)
    log.info(
        "activate_pass: pass %s active for partner %s (%s), npub %s, %s..%s",
        pass_row["id"],
        partner_id,
        member_number,
        npub,
        date_start,
        date_end,
    )
    return "activated"


# -- revocation, expiry, reactivation -----------------------------------------------------------


def _partner_has_other_active_pass(svc: Services, partner_id: int, except_pass_id: int) -> bool:
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["partner_id", "=", partner_id], ["state", "=", "active"], ["id", "!=", except_pass_id]],
        ["id"],
        limit=1,
    )
    return bool(rows)


def _keycast_pubkey_for(svc: Services, pass_row: dict[str, Any]) -> str | None:
    pubkey = pass_row.get("keycast_pubkey")
    if pubkey:
        return pubkey
    partner_id = m2o_id(pass_row.get("partner_id"))
    if partner_id is None:
        return None
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["partner_id", "=", partner_id], ["keycast_pubkey", "!=", False]],
        ["keycast_pubkey"],
        limit=1,
        order="id desc",
    )
    return rows[0]["keycast_pubkey"] if rows else None


def _authentik_user_for(svc: Services, pass_row: dict[str, Any]) -> int | None:
    user_id = pass_row.get("authentik_user_id")
    if user_id:
        return int(user_id)
    partner_id = m2o_id(pass_row.get("partner_id"))
    if partner_id is None:
        return None
    partner = svc.odoo.read_one("res.partner", partner_id, ["belfry_authentik_user_id"])
    value = partner.get("belfry_authentik_user_id") if partner else None
    return int(value) if value else None


def _authentik_user_has_other_active_pass(svc: Services, user_pk: int, except_pass_id: int) -> bool:
    # Two partners can share one Authentik user (matched by e-mail), so the group is decided per
    # Authentik user, not per partner.
    rows = svc.odoo.search_read(
        "belfry.pass",
        [
            ["authentik_user_id", "=", user_pk],
            ["state", "=", "active"],
            ["id", "!=", except_pass_id],
        ],
        ["id"],
        limit=1,
    )
    return bool(rows)


def _suspend_unless_other_active(svc: Services, pass_row: dict[str, Any], reason: str) -> bool:
    """Suspend the Keycast user unless the partner has another active pass, and drop the
    Authentik user from the participants group unless it holds another active pass.

    Returns True if the Keycast suspension was sent.
    """
    suspended = False
    partner_id = m2o_id(pass_row.get("partner_id"))
    if partner_id is None or not _partner_has_other_active_pass(svc, partner_id, pass_row["id"]):
        pubkey = _keycast_pubkey_for(svc, pass_row)
        if pubkey:
            svc.keycast.set_user_status(
                pubkey, "suspended", reason=f"pass {pass_row['id']}: {reason}"
            )
            suspended = True
        else:
            log.info("pass %s has no Keycast pubkey, nothing to suspend", pass_row["id"])
    user_pk = _authentik_user_for(svc, pass_row)
    if user_pk is not None and not _authentik_user_has_other_active_pass(
        svc, user_pk, pass_row["id"]
    ):
        _set_participant(svc, user_pk, False)
    return suspended


def _close_pass(svc: Services, pass_row: dict[str, Any], state: str, reason: str) -> str:
    if pass_row.get("state") != state:
        svc.odoo.write(
            "belfry.pass",
            [pass_row["id"]],
            {"state": state, "revoked_at": now_utc_str(), "revoke_reason": reason},
        )
        result = state
    else:
        result = f"already_{state}"
    pass_row = dict(pass_row, state=state)
    # Relay access first (derived from the state just written), then the signer: a Keycast
    # failure must not leave the pubkey on the relay whitelist.
    sync_relay_whitelist(svc)
    # Always re-evaluate the suspension so a retry after a failed Keycast call completes the job.
    _suspend_unless_other_active(svc, pass_row, reason)
    return result


def revoke_pass(svc: Services, pass_id: int, reason: str) -> str:
    pass_row = svc.odoo.read_one("belfry.pass", pass_id, PASS_FIELDS)
    if pass_row is None:
        return "not_found"
    return _close_pass(svc, pass_row, "revoked", reason)


def revoke_for_refund(svc: Services, transaction_id: int) -> str:
    tx = svc.odoo.read_one(
        "payment.transaction", transaction_id, ["operation", "state", "source_transaction_id"]
    )
    if tx is None:
        return "not_found"
    if tx.get("operation") != "refund" or tx.get("state") != "done":
        log.info("revoke_for_refund: transaction %s is not a done refund", transaction_id)
        return "not_refund"
    source_id = m2o_id(tx.get("source_transaction_id"))
    if source_id is None:
        log.warning("revoke_for_refund: refund %s has no source transaction", transaction_id)
        return "no_source"
    rows = svc.odoo.search_read(
        "belfry.pass", [["transaction_id", "=", source_id]], ["id"], limit=1
    )
    if not rows:
        log.warning("revoke_for_refund: no pass for source transaction %s", source_id)
        return "no_pass"
    return revoke_pass(svc, rows[0]["id"], "refund")


def reactivate_pass(svc: Services, pass_id: int) -> str:
    pass_row = svc.odoo.read_one("belfry.pass", pass_id, PASS_FIELDS)
    if pass_row is None:
        return "not_found"
    date_end = odoo_date(pass_row.get("date_end"))
    if date_end is not None and date_end < today():
        log.info("reactivate_pass: pass %s ended on %s, leaving it closed", pass_id, date_end)
        return "expired"
    if pass_row.get("state") != "active":
        svc.odoo.write(
            "belfry.pass",
            [pass_id],
            {"state": "active", "revoked_at": False, "revoke_reason": False},
        )
        result = "reactivated"
    else:
        result = "already_active"
    pubkey = _keycast_pubkey_for(svc, pass_row)
    if pubkey:
        svc.keycast.set_user_status(pubkey, "active")
    user_pk = _authentik_user_for(svc, pass_row)
    if user_pk is not None:
        _set_participant(svc, user_pk, True)
    sync_relay_whitelist(svc)
    return result


def expire(svc: Services) -> dict[str, Any]:
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["date_end", "<", today().isoformat()]],
        PASS_FIELDS,
    )
    expired: list[int] = []
    for pass_row in rows:
        try:
            _close_pass(svc, pass_row, "expired", "expiry")
            expired.append(pass_row["id"])
        except Exception:
            log.exception("expire: pass %s failed", pass_row["id"])
    return {"expired": expired}


def reconcile(svc: Services) -> dict[str, Any]:
    cutoff = (today() - dt.timedelta(days=svc.settings.reconcile_days)).isoformat()
    invoices = svc.odoo.search_read(
        "account.move",
        [
            ["move_type", "=", "out_invoice"],
            ["state", "=", "posted"],
            ["payment_state", "in", list(PAID_STATES)],
            ["invoice_date", ">=", cutoff],
        ],
        ["id"],
    )
    invoice_ids = [inv["id"] for inv in invoices]
    active = (
        svc.odoo.search_read(
            "belfry.pass",
            [["invoice_id", "in", invoice_ids], ["state", "in", ["active", "revoked", "expired"]]],
            ["invoice_id"],
        )
        if invoice_ids
        else []
    )
    settled = {m2o_id(p["invoice_id"]) for p in active}
    activated: dict[int, str] = {}
    for invoice_id in invoice_ids:
        if invoice_id in settled:
            continue
        try:
            activated[invoice_id] = activate_pass(svc, invoice_id)
        except Exception:
            log.exception("reconcile: activate_pass(%s) failed", invoice_id)
            activated[invoice_id] = "error"

    refunds = svc.odoo.search_read(
        "payment.transaction",
        [["operation", "=", "refund"], ["state", "=", "done"]],
        ["source_transaction_id"],
    )
    source_ids = sorted({s for s in (m2o_id(r.get("source_transaction_id")) for r in refunds) if s})
    revoked: dict[int, str] = {}
    if source_ids:
        passes = svc.odoo.search_read(
            "belfry.pass",
            [["transaction_id", "in", source_ids], ["state", "=", "active"]],
            ["id"],
        )
        for pass_row in passes:
            try:
                revoked[pass_row["id"]] = revoke_pass(svc, pass_row["id"], "refund")
            except Exception:
                log.exception("reconcile: revoke_pass(%s) failed", pass_row["id"])
                revoked[pass_row["id"]] = "error"
    return {"activated": activated, "revoked": revoked, "participants": sync_participants(svc)}


# -- Stripe disputes ----------------------------------------------------------------------------


def find_pass_by_payment_intent(svc: Services, payment_intent: str) -> dict[str, Any] | None:
    rows = svc.odoo.search_read(
        "belfry.pass", [["stripe_payment_intent", "=", payment_intent]], PASS_FIELDS, limit=1
    )
    return rows[0] if rows else None


def handle_dispute_created(svc: Services, dispute: dict[str, Any]) -> tuple[str, int | None]:
    payment_intent = dispute.get("payment_intent")
    if isinstance(payment_intent, dict):
        payment_intent = payment_intent.get("id")
    if not payment_intent:
        return "no_payment_intent", None
    pass_row = find_pass_by_payment_intent(svc, payment_intent)
    if pass_row is None:
        return "no_pass", None
    if dispute.get("id"):
        svc.odoo.write("belfry.pass", [pass_row["id"]], {"stripe_dispute_id": dispute["id"]})
    return revoke_pass(svc, pass_row["id"], "chargeback"), pass_row["id"]


def handle_dispute_closed(svc: Services, dispute: dict[str, Any]) -> tuple[str, int | None]:
    if dispute.get("status") != "won":
        return f"ignored_status_{dispute.get('status')}", None
    payment_intent = dispute.get("payment_intent")
    if isinstance(payment_intent, dict):
        payment_intent = payment_intent.get("id")
    pass_row = find_pass_by_payment_intent(svc, payment_intent) if payment_intent else None
    if pass_row is None and dispute.get("id"):
        rows = svc.odoo.search_read(
            "belfry.pass", [["stripe_dispute_id", "=", dispute["id"]]], PASS_FIELDS, limit=1
        )
        pass_row = rows[0] if rows else None
    if pass_row is None:
        return "no_pass", None
    return reactivate_pass(svc, pass_row["id"]), pass_row["id"]
