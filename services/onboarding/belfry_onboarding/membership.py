"""Membership of the belfry: who may invite members, how requests to join are accepted, and the
requests themselves (Xavier, 2026-11-14).

Belfry membership and joining a group are two different things. A group (a channel with a
parent, ``pod-…`` ids) is made by any member, who is its admin and brings people into it
(members directly, anyone else as a guest of that group: guests.py). Membership of the belfry is
the admins' business unless they open it up here:

- **Who can invite members** (``invite``): ``admins`` (default) or ``members``. With ``members``
  any member (not a guest, not an agent) may make a member link (``POST /channel-invites``
  ``{"as": "member"}``, guests.py), for the default role only.
- **How requests to join are accepted** (``accept``): ``admins`` (default: an admin accepts or
  declines) or ``vouches``: once ``vouches`` (1–5) different members vouched for a request, it is
  accepted as an admin's acceptance would be (an invite mailed to the applicant: the invite page,
  name + e-mail, then the membership and its welcome mail). Admins can still accept or decline
  any request at any time.

Requests to join the belfry are the applications of ``/apply`` (apply.py; register model
``belfry.application``: name, e-mail, motivation, state ``new`` → ``invited`` | ``declined``).
A vouch is kept on the application (``vouched_by``: member keys); only the keys that are members
now count (not guests, not agents), so a vouch from someone who left stops counting.

Stored in the register param ``belfry.membership`` (JSON ``{"invite", "accept", "vouches"}``).
Needs the belfry's own register (REGISTER=belfry); with Odoo the read answers the defaults and
every change or request call answers 409 ``unsupported`` (OSV answers applications on its
steward pages).

API (belfry.<domain>/api/v1/…, NIP-98):

- ``GET /membership`` (anyone with an active pass) → ``{"invite": "admins"|"members", "accept":
  "admins"|"vouches", "vouches": 1..5, "may_invite_members": bool, "admin": bool}``.
- ``POST /membership`` ``{"invite"?, "accept"?, "vouches"?}`` (admins) → the same. Turning
  vouching on (or lowering the number) accepts the requests that have enough vouches already.
- ``POST /applications``:
  - ``{"list": true, "state"?: "new"|"all"}`` → ``{"applications": [{"id", "name",
    "motivation", "kind", "state", "created", "vouches", "vouched"}], "accept", "needed"}``.
    Admins see every request (with ``email``); members see the ones waiting, without e-mail,
    only while vouching is on (else 403 ``not_admin``).
  - ``{"decide": <id>, "approve": true|false}`` (admins) → ``{"application"}``.
  - ``{"vouch": <id>, "on"?: true|false}`` (members, while vouching is on; else 403
    ``not_vouching``) → ``{"application", "accepted": bool}``.

Errors (``{"error", "message"}``; clients show fixed words per code): 403 ``not_admin``, 403
``not_here``, 403 ``not_vouching``, 404 ``not_found``, 409 ``decided``, 422 ``invalid``, 409
``unsupported``.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .clients.odoo import OdooError
from .core import Services

log = logging.getLogger(__name__)

PARAM = "belfry.membership"
INVITE = ("admins", "members")
ACCEPT = ("admins", "vouches")
VOUCHES_MIN, VOUCHES_MAX = 1, 5
DEFAULTS: dict[str, Any] = {"invite": "admins", "accept": "admins", "vouches": 2}
BY_VOUCHES = "vouches"  # handled_by of an application accepted by its vouches
NO_STORE = {"Cache-Control": "no-store"}

_lock = threading.RLock()


def own(svc: Services) -> bool:
    from . import invite

    return invite.own(svc)


# ── the settings ────────────────────────────────────────────────────────────────────────────
def settings(svc: Services) -> dict[str, Any]:
    out = dict(DEFAULTS)
    if not own(svc):
        return out
    try:
        data = json.loads(svc.odoo.param(PARAM, "{}") or "{}")
    except ValueError:
        data = {}
    if isinstance(data, dict):
        if data.get("invite") in INVITE:
            out["invite"] = data["invite"]
        if data.get("accept") in ACCEPT:
            out["accept"] = data["accept"]
        n = data.get("vouches")
        if isinstance(n, int) and not isinstance(n, bool) and VOUCHES_MIN <= n <= VOUCHES_MAX:
            out["vouches"] = n
    return out


def change(svc: Services, data: dict[str, Any]) -> dict[str, Any]:
    from .holder_invites import ApiError

    if not own(svc):
        raise ApiError(409, "unsupported", "Membership settings need the belfry's own register.")
    with _lock:
        cur = settings(svc)
        if "invite" in data:
            if data["invite"] not in INVITE:
                raise ApiError(422, "invalid", 'invite: "admins" or "members".')
            cur["invite"] = data["invite"]
        if "accept" in data:
            if data["accept"] not in ACCEPT:
                raise ApiError(422, "invalid", 'accept: "admins" or "vouches".')
            cur["accept"] = data["accept"]
        if "vouches" in data:
            n = data["vouches"]
            if isinstance(n, bool) or not isinstance(n, int) or not VOUCHES_MIN <= n <= VOUCHES_MAX:
                raise ApiError(422, "invalid", f"vouches: {VOUCHES_MIN} to {VOUCHES_MAX}.")
            cur["vouches"] = n
        svc.odoo.set_param(PARAM, json.dumps(cur, sort_keys=True))
    log.info("membership: %s", cur)
    if cur["accept"] == "vouches":
        settle(svc)
    return cur


def is_admin(svc: Services, pubkey: str) -> bool:
    from . import apps

    return pubkey in apps.admin_pubkeys(svc)


def is_member(svc: Services, pubkey: str) -> bool:
    """A member of the belfry: an active pass that is neither a guest's nor an agent's."""
    from . import whois

    return whois.kinds(svc).get(pubkey) == "member"


def may_invite_members(svc: Services, pubkey: str, steward: bool = False) -> bool:
    if steward or is_admin(svc, pubkey):
        return True
    return settings(svc)["invite"] == "members" and is_member(svc, pubkey)


def read(svc: Services, caller: str) -> dict[str, Any]:
    from . import whois
    from .holder_invites import ApiError

    if caller not in whois.kinds(svc):
        raise ApiError(403, "not_here", "Only people of this belfry can ask.")
    return {
        **settings(svc),
        "may_invite_members": may_invite_members(svc, caller),
        "admin": is_admin(svc, caller),
    }


# ── requests to join the belfry (applications) ─────────────────────────────────────────────
FIELDS = ["name", "email", "motivation", "kind", "state", "create_date", "vouched_by"]


def _counted(svc: Services, keys: Any) -> list[str]:
    """The keys among KEYS that are members now (vouches of people who left stop counting)."""
    from . import whois

    kinds = whois.kinds(svc)
    seen: list[str] = []
    for k in keys if isinstance(keys, list) else []:
        if isinstance(k, str) and kinds.get(k) == "member" and k not in seen:
            seen.append(k)
    return seen


def _row(svc: Services, r: dict[str, Any], caller: str, admin: bool) -> dict[str, Any]:
    vouched = _counted(svc, r.get("vouched_by"))
    out = {
        "id": r["id"],
        "name": r.get("name") or "",
        "motivation": r.get("motivation") or "",
        "kind": r.get("kind") or "",
        "state": r.get("state") or "",
        "created": r.get("create_date") or "",
        "vouches": len(vouched),
        "vouched": caller in vouched,
    }
    if admin:
        out["email"] = r.get("email") or ""
    return out


def _get(svc: Services, aid: Any) -> dict[str, Any]:
    from .holder_invites import ApiError

    if isinstance(aid, bool) or not isinstance(aid, int):
        raise ApiError(422, "invalid", "id: a number.")
    rows = svc.odoo.search_read("belfry.application", [["id", "=", aid]], FIELDS)
    if not rows:
        raise ApiError(404, "not_found", "No such request.")
    return rows[0]


def _admin_email(svc: Services, caller: str) -> tuple[str, str]:
    from .core import m2o_id
    from .places import member_key

    row = member_key(svc, caller) or {}
    pid = m2o_id(row.get("partner_id"))
    partner = (svc.odoo.read("res.partner", [pid], ["email", "name"]) or [{}])[0] if pid else {}
    return (partner.get("email") or "admin").lower(), partner.get("name") or ""


def accept(svc: Services, aid: int, by_email: str, by_name: str = "") -> None:
    """Accept an application as an admin would: an invite mailed to the applicant."""
    from .holder_invites import ApiError

    try:
        svc.odoo.call(
            "belfry.application",
            "belfry_application_invite",
            [[aid], by_email, by_name or False],
        )
    except OdooError as exc:
        raise ApiError(409, "decided", "This request was answered already.") from exc


def decline(svc: Services, aid: int, by_email: str) -> None:
    from .holder_invites import ApiError

    try:
        svc.odoo.call("belfry.application", "belfry_application_decline", [[aid], by_email])
    except OdooError as exc:
        raise ApiError(409, "decided", "This request was answered already.") from exc


def settle(svc: Services) -> list[int]:
    """While vouching is on: accept every waiting request with enough vouches. Idempotent (an
    accepted one is no longer ``new``); called after a vouch, a settings change and by the
    service's round (followers.tick)."""
    if not own(svc):
        return []
    s = settings(svc)
    if s["accept"] != "vouches":
        return []
    done: list[int] = []
    with _lock:
        for r in svc.odoo.search_read("belfry.application", [["state", "=", "new"]], FIELDS):
            n = len(_counted(svc, r.get("vouched_by")))
            if n < s["vouches"]:
                continue
            try:
                accept(svc, r["id"], BY_VOUCHES, f"{n} members")
            except Exception as exc:  # noqa: BLE001 — logged; tried again on the next round
                log.warning("membership: request %s not accepted: %s", r["id"], exc)
                continue
            log.info("membership: request %s accepted by %d vouches", r["id"], n)
            done.append(r["id"])
    return done


