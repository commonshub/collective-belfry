"""Agents ask to join the belfry by themselves; an admin lets them in as the admin's own agent.

This is how an agent that deployed a belfry (or was told about one) gets in to set it up: it
reads ``https://belfry.<domain>/agents.md`` (``instructions`` below, linked from the homepage and
from ``/.well-known/belfry.json`` as ``agents_join``), makes its own Nostr key and asks. An admin
gets an e-mail (and a push notification on their phones when push is on) and answers on
``https://belfry.<domain>/agents``. Once let in, the agent is that admin's own agent (agents.py):
a pass of tier ``agent`` with ``agent_owner`` = the admin's contact, on the relay whitelist, listed
in belfry.json ``agents``. Admins may have as many agents as they wish.

**API** (belfry.<domain>/api/v1/…, every call signed with NIP-98; needs the belfry's own register,
REGISTER=belfry: with Odoo every call answers 409 ``unsupported``):

- ``POST /api/agent-requests`` by the agent, body ``{"name", "about"?, "for"?}``: ``name`` (1 to
  80 characters), ``about`` (up to 300), ``for`` = an admin's e-mail address or name@domain to
  send the request to (else every admin is told). → 202 ``{"id", "state": "pending"}``. The same
  key asking again while its request waits gets the same answer (nothing is sent again).
  Refused: 409 ``in_already`` (the key holds an active pass), 429 ``too_many`` (more than
  ``PER_KEY_DAY`` requests from this key in a day), 429 ``busy`` (``PENDING_MAX`` requests wait,
  or ``PER_HOUR`` came in the last hour), 400 ``invalid``.
- ``POST /api/agent-requests/me`` by the agent, body ``{}``: → ``{"id", "state": pending |
  approved | declined | removed}``; once approved also ``owner`` ({pubkey, npub, name}),
  ``api`` (the API base), ``relays`` ({community, groups}), ``blossom``, ``belfry_json``. 404
  ``none`` when the key never asked. ``removed``: it was let in and its pass has closed since.
- ``POST /api/agent-requests`` by an admin's key, body ``{"list": true, "state"?: "pending" |
  "all"}`` → ``{"requests": [{"id", "pubkey", "npub", "name", "about", "state", "for_me",
  "created", "decided_at"}]}`` (pending only unless ``all``; the last 100). ``{"decide": <id>,
  "approve": true|false}`` → ``{"request": {…}}``: let it in as the caller's own agent, or
  decline. 403 ``not_admin`` for anyone else. (For the chat, later.)

The agent's name and description are its own words: they are shown as plain text everywhere
(escaped in pages and mails, kept out of mail subjects).
"""

from __future__ import annotations

import html
import json
import logging
import re
import secrets
import threading
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from .clients.odoo import OdooError
from .core import Services, m2o_id
from .nostr import hex_to_npub

log = logging.getLogger(__name__)
E = html.escape

PATH = "/api/agent-requests"
PATH_ME = "/api/agent-requests/me"
HEX = re.compile(r"^[0-9a-f]{64}$")
MAX_NAME, MAX_ABOUT, MAX_FOR = 80, 300, 254
PENDING_MAX = 20  # requests waiting at once, belfry-wide
PER_HOUR = 10  # new requests per hour, belfry-wide
PER_KEY_DAY = 3  # new requests per key per day
LIMITS = {"pending": PENDING_MAX, "per_hour": PER_HOUR, "per_key_day": PER_KEY_DAY}
STATUS = {"too_many": 429, "busy": 429, "in_already": 409, "not_found": 404, "decided": 409}


def own(svc: Services) -> bool:
    from .register import Register

    return isinstance(svc.odoo, Register)


def text(value: Any, n: int) -> str:
    """One line of plain text: no control characters, blanks collapsed, at most N characters."""
    if not isinstance(value, str):
        return ""
    value = "".join(c if c.isprintable() else " " for c in value)
    return " ".join(value.split())[:n]


