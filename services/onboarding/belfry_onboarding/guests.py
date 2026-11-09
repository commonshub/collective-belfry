"""A channel's guests: someone without a membership, in one channel, for 30 days.

- A member asks for a link in the chat
  (``POST /api/channel-invites``, NIP-98: ``{"channel"}``). It is an invite of the belfry's own
  register (register.py) that carries the channel, the number of days and the inviter's key.
  It is for one person and works for a day unless the member asks for more (``"people"`` up to
  50, ``"link_days"`` up to 30). The link is ``https://chat.<domain>/join/<token>``.
- The chat (or the phone app) opens the link, shows what it is
  (``GET /api/channel-invites/<token>``) and joins with ONE signed request
  (``POST /api/channel-invites/<token>/join``, NIP-98), whoever is asking:
  the key of an account (member or guest) → that account gets the channel, no second account;
  a key nobody knows, with a name → a new guest: a contact, a guest pass, an account in the
  signer and its session, handed back in the answer. No e-mail, no code.
  The pass is of tier ``guest`` and ends after the days (core.py); the usual expiry closes it.
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
    partner = (
        svc.odoo.read_one("res.partner", m2o_id(row["partner_id"]) or 0, ["name", "email"]) or {}
    )
    return {**row, "name": (partner.get("name") or "")[:100], "email": partner.get("email") or ""}


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
    elif not g.get("email"):  # joined with a name only: "not saved" until they add an e-mail
        tags.append(["unsaved", "1"])
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


def join_url(svc: Services, token: str) -> str:
    """Where a guest link leads: the chat's own host, so the browser (or the phone app, as a
    universal link) that will hold the session is the one that asks to join."""
    return f"https://chat.{svc.settings.nip05_domain}/join/{token}"


def _option(data: Any, key: str, default: int, allowed: tuple[int, ...]) -> int:
    from .holder_invites import ApiError

    value = data.get(key, default) if isinstance(data, dict) else default
    if isinstance(value, bool) or not isinstance(value, int) or value not in allowed:
        raise ApiError(422, "invalid", f"{key}: one of {', '.join(map(str, allowed))}")
    return value


def register(app: FastAPI) -> None:
    register_agent_routes(app)
    register_join_routes(app)

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
        people, link_days = (
            _option(data, "people", 1, (1, 50)),
            _option(data, "link_days", 1, (1, 7, 30)),
        )
        try:
            row = await run_in_threadpool(
                s.odoo.create_guest_invite,
                who["partner_id"],
                pubkey,
                channel,
                DAYS,
                people,
                link_days,
            )
        except OdooError as exc:
            raise ApiError(409, "refused", str(exc.message or "Refused.")) from exc
        token = str(row["url"]).rsplit("/", 1)[-1]
        return JSONResponse(
            {
                "url": join_url(s, token),
                # what an agent reads: how to join with its own key
                "agent_url": row["url"] + ".md",
                "days": DAYS,
                "people": people,
                "link_days": link_days,
                "expires_at": row.get("expires_at") or None,
            },
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
    page = join_url(svc, token)
    return f"""# You are invited to #{channel} on {community}

{who} invited you as a guest of #{channel} for {days} days. A guest reads and writes in
#{channel} only. Everyone there sees that {who} invited you.

- **A person?** Open {page} in a browser: your name, and you are in.
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


# ── joining with a guest link: one signed request, whoever asks ─────────────────────────────
# A new person: the chat makes its session key, signs the request with it and sends a name. The
# belfry makes the guest's contact, a guest pass, an account in the signer (a derived key, like
# every member's, recoverable) and a session, and answers with that session; the chat binds it to
# its key the moment it connects (NIP-46). Saving the account later (an e-mail and the code mailed
# to it) is signed with the guest's identity through that session: nobody else can claim it.
# Someone with an account signs with that account instead and gets the channel on it.


def _in_channel(svc: Services, channel: str, pubkey: str) -> bool:
    from . import followers

    got = followers._relay(svc)
    if got is None:
        return False
    relay, sign = got
    try:
        found = relay.query([{"kinds": [39002], "#d": [channel], "limit": 1}], sign)
    except Exception:  # noqa: BLE001 — unknown: joining again does no harm
        return False
    return any(t[:2] == ["p", pubkey] for e in found[:1] for t in e.get("tags", []))


