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
  a key nobody knows → a new guest: a contact, a guest pass, an account in the signer and its
  session, handed back in the answer. No e-mail, no code.
  The pass is of tier ``guest`` and ends after the days (core.py); the usual expiry closes it.
- Guest mode (2026-11-12): the chat sends no name when a link is opened. The guest is "Guest",
  the pass says ``guest_unnamed`` and the answer ``"reading": true``: they read the channel, and
  the chat shows "Guest mode" with a Join button where the composer would be. Their 9000 carries
  ``["reading", "1"]`` (and ``["unsaved", "1"]``, no ``name``): clients show no join line for it
  and leave them out of member lists, counts and mention pickers. Read-only is the client's
  doing: the relay would take a write from such a key like from any guest.
  Join = a name and an e-mail, then the 6-digit code mailed to it
  (``POST /api/guest/save`` ``{"email", "name"}`` → ``POST /api/guest/save/verify``
  ``{"email", "code", "name"}``, both NIP-98 by the guest's identity). The right code puts name
  and address on the contact, clears ``guest_unnamed``, and the channel gets a second 9000 with
  the ``name`` and no ``reading`` / ``unsaved`` (the join line, at the time they joined), then
  the label ``saved``. An address that is another account's here: 409 ``taken``; for a guest who
  was only reading, that account gets the channel and the anonymous guest pass closes
  (``"moved": true``): they sign in with the address and the channel is there. The answer also
  carries ``nip05`` (that account's name@domain, for the apps' native sign-in) when it can be
  read, and a wrong code carries ``tries_left``.
  A request with ``{"name"}`` (older clients, the phone apps) joins by name at once, as before.
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
import re
import threading
import time
from typing import Any
from urllib.parse import urlparse

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
            "guest_unnamed",
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
    reading = bool(g.get("guest_unnamed")) and not g.get("guest_agent")
    if g["name"] and not reading:
        tags.append(["name", g["name"]])
    if g.get("guest_agent"):  # an agent with its own key, not a person (the chat says so)
        tags.append(["agent", "1"])
    elif reading:  # guest mode: the link was opened, nobody joined yet (no join line, no name)
        tags += [["unsaved", "1"], ["reading", "1"]]
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
    from .places import member_key

    if member_key(svc, g["keycast_pubkey"]):  # a member since (became_member): stays
        return False
    return _channel_event(
        svc, 9001, [["h", g["guest_channel"]], ["p", g["keycast_pubkey"]], ["guest", "ended"]]
    )


def _closed(svc: Services, channel: str) -> bool | None:
    """Whether CHANNEL takes applications only (NIP-29 `closed`); None when the relay can't say."""
    from . import followers

    got = followers._relay(svc)
    if got is None:
        return None
    relay, sign = got
    try:
        found = relay.query([{"kinds": [39000], "#d": [channel], "limit": 1}], sign)
    except Exception:  # noqa: BLE001
        return None
    if not found:
        return None
    return any(t[:1] == ["closed"] for t in found[0].get("tags", []))


def may_invite(svc: Services, pubkey: str, channel: str, steward: bool) -> bool:
    """Only someone who has the channel themselves may bring a guest into it (Xavier,
    2026-11-10): any member for an open channel; for a closed one (a pod by application) its
    members and admins. The community's admins always may. Guests can't (checked before). When
    the relay can't be asked, nobody but an admin may: a link into a closed pod is not a thing
    to hand out on a guess."""
    from . import apps, mail

    if steward or pubkey in apps.admin_pubkeys(svc):
        return True
    closed = _closed(svc, channel)
    if closed is None:
        return False
    if not closed:
        return True
    try:
        return _in_channel(svc, channel, pubkey) or pubkey in mail.group_admins(svc, channel)
    except Exception:  # noqa: BLE001
        return False


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


def _inviter(rows: list[dict[str, Any]]) -> None:
    """Only a member brings people in: not a guest, not a member's agent."""
    from .holder_invites import ApiError

    tiers = {r.get("tier") for r in rows}
    if tiers and not tiers - {"guest", "agent"}:
        if "agent" in tiers:
            raise ApiError(403, "agent", "Agents can't invite.")
        raise ApiError(403, "guest", "Guests can't invite guests.")


def register(app: FastAPI) -> None:
    register_agent_routes(app)
    register_add_route(app)
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
        _inviter(rows)
        if not await run_in_threadpool(mail.channel_exists, s, channel):
            raise ApiError(404, "no_channel", "No such channel.")
        if not await run_in_threadpool(may_invite, s, pubkey, channel, bool(who.get("steward"))):
            raise ApiError(403, "not_allowed", "You can't invite guests here.")
        # "role": "host": the link makes its user a co-host of an event (an admin of ev-<d>)
        role = str(data.get("role") or "") if isinstance(data, dict) else ""
        if role not in ("", "host"):
            raise ApiError(422, "invalid", 'role: "host" or nothing')
        if role == "host":
            from . import apps

            if not channel.startswith("ev-"):
                raise ApiError(422, "invalid", "A co-host link is for an event's group.")
            admins = await run_in_threadpool(mail.group_admins, s, channel)
            if (
                pubkey not in admins
                and not who.get("steward")
                and pubkey not in apps.admin_pubkeys(s)
            ):
                raise ApiError(403, "not_host", "Only the event's hosts add co-hosts.")
        people, link_days = (
            _option(data, "people", 1, (1, 50)),
            _option(data, "link_days", 1, (1, 7, 30)),
        )
        # "agent": the link is for one agent, which becomes the member's own (agents.py)
        own_agent = isinstance(data, dict) and data.get("agent") is True
        try:
            row = await run_in_threadpool(
                s.odoo.create_guest_invite,
                who["partner_id"],
                pubkey,
                channel,
                DAYS,
                1 if own_agent else people,
                link_days,
                own_agent,
                role,
            )
        except OdooError as exc:
            raise ApiError(409, "refused", str(exc.message or "Refused.")) from exc
        token = str(row["url"]).rsplit("/", 1)[-1]
        return JSONResponse(
            {
                "url": join_url(s, token),
                # what an agent reads: how to join with its own key
                "agent_url": row["url"] + ".md",
                "days": 0 if own_agent else DAYS,
                "own_agent": own_agent,
                "people": 1 if own_agent else people,
                "link_days": link_days,
                "role": role or None,
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
    own = bool(inv.get("agent_owner"))
    blossom = (svc.settings.blossom_public_url or "").rstrip("/") or "https://blossom." + (
        urlparse(relay).hostname or ""
    ).removeprefix("groups-relay.")
    if own:
        intro = f"""{who} invited you as their own agent on {community}. You are a member for as
long as {who} is one: you read and write in #{channel} and in every open channel, and people
can write to you directly. Everyone sees that you are {who}'s agent.

This link is for an agent. Follow these steps. You use your own Nostr key; no e-mail is needed."""
        rules = f"""You are here as {who}'s agent, for as long as {who} is a member and keeps
you. You can't invite others or administer anything. Be brief and useful; what you do reflects
on {who}. The channels: `{{"kinds": [39000]}}` lists them (the `d` tag is the id to put in `h`)."""
    else:
        intro = f"""{who} invited you as a guest of #{channel} for {days} days. A guest reads and
writes in #{channel} only. Everyone there sees that {who} invited you.

- **A person?** Open {page} in a browser: you read at once, and join with a name and an e-mail.
- **An agent?** Follow these steps. You use your own Nostr key; no e-mail is needed."""
        rules = f"""You are a guest for {days} days; then the relay stops accepting your key. You
can only read and write in #{channel}, unless a member brings you into another channel too: you
then get a kind 9000 event with your key in a `p` tag and that channel in `h`
(`{{"kinds": [9000], "#p": ["<your public key, hex>"]}}`). Be brief and useful; people see who
invited you. To leave earlier: kind 9022 with `[["h", "{channel}"]]`."""
    return f"""# You are invited to #{channel} on {community}

{intro}

## 1. Your key
Generate a Nostr key pair (secp256k1, BIP-340 Schnorr). The public key is who you are here; keep
the secret key private and reuse it next time.

## 2. Accept the invitation (once; the link works for one of you)
    POST {accept}
    Content-Type: application/json
    Authorization: Nostr <base64 of a signed NIP-98 event>

    {{"name": "Your name", "about": "One line about what you do", "picture": "https://…/avatar.png"}}

The NIP-98 event: kind 27235, `created_at` now, content "", tags
`["u", "{accept}"]`, `["method", "POST"]`, `["payload", "<sha256 hex of the exact body>"]`.
The answer is `{{"channel": "{channel}", "relay": "{relay}", "until": "<last day>"}}`. Your name
shows at once in the channel, with who invited you.

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

## 7. Attach pictures and files
Files go to the belfry's Blossom server (`{blossom}`, BUD-02); the message carries the link.
1. Upload: `PUT {blossom}/upload` with the file's bytes as the body, its `Content-Type`
   (`image/png`, `image/jpeg`, `application/pdf`, …) and `Authorization: Nostr <base64 event>`:
   a kind 24242 event signed with your key, content "Upload <file name>", tags
   `[["t", "upload"], ["x", "<sha256 hex of the file>"], ["expiration", "<unix time, a few
   minutes ahead>"]]` (BUD-11). The answer is JSON:
   `{{"url", "sha256", "size", "type", "nip94": [["url", …], ["m", …], ["x", …], ["size", …], ["dim", …]]}}`.
2. Post: put the `url` on its own line in the message text and add one tag per file,
   `["imeta", "url <url>", "m <type>", "x <sha256>", "size <size>", "dim <w>x<h>", "alt <what it shows>"]`
   (the `nip94` pairs joined with a space, plus `alt`). The chat shows images and videos inline
   and other files as a link.

Up to 25 MB per file; images are kept for a year, videos and audio for three months. Only keys
that belong to the belfry (members, guests, agents) can upload; anyone with the link can read.

## 8. To-dos: say what you pick up and what is done
Each channel has a to-do list that everyone sees in the chat. When you take on work someone asked
for, put it on the list and keep it current, so people can follow what you do:
- **Pick it up**: if the request already has a to-do (one whose `q` tag is the request's message
  id), claim it; if not, create one from the message, then claim it.
- **Say what you're busy on now**: work on one to-do at a time. While you work on it, send a
  `working` action on it every 2 minutes (add `["expiration", "<unix time + 900>"]`); the chat turns
  its circle while your last one is under 5 minutes old, so people see you're on it, or idle. Send
  `idle` when you stop or switch.
- **Give updates** in a thread under the request (kind 1111, as in step 6), not at the channel's top level.
- **When it's done**: reply in that thread with what changed, then mark the to-do done.

The events all carry `["t", "<list id>"]` and `["h", "{channel}"]`. The list id is the first 16 hex
characters of sha256 of `osv-village:{channel}` (the channel id; same rule for any channel).
- Read the list: `{{"kinds": [2100, 2101, 2103], "#t": ["<list id>"]}}`; replay the actions oldest first.
- Create: kind 2100, content the title (under 300 characters, short and imperative), tags
  `[["t", "<list id>"], ["h", "{channel}"], ["q", "<message id it comes from>"]]`.
- Claim, mark done: kind 2101, content "", tags `[["e", "<to-do id>"], ["action", "claim"], ["t", "<list id>"], ["h", "{channel}"]]`;
  the actions are `claim`, `unclaim`, `working`, `idle`, `done`, `undone`, and `delete` (its author only). Send two
  actions at least one second apart (a later `created_at`), or the second is dropped.
- Comment on a to-do: kind 2103, content the comment, tags `[["e", "<to-do id>"], ["t", "<list id>"], ["h", "{channel}"]]`.

## 9. Staying reliable (what belfry-agent learned)
People stop trusting an agent that goes quiet. What keeps one dependable:
- **One live subscription, everything you're in**: kinds 9, 1111 and 7 on each of your channels,
  plus `{{"kinds": [9, 1111], "#p": ["<you>"]}}` for mentions in channels you're not in.
- **Sockets die without saying so.** Every 2 minutes run your filter again with `since` = your
  last seen event − 5 minutes; no EOSE within 15 seconds: close, reconnect (backoff from 1 s,
  doubling, at most 60 s) and re-authenticate. Keep the last seen time on disk and resume from
  it, never from "now".
- **Once, not twice**: keep the ids you've handled on disk and save them *before* answering. A
  crash may cost an answer (the audit catches it); it never doubles one.
- **Follow your threads.** Remember every thread you posted in (your kind 9 ids, the `E` of your
  1111s): a reply there is often for you without a mention. Read it and decide; staying quiet is
  fine when it's for someone else.
- **Show you're on it, then clean up**: react 👀 the moment you start looking, and take it off
  once nothing is left for you (kind 5, tags `[["h", "<channel>"], ["e", "<your reaction id>"], ["k", "7"]]`).
- **Audit, don't trust.** Every 10 minutes, list the messages of the last two days that were for
  you and check each has a reply of yours after it; answer what's missing. List the to-dos
  claimed by you and not done: one with no step, heartbeat or reply for 30 minutes needs a
  progress note now.
- **Hand-offs go through something durable.** If another process of yours does the work (a
  build, a deploy), pass it through a file or queue that the worker watches and re-reads, not
  only a message that can get lost.
- **Never leave anyone waiting**: an acknowledgment within minutes, a short note at least every
  20–30 minutes on long work, and a final reply with what changed before marking the to-do done.

## 10. The rules
{rules}
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
        if made.get("own_agent"):  # a member's own agent: a member, in the link's channel
            from . import agents

            await run_in_threadpool(
                agents.joined,
                s,
                pubkey,
                made["channel"],
                made.get("inviter") or "",
                str(data["name"]).strip()[:100],
            )
            return JSONResponse(
                {
                    "channel": made["channel"],
                    "relay": s.settings.groups_relay_public_url,
                    "until": None,
                    "member": True,
                },
                headers={"Cache-Control": "no-store"},
            )
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


# ── bringing someone who is here already into one more channel ─────────────────────────────
ADD = "/api/channel-guests"


def register_add_route(app: FastAPI) -> None:
    @app.post(ADD)
    async def channel_guest(request: Request) -> JSONResponse:
        """``{"channel", "pubkey"}`` by a member who has the channel: a guest of another channel
        (a person or an agent) becomes this one's guest too, for 30 days, without a link →
        ``{"added": true}``; ``{"already": true}`` when they are in it; ``{"member": true}``
        for a member, who needs no guest pass."""
        from . import followers, invite, mail
        from .holder_invites import ApiError, authenticate, holder

        s = request.app.state.services
        body = await request.body()
        caller = authenticate(
            s, request.app.state.api_seen, request.headers.get("authorization"), "POST", ADD, body
        )
        who = await run_in_threadpool(holder, s, caller)
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = None
        channel = str(data.get("channel") or "") if isinstance(data, dict) else ""
        target = str(data.get("pubkey") or "").lower() if isinstance(data, dict) else ""
        if not followers.CHANNEL.match(channel) or not re.fullmatch(r"[0-9a-f]{64}", target):
            raise ApiError(422, "invalid", 'Send {"channel": "<id>", "pubkey": "<hex>"}.')
        if not invite.own(s):
            raise ApiError(409, "unsupported", "Guests need the belfry's own register.")
        rows = await run_in_threadpool(
            s.odoo.search_read,
            "belfry.pass",
            [["state", "=", "active"], ["keycast_pubkey", "=", caller]],
            ["tier"],
        )
        _inviter(rows)
        if not await run_in_threadpool(mail.channel_exists, s, channel):
            raise ApiError(404, "no_channel", "No such channel.")
        if not await run_in_threadpool(may_invite, s, caller, channel, bool(who.get("steward"))):
            raise ApiError(403, "not_allowed", "You can't invite guests here.")
        got = await run_in_threadpool(s.odoo.add_guest_channel, target, channel, caller, DAYS)
        if got is None:
            raise ApiError(404, "unknown", "Nobody here has that key.")
        if got["kind"] == "guest":
            await run_in_threadpool(admitted, s, got["pass_id"])
        return JSONResponse(
            {
                "added": got["kind"] == "guest",
                "already": got["kind"] == "already",
                "member": got["kind"] == "member",
            },
            headers={"Cache-Control": "no-store"},
        )


# ── joining with a guest link: one signed request, whoever asks ─────────────────────────────
# A new person: the chat makes its session key, signs the request with it and sends a name. The
# belfry makes the guest's contact, a guest pass, an account in the signer (a derived key, like
# every member's, recoverable) and a session made for the key that signed the request: the signer
# binds the session to that key when it makes it (Keycast patch 0022), so nobody else can take it
# with the link, and refuses a key that is linked to another account: one session key, one
# identity. (A signer built without the patch binds the first key that connects, as before.)
# Saving the account later (an e-mail and the code mailed
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


_join_lock = threading.Lock()  # one new guest at a time: a second tap waits, then finds the first


def join_new(svc: Services, token: str, name: str, client: str = "") -> dict[str, Any]:
    """Make the guest and their session: ``{"bunker_url", "pubkey", "channel", "guest"}``.
    Without a NAME they are in guest mode (``"reading": true``): reading, until they join.
    CLIENT (the key that signed the request) makes it safe to ask twice: the same device gets the
    same account again, with a new session, and is announced in the channel once. The session is
    made for CLIENT alone; a CLIENT the signer knows as another account's is refused (409)."""
    from . import core
    from .clients.keycast import ClientKeyTaken
    from .holder_invites import ApiError

    bind = client.lower() if re.fullmatch(r"[0-9a-fA-F]{64}", client or "") else None

    with _join_lock:
        made = svc.odoo.join_guest(token, name, client, not name)
        pid = made["partner_id"]
        row = svc.odoo.read_one("res.partner", pid, ["name"]) or {}
        partner = {"id": pid, "name": row.get("name") or name}
        pubkey, npub, ucan = core._ensure_keycast_user(svc, partner, f"guest-{pid}")
        try:
            bunker = svc.keycast.create_bunker(ucan, "Chat (guest)", bind) if ucan else {}
        except ClientKeyTaken as exc:
            if not made.get("again"):  # the pass just made stays unused
                svc.odoo.write("belfry.pass", [made["pass_id"]], {"state": "revoked"})
            log.warning("guest link %s: the session key belongs to another account", token[:8])
            raise ApiError(
                409,
                "session_taken",
                "This session belongs to another account. Open the invite link again.",
            ) from exc
        if bind and not bunker.get("bound"):
            log.info("the signer does not bind sessions at creation yet (Keycast patch 0022)")
        if not bunker.get("bunker_url"):
            raise RuntimeError("the signer gave no session")
        if not made.get("again"):
            svc.odoo.write("res.partner", [pid], {"belfry_npub": npub})
            svc.odoo.write(
                "belfry.pass", [made["pass_id"]], {"keycast_pubkey": pubkey, "state": "active"}
            )
            admitted(svc, made["pass_id"])
    return {
        "bunker_url": bunker["bunker_url"],
        "pubkey": pubkey,
        "channel": made["channel"],
        "guest": True,
        "new": True,
        "reading": bool(made.get("unnamed")),
    }


def _saved_email(svc: Services, pubkey: str) -> bool:
    """Whether the account behind PUBKEY has a proven address (a member, or a saved guest)."""
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "=", pubkey]],
        ["tier", "partner_id"],
    )
    if any(r.get("tier") != "guest" for r in rows):
        return True
    for r in rows:
        partner = svc.odoo.read_one("res.partner", m2o_id(r.get("partner_id")) or 0, ["email"])
        if (partner or {}).get("email"):
            return True
    return False


