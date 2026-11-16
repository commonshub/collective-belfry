"""A guest asks to become a member, from inside the app (2026-11-16; docs/membership.md, "A guest
asks"). The request is ``POST /api/applications`` with ``"apply"`` (membership.py routes it
here), NIP-98 by the guest's own key:

- ``{"apply": {"answer": "<text ≤ 2000>", "agreed"?: ["<2100 id>", …]}}`` → 200 ``{"application":
  {"id", "state", "vouches", "needed", "vouched_by": [{"pubkey", "name"}]}, "until": <unix end of
  the guest's latest guest pass>, "accept": "admins"|"vouches"}``. The caller holds an active
  guest pass (not an agent's) whose contact has a saved e-mail: else 409 ``guest_not_saved`` (a
  guest who hasn't saved their account yet) or 403 ``not_guest`` (members, agents, unknown
  keys). 422 ``invalid``: an empty or too long answer, ``agreed`` not a list of event ids. One
  open request per guest: another answers 409 ``pending`` with the same body (plus ``error``).
- ``{"apply": "status"}`` → the same body, ``application`` = the caller's latest request (any
  state: ``new``, ``accepted``, ``declined``) or null, plus ``agreed`` (the ids they ticked).
  Answered for whoever has a request on record (also once they are a member); others as above.

The request is a ``belfry.application`` of kind ``guest``: name and e-mail of the guest's
contact, ``pubkey``, ``guest_partner``, ``channels`` (the channels of their active guest passes),
``inviter`` (the key of whoever invited them, latest pass), ``since`` (their first guest day) and
``agreed``. Admins are told like for the web form (mail) and on their phones (push type
``membership_request``).

Accepting it (an admin's ``decide``, or enough vouches: membership.accept) sends no invite: the
guest becomes a member on the same key, with the default role, as when an admin makes a guest a
member (roles.make_member: membership pass, guest passes closed, label ``member`` in their
channels); state ``accepted``. Then their ticked agreements are signed for them (charter.py).
Declining (state ``declined``) leaves them a guest; while a request is open their guest passes
don't expire (core.expire asks ``holds``), and a pass whose last day passed meanwhile gets 24
hours from the decline (``guest_grace_until``).
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from typing import Any

from .core import Services, m2o_id

log = logging.getLogger(__name__)

ANSWER_MAX = 2000
GRACE_HOURS = 24
KIND = "guest"
ACCEPTED = "accepted"
_lock = threading.RLock()


class Pending(Exception):
    """A request is open already: the answer is 409 ``pending`` with this body."""

    def __init__(self, body: dict[str, Any]):
        super().__init__("pending")
        self.body = body


def _guest_passes(svc: Services, pubkey: str) -> list[dict[str, Any]]:
    return svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "=", pubkey]],
        [
            "tier",
            "partner_id",
            "guest_agent",
            "guest_channel",
            "guest_inviter",
            "date_start",
            "date_end",
            "guest_grace_until",
        ],
    )


def _end_unix(row: dict[str, Any]) -> int | None:
    """When a guest pass ends: its grace time after a decline, else the end of its last day."""
    grace = row.get("guest_grace_until")
    if grace:
        try:
            return int(dt.datetime.fromisoformat(str(grace)).replace(tzinfo=dt.UTC).timestamp())
        except ValueError:
            pass
    end = row.get("date_end")
    if not end:
        return None
    try:
        day = dt.date.fromisoformat(str(end)[:10]) + dt.timedelta(days=1)
    except ValueError:
        return None
    return int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.UTC).timestamp())


def until(svc: Services, pubkey: str) -> int | None:
    ends = [
        e
        for r in _guest_passes(svc, pubkey)
        if r.get("tier") == "guest"
        if (e := _end_unix(r)) is not None
    ]
    return max(ends) if ends else None


def guest_of(svc: Services, pubkey: str) -> dict[str, Any]:
    """The guest behind PUBKEY: ``{"partner", "name", "email", "passes"}``; raises 403
    ``not_guest`` / 409 ``guest_not_saved``."""
    from .holder_invites import ApiError

    rows = _guest_passes(svc, pubkey)
    guest = [r for r in rows if r.get("tier") == "guest"]
    if not guest or len(guest) != len(rows) or any(r.get("guest_agent") for r in guest):
        raise ApiError(403, "not_guest", "Only a guest asks to become a member this way.")
    pid = m2o_id(guest[0].get("partner_id"))
    partner = svc.odoo.read_one("res.partner", pid or 0, ["name", "email"]) or {}
    email = (partner.get("email") or "").strip()
    if not pid or not email:
        raise ApiError(409, "guest_not_saved", "Save your account with your e-mail first.")
    return {"partner": pid, "name": partner.get("name") or "", "email": email, "passes": guest}


def latest(svc: Services, pubkey: str) -> dict[str, Any] | None:
    from .membership import FIELDS

    rows = svc.odoo.search_read(
        "belfry.application",
        [["kind", "=", KIND], ["pubkey", "=", pubkey]],
        FIELDS,
        limit=1,
        order="id desc",
    )
    return rows[0] if rows else None


def open_for_partner(svc: Services, partner_id: int) -> bool:
    return bool(
        svc.odoo.search_read(
            "belfry.application",
            [["kind", "=", KIND], ["guest_partner", "=", partner_id], ["state", "=", "new"]],
            ["id"],
            limit=1,
        )
    )


def holds(svc: Services, pass_row: dict[str, Any]) -> bool:
    """Whether a guest pass must stay open past its last day: its guest's request is open, or
    the 24 hours after a decline are not over."""
    from . import invite

    if pass_row.get("tier") != "guest" or not invite.own(svc):
        return False
    grace = pass_row.get("guest_grace_until")
    if grace and str(grace) > _stamp(_now()):
        return True
    pid = m2o_id(pass_row.get("partner_id"))
    return bool(pid) and open_for_partner(svc, pid)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0)


def _stamp(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S")


def names(svc: Services, keys: list[str]) -> dict[str, str]:
    """The register's names of the people behind KEYS (active passes)."""
    out: dict[str, str] = {}
    if not keys:
        return out
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "in", list(keys)]],
        ["keycast_pubkey", "partner_id"],
    )
    pids = {r["keycast_pubkey"]: m2o_id(r.get("partner_id")) for r in rows}
    found = svc.odoo.read("res.partner", [p for p in set(pids.values()) if p], ["name"])
    by_id = {p["id"]: p.get("name") or "" for p in found}
    for k, p in pids.items():
        if p in by_id:
            out[k] = by_id[p]
    return out


