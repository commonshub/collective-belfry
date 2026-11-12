"""Roles of a belfry's members (Xavier, 2026-11-12, design option C on the canvas).

A role is a label and an optional line of description. Every member has exactly one; guests and
agents have none. Two are built in and can be renamed and described but never deleted:

- ``member``: what a member is unless an admin says otherwise;
- ``admin``: the belfry's admins. The truth stays where it was: the stewards group (console.py,
  ``apps.admin_pubkeys``, belfry.json "admins"); giving or taking this role goes through
  ``admins.change`` (with its last-admin rule), so the two never disagree.

Custom roles have no powers yet: they are labels shown on profiles and in members lists. One role
can be the default for new members (``member`` unless an admin picks another; never ``admin``):
the invite sheet starts on it and a new membership gets it (core._finish_activation). A member
without a stored role is a ``member``, whatever the default says: changing the default never
relabels the people who are here already.

Storage (the belfry's own register only, REGISTER=belfry): the list in the register param
``belfry.roles`` (JSON ``{"roles": [{"id", "label", "description"}], "default": "<id>"}``, built-ins
included once they were edited), a member's role on their contact (``res.partner.belfry_role``),
so it survives a new pass. With Odoo the API answers the two built-ins and refuses changes (409
``unsupported``).

The API (belfry.<domain>/api/v1/…, NIP-98):

- ``GET /roles`` (anyone with an active pass) → ``{"roles": [{"id", "label", "description",
  "builtin", "default", "powers"}], "default": "<id>", "members": {"<hex>": "<role id>"}}``.
  ``members`` lists every member's key (guests get ``{}``; they ask ``/whois`` for the keys they
  see). Admins also get ``"others": {"guest": n, "agent": n}``.
- ``POST /roles`` ``{"label", "description"?, "default"?: bool}`` (admins) → 201 ``{"role",
  "roles", "default"}``.
- ``POST /roles/<id>`` ``{"label"?, "description"?, "default"?: bool}`` (admins) → ``{"role",
  "roles", "default"}``. ``default: false`` on the default puts ``member`` back.
- ``POST /roles/<id>/delete`` ``{}`` (admins; not a built-in) → ``{"deleted", "moved": [hex],
  "roles", "default"}``: who had it gets the default role.
- ``POST /member-roles`` ``{"pubkey", "role"}`` (admins) → ``{"pubkey", "role", "admins"}``. A
  guest who saved their account becomes a member with that role (same key; their guest passes
  close, they stay in their channels); a guest without an e-mail: 400 ``guest_not_saved``; an
  agent: 400 ``agent``.

Errors (``{"error", "message"}``): 403 ``not_admin``, 404 ``unknown_role``, 422 ``invalid``
(label 1–40 characters, description up to 200), 409 ``taken`` (a role with that label exists),
400 ``builtin`` (deleting member/admin), 422 ``admin_default``, 400 ``not_member``, 400
``last_admin``, 409 ``too_many`` (50 roles), 409 ``unsupported``.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import unicodedata
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .core import Services, m2o_id

log = logging.getLogger(__name__)

PARAM = "belfry.roles"
MEMBER, ADMIN = "member", "admin"
BUILTIN: dict[str, dict[str, str]] = {
    MEMBER: {"label": "Member", "description": "Joins any open channel, invites guests, votes."},
    ADMIN: {"label": "Admin", "description": "Everything, plus settings and roles."},
}
RESERVED = {MEMBER, ADMIN, "guest", "agent", "steward", "default", "delete", "new"}
MAX_ROLES = 50
LABEL_MAX, DESCRIPTION_MAX = 40, 200
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
HEX = re.compile(r"^[0-9a-f]{64}$")
# membership lengths a member link can give (POST /channel-invites "membership")
MEMBERSHIP_DAYS = {"month": 30, "year": 365, "permanent": 0}

_lock = threading.RLock()


def own(svc: Services) -> bool:
    from . import invite

    return invite.own(svc)


# ── the list ────────────────────────────────────────────────────────────────────────────────
def _stored(svc: Services) -> dict[str, Any]:
    if not own(svc):
        return {}
    try:
        data = json.loads(svc.odoo.param(PARAM, "{}") or "{}")
    except ValueError:
        log.warning("roles: %s is not JSON; the built-ins only", PARAM)
        return {}
    return data if isinstance(data, dict) else {}


def listing(svc: Services) -> tuple[list[dict[str, Any]], str]:
    """Every role in order (member, the custom ones as they were added, admin) and the default."""
    data = _stored(svc)
    rows = [
        r for r in data.get("roles") or [] if isinstance(r, dict) and ID.match(str(r.get("id")))
    ]
    by_id = {r["id"]: r for r in rows}
    out: list[dict[str, Any]] = []

    def row(rid: str, r: dict[str, Any], builtin: bool) -> dict[str, Any]:
        return {
            "id": rid,
            "label": str(r.get("label") or BUILTIN.get(rid, {}).get("label") or rid)[:LABEL_MAX],
            "description": str(r.get("description") or "")[:DESCRIPTION_MAX]
            if "description" in r
            else BUILTIN.get(rid, {}).get("description", ""),
            "builtin": builtin,
            "powers": rid == ADMIN,
        }

    out.append(row(MEMBER, by_id.get(MEMBER, {}), True))
    out += [row(r["id"], r, False) for r in rows if r["id"] not in BUILTIN]
    out.append(row(ADMIN, by_id.get(ADMIN, {}), True))
    default = str(data.get("default") or MEMBER)
    if default == ADMIN or default not in {r["id"] for r in out}:
        default = MEMBER
    for r in out:
        r["default"] = r["id"] == default
    return out, default


def _save(svc: Services, roles: list[dict[str, Any]], default: str) -> None:
    keep = [{"id": r["id"], "label": r["label"], "description": r["description"]} for r in roles]
    svc.odoo.set_param(PARAM, json.dumps({"roles": keep, "default": default}, sort_keys=True))


def exists(svc: Services, rid: str) -> bool:
    return any(r["id"] == rid for r in listing(svc)[0])


def default_role(svc: Services) -> str:
    return listing(svc)[1]


def _slug(label: str) -> str:
    text = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:32] or "role"
    return text if text[0].isalnum() else "r" + text


def _clean(data: dict[str, Any], partial: bool) -> dict[str, Any]:
    from .holder_invites import ApiError

    out: dict[str, Any] = {}
    if "label" in data or not partial:
        label = (
            " ".join(str(data.get("label") or "").split())
            if isinstance(data.get("label"), str)
            else ""
        )
        if not label or len(label) > LABEL_MAX:
            raise ApiError(422, "invalid", f"label: 1 to {LABEL_MAX} characters.")
        out["label"] = label
    if "description" in data:
        d = data.get("description")
        if d is not None and not isinstance(d, str):
            raise ApiError(422, "invalid", "description: text.")
        d = " ".join((d or "").split())
        if len(d) > DESCRIPTION_MAX:
            raise ApiError(422, "invalid", f"description: at most {DESCRIPTION_MAX} characters.")
        out["description"] = d
    if "default" in data:
        if not isinstance(data["default"], bool):
            raise ApiError(422, "invalid", "default: true or false.")
        out["default"] = data["default"]
    return out


def _unique_label(roles: list[dict[str, Any]], label: str, skip: str = "") -> None:
    from .holder_invites import ApiError

    if any(r["id"] != skip and r["label"].casefold() == label.casefold() for r in roles):
        raise ApiError(409, "taken", "A role with that label exists.")


def create(svc: Services, data: dict[str, Any]) -> dict[str, Any]:
    from .holder_invites import ApiError

    vals = _clean(data, partial=False)
    with _lock:
        roles, default = listing(svc)
        if len(roles) >= MAX_ROLES:
            raise ApiError(409, "too_many", f"A belfry has at most {MAX_ROLES} roles.")
        _unique_label(roles, vals["label"])
        base, n = _slug(vals["label"]), 1
        rid = base if base not in RESERVED else base + "-2"
        taken = {r["id"] for r in roles} | RESERVED
        while rid in taken:
            n += 1
            rid = f"{base}-{n}"
        role = {
            "id": rid,
            "label": vals["label"],
            "description": vals.get("description", ""),
        }
        roles.insert(len(roles) - 1, {**role, "builtin": False, "powers": False})  # before admin
        if vals.get("default"):
            default = rid
        _save(svc, roles, default)
    return rid_answer(svc, rid)


def update(svc: Services, rid: str, data: dict[str, Any]) -> dict[str, Any]:
    from .holder_invites import ApiError

    vals = _clean(data, partial=True)
    with _lock:
        roles, default = listing(svc)
        role = next((r for r in roles if r["id"] == rid), None)
        if role is None:
            raise ApiError(404, "unknown_role", "No such role.")
        if "label" in vals:
            _unique_label(roles, vals["label"], rid)
            role["label"] = vals["label"]
        if "description" in vals:
            role["description"] = vals["description"]
        if vals.get("default") is True:
            if rid == ADMIN:
                raise ApiError(422, "admin_default", "Admin can't be the default role.")
            default = rid
        elif vals.get("default") is False and default == rid:
            default = MEMBER
        _save(svc, roles, default)
    return rid_answer(svc, rid)


def delete(svc: Services, rid: str) -> dict[str, Any]:
    from .holder_invites import ApiError

    with _lock:
        roles, default = listing(svc)
        if rid in BUILTIN:
            raise ApiError(400, "builtin", "Member and Admin are built in.")
        if not any(r["id"] == rid for r in roles):
            raise ApiError(404, "unknown_role", "No such role.")
        if default == rid:
            default = MEMBER
        moved = holders(svc, rid)
        to = default
        partners = svc.odoo.search_read("res.partner", [["belfry_role", "=", rid]], ["id"])
        if partners:
            svc.odoo.write("res.partner", [p["id"] for p in partners], {"belfry_role": to})
        _save(svc, [r for r in roles if r["id"] != rid], default)
    roles, default = listing(svc)
    log.info("roles: %s deleted, %d moved to %s", rid, len(moved), to)
    return {"deleted": rid, "moved": moved, "roles": roles, "default": default}


def rid_answer(svc: Services, rid: str) -> dict[str, Any]:
    roles, default = listing(svc)
    return {"role": next(r for r in roles if r["id"] == rid), "roles": roles, "default": default}


# ── who has which ───────────────────────────────────────────────────────────────────────────
def _member_rows(svc: Services) -> list[dict[str, Any]]:
    """Active non-guest, non-agent passes with a key: {key, partner}."""
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "!=", False]],
        ["keycast_pubkey", "tier", "partner_id"],
    )
    out: dict[str, int] = {}
    agents: set[str] = set()
    for r in rows:
        key, tier = r.get("keycast_pubkey"), r.get("tier")
        if tier == "agent":
            agents.add(key)
        elif tier != "guest" and key and (pid := m2o_id(r.get("partner_id"))):
            out.setdefault(key, pid)
    return [{"key": k, "partner": p} for k, p in out.items() if k not in agents]


def _stored_roles(svc: Services, partners: list[int]) -> dict[int, str]:
    if not own(svc) or not partners:
        return {}
    rows = svc.odoo.read("res.partner", sorted(set(partners)), ["belfry_role"])
    return {r["id"]: str(r.get("belfry_role") or "") for r in rows}


def members(svc: Services) -> dict[str, str]:
    """Every member's key → their role id."""
    from . import apps

    admins = apps.admin_pubkeys(svc)
    valid = {r["id"] for r in listing(svc)[0]}
    rows = _member_rows(svc)
    stored = _stored_roles(svc, [r["partner"] for r in rows])
    out: dict[str, str] = {}
    for r in rows:
        if r["key"] in admins:
            out[r["key"]] = ADMIN
            continue
        role = stored.get(r["partner"]) or MEMBER
        out[r["key"]] = role if role in valid and role != ADMIN else MEMBER
    return out