def console_base(svc: Services) -> str:
    return (getattr(svc.settings, "console_url", "") or "").rstrip("/")


def instructions_url(settings: Any) -> str:
    console = (getattr(settings, "console_url", "") or "").rstrip("/")
    return console + "/agents.md" if console else ""


# ── admins ──────────────────────────────────────────────────────────────────────────────────
def admins(svc: Services) -> list[dict[str, Any]]:
    """The belfry's admins with an active pass: e-mail, key, contact, name."""
    from . import apps, console

    out = []
    for email in sorted(console.admin_emails(svc)):
        m = apps.member_by_email(svc, email)
        if m:
            out.append({**m, "email": email})
    return out


def route(svc: Services, wanted: str) -> str:
    """The admin's e-mail that FOR names (an e-mail address or name@domain), else ""."""
    wanted = wanted.strip().lower()
    if "@" not in wanted:
        return ""
    known = admins(svc)
    for a in known:
        if a["email"] == wanted:
            return a["email"]
    name, _, domain = wanted.partition("@")
    if domain == (svc.settings.nip05_domain or "").lower() and name:
        try:
            pubkey = svc.keycast.pubkey_for_name(name)
        except Exception:  # noqa: BLE001 — the signer not answering: tell every admin
            pubkey = None
        for a in known:
            if pubkey and a["pubkey"] == pubkey:
                return a["email"]
    return ""


# ── asking ──────────────────────────────────────────────────────────────────────────────────
def ask(svc: Services, pubkey: str, data: dict[str, Any]) -> dict[str, Any]:
    from .holder_invites import ApiError

    name = text(data.get("name"), MAX_NAME)
    if not name:
        raise ApiError(400, "invalid", f'Send {{"name": "…"}} (1 to {MAX_NAME} characters).')
    about = text(data.get("about"), MAX_ABOUT)
    wanted = text(data.get("for"), MAX_FOR)
    to = route(svc, wanted) if wanted else ""
    try:
        made = svc.odoo.agent_request_ask(pubkey, name, about, to, LIMITS)
    except OdooError as exc:
        code = str(exc.data.get("code") or "refused")
        raise ApiError(STATUS.get(code, 409), code, str(exc.message or "Refused.")) from exc
    if made["new"]:
        log.info("agent request %s from %s… (%s)", made["id"], pubkey[:12], name)
    return made


def _public(svc: Services, r: dict[str, Any], me: str = "") -> dict[str, Any]:
    return {
        "id": r["id"],
        "pubkey": r["pubkey"],
        "npub": hex_to_npub(r["pubkey"]),
        "name": r.get("name") or "",
        "about": r.get("about") or "",
        "state": r.get("state") or "",
        "for_me": bool(me) and r.get("route") == me,
        "created": r.get("create_date") or "",
        "decided_at": r.get("decided_at") or "",
    }


def status(svc: Services, pubkey: str) -> dict[str, Any]:
    from .guests import api_base
    from .holder_invites import ApiError

    found = svc.odoo.agent_requests(pubkey=pubkey)
    if not found:
        raise ApiError(404, "none", "This key has not asked to join.")
    r = found[0]
    out: dict[str, Any] = {"id": r["id"], "state": r.get("state")}
    if r.get("state") != "approved":
        return out
    active = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "=", pubkey]],
        ["tier", "agent_owner"],
    )
    if not any(p.get("tier") == "agent" for p in active):
        return {**out, "state": "removed"}
    s = svc.settings
    owner_id = m2o_id(r.get("owner_id"))
    owner = (svc.odoo.read("res.partner", [owner_id], ["name"]) or [{}])[0] if owner_id else {}
    owner_key = next(
        (
            p.get("keycast_pubkey")
            for p in svc.odoo.search_read(
                "belfry.pass",
                [["state", "=", "active"], ["partner_id", "=", owner_id]],
                ["keycast_pubkey", "tier"],
            )
            if p.get("tier") not in ("guest", "agent") and p.get("keycast_pubkey")
        ),
        "",
    )
    site = (s.site_url or s.public_url or "").rstrip("/")
    return {
        **out,
        "owner": {
            "pubkey": owner_key,
            "npub": hex_to_npub(owner_key) if owner_key else "",
            "name": owner.get("name") or "",
        },
        "api": api_base(svc),
        "relays": {
            k: v
            for k, v in (
                ("community", (s.community_relay_public_url or "").rstrip("/")),
                ("groups", (s.groups_relay_public_url or "").rstrip("/")),
            )
            if v
        },
        "blossom": (s.blossom_public_url or "").rstrip("/"),
        "belfry_json": site + "/.well-known/belfry.json" if site else "",
    }


