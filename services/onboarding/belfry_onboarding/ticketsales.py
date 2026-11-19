"""Event tickets sold through the checkout (docs/sales.md §10, docs/checkin.md "Ticket prices").

A ticket type with a price (a kind 30402 of an event, events.py) is bought with
``POST /api/v1/checkout {"ticket": "<its d tag>", …}`` and paid like a product: by card
(Stripe) or against an invoice (Odoo). The order is an ordinary sales order (sales.py: states,
holds, the tick, testers' test orders); what differs is here:

- the price is the ticket's own, now (``events.price_of``), with its ``vat`` tag;
- the places are the ticket's ``quantity``, less the people who hold it and the unpaid orders
  that hold one;
- a paid order opens **no membership pass**: the buyer is admitted to the event, the way a host
  approving their application admits them (the event group's 9000 with ``["paid", "paid"]`` and
  the ``ticket`` tag, by the groups key). Someone with no account gets what a guest of the
  event gets (a contact, a guest pass of the event's group, an account in the signer), with
  the checkout's rule for the address: unproven until confirmed.

Off until an admin turns it on in the console (setting ``tickets``); a tester's test order goes
through while it is off.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import quote, urlparse

from . import events, sales
from .core import Services, m2o_id

log = logging.getLogger(__name__)

PREFIX = "ticket:"  # an order's ``product`` for a ticket: never a product's id (no colon there)
HOLDING = ("pending", "invoiced", "paid")  # an order that holds a place and admitted nobody yet
URL_OK = re.compile(r"^https://[A-Za-z0-9._~!$&'()*+,;=:@%/?{}-]+$")


@dataclass
class Wanted:
    ev: events.Event
    t: events.Ticket


# ── settings ────────────────────────────────────────────────────────────────────────────────
def on(svc: Services) -> bool:
    """The console's switch "Sell event tickets through the checkout"."""
    return bool(sales.settings(svc)["tickets"])


def odoo_product(st: dict[str, Any]) -> int | None:
    """The Odoo product a ticket's invoice line uses: the one picked for tickets, else the
    server's fallback (``ACCOUNTING_PRODUCTS``: ``ticket`` or ``*``), else the product every
    Belfry product is invoiced as when they all share one."""
    picked = set((st.get("odoo_products") or {}).values())
    fallback = st.get("fallback_products") or {}
    got = (
        st.get("ticket_product")
        or fallback.get("ticket")
        or fallback.get("*")
        or (next(iter(picked)) if len(picked) == 1 else None)
    )
    return int(got) if got else None


def can_sell(svc: Services, env: str = "live") -> bool:
    """Tickets can be invoiced and paid in ENV (the switch is asked apart)."""
    st = sales.settings(svc, env)
    return bool(
        sales.odoo_ready(svc, env)
        and odoo_product(st)
        and st["tax_included"]
        and st["tax_excluded"]
        and (sales.stripe_ready(svc, env) or sales.bank_ready(svc))
    )


def missing(svc: Services, env: str = "live") -> str:
    """What an admin still has to set before tickets can be sold in ENV, as one sentence naming
    the console's places ("" when nothing is missing). Shown with `test_not_set_up` and
    `cannot_sell`, so the person testing knows which piece it is."""
    st = sales.settings(svc, env)
    where = "Test" if env == "test" else "Production"
    parts = []
    if not sales.odoo_ready(svc, env):
        parts.append(f"connect Odoo (Plugins → Odoo → {where})")
    else:
        if not odoo_product(st):
            parts.append(
                f"pick the Odoo product event tickets are invoiced as (Plugins → Odoo → {where})"
            )
        if not (st["tax_included"] and st["tax_excluded"]):
            parts.append(f"pick both VAT taxes (Plugins → Odoo → {where})")
    if not (sales.stripe_ready(svc, env) or sales.bank_ready(svc)):
        parts.append(f"connect Stripe (Plugins → Stripe → {where}) or set the bank account (Sales)")
    if not parts:
        return ""
    return "Still to do in the console: " + "; ".join(parts) + "."