def role_of(svc: Services, pubkey: str) -> str | None:
    """PUBKEY's role; None for a guest, an agent or a stranger."""
    return members(svc).get(pubkey)


def holders(svc: Services, rid: str) -> list[str]:
    return sorted(k for k, r in members(svc).items() if r == rid)


def give_default(svc: Services, partner_id: int) -> None:
    """A new membership (core._finish_activation): the default role, unless they have one."""
    if not own(svc):
        return
    default = default_role(svc)
    if default == MEMBER:
        return
    row = svc.odoo.read_one("res.partner", partner_id, ["belfry_role"]) or {}
    if not row.get("belfry_role"):
        svc.odoo.write("res.partner", [partner_id], {"belfry_role": default})


def _partner_of(svc: Services, pubkey: str) -> int | None:
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "=", pubkey]],
        ["partner_id", "tier"],
    )
    return next((m2o_id(r.get("partner_id")) for r in rows if r.get("tier") != "guest"), None)


def after_admin_change(svc: Services, pubkey: str, admin: bool) -> None:
    """admins.change gave or took the admin role: the stored role follows."""
    if not own(svc):
        return
    pid = _partner_of(svc, pubkey)
    if pid is None:
        return
    row = svc.odoo.read_one("res.partner", pid, ["belfry_role"]) or {}
    if admin:
        svc.odoo.write("res.partner", [pid], {"belfry_role": ADMIN})
    elif row.get("belfry_role") in (ADMIN, None, False, ""):
        svc.odoo.write("res.partner", [pid], {"belfry_role": default_role(svc)})