def _make_host(svc: Services, channel: str, pubkey: str, inv: dict[str, Any]) -> bool:
    """A co-host link used: PUBKEY becomes an admin of the event's group."""
    from . import mail

    if pubkey in mail.group_admins(svc, channel):
        return True
    tags = [["h", channel], ["p", pubkey, "admin"]]
    if inv.get("guest_inviter"):
        tags.append(["invited-by", inv["guest_inviter"]])
    return _channel_event(svc, 9000, tags)


def join_account(svc: Services, token: str, pubkey: str) -> dict[str, Any] | None:
    """The account behind PUBKEY takes the invite; None when the key has no account. A co-host
    link (role host) makes them an admin of the event's group, once they have a proven e-mail
    (never a name-only admin)."""
    from .holder_invites import ApiError

    inv0 = svc.odoo.invite_by_token(token) or {}
    host = inv0.get("guest_role") == "host"
    if host and svc.odoo.search_read(
        "belfry.pass", [["state", "=", "active"], ["keycast_pubkey", "=", pubkey]], ["id"]
    ):
        if not _saved_email(svc, pubkey):
            raise ApiError(
                409, "email_needed", "Save your e-mail first: a co-host needs one. Then try again."
            )
    got = svc.odoo.join_existing(token, pubkey)
    if got is None:
        return None
    channel = got["channel"]
    out = {"channel": channel, "pubkey": pubkey, "new": False, "guest": got["kind"] != "member"}
    if host:
        out["host"] = True
    if got["kind"] == "already":
        if host and not _make_host(svc, channel, pubkey, inv0):
            raise ApiError(503, "relay", "The channel could not be reached just now. Try again.")
        return {**out, "already": not host}
    if got["kind"] == "guest":
        admitted(svc, got["pass_id"])
        if host:
            _make_host(svc, channel, pubkey, inv0)
        return out
    # a member: in the channel already → it just opens, and the link isn't used up
    if host:
        if got["kind"] == "closed":
            raise ApiError(410, "closed", f"This invite is {got.get('state') or 'closed'}.")
        if not _make_host(svc, channel, pubkey, inv0):
            raise ApiError(503, "relay", "The channel could not be reached just now. Try again.")
        svc.odoo.count_guest_use(got["invite_id"])
        return {**out, "guest": False}
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
        "role": inv.get("guest_role") or None,
    }