def clean_checkout_url(svc: Services, raw: Any) -> str:
    """The console's "Where people buy tickets": a template on the belfry's own domain with
    only the placeholders ``{link}`` and ``{id}``; "" = none. ValueError when it is not one."""
    url = str(raw or "").strip()
    if not url:
        return ""
    plain = url.replace("{link}", "x").replace("{id}", "x")
    if len(url) > 500 or not URL_OK.match(url) or "{" in plain or "}" in plain:
        raise ValueError("not a template")
    try:
        u = urlparse(plain)
        port = u.port
    except ValueError as exc:
        raise ValueError("not a template") from exc
    host = (u.hostname or "").lower()
    domain = svc.settings.nip05_domain.lower().strip(".")
    if u.scheme != "https" or "@" in u.netloc or port is not None or u.fragment:
        raise ValueError("not a template")
    if not re.fullmatch(r"[a-z0-9.-]+", host) or ".." in host or u.netloc.lower() != host:
        raise ValueError("not a template")
    if not (host == domain or host.endswith("." + domain)):
        raise ValueError("not on this domain")
    # the placeholders stand in the path or the query, never in the host
    if "{" in urlparse(url).netloc:
        raise ValueError("not a template")
    return url


def checkout_url(svc: Services, ev: events.Event, t: events.Ticket, template: str = "") -> str:
    """The template filled for one ticket ("" when none is set): ``{link}`` = the event's
    address (its slug, else its id), ``{id}`` = the ticket's d tag, both URL-encoded."""
    template = template or sales.settings(svc)["checkout_url"]
    if not template:
        return ""
    return template.replace("{link}", quote(events.link_of(svc, ev), safe="")).replace(
        "{id}", quote(t.d, safe="")
    )


# ── what a ticket costs and how many are left ───────────────────────────────────────────────
def amounts(t: events.Ticket) -> tuple[int, int, Decimal]:
    """(net, gross, VAT rate) of the ticket's price now, in cents. ``["vat", rate]``: the price
    is before VAT; ``["vat", rate, "included"]``: it includes it; no ``vat`` tag: rate 0, the
    price is the total."""
    amount = sales.cents(t.amount)
    rate = Decimal(t.vat or "0")
    if rate and not t.vat_included:
        net = amount
        gross = net + sales._round(Decimal(net) * rate / 100)
    else:
        gross = amount
        net = sales._round(Decimal(gross) * 100 / (100 + rate))
    return net, gross, rate


def unit(w: Wanted, kind: str) -> int:
    """The order's unit price as sales.split and the invoice line take it: with VAT for a
    person (Odoo's price-included tax), before VAT for an organisation (the tax on top)."""
    net, gross, _rate = amounts(w.t)
    return net if kind == "organisation" else gross


def held(svc: Services) -> Counter:
    """Places held by unpaid (or paid, not yet admitted) orders, per ticket. A tester's test
    orders hold none."""
    return Counter(
        o["ticket"]
        for o in sales.store(svc).orders(HOLDING)
        if o.get("ticket") and sales.env_of(o) == "live"
    )


def left(t: events.Ticket, ppl: events.People, holds: Counter) -> int | None:
    if t.quantity is None:
        return None
    return max(0, t.quantity - ppl.taken(t.d) - holds[t.d])


def left_now(svc: Services, w: Wanted) -> int | None:
    if w.t.quantity is None:
        return None
    return left(w.t, events.people(svc, w.ev), held(svc))


def full(svc: Services, o: dict[str, Any]) -> bool:
    """Whether an order's ticket has no place left (for the ``oversold`` flag of an order paid
    after it ended). Never raises."""
    try:
        w = _load(svc, o["ticket"])
        lft = left_now(svc, w) if w else None
        return lft is not None and lft <= 0
    except Exception:  # noqa: BLE001 — a flag only
        return False


def _why_not(svc: Services, ev: events.Event, t: events.Ticket, currency: str) -> str | None:
    """Why this ticket is not one the checkout sells, whoever asks (None: it is)."""
    if ev.status == "cancelled" or ev.ended():
        return "event_over"
    if t.implicit or not t.priced:
        return "free"
    if not t.on_sale or not t.amount:
        return "not_on_sale"  # no price now, or a price tag that can't be read
    if t.currency != currency:
        return "currency"
    try:
        if sales.cents(t.amount) <= 0:
            return "free"
    except (ValueError, ArithmeticError):
        return "not_on_sale"
    if t.closed:
        return "needs_application"
    return None