def make_member(svc: Services, pubkey: str, role: str, days: int = 0) -> bool:
    """A guest who saved their account (an e-mail on it) becomes a member: the same key, a
    membership of ROLE for DAYS days (0: no end), their guest passes closed (they stay in their
    channels, guests.became_member). False when PUBKEY is a member already (nothing changes)."""
    from . import apps, console, core, guests
    from .holder_invites import ApiError
    from .places import member_key

    if member_key(svc, pubkey) is not None:
        return False
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "=", pubkey]],
        ["tier", "partner_id", "guest_agent"],
    )
    rows = [r for r in rows if r.get("tier") == "guest"]
    if not rows:
        raise ApiError(400, "not_member", "Nobody here has that key.")
    if any(r.get("guest_agent") for r in rows):
        raise ApiError(400, "agent", "An agent can't be a member this way.")
    pid = m2o_id(rows[0].get("partner_id"))
    partner = svc.odoo.read_one("res.partner", pid or 0, ["email"]) or {}
    email = (partner.get("email") or "").strip().lower()
    if not pid or not email:
        raise ApiError(
            400, "guest_not_saved", "This guest has no e-mail address yet: they save it first."
        )
    with _lock:
        if member_key(svc, pubkey) is not None:
            return False
        svc.odoo.write("res.partner", [pid], {"belfry_role": role})
        pass_id = svc.odoo.create(
            "belfry.pass",
            {
                "partner_id": pid,
                "tier": "member",
                "membership_days": int(days) or False,
                "manual_paid_by": "member invite",
            },
        )
        core.activate_free_pass(svc, pass_id)
    if member_key(svc, pubkey) is None:
        raise ApiError(500, "not_promoted", "The membership did not open.")
    if role == ADMIN:
        console.set_admin(svc, email, True)
        apps.forget()
        console._admins["at"] = 0.0
    guests.became_member(svc, pubkey)
    core.sync_relay_whitelist(svc)
    log.info("roles: %s… a member now (%s, %s days)", pubkey[:8], role, days or "no end")
    return True


