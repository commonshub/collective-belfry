"""A channel's guests: someone without a membership, in one channel, for 30 days.

- A member asks for a link in the chat
  (``POST /api/channel-invites``, NIP-98: ``{"channel"}``). It is an invite of the belfry's own
  register (register.py) that carries the channel, the number of days and the inviter's key.
- The invite page (invite.py) says what it is; the guest gives a name and an e-mail address and
  confirms it. The pass that opens is of tier ``guest`` and ends after the days (core.py), so
  sign-in (connect.py) works as for members, and the usual expiry closes it.
- ``admitted``: the groups key puts the guest in the channel (kind 9000, ``invited-by`` the
  inviter, the guest's ``name``); the chat shows "<name> joined as a guest, invited by <inviter>".
  The groups relay (zooid patch 0002, ``[groups] guests = true``) lets a key that is in a group
  but not a relay member read and write in its groups only. Guests are on no other whitelist
  (core.active_pubkeys): not the community relay, not the git server, not the console.
- ``left``: when the pass closes (expiry or revocation), kind 9001 takes them out again.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .clients.odoo import OdooError
from .core import Services, m2o_id

log = logging.getLogger(__name__)

API = "/api/channel-invites"
DAYS = 30


def _channel_event(svc: Services, kind: int, tags: list[list[str]]) -> bool:
    from . import followers

    got = followers._relay(svc)
    if got is None:
        log.warning("guests: no groups relay")
        return False
    relay, sign = got
    try:
        relay.publish(
            sign({"kind": kind, "created_at": int(time.time()), "tags": tags, "content": ""}), sign
        )
        return True
    except Exception as exc:  # noqa: BLE001 — logged; an admin can redo it from the chat
        log.warning("guests: kind %s not published: %s", kind, exc)
        return False


def _guest(svc: Services, pass_id: int) -> dict[str, Any] | None:
    row = svc.odoo.read_one(
        "belfry.pass",
        pass_id,
        [
            "partner_id",
            "tier",
            "keycast_pubkey",
            "guest_channel",
            "guest_inviter",
            "date_end",
            "guest_agent",
        ],
    )
    if not row or row.get("tier") != "guest" or not row.get("guest_channel"):
        return None
    if not row.get("keycast_pubkey"):
        return None
    partner = svc.odoo.read_one("res.partner", m2o_id(row["partner_id"]) or 0, ["name"]) or {}
    return {**row, "name": (partner.get("name") or "")[:100]}


def admitted(svc: Services, pass_id: int) -> bool:
    """The guest's pass opened: put them in their channel."""
    g = _guest(svc, pass_id)
    if g is None:
        return False
    tags = [
        ["h", g["guest_channel"]],
        ["p", g["keycast_pubkey"], "member"],
        ["guest", str(g.get("date_end") or "")],
    ]
    if g.get("guest_inviter"):
        tags.append(["invited-by", g["guest_inviter"]])
    if g["name"]:
        tags.append(["name", g["name"]])
    if g.get("guest_agent"):  # an agent with its own key, not a person (the chat says so)
        tags.append(["agent", "1"])
    ok = _channel_event(svc, 9000, tags)
    log.info("guests: pass %s into #%s: %s", pass_id, g["guest_channel"], ok)
    return ok


def left(svc: Services, pass_id: int) -> bool:
    """The guest's pass closed: out of the channel."""
    g = _guest(svc, pass_id)
    if g is None:
        return False
    return _channel_event(
        svc, 9001, [["h", g["guest_channel"]], ["p", g["keycast_pubkey"]], ["guest", "ended"]]
    )


def may_invite(svc: Services, pubkey: str, channel: str, steward: bool) -> bool:
    """Any member may bring a guest into a channel (the guest sees that channel only, and the
    channel sees who invited them). Guests themselves can't (checked before)."""
    return True


def register(app: FastAPI) -> None:
    register_agent_routes(app)

    @app.post(API)
    async def channel_invite(request: Request) -> JSONResponse:
        """A guest link for a channel: ``{"channel"}`` → ``{"url", "days", "expires_at"}``."""
        from . import followers, invite, mail
        from .holder_invites import ApiError, authenticate, holder

        s = request.app.state.services
        body = await request.body()
        pubkey = authenticate(
            s, request.app.state.api_seen, request.headers.get("authorization"), "POST", API, body
        )
        who = await run_in_threadpool(holder, s, pubkey)
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = None
        channel = str((data or {}).get("channel") or "") if isinstance(data, dict) else ""
        if not followers.CHANNEL.match(channel):
            raise ApiError(422, "invalid", "channel: a group id")
        if not invite.own(s):
            raise ApiError(409, "unsupported", "Guests need the belfry's own register.")
        rows = await run_in_threadpool(
            s.odoo.search_read,
            "belfry.pass",
            [["state", "=", "active"], ["keycast_pubkey", "=", pubkey]],
            ["tier"],
        )
        if any(r.get("tier") == "guest" for r in rows) and not any(
            r.get("tier") != "guest" for r in rows
        ):
            raise ApiError(403, "guest", "Guests can't invite guests.")
        if not await run_in_threadpool(mail.channel_exists, s, channel):
            raise ApiError(404, "no_channel", "No such channel.")
        if not await run_in_threadpool(may_invite, s, pubkey, channel, bool(who.get("steward"))):
            raise ApiError(403, "not_allowed", "You can't invite guests here.")
        try:
            row = await run_in_threadpool(
                s.odoo.create_guest_invite, who["partner_id"], pubkey, channel, DAYS
            )
        except OdooError as exc:
            raise ApiError(409, "refused", str(exc.message or "Refused.")) from exc
        return JSONResponse(
            {"url": row["url"], "days": DAYS, "expires_at": row.get("expires_at") or None},
            headers={"Cache-Control": "no-store"},
        )


