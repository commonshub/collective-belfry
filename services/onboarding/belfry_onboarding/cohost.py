"""Inviting someone already in the community to co-host an event (docs/checkin.md, "Inviting a
member to co-host").

Xavier, 2026-11-21: a host invites a member to co-host without sending a link by hand. The
invitation lives in the belfry's own register (one per event and person); the invitee gets a
push notification and answers in the app. Accepting makes them a co-host exactly as a co-host
link does (guests._make_host: a 9000 ``["h", ev-<d>] ["p", <key>, "admin"] ["invited-by",
<host>]`` by the groups key). Guests and agents are never invited this way: a guest gets the
existing co-host link (POST /channel-invites ``"role": "host"``), which asks for an e-mail first.

API (belfry.<domain>/api/v1/…, NIP-98):

- ``POST /events/<d>/cohost-invites {"pubkey"}`` by a host (the creator, a co-host, any admin of
  the event's group, a belfry admin) → ``{"invite": {...}}``; again while pending → the same
  invitation with ``"already": true``;
- ``POST /events/<d>/cohost-invites/answer {"accept": true | false}`` by the invitee;
- ``POST /events/<d>/cohost-invites/withdraw {"pubkey"}`` by a host;
- ``GET /events/<link>``: ``me.cohost_invite`` (pending, for the invitee) and, for hosts,
  ``me.cohost_invites``.

A pending invitation lapses when the event is over or ``DAYS`` after it was sent (read as
expired; it is never shown and can be sent again).
"""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import events
from .core import Services, m2o_id

log = logging.getLogger(__name__)

DAYS = 30
HEX = re.compile(r"^[0-9a-f]{64}$")
PATH = events.API + "/{d}/cohost-invites"
# the chat writes these; never the community's website or its previews (discovery.py)
CHAT_ONLY = re.compile(r"^/api/events/[^/]+/cohost-invites(?:/answer|/withdraw)?$")
_lock = threading.Lock()


def _own(svc: Services) -> None:
    from . import invite
    from .holder_invites import ApiError

    if not invite.own(svc):
        raise ApiError(409, "unsupported", "Co-host invitations need the belfry's own register.")


def own(svc: Services) -> bool:
    from . import invite

    return invite.own(svc)


def is_host(svc: Services, ev: events.Event, pubkey: str) -> bool:
    """Who may invite and withdraw: the event's hosts (its creator and every admin of its group,
    by the relay or by role) and the belfry's admins."""
    from . import checkin

    return bool(pubkey) and (pubkey in ev.hosts or pubkey in checkin._belfry_admins(svc))


def lapsed(ev: events.Event, row: dict[str, Any], now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return row["state"] == "pending" and (ev.ended() or now - int(row["at"]) > DAYS * 86400)


def pending(svc: Services, ev: events.Event, pubkey: str) -> dict[str, Any] | None:
    row = svc.odoo.cohost_get(ev.d, pubkey)
    if row is None or row["state"] != "pending" or lapsed(ev, row):
        return None
    return row


def _people(svc: Services, keys: set[str]) -> dict[str, dict[str, Any]]:
    from . import eventchat

    try:
        return eventchat.people(svc, keys)
    except Exception as exc:  # noqa: BLE001 — names are a nicety here
        log.info("cohost: people not read: %s", type(exc).__name__)
        return {}


def _who(found: dict[str, dict[str, Any]], key: str) -> dict[str, Any]:
    from .eventchat import NOBODY

    p = found.get(key) or {}
    return {"name": p.get("name") or NOBODY, "slug": p.get("slug"), "picture": p.get("picture")}


def _shown(row: dict[str, Any], by: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "by": by, "at": int(row["at"]), "state": row["state"]}


def _member(svc: Services, pubkey: str) -> str:
    """``member`` (an active pass that is not a guest's or an agent's), ``guest``, ``agent`` or
    ``none``."""
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "=", pubkey]],
        ["tier", "guest_agent"],
    )
    if any(r.get("tier") == "agent" or r.get("guest_agent") for r in rows):
        return "agent"
    if any(r.get("tier") not in ("guest", "agent") for r in rows):
        return "member"
    return "guest" if rows else "none"