def body(svc: Services, row: dict[str, Any] | None, pubkey: str) -> dict[str, Any]:
    from . import membership

    s = membership.settings(svc)
    app: dict[str, Any] | None = None
    if row is not None:
        vouched = membership._counted(svc, row.get("vouched_by"))
        who = names(svc, vouched)
        app = {
            "id": row["id"],
            "state": row.get("state") or "",
            "vouches": len(vouched),
            "needed": s["vouches"],
            "vouched_by": [{"pubkey": k, "name": who.get(k, "")} for k in vouched],
        }
    return {"application": app, "until": until(svc, pubkey), "accept": s["accept"]}


def apply(svc: Services, caller: str, data: Any) -> dict[str, Any]:
    from . import charter, membership
    from .holder_invites import ApiError

    if not membership.own(svc):
        raise ApiError(409, "unsupported", "Requests need the belfry's own register.")
    if data == "status":
        row = latest(svc, caller)
        if row is None:
            guest_of(svc, caller)  # 403 / 409 for someone who isn't a guest
        out = body(svc, row, caller)
        out["agreed"] = list((row or {}).get("agreed") or [])
        return out
    if not isinstance(data, dict):
        raise ApiError(422, "invalid", 'Send {"apply": {"answer": "…"}} or {"apply": "status"}.')
    answer = data.get("answer")
    if not isinstance(answer, str) or not answer.strip() or len(answer) > ANSWER_MAX:
        raise ApiError(422, "invalid", f"answer: a few words, at most {ANSWER_MAX} characters.")
    agreed = charter.clean_ids(data.get("agreed"))
    if agreed is None:
        raise ApiError(422, "invalid", "agreed: a list of agreement ids.")
    g = guest_of(svc, caller)
    with _lock:
        row = latest(svc, caller)
        if row is not None and row.get("state") == "new":
            raise Pending(body(svc, row, caller))
        passes = sorted(g["passes"], key=lambda r: -int(r["id"]))
        channels = sorted({r["guest_channel"] for r in passes if r.get("guest_channel")})
        starts = sorted(str(r["date_start"]) for r in passes if r.get("date_start"))
        aid = svc.odoo.create(
            "belfry.application",
            {
                "name": g["name"][:200] or "Guest",
                "email": g["email"][:200],
                "motivation": answer.strip(),
                "kind": KIND,
                "state": "new",
                "pubkey": caller,
                "guest_partner": g["partner"],
                "channels": channels,
                "inviter": next((r["guest_inviter"] for r in passes if r.get("guest_inviter")), ""),
                "since": starts[0] if starts else False,
                "agreed": agreed,
                "vouched_by": [],
                "handled_by": False,
            },
        )
    log.info("membership: guest %s… asks to become a member (request %s)", caller[:8], aid)
    row = membership._get(svc, aid)
    try:
        notify(svc, row)
    except Exception:  # noqa: BLE001 — the request is in; the admins see it in the chat
        log.exception("membership: admins not told about request %s", aid)
    return body(svc, row, caller)


