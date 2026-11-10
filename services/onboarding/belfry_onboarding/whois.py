"""Who a key is here: ``POST /api/whois`` (belfry.<domain>/api/v1/whois), NIP-98.

Apps can't tell a guest from a member from events alone (a guest promoted to member has no
event saying so unless a label was published). The register knows, so it answers:

- ``{"pubkeys": ["<hex>", …]}`` (at most 500) → ``{"people": {"<hex>": "member" | "agent" |
  "guest"}}``. A key holding no active pass is left out. "member" wins over "agent" and
  "guest" when one key holds several passes; "agent" means a member's own agent (agents.py,
  its member is in belfry.json ``agents``).
- Anyone holding an active pass may ask (guests too: they see who joined their channel).
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .core import Services

PATH = "/api/whois"
HEX = re.compile(r"^[0-9a-f]{64}$")
MAX_KEYS = 500
RANK = {"guest": 0, "agent": 1, "member": 2}


def kinds(svc: Services) -> dict[str, str]:
    """Every key with an active pass → member / agent / guest."""
    out: dict[str, str] = {}
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "!=", False]],
        ["keycast_pubkey", "tier"],
    )
    for r in rows:
        key = r.get("keycast_pubkey")
        if not key:
            continue
        tier = r.get("tier")
        kind = tier if tier in ("guest", "agent") else "member"
        if key not in out or RANK[kind] > RANK[out[key]]:
            out[key] = kind
    return out


def answer(svc: Services, caller: str, keys: list[str]) -> dict[str, Any]:
    from .holder_invites import ApiError

    known = kinds(svc)
    if caller not in known:
        raise ApiError(403, "not_here", "Only people of this belfry can ask.")
    return {"people": {k: known[k] for k in keys if k in known}}


def register(app: FastAPI) -> None:
    from .holder_invites import ApiError, authenticate

    @app.post(PATH)
    async def whois(request: Request) -> JSONResponse:
        s = request.app.state.services
        body = await request.body()
        caller = authenticate(
            s, request.app.state.api_seen, request.headers.get("authorization"), "POST", PATH, body
        )
        try:
            data: Any = json.loads(body or b"{}")
        except ValueError:
            data = None
        keys = data.get("pubkeys") if isinstance(data, dict) else None
        if (
            not isinstance(keys, list)
            or len(keys) > MAX_KEYS
            or not all(isinstance(k, str) and HEX.match(k) for k in keys)
        ):
            raise ApiError(
                400, "invalid", f'Send {{"pubkeys": ["<hex>", …]}} (at most {MAX_KEYS}).'
            )
        out = await run_in_threadpool(answer, s, caller, list(dict.fromkeys(keys)))
        return JSONResponse(out, headers={"Cache-Control": "no-store"})