def applications(svc: Services, caller: str, data: dict[str, Any]) -> dict[str, Any]:
    from .holder_invites import ApiError

    if not own(svc):
        raise ApiError(409, "unsupported", "Requests need the belfry's own register.")
    admin = is_admin(svc, caller)
    member = admin or is_member(svc, caller)
    if not member:
        raise ApiError(403, "not_here", "Only members of this belfry can see its requests.")
    s = settings(svc)
    vouching = s["accept"] == "vouches"

    if "decide" in data:
        if not admin:
            raise ApiError(403, "not_admin", "Only the belfry's admins answer requests.")
        if not isinstance(data.get("approve"), bool):
            raise ApiError(422, "invalid", 'Send {"decide": <id>, "approve": true|false}.')
        r = _get(svc, data["decide"])
        if r.get("state") != "new":
            raise ApiError(409, "decided", "This request was answered already.")
        email, name = _admin_email(svc, caller)
        if data["approve"]:
            accept(svc, r["id"], email, name)
        else:
            decline(svc, r["id"], email)
        log.info("membership: request %s %s by %s…", r["id"], data["approve"], caller[:8])
        return {"application": _row(svc, _get(svc, r["id"]), caller, True)}

    if "vouch" in data:
        if not vouching:
            raise ApiError(403, "not_vouching", "Requests here are accepted by the admins.")
        on = data.get("on", True)
        if not isinstance(on, bool):
            raise ApiError(422, "invalid", "on: true or false.")
        if not is_member(svc, caller):  # an admin without a member's pass can't vouch
            raise ApiError(403, "not_here", "Only members vouch.")
        with _lock:
            r = _get(svc, data["vouch"])
            if r.get("state") != "new":
                raise ApiError(409, "decided", "This request was answered already.")
            keys = [k for k in (r.get("vouched_by") or []) if isinstance(k, str)]
            if on and caller not in keys:
                keys.append(caller)
            elif not on:
                keys = [k for k in keys if k != caller]
            svc.odoo.write("belfry.application", [r["id"]], {"vouched_by": keys})
        log.info(
            "membership: %s… %s request %s",
            caller[:8],
            "vouches for" if on else "unvouches",
            r["id"],
        )
        accepted = r["id"] in settle(svc)
        return {"application": _row(svc, _get(svc, r["id"]), caller, admin), "accepted": accepted}

    if not admin and not vouching:
        raise ApiError(403, "not_admin", "Only the belfry's admins see requests to join.")
    domain = [] if data.get("state") == "all" and admin else [["state", "=", "new"]]
    rows = svc.odoo.search_read("belfry.application", domain, FIELDS, limit=100, order="id desc")
    return {
        "applications": [_row(svc, r, caller, admin) for r in rows],
        "accept": s["accept"],
        "needed": s["vouches"],
    }