def join_new(svc: Services, token: str, name: str) -> dict[str, Any]:
    """Make the guest and their session: ``{"bunker_url", "pubkey", "channel", "guest"}``."""
    from . import core

    made = svc.odoo.join_guest(token, name)
    pid = made["partner_id"]
    partner = {"id": pid, "name": name}
    pubkey, npub, ucan = core._ensure_keycast_user(svc, partner, f"guest-{pid}")
    svc.odoo.write("res.partner", [pid], {"belfry_npub": npub})
    svc.odoo.write("belfry.pass", [made["pass_id"]], {"keycast_pubkey": pubkey, "state": "active"})
    bunker = svc.keycast.create_bunker(ucan, "Chat (guest)") if ucan else {}
    if not bunker.get("bunker_url"):
        raise RuntimeError("the signer gave no session")
    admitted(svc, made["pass_id"])
    return {
        "bunker_url": bunker["bunker_url"],
        "pubkey": pubkey,
        "channel": made["channel"],
        "guest": True,
        "new": True,
    }


def join_account(svc: Services, token: str, pubkey: str) -> dict[str, Any] | None:
    """The account behind PUBKEY takes the invite; None when the key has no account."""
    from .holder_invites import ApiError

    got = svc.odoo.join_existing(token, pubkey)
    if got is None:
        return None
    channel = got["channel"]
    out = {"channel": channel, "pubkey": pubkey, "new": False, "guest": got["kind"] != "member"}
    if got["kind"] == "already":
        return {**out, "already": True}
    if got["kind"] == "guest":
        admitted(svc, got["pass_id"])
        return out
    # a member: in the channel already → it just opens, and the link isn't used up
    if _in_channel(svc, channel, pubkey):
        return {**out, "guest": False, "already": True}
    if got["kind"] == "closed":
        raise ApiError(410, "closed", f"This invite is {got.get('state') or 'closed'}.")
    inv = svc.odoo.invite_by_token(token) or {}
    partner = svc.odoo.read_one("res.partner", got["partner_id"], ["name"]) or {}
    tags = [["h", channel], ["p", pubkey, "member"]]
    if inv.get("guest_inviter"):
        tags.append(["invited-by", inv["guest_inviter"]])
    if partner.get("name"):
        tags.append(["name", str(partner["name"])[:100]])
    if not _channel_event(svc, 9000, tags):
        raise ApiError(503, "relay", "The channel could not be reached just now. Try again.")
    svc.odoo.count_guest_use(got["invite_id"])
    return {**out, "guest": False}


def invite_info(svc: Services, token: str) -> dict[str, Any] | None:
    """What the join screen shows before anyone signs anything."""
    from . import followers, theme

    inv = svc.odoo.invite_by_token(token)
    if inv is None or not inv.get("guest_channel"):
        return None
    channel = inv["guest_channel"]
    title, about = channel, ""
    got = followers._relay(svc)
    if got is not None:
        relay, sign = got
        try:
            found = relay.query([{"kinds": [39000], "#d": [channel], "limit": 1}], sign)
        except Exception:  # noqa: BLE001 — the id is a fine name
            found = []
        for t in found[0].get("tags", []) if found else []:
            if t[:1] == ["name"] and len(t) > 1 and t[1]:
                title = str(t[1])[:80]
            if t[:1] == ["about"] and len(t) > 1:
                about = str(t[1])[:300]
    return {
        "community": theme.current().name,
        "channel": channel,
        "channel_name": title,
        "about": about,
        "inviter": (inv.get("inviter_name") or "")[:100],
        "state": inv["state"] if inv["state"] in ("open", "used", "expired", "revoked") else "used",
        "days": int(inv.get("guest_days") or DAYS),
    }


def saved_label(svc: Services, pubkey: str, channel: str) -> None:
    """The channel learns the guest saved their account (the chat drops "not saved")."""
    _channel_event_kind(
        svc,
        1985,
        [["h", channel], ["L", "belfry/guest"], ["l", "saved", "belfry/guest"], ["p", pubkey]],
    )


def _channel_event_kind(svc: Services, kind: int, tags: list[list[str]]) -> bool:
    return _channel_event(svc, kind, tags)