def _members_only(ev: events.Event, t: events.Ticket) -> bool:
    return t.members or ev.visibility == "members"


def fields(
    svc: Services,
    ev: events.Event,
    tks: list[events.Ticket],
    ppl: events.People,
    viewer: str | None,
) -> dict[str, dict[str, Any]]:
    """Per ticket d, for ``GET /events/<link>`` and the public page: ``left`` (places, counting
    the orders that hold one), ``can_pay`` (this viewer could buy it through the checkout now)
    and ``checkout_url`` (the console's template filled, when can_pay and one is set)."""
    if not sales.own(svc):
        return {}
    st = sales.settings(svc)
    live = st["tickets"] and can_sell(svc, "live")
    test = False
    if viewer and not live:
        from . import testers

        test = testers.is_tester_key(svc, viewer) and can_sell(svc, "test")
    inside: bool | None = None
    holds = held(svc)
    out: dict[str, dict[str, Any]] = {}
    for t in tks:
        lft = left(t, ppl, holds)
        ok = (
            (live or test)
            and _why_not(svc, ev, t, st["currency"]) is None
            and (lft is None or lft > 0)
        )
        if ok and _members_only(ev, t):
            if inside is None:
                inside = bool(viewer) and (viewer in ev.hosts or events.is_member(svc, viewer))
            ok = inside
        url = checkout_url(svc, ev, t, st["checkout_url"]) if ok else ""
        out[t.d] = {"left": lft, "can_pay": bool(ok), "checkout_url": url or None}
    return out


def can_pay(svc: Services, ev: events.Event, t: events.Ticket, viewer: str | None) -> bool:
    try:
        got = fields(svc, ev, [t], events.people(svc, ev), viewer)
    except Exception:  # noqa: BLE001 — a hint on an error's body
        return False
    return bool(got.get(t.d, {}).get("can_pay"))


# ── the checkout's part ─────────────────────────────────────────────────────────────────────
def _load(svc: Services, d: str) -> Wanted | None:
    evd, sep, _tid = d.partition(":")
    if not sep or not events.D_RE.match(evd) or len(d) > 100:
        return None
    ev = events.load(svc, evd)
    if ev is None:
        return None
    t = next((t for t in events.tickets(svc, ev) if t.d == d), None)
    return Wanted(ev, t) if t is not None else None


def _why_no_offer(ev: events.Event, t: events.Ticket, currency: str) -> str | None:
    """Why nobody can ask for this ticket against a contribution (``POST /offers``): the rules
    of ``_why_not`` but the time. A ticket whose sale has not started can be asked for."""
    if ev.status == "cancelled" or ev.ended():
        return "event_over"
    if t.implicit or not t.priced:
        return "free"
    if t.on_sale and t.amount:
        if t.currency != currency:
            return "currency"
        try:
            if sales.cents(t.amount) <= 0:
                return "free"
        except (ValueError, ArithmeticError):
            pass
    elif t.prices and not any(r.get("currency") == currency for r in t.prices):
        return "currency"
    if t.closed:
        return "needs_application"
    return None


def find(svc: Services, d: str, offer: bool = False) -> Wanted:
    """The ticket ``POST /checkout`` names, when the checkout sells it; ApiError otherwise.
    OFFER: the ticket ``POST /offers`` names (it need not be on sale now)."""
    from .holder_invites import ApiError

    if not sales.own(svc):
        raise ApiError(409, "unsupported", "Selling needs the belfry's own register.")
    try:
        w = _load(svc, d.strip())
    except Exception as exc:  # noqa: BLE001 — the relay
        log.warning("ticket sales: the event could not be read: %s", type(exc).__name__)
        raise ApiError(503, "unavailable", "The event can't be read just now.") from exc
    if w is None:
        raise ApiError(404, "unknown_ticket", "No such ticket.")
    currency = sales.settings(svc)["currency"]
    why = _why_no_offer(w.ev, w.t, currency) if offer else _why_not(svc, w.ev, w.t, currency)
    if why:
        status, text = {
            "event_over": (409, "This event is over or cancelled."),
            "free": (409, "This ticket is free: take it on the event's page."),
            "not_on_sale": (409, "This ticket is not on sale now."),
            "currency": (409, "This ticket's price is settled with the hosts, not paid here."),
            "needs_application": (409, "This ticket takes an application."),
        }[why]
        raise ApiError(status, why, text)
    return w