# ── agents as guests: their own key, no e-mail ──────────────────────────────────────────────
def accept_path(token: str) -> str:
    return f"/api/guest-invites/{token}/accept"


def api_base(svc: Services) -> str:
    console = (getattr(svc.settings, "console_url", "") or "").rstrip("/")
    return console + "/api/v1" if console else svc.settings.public_url.rstrip("/") + "/api"


def agent_instructions(svc: Services, inv: dict[str, Any], token: str) -> str:
    """What an agent reads at /invite/<token>.md: how to accept, listen and speak."""
    from . import theme

    community = theme.current().name
    channel = inv["guest_channel"]
    days = int(inv.get("guest_days") or DAYS)
    who = inv.get("inviter_name") or "A member"
    relay = svc.settings.groups_relay_public_url
    accept = api_base(svc) + accept_path(token).removeprefix("/api")
    page = f"{svc.settings.public_url.rstrip('/')}/invite/{token}"
    return f"""# You are invited to #{channel} on {community}

{who} invited you as a guest of #{channel} for {days} days. A guest reads and writes in
#{channel} only. Everyone there sees that {who} invited you.

- **A person?** Open {page} in a browser: name, e-mail, one click in the mail.
- **An agent?** Follow these steps. You use your own Nostr key; no e-mail is needed.

## 1. Your key
Generate a Nostr key pair (secp256k1, BIP-340 Schnorr). The public key is who you are here; keep
the secret key private and reuse it next time.

## 2. Accept the invitation (once; the link works for one guest)
    POST {accept}
    Content-Type: application/json
    Authorization: Nostr <base64 of a signed NIP-98 event>

    {{"name": "Your name", "about": "One line about what you do", "picture": "https://…/avatar.png"}}

The NIP-98 event: kind 27235, `created_at` now, content "", tags
`["u", "{accept}"]`, `["method", "POST"]`, `["payload", "<sha256 hex of the exact body>"]`.
The answer is `{{"channel": "{channel}", "relay": "{relay}", "until": "<last day>"}}`. Your name
shows at once in the channel: "<name> joined as a guest, invited by {who}".

## 3. Connect to the channel's relay
Open a WebSocket to `{relay}`. It sends `["AUTH", "<challenge>"]`; answer
`["AUTH", <event>]` with a signed kind 22242 event, tags `["relay", "{relay}"]` and
`["challenge", "<challenge>"]` (NIP-42). Until then it answers `auth-required`.

## 4. Set your name, description and avatar
Publish a kind 0 event (NIP-01) on that relay, content a JSON string:
`{{"name": "…", "about": "…", "picture": "https://…"}}`. Publish it again to change it.

## 5. Listen
    ["REQ", "belfry", {{"kinds": [9, 1111, 7], "#h": ["{channel}"], "since": <unix time now>}}]

Kind 9 = a message, 1111 = a reply in a thread (its `E` tag is the message it hangs off), 7 = a
reaction. A message that mentions you has `["p", "<your public key, hex>"]` and `nostr:npub1…`
in its text. For the history, the same filter without `since` and with `"limit": 50`. Names and
pictures: kind 0 events, `{{"kinds": [0], "authors": [...]}}`.

## 6. Speak
- A message: kind 9, content your text (basic Markdown: **bold**, *italic*, `code`, lists,
  > quotes, links), tags `[["h", "{channel}"]]`.
- A reply in a thread (preferred when you answer someone): kind 1111, tags
  `[["h", "{channel}"], ["E", "<message id>", "", "<its author>"], ["K", "9"],
  ["P", "<its author>"], ["e", "<message id>", "", "<its author>"], ["k", "9"],
  ["p", "<its author>"]]`.
- Mention someone: write `nostr:<their npub>` in the text and add `["p", "<their hex key>"]`.
- React: kind 7, content an emoji, tags
  `[["h", "{channel}"], ["e", "<message id>"], ["p", "<its author>"]]`.

## 7. The rules
You are a guest for {days} days; then the relay stops accepting your key. You can only read and
write in #{channel}. Be brief and useful; people see who invited you. To leave earlier: kind
9022 with `[["h", "{channel}"]]`.
"""


def register_agent_routes(app: FastAPI) -> None:
    @app.post("/api/guest-invites/{token}/accept")
    async def accept_as_agent(request: Request, token: str) -> JSONResponse:
        """An agent takes a guest invite with its own key (NIP-98): ``{"name", "about"?,
        "picture"?}`` → ``{"channel", "relay", "until"}``."""
        from . import invite
        from .holder_invites import ApiError, authenticate

        s = request.app.state.services
        body = await request.body()
        pubkey = authenticate(
            s,
            request.app.state.api_seen,
            request.headers.get("authorization"),
            "POST",
            accept_path(token),
            body,
        )
        if not invite.own(s):
            raise ApiError(409, "unsupported", "Guests need the belfry's own register.")
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict) or not str(data.get("name") or "").strip():
            raise ApiError(422, "invalid", "Send a JSON object with your name.")
        try:
            made = await run_in_threadpool(
                s.odoo.accept_agent_guest, token, pubkey, str(data["name"]).strip()[:100]
            )
        except OdooError as exc:
            raise ApiError(409, "refused", str(exc.message or "Refused.")) from exc
        await run_in_threadpool(admitted, s, made["pass_id"])
        row = await run_in_threadpool(s.odoo.read_one, "belfry.pass", made["pass_id"], ["date_end"])
        return JSONResponse(
            {
                "channel": made["channel"],
                "relay": s.settings.groups_relay_public_url,
                "until": (row or {}).get("date_end"),
            },
            headers={"Cache-Control": "no-store"},
        )
