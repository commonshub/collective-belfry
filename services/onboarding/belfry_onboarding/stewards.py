"""Who the belfry's admins ("stewards") are, in one place.

Two homes, one set of functions (docs/own-signer.md, phase 2):

- **Authentik** (``SIGNER=keycast``, the default until a belfry is migrated): the members of
  the group ``STEWARD_GROUP``; an admin is an Authentik user, matched to people by e-mail.
- **The register** (``SIGNER=belfry``: a belfry without Keycast and without Authentik): the
  boolean ``res.partner.belfry_steward``. Nothing here or anywhere else in the service talks to
  Authentik in that mode (``NoAuthentik`` makes a forgotten caller fail loudly).

``import_from_authentik`` copies the group into the register once, for the migration.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .core import Services

log = logging.getLogger(__name__)

FIELD = "belfry_steward"


def own_mode(settings: Any) -> bool:
    """This belfry runs without Authentik: admins live in the register, nobody gets an
    Authentik user. Decided by ``SIGNER`` alone (``belfry``): the own signer has no OIDC side
    for Authentik to sit in front of, so the two leave together."""
    return str(getattr(settings, "signer", "") or "keycast").strip().lower() == "belfry"


def own(svc: Services) -> bool:
    return own_mode(svc.settings)


class NoAuthentik:
    """``services.authentik`` of a belfry without Authentik: every use is a bug."""

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(
            f"Authentik is not part of this belfry (SIGNER=belfry); nothing may call {name}()"
        )


def check_settings(settings: Any) -> None:
    """Refuse at start what cannot work. Raises ValueError with what to change."""
    signin = (getattr(settings, "pass_signin", "") or "belfry").strip().lower()
    if own_mode(settings):
        if signin == "authentik":
            raise ValueError(
                "SIGNER=belfry runs without Authentik, so PASS_SIGNIN=authentik cannot work: "
                "set PASS_SIGNIN=belfry (the belfry's own e-mail and code sign-in)"
            )
        return
    missing = [
        env
        for env, name in (
            ("AUTHENTIK_TOKEN", "authentik_token"),
            ("KEYCAST_ADMIN_TOKEN", "keycast_admin_token"),
            ("KEYCAST_SERVICE_TOKEN", "keycast_service_token"),
        )
        if not getattr(settings, name, "")
    ]
    if missing:
        raise ValueError(f"SIGNER=keycast needs {', '.join(missing)}")


def _flagged(svc: Services, fields: list[str]) -> list[dict[str, Any]]:
    return list(svc.odoo.search_read("res.partner", [[FIELD, "=", True]], fields))


def emails(svc: Services) -> list[str]:
    """E-mail addresses of the admins, as written; sorted. Raises when their home can't be
    read (callers decide what a missing answer means)."""
    if not own(svc):
        return list(svc.authentik.group_member_emails(svc.settings.steward_group))
    return sorted({str(p["email"]).strip() for p in _flagged(svc, ["email"]) if p.get("email")})


def is_steward(svc: Services, partner_id: int | None, authentik_user: Any = None) -> bool:
    """Whether this person is an admin. ``authentik_user``: the person's Authentik user id
    (``belfry_authentik_user_id``), read only on a belfry with Authentik: after a migration the
    stored ids mean nothing and must never be compared with anything."""
    if own(svc):
        if not partner_id:
            return False
        row = svc.odoo.read_one("res.partner", int(partner_id), [FIELD]) or {}
        return bool(row.get(FIELD))
    return bool(authentik_user) and int(authentik_user) in svc.authentik.group_member_pks(
        svc.settings.steward_group
    )


def _partners(svc: Services, email: str) -> list[dict[str, Any]]:
    email = email.strip()
    if "@" not in email:
        return []
    rows = svc.odoo.search_read("res.partner", [["email", "=ilike", email]], ["email", FIELD])
    # Odoo's =ilike reads % and _ as wildcards: only the address itself counts
    return [p for p in rows if (p.get("email") or "").strip().lower() == email.lower()]


def mark(svc: Services, partner_id: int, steward: bool) -> None:
    """Register mode only: set or clear the flag on one person."""
    svc.odoo.write("res.partner", [int(partner_id)], {FIELD: bool(steward)})


def remove(svc: Services, email: str) -> None:
    """The person with this address is no admin any more. Their membership stays."""
    if own(svc):
        ids = [int(p["id"]) for p in _partners(svc, email) if p.get(FIELD)]
        if ids:
            svc.odoo.write("res.partner", ids, {FIELD: False})
        log.info("stewards: %s removed (%d people)", email, len(ids))
        return
    ak = svc.authentik
    group = ak.find_group(svc.settings.steward_group)
    if group is None:
        raise RuntimeError(f"group {svc.settings.steward_group} missing")
    for user in ak.find_users_by_email(email):
        ak.remove_user_from_group(group["pk"], int(user["pk"]))


def import_from_authentik(svc: Services, authentik: Any) -> dict[str, Any]:
    """Copy Authentik's stewards group into the register (the migration's step; safe to run
    again). ``authentik``: an AuthentikClient, given by the caller because ``svc.authentik`` is
    not one once the belfry runs without it. People are matched by e-mail; an admin with no
    person in the register is reported, never created (they have no membership to carry)."""
    wanted = sorted({e.strip() for e in authentik.group_member_emails(svc.settings.steward_group)})
    marked: list[str] = []
    already: list[str] = []
    missing: list[str] = []
    for email in wanted:
        people = _partners(svc, email)
        if not people:
            missing.append(email)
            continue
        todo = [int(p["id"]) for p in people if not p.get(FIELD)]
        if todo:
            svc.odoo.write("res.partner", todo, {FIELD: True})
            marked.append(email)
        else:
            already.append(email)
    log.info(
        "stewards imported from Authentik: %d marked, %d already, %d without a person",
        len(marked),
        len(already),
        len(missing),
    )
    return {"marked": marked, "already": already, "missing": missing}