def notify(svc: Services, row: dict[str, Any]) -> None:
    """Mail the admins (like a request from the web form) and push to their phones."""
    from . import agentrequests, console
    from .agentrequests import text

    people = agentrequests.admins(svc)
    emails = sorted({e for e in console.admin_emails(svc) if e and "@" in e})
    community = svc.settings.community_name or "your belfry"
    chat = f"https://chat.{svc.settings.nip05_domain}/#/requests"
    channels = ", ".join("#" + c for c in row.get("channels") or [])
    mailer = getattr(svc.odoo, "mailer", None)
    if mailer is not None and emails:
        mailer.send(
            emails,
            f"{community}: a guest asks to become a member",
            [
                f"{row.get('name') or 'A guest'} ({row.get('email') or ''}), a guest of "
                f"{channels or 'a channel'}, asks to become a member:",
                row.get("motivation") or "",
                "Answer in the chat, under Members (top of the channel list).",
            ],
            button=(chat, "Review the request"),
            footer="You get this because you are an admin of " + community + ".",
        )
    _push(
        svc,
        [a["pubkey"] for a in people],
        "A guest asks to become a member: " + text(row.get("name"), 40),
        chat,
    )


def _push(svc: Services, keys: list[str], line: str, url: str) -> int:
    from . import push as push_mod

    try:
        if not keys or not push_mod.enabled(svc):
            return 0
        devices = svc.odoo.push_devices(keys)
        if not devices:
            return 0
        gateway = push_mod.Gateway(svc)
        plain = {
            "v": 1,
            "domain": svc.settings.nip05_domain,
            "type": "membership_request",
            "title": svc.settings.community_name or "Belfry",
            "body": line,
            "url": url,
        }
        sent = 0
        for d in devices:
            payload = push_mod.encrypt(d["key"], push_mod.fit(plain))
            if gateway.send(d, payload, "membership_request", True) == "ok":
                sent += 1
        return sent
    except Exception:  # noqa: BLE001 — the mail and the chat carry it anyway
        log.exception("membership: no push for a guest's request")
        return 0


# ── the answer ──────────────────────────────────────────────────────────────────────────────
def accept(svc: Services, row: dict[str, Any], by: str) -> None:
    """The guest becomes a member on the same key with the default role; their ticked
    agreements are signed for them. Raises 409 ``gone`` when they are no longer a guest here."""
    from . import roles
    from .holder_invites import ApiError
    from .places import member_key

    pubkey = row.get("pubkey") or ""
    if member_key(svc, pubkey) is None:
        try:
            roles.make_member(svc, pubkey, roles.default_role(svc), 0)
        except ApiError as exc:
            if exc.error in ("not_member", "agent", "guest_not_saved"):
                raise ApiError(409, "gone", "This guest is no longer here.") from exc
            raise
    svc.odoo.write(
        "belfry.application",
        [row["id"]],
        {"state": ACCEPTED, "handled_by": by, "handled_at": _stamp(_now())},
    )
    log.info("membership: guest request %s accepted (%s)", row["id"], by)
    ticks(svc, row)


def ticks(svc: Services, row: dict[str, Any], partner: int | None = None) -> list[str]:
    """Sign the agreements the person ticked before they were in (charter.publish_ticks), as
    PARTNER (default: the guest's contact)."""
    from . import charter

    agreed = [a for a in row.get("agreed") or [] if isinstance(a, str)]
    if not agreed:
        return []
    pid = partner or row.get("guest_partner")
    if not pid:
        return []
    try:
        asked = int(
            dt.datetime.fromisoformat(str(row.get("create_date")))
            .replace(tzinfo=dt.UTC)
            .timestamp()
        )
    except ValueError:
        asked = int(_now().replace(tzinfo=dt.UTC).timestamp())
    done = charter.publish_ticks(svc, int(pid), agreed, asked)
    if done:
        svc.odoo.write("belfry.application", [row["id"]], {"agreed_published": done})
    return done


def declined(svc: Services, row: dict[str, Any]) -> None:
    """A guest's request was declined: they stay a guest; a pass whose last day passed while the
    request was open gets GRACE_HOURS from now."""
    pid = row.get("guest_partner")
    if not pid:
        return
    now = _now()
    grace = now + dt.timedelta(hours=GRACE_HOURS)
    for r in svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["partner_id", "=", pid], ["tier", "=", "guest"]],
        ["date_end"],
    ):
        if str(r.get("date_end") or "9999") < now.date().isoformat():
            svc.odoo.write(
                "belfry.pass",
                [r["id"]],
                {"date_end": grace.date().isoformat(), "guest_grace_until": _stamp(grace)},
            )


def after_invite(svc: Services, invite_id: int) -> list[str]:
    """Someone who applied on the web form (with ticked agreements) took their invite and is a
    member now: sign those agreements for them."""
    rows = svc.odoo.search_read(
        "belfry.application",
        [["invite_id", "=", invite_id]],
        ["agreed", "create_date", "agreed_published"],
        limit=1,
    )
    if not rows or not rows[0].get("agreed") or rows[0].get("agreed_published"):
        return []
    inv = svc.odoo.read_one("belfry.invite", invite_id, ["used_by_partner_id"]) or {}
    pid = m2o_id(inv.get("used_by_partner_id"))
    return ticks(svc, rows[0], pid) if pid else []