def set_member(svc: Services, caller: str, pubkey: str, role: str) -> dict[str, Any]:
    from . import admins, apps
    from .holder_invites import ApiError
    from .places import member_key

    apps.forget()
    if caller not in apps.admin_pubkeys(svc):
        raise ApiError(403, "not_admin", "Only the belfry's admins give roles.")
    if not exists(svc, role):
        raise ApiError(404, "unknown_role", "No such role.")
    if not own(svc) and role not in BUILTIN:
        raise ApiError(409, "unsupported", "Roles need the belfry's own register.")
    is_member = member_key(svc, pubkey) is not None
    agent = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "=", pubkey], ["tier", "=", "agent"]],
        ["id"],
    )
    if agent:
        raise ApiError(400, "agent", "An agent has no role.")
    if not is_member:  # a guest: a member with that role, once their account has an e-mail
        if role == ADMIN:
            admins.change(svc, caller, pubkey, True)  # its own checks and messages
        else:
            make_member(svc, pubkey, role)
    elif role == ADMIN:
        admins.change(svc, caller, pubkey, True)
    else:
        if pubkey in apps.admin_pubkeys(svc):
            admins.change(svc, caller, pubkey, False)  # last_admin stays refused
        pid = _partner_of(svc, pubkey)
        if pid is not None and own(svc):
            svc.odoo.write("res.partner", [pid], {"belfry_role": role})
    apps.forget()
    return {
        "pubkey": pubkey,
        "role": role_of(svc, pubkey),
        "admins": sorted(apps.admin_pubkeys(svc)),
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

    def admin_only(svc: Services, caller: str) -> None:
        from . import apps

        if caller not in apps.admin_pubkeys(svc):
            raise ApiError(403, "not_admin", "Only the belfry's admins change roles.")
        if not own(svc):
            raise ApiError(409, "unsupported", "Roles need the belfry's own register.")

    def read(svc: Services, caller: str) -> dict[str, Any]:
        from . import apps, whois

        kinds = whois.kinds(svc)
        if caller not in kinds:
            raise ApiError(403, "not_here", "Only people of this belfry can ask.")
        roles, default = listing(svc)
        out: dict[str, Any] = {"roles": roles, "default": default, "members": {}}
        if kinds[caller] != "guest":
            out["members"] = members(svc)
        if caller in apps.admin_pubkeys(svc):
            counts = {"guest": 0, "agent": 0}
            for kind in kinds.values():
                if kind in counts:
                    counts[kind] += 1
            out["others"] = counts
        return out

    NO_STORE = {"Cache-Control": "no-store"}

    @app.get("/api/roles")
    async def roles_get(request: Request) -> JSONResponse:
        s = request.app.state.services
        caller = caller_of(request, "GET", "/api/roles", b"")
        return JSONResponse(await run_in_threadpool(read, s, caller), headers=NO_STORE)

    @app.post("/api/roles")
    async def roles_create(request: Request) -> JSONResponse:
        s = request.app.state.services
        body = await request.body()
        caller = caller_of(request, "POST", "/api/roles", body)
        data = body_of(body)
        await run_in_threadpool(admin_only, s, caller)
        out = await run_in_threadpool(create, s, data)
        return JSONResponse(out, status_code=201, headers=NO_STORE)

    @app.post("/api/roles/{rid}")
    async def roles_update(request: Request, rid: str) -> JSONResponse:
        s = request.app.state.services
        body = await request.body()
        caller = caller_of(request, "POST", f"/api/roles/{rid}", body)
        data = body_of(body)
        await run_in_threadpool(admin_only, s, caller)
        return JSONResponse(await run_in_threadpool(update, s, rid, data), headers=NO_STORE)

    @app.post("/api/roles/{rid}/delete")
    async def roles_delete(request: Request, rid: str) -> JSONResponse:
        s = request.app.state.services
        body = await request.body()
        caller = caller_of(request, "POST", f"/api/roles/{rid}/delete", body)
        await run_in_threadpool(admin_only, s, caller)
        return JSONResponse(await run_in_threadpool(delete, s, rid), headers=NO_STORE)

    @app.post("/api/member-roles")
    async def member_roles(request: Request) -> JSONResponse:
        s = request.app.state.services
        body = await request.body()
        caller = caller_of(request, "POST", "/api/member-roles", body)
        data = body_of(body)
        target = str(data.get("pubkey") or "").lower()
        role = str(data.get("role") or "")
        if not HEX.match(target) or not role:
            raise ApiError(422, "invalid", 'Send {"pubkey": "<hex>", "role": "<id>"}.')
        out = await run_in_threadpool(set_member, s, caller, target, role)
        return JSONResponse(out, headers=NO_STORE)