def register_join_routes(app: FastAPI) -> None:
    @app.get("/api/channel-invites/{token}")
    async def channel_invite_info(request: Request, token: str) -> JSONResponse:
        """The invite, for the join screen: community, channel, who invites, whether it's open."""
        info = await run_in_threadpool(invite_info, request.app.state.services, token)
        if info is None:
            return JSONResponse(
                {"error": "not_found", "message": "No such invite."}, status_code=404
            )
        return JSONResponse(info, headers={"Cache-Control": "no-store"})

    @app.post("/api/channel-invites/{token}/join")
    async def channel_invite_join(request: Request, token: str) -> JSONResponse:
        """Join with a guest link (NIP-98). Signed by an account's key: that account gets the
        channel → ``{"channel", "new": false}``. Signed by a key nobody knows, with
        ``{"name"}``: a new guest → ``{"channel", "new": true, "bunker_url", "pubkey"}``; the
        signing key is the one that will hold the session."""
        from . import invite
        from .holder_invites import ApiError, authenticate

        s = request.app.state.services
        body = await request.body()
        pubkey = authenticate(
            s,
            request.app.state.api_seen,
            request.headers.get("authorization"),
            "POST",
            f"/api/channel-invites/{token}/join",
            body,
        )
        if not invite.own(s):
            raise ApiError(409, "unsupported", "Guests need the belfry's own register.")
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_json", "Send a JSON object.")
        try:
            out = await run_in_threadpool(join_account, s, token, pubkey)
            if out is None:
                name = " ".join(str(data.get("name") or "").split())[:100]
                if not name:
                    raise ApiError(422, "name_needed", "Tell us your name.")
                out = await run_in_threadpool(join_new, s, token, name)
        except OdooError as exc:
            raise ApiError(410, "closed", str(exc.message or "This invite is closed.")) from exc
        except ApiError:
            raise
        except Exception as exc:
            log.exception("guest link %s: joining failed", token[:8])
            raise ApiError(503, "failed", "Joining did not work just now. Try again.") from exc
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    async def guest_of(
        request: Request, path: str, body: bytes
    ) -> tuple[Services, str, dict[str, Any]]:
        from .holder_invites import ApiError, authenticate

        s = request.app.state.services
        pubkey = authenticate(
            s, request.app.state.api_seen, request.headers.get("authorization"), "POST", path, body
        )
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_json", "Send a JSON object.")
        return s, pubkey, data

    @app.post("/api/guest/save")
    async def guest_save(request: Request) -> JSONResponse:
        """``{"email"}``: a 6-digit code to that address, for the signed-in guest's key."""
        from .holder_invites import ApiError

        s, pubkey, data = await guest_of(request, "/api/guest/save", await request.body())
        email = str(data.get("email") or "").strip()
        if "@" not in email or len(email) > 254:
            raise ApiError(422, "invalid", "That is not an e-mail address.")
        row = await run_in_threadpool(
            s.odoo.call,
            "belfry.key.code",
            "belfry_key_code_send",
            [email, pubkey, "Save your guest account"],
        )
        if row.get("state") == "too_many":
            raise ApiError(429, "too_many", "Too many codes for now. Try again in half an hour.")
        return JSONResponse({"sent": True}, status_code=202)

    @app.post("/api/guest/save/verify")
    async def guest_save_verify(request: Request) -> JSONResponse:
        """``{"email", "code"}``: the address goes on the guest's own contact."""
        from .holder_invites import ApiError

        s, pubkey, data = await guest_of(request, "/api/guest/save/verify", await request.body())
        out = await run_in_threadpool(
            s.odoo.save_guest_email,
            pubkey,
            str(data.get("email") or ""),
            str(data.get("code") or ""),
        )
        state = out.get("state")
        if state == "wrong_code":
            raise ApiError(
                422,
                "wrong_code",
                f"That code is not right ({out.get('tries_left', 0)} tries left).",
            )
        if state == "expired":
            raise ApiError(410, "expired", "That code expired. Send a new one.")
        if state == "taken":
            raise ApiError(
                409, "taken", "This address belongs to a member already: sign in with it instead."
            )
        if state != "saved":
            raise ApiError(409, "not_guest", "This key holds no guest pass.")
        rows = await run_in_threadpool(
            s.odoo.search_read,
            "belfry.pass",
            [["keycast_pubkey", "=", pubkey], ["state", "=", "active"]],
            ["guest_channel"],
        )
        for r in rows:
            if r.get("guest_channel"):
                await run_in_threadpool(saved_label, s, pubkey, r["guest_channel"])
        return JSONResponse({"saved": True})