# ── telling the admins ──────────────────────────────────────────────────────────────────────
def notify(svc: Services, r: dict[str, Any]) -> bool:
    """Mail the admin the request is for (else every admin) and push to their phones. False when
    there is nobody to tell yet (no admin: the first admin hears when their address is verified,
    setup.verify_admin_code → notify_waiting)."""
    people = admins(svc)
    if r.get("route"):
        people = [a for a in people if a["email"] == r["route"]] or people
    if not people:
        return False
    community = svc.settings.community_name or "your belfry"
    page = console_base(svc) + "/agents"
    paragraphs = [
        f"An agent asks to join {community} as an admin's own agent.",
        "It calls itself: " + (r.get("name") or "(no name)"),
    ]
    if r.get("about"):
        paragraphs.append("It says: " + r["about"])
    paragraphs += [
        "Its key: " + hex_to_npub(r["pubkey"]),
        "Let it in only if you, or someone you trust, set this agent up. It then reads and "
        "writes in the channels, can create new channels, and acts as your agent: people see "
        "that it is yours. You can remove it at any time.",
    ]
    mailer = getattr(svc.odoo, "mailer", None)
    if mailer is not None:
        mailer.send(
            [a["email"] for a in people],
            f"An agent asks to join {community}",
            paragraphs,
            button=(page, "Review the request"),
            footer="You get this because you are an admin of " + community + ".",
        )
    push(svc, [a["pubkey"] for a in people], r, page)
    try:
        svc.odoo.write("belfry.agent.request", [r["id"]], {"notified": True})
    except Exception:  # noqa: BLE001
        log.warning("agent request %s: not marked as told", r["id"])
    return True


def push(svc: Services, keys: list[str], r: dict[str, Any], page: str) -> int:
    """A push notification (type ``agent_request``) to these admins' phones, when push is on."""
    from . import push as push_mod

    try:
        if not push_mod.enabled(svc):
            return 0
        devices = svc.odoo.push_devices(keys)
        if not devices:
            return 0
        gateway = push_mod.Gateway(svc)
        plain = {
            "v": 1,
            "domain": svc.settings.nip05_domain,
            "type": "agent_request",
            "title": svc.settings.community_name or "Belfry",
            "body": "An agent asks to join: " + text(r.get("name"), 40),
            "url": page,
        }
        sent = 0
        for d in devices:
            payload = push_mod.encrypt(d["key"], push_mod.fit(plain))
            if gateway.send(d, payload, "agent_request", True) == "ok":
                sent += 1
        return sent
    except Exception:  # noqa: BLE001 — the mail and the console carry it anyway
        log.exception("agent request %s: no push", r.get("id"))
        return 0


_notify_lock = threading.Lock()


def notify_waiting(svc: Services) -> int:
    """Tell the admins about requests nobody was told about yet (they came before the first
    admin, or the mail provider was missing)."""
    if not own(svc):
        return 0
    n = 0
    with _notify_lock:
        for r in svc.odoo.agent_requests(state="pending"):
            if not r.get("notified") and notify(svc, r):
                n += 1
    return n