def name(w: Wanted) -> str:
    """The ticket by name: "<ticket> · <event>"."""
    return f"{w.t.title} · {w.ev.title}"[:200]


def as_product(w: Wanted) -> dict[str, Any]:
    """The ticket in the shape the checkout knows a product by. The name is the invoice's and
    Stripe's line: "<ticket> · <event>"."""
    _net, _gross, rate = amounts(w.t)
    return {
        "id": PREFIX + w.t.d,
        "name": name(w),
        "tier": "ticket",
        "role": None,
        "valid_from": None,
        "valid_until": None,
        "vat_rate": float(rate),
        "quantity": w.t.quantity,
        "on_sale": True,
    }


def link(svc: Services, d: str) -> str:
    """An event's address by its d (events.link_of, without reading the event)."""
    return events.store(svc).current_slug(d) or d


def one_day(ev: events.Event) -> str:
    """The event's date as the event page writes one (events.when_text), when the event is on
    one day; "" otherwise."""
    if ev.start is None:
        return ""
    if isinstance(ev.start, dt.datetime):
        tz = events._zone(ev.tz)
        s = ev.start.astimezone(tz)
        if isinstance(ev.end, dt.datetime) and ev.end.astimezone(tz).date() != s.date():
            return ""
        return s.strftime("%A %-d %B %Y")
    if isinstance(ev.end, dt.date) and ev.end != ev.start:
        return ""
    return events.when_text(ev)


def offer_url(svc: Services, f: dict[str, Any]) -> str:
    """Where the author of an approved offer for a ticket goes on: the console's template
    filled for the offer's event and ticket, with ``offer=<id>`` ("" when none is set)."""
    template = sales.settings(svc)["checkout_url"]
    if not template or not f.get("ticket") or not f.get("event"):
        return ""
    url = template.replace("{link}", quote(link(svc, f["event"]), safe="")).replace(
        "{id}", quote(f["ticket"], safe="")
    )
    return url + ("&" if "?" in url else "?") + "offer=" + quote(f["id"], safe="")


def order_fields(w: Wanted) -> dict[str, Any]:
    return {
        "ticket": w.t.d,
        "event": w.ev.d,
        "event_title": w.ev.title,
        "ticket_title": w.t.title,
    }


def _pubkey_of(svc: Services, partner_id: int) -> str:
    """The key of PARTNER's account: of a member's pass first, else a guest's."""
    rows = [
        r
        for r in svc.odoo.search_read(
            "belfry.pass",
            [["partner_id", "=", int(partner_id)], ["state", "=", "active"]],
            ["keycast_pubkey", "tier"],
        )
        if r.get("keycast_pubkey") and r.get("tier") != "agent"
    ]
    rows.sort(key=lambda r: r.get("tier") == "guest")
    return str(rows[0]["keycast_pubkey"]) if rows else ""


def check(
    svc: Services,
    w: Wanted,
    key: str,
    env: str,
    tester: int | None,
    owner: dict[str, Any] | None,
    switch: bool = True,
) -> None:
    """Whether this buyer may order the ticket: the switch (a tester's test order passes while
    it is off; SWITCH false: an approved offer at 100%, where nothing is sold), members-only
    tickets, one ticket of an event per person."""
    from .holder_invites import ApiError

    if switch and env == "live" and not on(svc):
        raise ApiError(409, "tickets_off", "Tickets of events are not sold here yet.")
    if _members_only(w.ev, w.t) and tester is None:
        if not (key in w.ev.hosts or events.is_member(svc, key)):
            raise ApiError(403, "members_only", "This ticket is for members.")
    who = _pubkey_of(svc, tester) if tester is not None else (key if owner else "")
    if who and who in events.people(svc, w.ev).holders:
        raise ApiError(409, "already", "You have a ticket for this event already.")
    for o in sales.store(svc).orders(("paid", "active"), key=key):
        if o.get("event") == w.ev.d and o.get("ticket") != w.t.d and sales.env_of(o) == env:
            # one key is one person: a second ticket of the same event would admit nobody
            raise ApiError(409, "already", "You have a ticket for this event already.")