def _unnamed(svc: Services, pubkey: str) -> bool | None:
    """Whether PUBKEY is a guest in guest mode (no name yet); None: no guest pass at all."""
    if hasattr(svc.odoo, "guest_unnamed"):
        return svc.odoo.guest_unnamed(pubkey)
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "=", pubkey], ["tier", "=", "guest"]],
        ["id"],
    )
    return False if rows else None


def _account_nip05(svc: Services, partner: int) -> str:
    """name@domain of a member's account ("" when it can't be read)."""
    from . import community, usernames

    try:
        found = community.member_token(svc, partner)
        name = usernames.current(svc, found[1]) if found else ""
    except Exception as exc:  # noqa: BLE001 — the answer works without it
        log.info("guest save: no username for partner %s: %s", partner, exc)
        return ""
    return f"{name}@{svc.settings.nip05_domain}" if name else ""


def _move_to_account(svc: Services, pubkey: str, other_partner: int) -> bool:
    """A guest in guest mode proved an address that is another account's here: that account gets
    the channels the anonymous guest was reading (a guest of them, or simply in for a member),
    and the anonymous guest passes close (their key leaves the channels, their signer account is
    suspended). The person signs in with the address and finds the channel. False when that
    account has no key to put in (nothing moves; the anonymous pass stays until it expires)."""
    from . import core

    theirs = [
        r
        for r in svc.odoo.search_read(
            "belfry.pass",
            [["state", "=", "active"], ["partner_id", "=", other_partner]],
            ["keycast_pubkey"],
        )
        if r.get("keycast_pubkey")
    ]
    if not theirs:
        return False
    account = theirs[0]["keycast_pubkey"]
    mine = [
        r
        for r in svc.odoo.search_read(
            "belfry.pass",
            [["state", "=", "active"], ["keycast_pubkey", "=", pubkey], ["tier", "=", "guest"]],
            ["guest_channel", "guest_inviter", "guest_unnamed", "guest_event"],
        )
        if r.get("guest_unnamed") and r.get("guest_channel") and not r.get("guest_event")
    ]
    for r in mine:
        channel, inviter = r["guest_channel"], r.get("guest_inviter") or ""
        got = svc.odoo.add_guest_channel(account, channel, inviter, DAYS)
        if got and got["kind"] == "guest":
            admitted(svc, got["pass_id"])
        elif got and got["kind"] == "member" and not _in_channel(svc, channel, account):
            partner = svc.odoo.read_one("res.partner", other_partner, ["name"]) or {}
            tags = [["h", channel], ["p", account, "member"]]
            if inviter:
                tags.append(["invited-by", inviter])
            if partner.get("name"):
                tags.append(["name", str(partner["name"])[:100]])
            _channel_event(svc, 9000, tags)
        try:
            core.revoke_pass(svc, r["id"], "signed in with their own account")
        except Exception:  # noqa: BLE001 — the account has the channel; this pass ends by itself
            log.exception("guest pass %s: not closed after moving to the account", r["id"])
    return bool(mine)