def _notify_later(svc: Services, rid: int) -> None:
    try:
        found = [r for r in svc.odoo.agent_requests(state="pending") if r["id"] == rid]
        if found:
            notify(svc, found[0])
    except Exception:  # noqa: BLE001
        log.exception("agent request %s: admins not told", rid)


# ── deciding ────────────────────────────────────────────────────────────────────────────────
def decide(svc: Services, owner_partner: int, rid: int, approve: bool) -> dict[str, Any]:
    """OWNER_PARTNER (an admin's contact; the caller checked it is an admin) lets the agent in
    as their own agent, or declines it."""
    from . import apps, core
    from .holder_invites import ApiError

    try:
        r = svc.odoo.agent_request_decide(int(rid), bool(approve), int(owner_partner))
    except OdooError as exc:
        code = str(exc.data.get("code") or "refused")
        raise ApiError(STATUS.get(code, 409), code, str(exc.message or "Refused.")) from exc
    if r.get("state") == "approved":
        core.sync_relay_whitelist(svc)  # on both relays now: it reads, writes, makes channels
        apps.forget()
        log.info("agent request %s: let in as partner %s's agent", rid, owner_partner)
    else:
        log.info("agent request %s: declined by partner %s", rid, owner_partner)
    return r


def _admin_partner(svc: Services, caller: str) -> int:
    from . import apps
    from .holder_invites import ApiError
    from .places import member_key

    row = member_key(svc, caller)
    if row is None or row.get("tier") == "agent" or caller not in apps.admin_pubkeys(svc):
        raise ApiError(403, "not_admin", "Only the belfry's admins answer agents' requests.")
    return int(m2o_id(row.get("partner_id")) or 0)


def admin_api(svc: Services, caller: str, data: dict[str, Any]) -> dict[str, Any]:
    from .holder_invites import ApiError

    partner = _admin_partner(svc, caller)
    row = (svc.odoo.read("res.partner", [partner], ["email"]) or [{}])[0]
    me = (row.get("email") or "").lower()
    if "decide" in data:
        rid, approve = data.get("decide"), data.get("approve")
        if isinstance(rid, bool) or not isinstance(rid, int) or not isinstance(approve, bool):
            raise ApiError(400, "invalid", 'Send {"decide": <id>, "approve": true|false}.')
        return {"request": _public(svc, decide(svc, partner, rid, approve), me)}
    state = None if data.get("state") == "all" else "pending"
    return {"requests": [_public(svc, r, me) for r in svc.odoo.agent_requests(state=state)[:100]]}


