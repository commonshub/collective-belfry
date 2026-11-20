"""Who a key is here: ``POST /api/whois`` (belfry.<domain>/api/v1/whois), NIP-98.

Apps can't tell a guest from a member from events alone (a guest promoted to member has no
event saying so unless a label was published). The register knows, so it answers:

- ``{"pubkeys": ["<hex>", …]}`` (at most 500) → ``{"people": {"<hex>": "member" | "agent" |
  "guest"}}``. A key holding no active pass is left out. "member" wins over "agent" and
  "guest" when one key holds several passes; "agent" means a member's own agent (agents.py,
  its member is in belfry.json ``agents``).
- Anyone holding an active pass may ask (guests too: they see who joined their channel).
- The answer also has ``"roles": {"<hex>": "<role id>"}`` for the members among the keys
  (roles.py; guests and agents have no role). Labels: ``GET /api/roles``.
- And ``"testers": ["<hex>", …]``: the keys among those asked that belong to a tester
  (testers.py: an account for trying payments; its purchases go through the test environment).
  Members still, so "people" says "member"; apps label them "Tester".
- And ``"tags": {"<hex>": ["tag", …]}``: the profile tags of the **guests** among the keys
  asked who have some. A guest's tags are kept on their contact (set on the website,
  sitesessions.py) because guests are not on the members' relay; a member's are in their own
  kind 30078 ``d`` = ``belfry:profile`` event, which the apps read themselves, so members are
  never in ``tags``. A guest without tags is left out.
- ``{"directory": true}`` (members only, #issues 2026-11-15: the chat's Members directory) →
  ``{"members": [{"pubkey", "name", "role"?}], "agents": {"<agent>": "<its member>"},
  "names": {"<agent>": "<name>"}}``: everyone holding a member's pass, and members' own agents;
  a tester's entry has ``"tester": true``. Guests are not in the directory, so it has no
  ``tags``.
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
    out: dict[str, Any] = {"people": {k: known[k] for k in keys if k in known}}
    # a member's role (roles.py), apart: "people" keeps its three values for the apps
    from . import roles

    try:
        held = roles.members(svc)
        out["roles"] = {k: held[k] for k in keys if k in held}
    except Exception:  # noqa: BLE001 — the kinds answer anyway
        pass
    from . import testers

    try:
        found = testers.tester_pubkeys(svc, set(keys))
    except Exception:  # noqa: BLE001
        found = set()
    out["testers"] = sorted(found)
    try:
        out["tags"] = guest_tags(svc, [k for k in keys if known.get(k) == "guest"])
    except Exception:  # noqa: BLE001
        out["tags"] = {}
    return out


def guest_tags(svc: Services, keys: list[str]) -> dict[str, list[str]]:
    """The profile tags of the guests among KEYS, from their contacts (``belfry_tags``); a
    guest without tags is left out."""
    from . import sitesessions
    from .core import m2o_id

    if not keys:
        return {}
    want = set(keys)
    partner: dict[str, int] = {}
    for r in svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["tier", "=", "guest"], ["keycast_pubkey", "!=", False]],
        ["keycast_pubkey", "partner_id"],
    ):
        pid = m2o_id(r.get("partner_id"))
        if r.get("keycast_pubkey") in want and pid:
            partner.setdefault(str(r["keycast_pubkey"]), int(pid))
    if not partner:
        return {}
    rows = svc.odoo.read("res.partner", sorted(set(partner.values())), ["belfry_tags"])
    held = {r["id"]: sitesessions.clean_tags(r.get("belfry_tags")) for r in rows}
    return {k: held[p] for k, p in partner.items() if held.get(p)}


def directory(svc: Services, caller: str) -> dict[str, Any]:
    """Every member of the belfry, and the members' own agents (their member's key)."""
    from . import mentions, roles
    from .holder_invites import ApiError

    known = kinds(svc)
    if known.get(caller) != "member":
        raise ApiError(403, "not_member", "Only members see who the members are.")
    members = sorted(k for k, v in known.items() if v == "member")
    try:
        owners = {a: o for a, o in svc.odoo.agent_owners().items() if known.get(a) == "agent"}
    except Exception:  # noqa: BLE001
        owners = {}
    try:
        held = roles.members(svc)
    except Exception:  # noqa: BLE001
        held = {}
    names = mentions._names(svc, set(members) | set(owners))
    from . import testers

    try:
        tested = testers.tester_pubkeys(svc, set(members))
    except Exception:  # noqa: BLE001
        tested = set()
    return {
        "members": [
            {
                "pubkey": k,
                "name": names.get(k, ""),
                **({"role": held[k]} if k in held else {}),
                **({"tester": True} if k in tested else {}),
            }
            for k in members
        ],
        "agents": owners,
        "names": {a: names.get(a, "") for a in owners},
    }


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
        if isinstance(data, dict) and data.get("directory") is True:
            got = await run_in_threadpool(directory, s, caller)
            return JSONResponse(got, headers={"Cache-Control": "no-store"})
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
