"""A member's own agents: ``POST /api/agents`` (belfry.<domain>/api/v1/agents), NIP-98.

A channel's guest can be an agent (guests.py: its own key, 30 days, one channel). A member can
also bring an agent of their own, which is a member for as long as they are one:

- ``{}`` → ``{"agents": [{"pubkey", "name", "since"}], "limit": n}``: the caller's agents.
- ``{"pubkey", "mine": true}``: an agent that is a guest here becomes the caller's agent. Only
  the member who invited it (or an admin, for themselves) can do that, within their limit.
- ``{"pubkey", "mine": false}``: the agent is removed (its member, or an admin).
- ``{"limit": n}`` by an admin: how many agents each member can bring (0 to 20).
- A link made with ``"agent": true`` (guests.py) brings a new agent in as the inviter's own.

The agent holds a pass of tier ``agent`` with ``agent_owner`` = its member's contact. It is on
the relay whitelist like a member (core.active_pubkeys), signs with its own key, has no e-mail,
no signer account, can't invite and can't be an admin. When its member's membership closes, so
does the agent's pass (core._close_pass → ``closed``), and it is taken out of every channel.
The limit is the register's ``belfry.agents_per_member`` (1 unless set), or
``belfry.agents_per_tier.<tier>`` for a membership tier. belfry.json lists agent → member.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .clients.odoo import OdooError
from .core import Services, m2o_id

log = logging.getLogger(__name__)

PATH = "/api/agents"
HEX = re.compile(r"^[0-9a-f]{64}$")
MAX_LIMIT = 20


def out_of_channels(svc: Services, pubkey: str) -> int:
    """Take PUBKEY out of every channel it is listed in (kind 9001). The groups relay lets a key
    that is in a channel but is no relay member read that channel (zooid patch 0002, guests), so
    someone who stops being a member must leave the channels too."""
    from . import followers, guests

    got = followers._relay(svc)
    if got is None:
        return 0
    relay, sign = got
    try:
        lists = relay.query([{"kinds": [39002], "limit": 2000}], sign)
    except Exception as exc:  # noqa: BLE001
        log.warning("agents: channel lists not read: %s", exc)
        return 0
    n = 0
    for e in lists:
        tags = e.get("tags", [])
        channel = next((t[1] for t in tags if t[:1] == ["d"] and len(t) > 1), "")
        if channel and any(t[:2] == ["p", pubkey] for t in tags):
            if guests._channel_event(svc, 9001, [["h", channel], ["p", pubkey], ["left", "1"]]):
                n += 1
    return n


def closed(svc: Services, pass_row: dict[str, Any]) -> None:
    """A pass that is not a guest's closed (core._close_pass). An agent: out of its channels. A
    member with no other membership: their agents go, and they leave the channels."""
    from . import core, invite

    if not invite.own(svc):
        return
    pubkey = pass_row.get("keycast_pubkey") or ""
    partner = m2o_id(pass_row.get("partner_id"))
    if pass_row.get("tier") == "agent":
        if pubkey:
            out_of_channels(svc, pubkey)
        return
    if partner is None or any(  # still a member by another pass
        r.get("tier") not in ("guest", "agent")
        for r in svc.odoo.search_read(
            "belfry.pass", [["state", "=", "active"], ["partner_id", "=", partner]], ["tier"]
        )
    ):
        return
    for a in svc.odoo.agents_of(partner):
        log.info("agents: pass %s closes with its member's", a["pass_id"])
        core.revoke_pass(svc, a["pass_id"], "their member left")
    if pubkey and not svc.odoo.search_read(
        "belfry.pass", [["state", "=", "active"], ["keycast_pubkey", "=", pubkey]], ["id"], limit=1
    ):
        out_of_channels(svc, pubkey)


def joined(svc: Services, pubkey: str, channel: str, inviter: str, name: str) -> None:
    """A new agent came in as a member's own (a link made for it): into the link's channel, and
    onto the relay."""
    from . import core, guests

    core.sync_relay_whitelist(svc)
    tags = [["h", channel], ["p", pubkey, "member"], ["agent", "1"]]
    if inviter:
        tags.append(["invited-by", inviter])
    if name:
        tags.append(["name", name[:100]])
    guests._channel_event(svc, 9000, tags)
    guests._channel_event(
        svc,
        1985,
        [["h", channel], ["L", "belfry/guest"], ["l", "member", "belfry/guest"], ["p", pubkey]],
    )


def _owner(svc: Services, caller: str) -> int:
    from .holder_invites import ApiError
    from .places import member_key

    row = member_key(svc, caller)
    if row is None or row.get("tier") == "agent":
        raise ApiError(403, "not_member", "Only members have agents of their own.")
    return int(m2o_id(row.get("partner_id")) or 0)


def mine(svc: Services, caller: str) -> dict[str, Any]:
    owner = _owner(svc, caller)
    return {
        "agents": [
            {"pubkey": a["pubkey"], "name": a["name"], "since": a["since"]}
            for a in svc.odoo.agents_of(owner)
        ],
        "limit": svc.odoo.agents_limit(owner),
    }


def change(svc: Services, caller: str, target: str, on: bool) -> dict[str, Any]:
    from . import apps, core, guests
    from .holder_invites import ApiError

    owner = _owner(svc, caller)
    admin = caller in apps.admin_pubkeys(svc)
    if on:
        try:
            svc.odoo.make_agent(target, owner, caller, admin)
        except OdooError as exc:
            code = str(exc.data.get("code") or "refused")
            raise ApiError(409, code, str(exc.message or "Refused.")) from exc
        core.sync_relay_whitelist(svc)
        guests.became_member(svc, target)  # the guest passes close; it stays in those channels
    else:
        rows = [
            r
            for r in svc.odoo.search_read(
                "belfry.pass",
                [["state", "=", "active"], ["keycast_pubkey", "=", target]],
                ["tier", "agent_owner"],
            )
            if r.get("tier") == "agent"
        ]
        if not rows:
            raise ApiError(404, "not_agent", "This is not a member's agent.")
        if not admin and any(m2o_id(r.get("agent_owner")) != owner for r in rows):
            raise ApiError(403, "not_yours", "Only its member or an admin removes an agent.")
        for r in rows:
            core.revoke_pass(svc, r["id"], "removed by its member")
    return mine(svc, caller)


def set_limit(svc: Services, caller: str, limit: Any) -> int:
    from . import apps
    from .holder_invites import ApiError

    if caller not in apps.admin_pubkeys(svc):
        raise ApiError(403, "not_admin", "Only the belfry's admins set this.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= MAX_LIMIT:
        raise ApiError(422, "invalid", f"limit: 0 to {MAX_LIMIT}")
    svc.odoo.set_param("belfry.agents_per_member", str(limit))
    return limit


def register(app: FastAPI) -> None:
    from . import invite
    from .holder_invites import ApiError, authenticate

    @app.post(PATH)
    async def agents(request: Request) -> JSONResponse:
        s = request.app.state.services
        body = await request.body()
        caller = authenticate(
            s, request.app.state.api_seen, request.headers.get("authorization"), "POST", PATH, body
        )
        if not invite.own(s):
            raise ApiError(409, "unsupported", "Agents need the belfry's own register.")
        try:
            data: Any = json.loads(body or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_json", "Send a JSON object.")
        if "limit" in data:
            await run_in_threadpool(set_limit, s, caller, data["limit"])
        if "pubkey" in data:
            target = str(data.get("pubkey") or "").lower()
            if not HEX.match(target) or not isinstance(data.get("mine"), bool):
                raise ApiError(400, "invalid", 'Send {"pubkey": "<hex>", "mine": true|false}.')
            out = await run_in_threadpool(change, s, caller, target, data["mine"])
        else:
            out = await run_in_threadpool(mine, s, caller)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})