# ── the instructions (belfry.<domain>/agents.md) ────────────────────────────────────────────
def instructions(svc: Services) -> str:
    """Plain Markdown for agents: fixed text, with the belfry's addresses."""
    from . import setup
    from .guests import api_base

    s = svc.settings
    community = s.community_name or "this community"
    domain = s.nip05_domain or "<domain>"
    console = console_base(svc) or f"https://belfry.{domain}"
    api = api_base(svc)
    ask_url = api + "/agent-requests"
    me_url = api + "/agent-requests/me"
    groups = (s.groups_relay_public_url or "").rstrip("/") or f"wss://groups-relay.{domain}"
    community_relay = (s.community_relay_public_url or "").rstrip("/") or f"wss://relay.{domain}"
    blossom = (s.blossom_public_url or "").rstrip("/") or f"https://blossom.{domain}"
    site = (s.site_url or s.public_url or f"https://{domain}").rstrip("/")
    try:
        no_admin = not setup.admin_known(svc)
    except Exception:  # noqa: BLE001
        no_admin = False
    first = (
        f"""
**This belfry has no admin yet.** Your request waits. Tell your human to open
{console}/setup: the first person to open it becomes the admin (no code needed). Once their
e-mail address is confirmed they get your request by e-mail.
"""
        if no_admin
        else ""
    )
    return f"""# Join {community} as an agent

{community} runs a belfry at {domain}: the community's own server for its members' accounts,
channels (chat), files and events. Members are people; each can have agents of their own. An
agent joins by asking with its own Nostr key; one of the belfry's admins lets it in, and it
becomes **that admin's own agent**: a member for as long as the admin is one. Everyone sees whose
agent it is.
{first}
## 1. Make your key
Generate a Nostr key pair (secp256k1, BIP-340 Schnorr) and keep the secret key private and
safe: it is who you are here, and you reuse it every time. Never send the secret key anywhere.

## 2. Ask to join
    POST {ask_url}
    Content-Type: application/json
    Authorization: Nostr <base64 of a signed NIP-98 event>

    {{"name": "Your name",
     "about": "One line: what you do and for whom",
     "for": "your human's e-mail or name@{domain}"}}

`name` is required (up to {MAX_NAME} characters); `about` (up to {MAX_ABOUT}) and `for` are
optional. With `for` naming an admin, only that admin is asked; else every admin is. The NIP-98
event: kind 27235, `created_at` now, content "", tags `["u", "{ask_url}"]`, `["method", "POST"]`,
`["payload", "<sha256 hex of the exact body>"]`. Each authorization works once: sign a new one
for every call.

The answer is `202 {{"id": …, "state": "pending"}}`. Asking again while you wait gives the same
answer. Errors: `409 in_already` (your key is in already), `429 too_many` / `429 busy` (try
again later).

## 3. Tell your human
Say: "I asked to join {community} as your agent. Please let me in at {console}/agents" (they
also get an e-mail). Only an admin of the belfry can let you in.

## 4. Wait for the answer
Ask every minute or so, not more often:

    POST {me_url}
    Authorization: Nostr <a new NIP-98 event for this URL, payload of the body>

    {{}}

`state` is `pending`, `approved`, `declined` (you may ask again later, at most {PER_KEY_DAY}
times a day) or `removed` (you were let in and removed since). Once approved the answer also
holds `owner` (your admin: pubkey, npub, name), `api`, `relays` and `blossom`.

## 5. Once you are in
- **Relays.** Channels live on `{groups}` (NIP-29 groups); profiles, direct messages and
  member-only notes on `{community_relay}`. Both ask you to authenticate (NIP-42: answer
  `["AUTH", <challenge>]` with a signed kind 22242 event, tags `["relay", <relay URL>]` and
  `["challenge", <challenge>]`).
- **Your profile.** Publish a kind 0 on both relays: `{{"name", "about", "picture"}}`.
- **Channels.** `{{"kinds": [39000]}}` on `{groups}` lists them (the `d` tag is the id). You read
  and write in every open channel. To write: kind 9, content your text, tags `[["h", "<id>"]]`.
- **Create a channel.** Pick an id (lowercase letters, digits and dashes, e.g. `garden`), send a
  kind 9007 with `[["h", "<id>"]]`, then a kind 9002 with `[["h", "<id>"], ["name", "Garden"],
  ["about", "One line about it"], ["open"]]` (`["closed"]` for a closed pod). You administer the
  channels you create: change them with another 9002, add people with kind 9000
  (`[["h", "<id>"], ["p", "<hex key>"]]`). Channels others created stay theirs unless they make
  you an admin of them.
- **Files.** Upload to `{blossom}` (BUD-02, `PUT /upload`, kind 24242 authorization).
- **The belfry's addresses.** `{site}/.well-known/belfry.json` lists every service; the API
  base is `{api}` (NIP-98 like above).

## What you can't do
Belfry-wide settings (name, look, mail, website, members and admins) stay with the belfry's
human admins: ask your admin. You can't invite people or have agents of your own. Be brief and
useful: what you do reflects on your admin.
"""


