"""Testers (docs/roles.md "Tester", docs/sales.md §9): member accounts made for trying the Stripe
and Odoo integrations. Everything a tester buys goes through the TEST environment of the Plugins
page (Stripe's test mode and a test Odoo database), never the real ones.

- A flag on the person, ``res.partner.belfry_tester``, in the belfry's own register only (the
  register keeps any field; an Odoo register never has testers). Not one of the editable roles.
- Given only when the person is created: the console's "Add people" modal, by e-mail (refused
  when the address already has an account: ``email_taken``) or with a tester invite link (the
  flag is set when a NEW person joins with it; someone who had an account already gets nothing
  from it). No API or screen adds the flag to an existing person, and nothing removes it: the
  only way out is removing the person.
- A tester is never an admin (admins.change, roles.set_member and console.set_admin refuse with
  ``tester``).
- belfry.json is public and never lists testers; ``POST /api/v1/whois`` (people of the belfry
  only) answers ``"testers": [hex…]`` among the keys asked.
"""

from __future__ import annotations

import logging
from typing import Any

from .core import Services, m2o_id

log = logging.getLogger(__name__)

FIELD = "belfry_tester"
EXPLAIN = (
    "A tester is for trying payments. What a tester buys goes through Stripe's test mode and "
    "your test Odoo, with test cards and no real money. Make a separate person for this: it "
    "can't be given to someone who is already a member."
)


def own(svc: Services) -> bool:
    from . import invite

    return invite.own(svc)


def is_tester(svc: Services, partner_id: int | None) -> bool:
    if not partner_id or not own(svc):
        return False
    row = svc.odoo.read_one("res.partner", int(partner_id), [FIELD]) or {}
    return bool(row.get(FIELD))


def _partners_of_key(svc: Services, pubkey: str) -> set[int]:
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "=", pubkey]],
        ["partner_id"],
    )
    return {p for r in rows if (p := m2o_id(r.get("partner_id")))}


def is_tester_key(svc: Services, pubkey: str) -> bool:
    if not pubkey or not own(svc):
        return False
    return any(is_tester(svc, p) for p in _partners_of_key(svc, pubkey))


def tester_by_email(svc: Services, email: str) -> int | None:
    """The tester whose PROVEN address (``email``, never a pending one) is EMAIL and who holds
    an active pass, or None. This alone decides whether an order is a test order."""
    email = (email or "").strip().lower()
    if "@" not in email or not own(svc):
        return None
    rows = svc.odoo.search_read(
        "res.partner", [["email", "=ilike", email]], ["email", FIELD], limit=20
    )
    for r in rows:
        if not r.get(FIELD) or (r.get("email") or "").strip().lower() != email:
            continue
        if svc.odoo.search_read(
            "belfry.pass", [["partner_id", "=", r["id"]], ["state", "=", "active"]], ["id"], limit=1
        ):
            return int(r["id"])
    return None


def tester_partners(svc: Services) -> set[int]:
    if not own(svc):
        return set()
    return {r["id"] for r in svc.odoo.search_read("res.partner", [[FIELD, "=", True]], ["id"])}


def tester_pubkeys(svc: Services, among: set[str] | None = None) -> set[str]:
    """The keys of testers' active passes (limited to AMONG when given)."""
    partners = tester_partners(svc)
    if not partners:
        return set()
    rows = svc.odoo.search_read(
        "belfry.pass", [["state", "=", "active"]], ["partner_id", "keycast_pubkey"]
    )
    out = {
        r["keycast_pubkey"]
        for r in rows
        if r.get("keycast_pubkey") and m2o_id(r.get("partner_id")) in partners
    }
    return out & among if among is not None else out


def email_taken(svc: Services, email: str) -> bool:
    """Whether any contact has this address, proven or waiting for its proof."""
    email = (email or "").strip().lower()
    for field in ("email", "belfry_pending_email"):
        for r in svc.odoo.search_read("res.partner", [[field, "=ilike", email]], [field], limit=5):
            if (r.get(field) or "").strip().lower() == email:
                return True
    return False


def add_tester(svc: Services, email: str, name: str) -> str:
    """The console's "Add by e-mail" with Tester: a NEW person with a membership and the flag.
    ValueError (fixed messages) when the address has an account already or isn't one."""
    from . import console, steward

    email = (email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise ValueError("That is not an e-mail address.")
    if not own(svc):
        raise ValueError("Testers need the belfry's own register.")
    if email_taken(svc, email):
        raise ValueError(
            "This address has an account already. A tester must be a new person: use another "
            "address."
        )
    name = name.strip() or email
    pid = svc.odoo.create("res.partner", {"name": name, "email": email, FIELD: True})
    steward.steward_pass(svc, email, name)  # finds that contact and opens its membership
    console._admins["at"] = 0.0
    log.info("testers: partner %s created as a tester", pid)
    return email


def fresh_for_invite(svc: Services, partner_id: int, invite_id: int) -> bool:
    """Whether PARTNER is a new person brought in by this invite: every pass it ever had came
    with it. A tester link makes only such a person a tester."""
    rows = svc.odoo.search_read(
        "belfry.pass", [["partner_id", "=", int(partner_id)]], ["invite_id"]
    )
    return bool(rows) and all(m2o_id(r.get("invite_id")) == int(invite_id) for r in rows)


def mark(svc: Services, partner_id: int) -> None:
    svc.odoo.write("res.partner", [int(partner_id)], {FIELD: True})
    log.info("testers: partner %s is a tester (joined with a tester link)", partner_id)


def refuse_admin(svc: Services, pubkey: str = "", email: str = "") -> None:
    """ApiError 409 ``tester`` when the person is a tester."""
    from .holder_invites import ApiError

    hit = is_tester_key(svc, pubkey) if pubkey else False
    if not hit and email and own(svc):
        rows = svc.odoo.search_read(
            "res.partner", [["email", "=ilike", email.strip()]], [FIELD], limit=5
        )
        hit = any(r.get(FIELD) for r in rows)
    if hit:
        raise ApiError(409, "tester", "A tester can't be an admin or get another role.")


def info(svc: Services) -> dict[str, Any]:
    return {"count": len(tester_partners(svc))}