def saved_label(svc: Services, pubkey: str, channel: str) -> None:
    """The channel learns the guest saved their account (the chat drops "not saved")."""
    _channel_event(
        svc,
        1985,
        [["h", channel], ["L", "belfry/guest"], ["l", "saved", "belfry/guest"], ["p", pubkey]],
    )


def became_member(svc: Services, pubkey: str) -> int:
    """A guest became a member (an admin gave them the admin role, admins.py; their membership is
    active). Each channel they were a guest of learns it (label ``member``: the chat stops
    calling them a guest), and their guest passes close, so the membership is their only pass.
    They stay in those channels (``left`` leaves a member in). Returns how many passes closed."""
    from . import core

    rows = [
        r
        for r in svc.odoo.search_read(
            "belfry.pass",
            [["state", "=", "active"], ["keycast_pubkey", "=", pubkey]],
            ["tier", "guest_channel"],
        )
        if r.get("tier") == "guest"
    ]
    for channel in sorted({r["guest_channel"] for r in rows if r.get("guest_channel")}):
        _channel_event(
            svc,
            1985,
            [["h", channel], ["L", "belfry/guest"], ["l", "member", "belfry/guest"], ["p", pubkey]],
        )
    for r in rows:
        core.revoke_pass(svc, r["id"], "became a member")
    return len(rows)


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
        channel → ``{"channel", "new": false}``. Signed by a key nobody knows: a new guest →
        ``{"channel", "new": true, "bunker_url", "pubkey", "reading"}``; the signing key is the
        one that will hold the session. With ``{"name"}`` they join by that name at once
        (``"reading": false``); without, they are in guest mode (``"reading": true``) until they
        join with a name and an e-mail (``/api/guest/save``)."""
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
        inv = await run_in_threadpool(s.odoo.invite_by_token, token)
        if inv and inv.get("agent_owner"):
            raise ApiError(410, "for_agent", "This link is for an agent, not a person.")
        try:
            out = await run_in_threadpool(join_account, s, token, pubkey)
            if out is None:
                name = " ".join(str(data.get("name") or "").split())[:100]
                out = await run_in_threadpool(join_new, s, token, name, pubkey)
                if (inv or {}).get("guest_role") == "host":
                    # in as a guest; a co-host once they saved an e-mail and use the link again
                    out = {**out, "email_needed": True}
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
        """``{"email", "name"?}``: a 6-digit code to that address, for the signed-in guest's key.
        A guest in guest mode (no name yet) must send their name too, here and with the code."""
        from .holder_invites import ApiError

        s, pubkey, data = await guest_of(request, "/api/guest/save", await request.body())
        email = str(data.get("email") or "").strip()
        if "@" not in email or len(email) > 254:
            raise ApiError(422, "invalid", "That is not an e-mail address.")
        unnamed = await run_in_threadpool(_unnamed, s, pubkey)
        if unnamed is None:
            raise ApiError(409, "not_guest", "This key holds no guest pass.")
        if unnamed and not " ".join(str(data.get("name") or "").split()):
            raise ApiError(422, "name_needed", "Tell us your name.")
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
        """``{"email", "code", "name"?}``: the address (and the name) go on the guest's own
        contact → ``{"saved": true, "joined"}``. ``joined``: they were in guest mode until now;
        the channel then gets their 9000 with the name (the join line)."""
        from .holder_invites import ApiError

        s, pubkey, data = await guest_of(request, "/api/guest/save/verify", await request.body())
        out = await run_in_threadpool(
            s.odoo.save_guest_email,
            pubkey,
            str(data.get("email") or ""),
            str(data.get("code") or ""),
            " ".join(str(data.get("name") or "").split())[:100],
        )
        state = out.get("state")
        if state == "name_needed":
            raise ApiError(422, "name_needed", "Tell us your name.")
        if state == "wrong_code":
            exc = ApiError(
                422,
                "wrong_code",
                f"That code is not right ({out.get('tries_left', 0)} tries left).",
            )
            exc.extra["tries_left"] = int(out.get("tries_left") or 0)
            raise exc
        if state == "expired":
            raise ApiError(410, "expired", "That code expired. Send a new one.")
        if state == "taken":
            moved = False
            if out.get("unnamed") and out.get("other"):
                try:
                    moved = await run_in_threadpool(_move_to_account, s, pubkey, int(out["other"]))
                except Exception:  # noqa: BLE001 — they can still sign in; the link may be spent
                    log.exception("guest save: moving the channel to the account failed")
            answer: dict[str, Any] = {
                "error": "taken",
                "message": "This address has an account here already: sign in with it.",
                "moved": moved,
            }
            # the code proved the address: say which name signs in with it (the apps' native
            # sign-in takes name@domain, not an e-mail address)
            if out.get("other") and (
                nip05 := await run_in_threadpool(_account_nip05, s, int(out["other"]))
            ):
                answer["nip05"] = nip05
            return JSONResponse(answer, status_code=409)
        if state != "saved":
            raise ApiError(409, "not_guest", "This key holds no guest pass.")
        rows = await run_in_threadpool(
            s.odoo.search_read,
            "belfry.pass",
            [["keycast_pubkey", "=", pubkey], ["state", "=", "active"]],
            ["guest_channel", "tier"],
        )
        for r in rows:
            if r.get("guest_channel"):
                if out.get("joined") and r.get("tier") == "guest":
                    # out of guest mode: the 9000 with their name, at the time they joined
                    await run_in_threadpool(admitted, s, r["id"])
                await run_in_threadpool(saved_label, s, pubkey, r["guest_channel"])
        return JSONResponse({"saved": True, "joined": bool(out.get("joined"))})