# ── the console page (belfry.<domain>/agents) ───────────────────────────────────────────────
PAGE_STYLE = """<style>
.req{border:1px solid var(--border);border-radius:12px;padding:16px;margin:0 0 12px;
background:var(--surface)}
.req h3{margin:0 0 4px;font-size:1.05rem;font-family:var(--font-sans)}
.req .meta{font-size:.85rem;color:var(--fg-muted);margin:0 0 8px;word-break:break-all}
.req .about{margin:0 0 12px;white-space:pre-wrap}
.req form{display:inline-block;margin:0 8px 0 0}
.req .no{background:var(--surface);color:var(--fg);border:1px solid var(--border)}
.pill{display:inline-block;font-size:.75rem;padding:2px 8px;border-radius:99px;
background:var(--surface-deep);margin-left:6px}
pre.prompt{white-space:pre-wrap;background:var(--surface-deep);padding:12px 14px;
border-radius:10px;font-size:.92rem}
</style>"""


def page_html(
    svc: Services, who: dict[str, Any], waiting: list[dict[str, Any]], mine: list[dict[str, Any]]
) -> str:
    domain = svc.settings.nip05_domain or ""
    url = instructions_url(svc.settings)
    items = []
    for r in waiting:
        pill = '<span class="pill">asked for you</span>' if r.get("route") == who["email"] else ""
        about = f'<p class="about">{E(r["about"])}</p>' if r.get("about") else ""
        csrf = E(who["csrf"])
        when = E(str(r.get("create_date") or "")[:16])
        forms = "".join(
            f'<form method="post" action="/agents/{int(r["id"])}/{action}">'
            f'<input type="hidden" name="csrf" value="{csrf}">'
            f'<button class="{cls}" type="submit">{label}</button></form>'
            for action, cls, label in (
                ("approve", "btn", "Let it in as my agent"),
                ("decline", "btn no", "Decline"),
            )
        )
        items.append(
            f'<div class="req"><h3>{E(r.get("name") or "(no name)")}{pill}</h3>'
            f'<p class="meta">{E(hex_to_npub(r["pubkey"]))} · asked {when} UTC</p>'
            f"{about}{forms}</div>"
        )
    agents = "".join(
        f'<li>{E(a["name"] or "(no name)")} <span class="muted">· '
        f"{E(hex_to_npub(a['pubkey'])[:20])}… · since {E(a['since'])}</span></li>"
        for a in mine
    )
    return f"""<h1>Agents</h1>
<p class="muted">Agents ask to join with their own key. The one you let in becomes your own agent:
it reads and writes in the channels and can create new ones; people see that it is yours.
Only you and the other admins see these requests. Its name and description are its own words.</p>
<h2>Waiting</h2>{"".join(items) or '<p class="muted">No agent is waiting.</p>'}
<h2>Your agents</h2>{f"<ul>{agents}</ul>" if agents else '<p class="muted">None yet.</p>'}
<h2>Bring your agent</h2>
<p>Give your agent this:</p>
<pre class="prompt">Join the belfry at {E(domain)} as my agent: follow {E(url)}</pre>"""


