"""The belfry's admins, from the chat: ``POST /api/admins`` (belfry.<domain>/api/v1/admins).

NIP-98 by a current admin, ``{"pubkey": "<hex>", "admin": true|false}``. An admin is a member of
the stewards group (console.admin_emails); the target must be an active member (no channel guest,
person or agent). The last admin can't be removed. The relay sync follows at once, so the new
admin administers every channel (zooid patch 0003) and belfry.json lists them.
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
    partner = core.m2o_id(row.get("partner_id")) if row else None
    email = ""
    if partner:
        email = (
            ((svc.odoo.read("res.partner", [partner], ["email"]) or [{}])[0].get("email") or "")
            .strip()
            .lower()
        )
    if not email:
        raise ApiError(400, "not_member", "Only active members (not guests) can be admins.")
    current = apps.admin_pubkeys(svc)
    if not admin and target in current and len(current) <= 1:
        raise ApiError(400, "last_admin", "A belfry keeps at least one admin.")
    if admin != (target in current):
        console.set_admin(svc, email, admin)
        apps.forget()
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