# ── a paid order: the buyer is admitted ─────────────────────────────────────────────────────
def _ticket_of(svc: Services, ev: events.Event, o: dict[str, Any]) -> events.Ticket:
    """The order's ticket as it is now; when the hosts removed it since, as it was sold."""
    t = next((t for t in events.tickets(svc, ev) if t.d == o["ticket"]), None)
    if t is not None:
        t.priced = True  # it was paid for, whatever its price tag says today
        return t
    return events.Ticket(
        d=o["ticket"],
        id=o["ticket"].split(":", 1)[1],
        title=o.get("ticket_title") or "Ticket",
        priced=True,
    )


def _admit(
    svc: Services, ev: events.Event, o: dict[str, Any], pubkey: str, extra: list[list[str]]
) -> str:
    """The event group's 9000 for PUBKEY with the order's ticket, as a host approving their
    application publishes it (events.decide). "admitted", or "already" when they hold a ticket
    of this event."""
    t = _ticket_of(svc, ev, o)
    if sales.env_of(o) == "test":
        extra = [*extra, ["test", "1"]]  # a tester's ticket takes no place (events.people)
    with events._issue_lock:
        ppl = events.people(svc, ev)
        if pubkey in ppl.holders:
            return "already"
        app = ppl.applications.get(pubkey)
        tags = events._ticket_tags(
            ev, pubkey, t, [["by", "checkout"], *([["e", app["id"]]] if app else []), *extra]
        )
        if o.get("offer") and o["total"]["percent_off"] == 100:
            # an approved request (in kind, sponsored) settled it, not a payment
            tags = [["paid", "offer"] if x[:1] == ["paid"] else x for x in tags]
        if not events.publish(svc, 9000, tags):
            raise RuntimeError("the event's group could not be reached")
    return "admitted"


def _new_guest(svc: Services, ev: events.Event, o: dict[str, Any]) -> tuple[int, int, str, Any]:
    """A buyer nobody knows (a browser key): the contact and guest pass of the event's group an
    event's guest gets (events.new_guest), with the checkout's rule for the address: kept as
    ``belfry_pending_email`` until it is proven. The same key again: the same guest.
    (partner, pass, pubkey, signer token)."""
    from . import core

    made = svc.odoo.event_guest(ev.group, o["name"], "", o["key"], ev.guest_until(), "active")
    pid, pass_id = int(m2o_id(made["partner_id"])), int(made["pass_id"])
    row = svc.odoo.read_one("res.partner", pid, ["name", "email", "belfry_pending_email"]) or {}
    vals: dict[str, Any] = {"belfry_sale_order": o["id"]}
    if o.get("odoo_partner"):
        vals["belfry_odoo_partner"] = o["odoo_partner"]
    if not row.get("email") and not row.get("belfry_pending_email"):
        vals["belfry_pending_email"] = o["email"]
    svc.odoo.write("res.partner", [pid], vals)
    partner = {"id": pid, "name": row.get("name") or o["name"]}
    pubkey, npub, ucan = core._ensure_keycast_user(svc, partner, f"guest-{pid}")
    if not made.get("again"):
        svc.odoo.write("res.partner", [pid], {"belfry_npub": npub})
    had = svc.odoo.read_one("belfry.pass", pass_id, ["event_email", "sale_order"]) or {}
    pvals: dict[str, Any] = {"keycast_pubkey": pubkey, "state": "active"}
    if not had.get("event_email"):
        pvals["event_email"] = o["email"]  # where the event's own mails go (events._email_of)
    if not had.get("sale_order"):
        pvals["sale_order"] = o["id"]
    svc.odoo.write("belfry.pass", [pass_id], pvals)
    return pid, pass_id, pubkey, ucan