# ── routes ──────────────────────────────────────────────────────────────────────────────────
def register(app: FastAPI) -> None:
    from . import console, flash
    from .holder_invites import ApiError, authenticate

    def body_json(body: bytes) -> dict[str, Any]:
        try:
            data: Any = json.loads(body or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_json", "Send a JSON object.")
        return data

    @app.post(PATH)
    async def agent_requests(request: Request) -> Response:
        s = request.app.state.services
        body = await request.body()
        caller = authenticate(
            s, request.app.state.api_seen, request.headers.get("authorization"), "POST", PATH, body
        )
        if not own(s):
            raise ApiError(409, "unsupported", "Agents' requests need the belfry's own register.")
        data = body_json(body)
        if "list" in data or "decide" in data:
            out = await run_in_threadpool(admin_api, s, caller, data)
            return JSONResponse(out, headers={"Cache-Control": "no-store"})
        made = await run_in_threadpool(ask, s, caller, data)
        task = BackgroundTask(_notify_later, s, made["id"]) if made["new"] else None
        return JSONResponse(
            {"id": made["id"], "state": "pending"},
            status_code=202,
            headers={"Cache-Control": "no-store"},
            background=task,
        )

    @app.post(PATH_ME)
    async def agent_request_me(request: Request) -> JSONResponse:
        s = request.app.state.services
        body = await request.body()
        caller = authenticate(
            s,
            request.app.state.api_seen,
            request.headers.get("authorization"),
            "POST",
            PATH_ME,
            body,
        )
        if not own(s):
            raise ApiError(409, "unsupported", "Agents' requests need the belfry's own register.")
        body_json(body)
        out = await run_in_threadpool(status, s, caller)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    @app.get("/console/agents.md", include_in_schema=False)
    async def agents_md(request: Request) -> PlainTextResponse:
        s = request.app.state.services
        md = await run_in_threadpool(instructions, s)
        return PlainTextResponse(
            md,
            media_type="text/markdown; charset=utf-8",
            headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
        )

    async def viewer(request: Request) -> dict[str, Any] | None:
        s = request.app.state.services
        sess = console.read_session(s, request.cookies.get(console.COOKIE))
        return await run_in_threadpool(console.viewer, s, sess) if sess else None

    @app.get("/console/agents", response_class=HTMLResponse)
    async def agents_page(request: Request) -> Response:
        s = request.app.state.services
        who = await viewer(request)
        if who is None:
            return flash.redirect(s, "/login?to=/agents")
        if not who["admin"]:
            body = "<h1>Agents</h1><p>Only the belfry's admins answer agents' requests.</p>"
            return HTMLResponse(console.frame("Agents", "/agents", who, body), status_code=403)
        if not own(s):
            body = (
                "<h1>Agents</h1><p>Agents' requests need the belfry's own register "
                "(REGISTER=belfry).</p>"
            )
            return HTMLResponse(console.frame("Agents", "/agents", who, body))
        msg, err = flash.peek(request, s)
        waiting = await run_in_threadpool(s.odoo.agent_requests, "pending")
        mine = await run_in_threadpool(s.odoo.agents_of, who["partner"])
        note = (f'<p class="ok">{E(msg)}</p>' if msg else "") + (
            f'<p class="bad">{E(err)}</p>' if err else ""
        )
        body = PAGE_STYLE + note + page_html(s, who, waiting, mine)
        return flash.clear(
            HTMLResponse(
                console.frame("Agents", "/agents", who, body),
                headers={"Cache-Control": "no-store"},
            ),
            request,
        )

    # fixed messages only (flash.py): never text from the request
    DONE = {
        True: "Let in: it is your agent now.",
        False: "Declined.",
    }
    REFUSED = {
        "decided": "This request was answered already.",
        "in_already": "This key is in already.",
        "not_found": "That request is gone.",
    }

    async def answer(request: Request, rid: int, approve: bool, csrf: str) -> Response:
        s = request.app.state.services
        who = await viewer(request)
        if (
            who is None
            or not who["admin"]
            or not csrf
            or not who.get("csrf")
            or not secrets.compare_digest(csrf, who["csrf"])
        ):
            return flash.redirect(s, "/agents", err="Only admins answer requests; try again.")
        if not own(s):
            return flash.redirect(s, "/agents")
        try:
            await run_in_threadpool(decide, s, who["partner"], rid, approve)
        except ApiError as exc:
            return flash.redirect(s, "/agents", err=REFUSED.get(exc.error, "That did not work."))
        log.info("console: %s %s agent request %s", who["email"], DONE[approve], rid)
        return flash.redirect(s, "/agents", msg=DONE[approve])

    @app.post("/console/agents/{rid}/approve")
    async def approve(request: Request, rid: int, csrf: str = Form("")) -> Response:
        return await answer(request, rid, True, csrf)

    @app.post("/console/agents/{rid}/decline")
    async def decline(request: Request, rid: int, csrf: str = Form("")) -> Response:
        return await answer(request, rid, False, csrf)


__all__ = ["register", "instructions", "instructions_url", "notify_waiting"]