# ── the API ─────────────────────────────────────────────────────────────────────────────────
def register(app: FastAPI) -> None:
    from .holder_invites import ApiError, authenticate

    def caller_of(request: Request, method: str, path: str, body: bytes) -> str:
        s = request.app.state.services
        return authenticate(
            s, request.app.state.api_seen, request.headers.get("authorization"), method, path, body
        )

    def body_of(body: bytes) -> dict[str, Any]:
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_json", "Send a JSON object.")
        return data

    @app.get("/api/membership")
    async def membership_get(request: Request) -> JSONResponse:
        s = request.app.state.services
        caller = caller_of(request, "GET", "/api/membership", b"")
        return JSONResponse(await run_in_threadpool(read, s, caller), headers=NO_STORE)

    @app.post("/api/membership")
    async def membership_set(request: Request) -> JSONResponse:
        s = request.app.state.services
        body = await request.body()
        caller = caller_of(request, "POST", "/api/membership", body)
        data = body_of(body)

        def run() -> dict[str, Any]:
            if not is_admin(s, caller):
                raise ApiError(403, "not_admin", "Only the belfry's admins change this.")
            change(s, data)
            return read(s, caller)

        return JSONResponse(await run_in_threadpool(run), headers=NO_STORE)

    @app.post("/api/applications")
    async def applications_api(request: Request) -> JSONResponse:
        s = request.app.state.services
        body = await request.body()
        caller = caller_of(request, "POST", "/api/applications", body)
        data = body_of(body)
        out = await run_in_threadpool(applications, s, caller, data)
        return JSONResponse(out, headers=NO_STORE)