def activate(svc: Services, o: dict[str, Any]) -> None:
    """The order is paid: its buyer holds the ticket. No membership pass is made."""
    from .clients.keycast import ClientKeyTaken

    st = sales.store(svc)
    oid = o["id"]
    test = sales.env_of(o) == "test"
    ev = events.load(svc, o["event"])
    if ev is None:
        raise RuntimeError("the event could not be read")  # the tick tries again
    pid = o.get("partner_id")
    extra: list[list[str]] = []
    ucan = None
    if test:
        # only on the account of the tester it was made for, while they still are one
        from . import testers

        if not pid or testers.tester_by_email(svc, o["email"]) != int(pid):
            st.update(oid, step="stuck", stuck_step="activate", flags=sales._flag(o, "tester_gone"))
            log.warning("sales: test order %s: its tester is gone; no ticket", oid)
            return
        pubkey = _pubkey_of(svc, int(pid))
    elif o.get("account"):
        pubkey = o["key"]  # a signed-in member or guest signs with their account's own key
        acct = svc.odoo.read_one("res.partner", pid, ["email", "belfry_pending_email"]) or {}
        if not acct.get("email") and not acct.get("belfry_pending_email"):
            svc.odoo.write("res.partner", [pid], {"belfry_pending_email": o["email"]})
    else:
        pid, pass_id, pubkey, ucan = _new_guest(svc, ev, o)
        o = st.update(oid, partner_id=pid, pass_id=pass_id)
        extra = [["name", o["name"]], ["guest", ev.guest_until()]]
    if not pubkey:
        st.update(oid, step="stuck", stuck_step="activate", flags=sales._flag(o, "pass_closed"))
        log.warning("sales: order %s: its buyer has no account any more; no ticket", oid)
        return
    if o.get("account") or test:
        if not events.is_member(svc, pubkey):
            # a guest of another channel: one more guest pass, this event's group's
            svc.odoo.event_guest_pass(pubkey, ev.group, ev.guest_until(), "active")
    flags = []
    if o.get("ticket_pubkey") != pubkey:  # the first try (a retry finds its own 9000)
        if pubkey in events.people(svc, ev).holders:
            # they held a ticket of this event before this order was paid: the admins look
            flags.append("already_admitted")
        if ev.status == "cancelled" or ev.ended():
            flags.append("event_over")  # paid after the event ended or was cancelled
        o = st.update(oid, ticket_pubkey=pubkey, flags=sales._flag(o, *flags))
    _admit(svc, ev, o, pubkey, extra)
    sales.mark_erase(svc, pid, o)
    if ucan and not o.get("bunker_url"):
        # the browser that paid is signed in (never for a test order: nothing is created there)
        try:
            bunker = svc.keycast.create_bunker(ucan, "Tickets", o["key"])
        except ClientKeyTaken:
            log.warning("sales: order %s: the browser key belongs to another account", oid)
            bunker = {}
        o = st.update(oid, bunker_url=bunker.get("bunker_url") or None)
    o = st.update(oid, state="active", activated=sales.iso(sales.now()), error=None)
    o = st.update(oid, step=sales.next_after_pass(o))
    if o["step"] != "invoice":  # else the receipt waits for the invoice (with its PDF)
        from . import salesmail

        salesmail.queue(svc, "receipt", oid, o["email"])
    log.info("sales: order %s active (a ticket of event %s)", oid, o["event"])


def moved(svc: Services, o: dict[str, Any], temp: int, other: int) -> None:
    """The buyer's address was proven and belongs to another account already (sales.confirm):
    the ticket goes to that account's own key. The caller closes the temporary guest pass, which
    takes the temporary key out of the event's group."""
    st = sales.store(svc)
    ev = events.load(svc, o["event"])
    pubkey = _pubkey_of(svc, other)
    flags: list[str] = []
    if ev is None or not pubkey:
        flags.append("pass_closed")  # nothing to admit: the admins look
    else:
        if not events.is_member(svc, pubkey):
            svc.odoo.event_guest_pass(pubkey, ev.group, ev.guest_until(), "active")
        _admit(svc, ev, o, pubkey, [])
    done = st.update(
        o["id"],
        partner_id=other,
        pass_id=None,
        moved_from=temp,
        bunker_url=None,
        ticket_pubkey=pubkey or o.get("ticket_pubkey"),
        flags=sales._flag(o, *flags),
    )
    sales.mark_erase(svc, other, done)


def refunded(svc: Services, o: dict[str, Any]) -> None:
    """The order's invoice was credited in full after its buyer was admitted: nobody is taken
    out of the event by itself; the order is flagged for the admins."""
    sales.store(svc).update(
        o["id"], closed=sales.iso(sales.now()), flags=sales._flag(o, "refunded_admitted")
    )
    log.warning("sales: order %s: refunded after its buyer was admitted to the event", o["id"])


__all__ = ["activate", "can_pay", "can_sell", "check", "fields", "find", "moved", "on", "refunded"]