def _refuse_not_member(kind: str) -> None:
    from .holder_invites import ApiError

    if kind == "member":
        return
    if kind == "guest":
        exc = ApiError(
            409,
            "not_member",
            "Only members are invited this way. Send a guest a co-host link instead.",
        )
        exc.extra = {"guest": True}
    elif kind == "agent":
        exc = ApiError(409, "not_member", "An agent can't co-host.")
    else:
        exc = ApiError(409, "not_member", "This person is not a member here.")
    raise exc


def _refuse_closed(svc: Services, ev: events.Event) -> None:
    from . import checkin
    from .holder_invites import ApiError

    if ev.ended():
        raise ApiError(409, "event_over", "This event is over.")
    if ev.status == "cancelled":
        raise ApiError(409, "cancelled", "This event is cancelled.")
    checkin.refuse_archived(svc, ev.group)


def _already_host(svc: Services, ev: events.Event, pubkey: str) -> bool:
    """Shown as the host or a co-host already (checkin.viewer_roles: the community's admins,
    who administer every group, can still be invited to be listed as co-hosts)."""
    from . import checkin

    st = checkin.group_state(svc, ev.group)
    return bool({"host", "cohost"} & set(checkin.viewer_roles(svc, st, pubkey, ev.creator)))


def invite(svc: Services, ev: events.Event, caller: str, data: dict[str, Any]) -> dict[str, Any]:
    from .holder_invites import ApiError

    _own(svc)
    target = str(data.get("pubkey") or "").strip().lower()
    if not HEX.match(target):
        raise ApiError(422, "invalid", 'Send {"pubkey"}.')
    if not is_host(svc, ev, caller):
        raise ApiError(403, "not_allowed", "Only the event's hosts invite co-hosts.")
    _refuse_closed(svc, ev)
    if target == caller or _already_host(svc, ev, target):
        raise ApiError(409, "already_host", "This person hosts this event already.")
    _refuse_not_member(_member(svc, target))
    with _lock:
        row = pending(svc, ev, target)
        if row is not None:
            found = _people(svc, {row["by"]})
            return {"invite": _shown(row, _who(found, row["by"])), "already": True}
        row = {
            "d": ev.d,
            "pubkey": target,
            "id": secrets.token_hex(8),
            "by": caller,
            "at": int(time.time()),
            "state": "pending",
            "answered": None,
        }
        svc.odoo.cohost_put(row)
    found = _people(svc, {caller})
    by = _who(found, caller)
    notify(svc, ev, target, by["name"])
    return {"invite": _shown(row, by)}


def answer(svc: Services, ev: events.Event, caller: str, data: dict[str, Any]) -> dict[str, Any]:
    from . import guests
    from .holder_invites import ApiError

    _own(svc)
    accept = data.get("accept")
    if not isinstance(accept, bool):
        raise ApiError(422, "invalid", 'Send {"accept": true | false}.')
    if ev.ended():
        raise ApiError(409, "event_over", "This event is over.")
    with _lock:
        row = pending(svc, ev, caller)
        if row is None:
            raise ApiError(404, "no_invite", "There is no invitation to answer.")
        if not accept:
            svc.odoo.cohost_answer(ev.d, caller, row["id"], "declined", int(time.time()))
            return {"state": "declined"}
        _refuse_closed(svc, ev)
        _refuse_not_member(_member(svc, caller))
        # exactly what a co-host link does (guests._join_account)
        if not guests._make_host(svc, ev.group, caller, {"guest_inviter": row["by"]}):
            raise ApiError(503, "relay", "The event could not be reached just now. Try again.")
        svc.odoo.cohost_answer(ev.d, caller, row["id"], "accepted", int(time.time()))
    return {"state": "accepted", "host": True}


def withdraw(svc: Services, ev: events.Event, caller: str, data: dict[str, Any]) -> dict[str, Any]:
    from .holder_invites import ApiError

    _own(svc)
    target = str(data.get("pubkey") or "").strip().lower()
    if not HEX.match(target):
        raise ApiError(422, "invalid", 'Send {"pubkey"}.')
    if not is_host(svc, ev, caller):
        raise ApiError(403, "not_allowed", "Only the event's hosts withdraw invitations.")
    with _lock:
        if pending(svc, ev, target) is None:
            raise ApiError(404, "no_invite", "There is no pending invitation for this person.")
        svc.odoo.cohost_drop(ev.d, target)
    return {"withdrawn": True}


