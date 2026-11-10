"""The belfry's admins, from the chat: ``POST /api/admins`` (belfry.<domain>/api/v1/admins).

NIP-98 by a current admin, ``{"pubkey": "<hex>", "admin": true|false}``. An admin is a member of
the stewards group (console.admin_emails); the target must be an active member. A channel's guest
who saved their account (an e-mail on it) becomes a member with the role: same key, a membership
next to the guest passes (guests.became_member). A guest without an e-mail and an agent can't.
The last admin can't be removed. The relay sync follows at once, so the new admin administers
every channel (zooid patch 0003) and belfry.json lists them.
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .core import Services

PATH = "/api/admins"
HEX = re.compile(r"^[0-9a-f]{64}$")


def change(svc: Services, caller: str, target: str, admin: bool) -> list[str]:
    from . import apps, console, core
    from .holder_invites import ApiError
    from .places import member_key

    apps.forget()
    console._admins["at"] = 0.0
    if caller not in apps.admin_pubkeys(svc):
        raise ApiError(403, "not_admin", "Only the belfry's admins give the admin role.")
    row = member_key(svc, target)
    guest_passes: list[dict[str, Any]] = []
    if row is None:
        guest_passes = [
            r
            for r in svc.odoo.search_read(
                "belfry.pass",
                [["state", "=", "active"], ["keycast_pubkey", "=", target]],
                ["tier", "partner_id", "guest_agent"],
            )
            if r.get("tier") == "guest"
        ]
        if any(r.get("guest_agent") for r in guest_passes):
            raise ApiError(400, "agent", "An agent can't be an admin.")
        row = guest_passes[0] if guest_passes else None
    partner = core.m2o_id(row.get("partner_id")) if row else None
    email = ""
    if partner:
        email = (
            ((svc.odoo.read("res.partner", [partner], ["email"]) or [{}])[0].get("email") or "")
            .strip()
            .lower()
        )
    if not email and guest_passes:
        raise ApiError(
            400,
            "guest_not_saved",
            "This guest has no e-mail address yet. Once they save their account, "
            "you can make them an admin.",
        )
    if not email or (guest_passes and not admin):
        raise ApiError(400, "not_member", "Only active members can be admins.")
    current = apps.admin_pubkeys(svc)
    if not admin and target in current and len(current) <= 1:
        raise ApiError(400, "last_admin", "A belfry keeps at least one admin.")
    if admin != (target in current):
        console.set_admin(svc, email, admin)  # opens the admin's membership (steward_pass)
        apps.forget()
        if guest_passes:
            from . import guests

            if member_key(svc, target) is None:
                raise ApiError(500, "not_promoted", "The guest's membership did not open.")
            guests.became_member(svc, target)
    core.sync_relay_whitelist(svc)
    return sorted(apps.admin_pubkeys(svc))


def register(app: FastAPI) -> None:
    from .holder_invites import ApiError, authenticate

    @app.post(PATH)
    async def set_admin(request: Request) -> JSONResponse:
        s = request.app.state.services
        body = await request.body()
        caller = authenticate(
            s, request.app.state.api_seen, request.headers.get("authorization"), "POST", PATH, body
        )
        try:
            data: Any = json.loads(body or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("admin"), bool):
            raise ApiError(400, "invalid_json", 'Send {"pubkey": "<hex>", "admin": true|false}.')
        target = str(data.get("pubkey") or "").lower()
        if not HEX.match(target):
            raise ApiError(400, "invalid", "pubkey: 64 hex characters.")
        admins = await run_in_threadpool(change, s, caller, target, data["admin"])
        return JSONResponse({"ok": True, "admins": admins}, headers={"Cache-Control": "no-store"})