def viewer_fields(svc: Services, ev: events.Event, viewer: str) -> dict[str, Any]:
    """``me.cohost_invite`` (the viewer's pending invitation, else null) and, for hosts,
    ``me.cohost_invites`` (everyone invited: pending, declined or accepted; lapsed ones are left
    out). Nothing with Odoo."""
    if not viewer or not own(svc):
        return {}
    out: dict[str, Any] = {"cohost_invite": None}
    mine = pending(svc, ev, viewer)
    host = is_host(svc, ev, viewer)
    rows = [r for r in svc.odoo.cohost_list(ev.d) if not lapsed(ev, r)] if host else []
    keys = {r["by"] for r in rows} | {r["pubkey"] for r in rows}
    if mine:
        keys.add(mine["by"])
    found = _people(svc, keys) if keys else {}
    if mine:
        out["cohost_invite"] = {
            "id": mine["id"],
            "by": _who(found, mine["by"]),
            "at": int(mine["at"]),
        }
    if host:
        out["cohost_invites"] = [
            {
                # the key is for withdrawing; apps show the person by name only
                "pubkey": r["pubkey"],
                "person": _who(found, r["pubkey"]),
                "by": _who(found, r["by"]),
                "state": r["state"],
                "at": int(r["answered"] or r["at"]),
            }
            for r in rows
        ]
    return out


# ── the push notification ───────────────────────────────────────────────────────────────────
def notify(svc: Services, ev: events.Event, pubkey: str, inviter: str) -> int:
    """Push type ``cohost_invite`` to the invitee's phones, when push is on. Nothing when they
    turned the event's group off in their notification settings. No e-mail."""
    from . import push as push_mod

    try:
        if not push_mod.enabled(svc):
            return 0
        devices = svc.odoo.push_devices([pubkey])
        if not devices:
            return 0
        partner = next((m2o_id(d.get("partner_id")) for d in devices if d.get("partner_id")), None)
        if partner:
            prefs = push_mod.get_settings(svc, int(partner))
            if prefs["channels"].get(ev.group) == "off":
                return 0
        gateway = push_mod.Gateway(svc)
        plain = {
            "v": 1,
            "domain": svc.settings.nip05_domain,
            "type": "cohost_invite",
            "title": f"{inviter} invited you to co-host",
            "body": ev.title or "An event",
            "channel": ev.group,
            "url": events.chat_url(svc, ev),
        }
        sent = 0
        for d in devices:
            payload = push_mod.encrypt(d["key"], push_mod.fit(plain))
            if gateway.send(d, payload, ev.group, True) == "ok":
                sent += 1
        return sent
    except Exception:  # noqa: BLE001 — the app shows the invitation on the event anyway
        log.exception("cohost %s: no push", ev.d)
        return 0


# ── routes ──────────────────────────────────────────────────────────────────────────────────
def register(app: FastAPI) -> None:
    from .holder_invites import ApiError

    async def event_of(s: Services, d: str) -> events.Event:
        ev = await run_in_threadpool(events.load, s, d)
        if ev is None:
            raise ApiError(404, "unknown", "No such event.")
        return ev

    async def run(request: Request, d: str, suffix: str, fn: Any) -> JSONResponse:
        path = f"{events.API}/{d}/cohost-invites{suffix}"
        s, caller, data = await events._signed(request, "POST", path)
        _own(s)
        ev = await event_of(s, d)
        out = await run_in_threadpool(fn, s, ev, caller, data)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    @app.post(PATH)
    async def cohost_invite(request: Request, d: str) -> JSONResponse:
        return await run(request, d, "", invite)

    @app.post(PATH + "/answer")
    async def cohost_answer(request: Request, d: str) -> JSONResponse:
        return await run(request, d, "/answer", answer)

    @app.post(PATH + "/withdraw")
    async def cohost_withdraw(request: Request, d: str) -> JSONResponse:
        return await run(request, d, "/withdraw", withdraw)


__all__ = ["register", "invite", "answer", "withdraw", "viewer_fields", "notify"]
