"""Pages every belfry serves by default: ``/monitor``, ``/changelog`` and ``/proposals``.

- ``/monitor`` (members): the community relay live, like a relay monitor: every event as it
  arrives, filtered by author and kind, paused, inspected. The relay is members-only, so the page
  reads through this service (the belfry's key, which the whitelist carries) as server-sent events,
  and only for a signed-in member with an active pass (the pass page's session, mypass.py).
- ``/changelog`` (public): every change to this belfry and who started it: the deployed code's
  commits (author and co-authors) and the operations run on the server (`belfry config`,
  `apply`, `update`), from ``infra/scripts/changelog.sh`` (mounted at CHANGELOG_PATH).
- ``/proposals`` (members): any member proposes something, others support it and comment. Nostr
  behind the scenes on the community relay, the same shape as a public feedback board: a
  proposal is a kind-1 note tagged ``t=proposal`` and addressed to the belfry's key (``p``),
  with a ``subject``; comments are kind-1 replies (``e`` root); support is a kind-7 ``+``. Notes
  are signed as the member through the signer (Keycast); members who hold their own key post
  the same note from any Nostr client.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import html
import json
import logging
import time
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from . import mypass, nostr
from .claim import page
from .core import Services
from .homepage import belfry_key, belfry_pubkey
from .nip46check import sign_event

log = logging.getLogger(__name__)
E = html.escape

PROPOSAL_TAG = "proposal"
CACHE_SECONDS = 15.0
MAX_TITLE = 140
MAX_BODY = 8000

KIND_NAMES = {
    0: "profile",
    1: "note",
    3: "follows",
    5: "delete",
    6: "repost",
    7: "reaction",
    9: "group message",
    1059: "gift wrap (DM)",
    8550: "app approval request",
    8551: "app approved",
    8552: "app approval withdrawn",
    1063: "file",
    10002: "relay list",
    10050: "DM relays",
    10063: "blossom servers",
    15128: "nsite",
    30023: "article",
    31922: "date event",
    31923: "time event",
    31925: "RSVP",
    35128: "named nsite",
}


# ── who is asking ───────────────────────────────────────────────────────────────────────────
def member_partner(svc: Services, cookie: str | None) -> int | None:
    """The signed-in member's partner id when they hold an active pass, else None."""
    if not mypass.enabled(svc):
        return None
    sess = mypass._verify(svc, cookie, "session")
    if not sess:
        return None
    row = mypass.partner_pass(svc, sess["partner"])
    return sess["partner"] if row and row.get("state") == "active" else None


def belfry_signer(svc: Services):
    key = belfry_key(svc.settings)
    if key is None:
        raise RuntimeError("no belfry key (secret belfry_nsec)")
    return lambda event: sign_event(key.secret, event)


def member_signer(svc: Services, partner_id: int):
    """(pubkey, sign) for a member whose key the signer holds; None for a key of their own."""
    found = member_token(svc, partner_id)
    if found is None:
        return None
    pubkey, token = found

    def sign(unsigned: dict[str, Any]) -> dict[str, Any]:
        return svc.keycast.sign_event(token, {"pubkey": pubkey, **unsigned})

    return pubkey, sign


def member_token(svc: Services, partner_id: int) -> tuple[str, str] | None:
    """(pubkey, signer token) for a member whose key the signer holds; None for a key of their
    own."""
    rows = svc.odoo.read(
        "res.partner",
        [partner_id],
        ["name", "belfry_member_number", "belfry_key_version", "belfry_key_custody"],
    )
    if not rows or rows[0].get("belfry_key_custody") == "self":
        return None
    partner = rows[0]
    number = partner.get("belfry_member_number") or f"member-{partner_id}"
    # idempotent: the existing account's key and a fresh token scoped to it
    result = svc.keycast.preload_user(
        vine_id=f"odoo-partner-{partner_id}",
        username=str(number).lower(),
        display_name=partner.get("name") or str(number),
        key_version=int(partner.get("belfry_key_version") or 0),
    )
    return result["pubkey"], result["token"]


def query(svc: Services, filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if svc.relay is None:
        return []
    # the community relay checks signatures when it stores an event (rnostr)
    return svc.relay.query(filters, belfry_signer(svc))


def names_for(svc: Services, pubkeys: list[str]) -> dict[str, str]:
    """Display names from kind-0 profiles; the npub (shortened) when there is none."""
    pubkeys = sorted(set(pubkeys))[:200]
    names: dict[str, tuple[int, str]] = {}
    if pubkeys:
        for e in query(svc, [{"kinds": [0], "authors": pubkeys}]):
            try:
                c = json.loads(e.get("content") or "{}")
            except ValueError:
                continue
            name = (c.get("display_name") or c.get("name") or "").strip()
            if name and e["created_at"] >= names.get(e["pubkey"], (0, ""))[0]:
                names[e["pubkey"]] = (e["created_at"], name)
    out = {}
    for pk in pubkeys:
        if pk in names:
            out[pk] = names[pk][1]
        else:
            npub = nostr.hex_to_npub(pk)
            out[pk] = npub[:12] + "…" + npub[-4:]
    if belfry_pubkey(svc.settings) in out:
        out[belfry_pubkey(svc.settings)] = svc.settings.community_name or "This belfry"
    return out


def when(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%-d %b %Y, %H:%M UTC")


def text_html(text: str) -> str:
    return E(text).replace("\n", "<br>")


STYLE = """<style>
.tabs{display:flex;gap:14px;margin:0 0 16px;font-size:.95rem}.tabs a{text-decoration:none}
.tabs a.on{font-weight:600;text-decoration:underline}
.item{border-top:1px solid var(--border);padding:12px 0}.item:first-child{border-top:0}
.meta{font-size:.85rem;color:var(--fg-muted)}
input[type=text],textarea,select{width:100%;box-sizing:border-box;font:inherit;padding:8px 10px;
border-radius:8px;border:1px solid var(--border);background:var(--surface-deep);color:inherit}
textarea{min-height:120px}button{font:inherit;cursor:pointer}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.pill{display:inline-block;font-size:.8rem;padding:1px 8px;border-radius:99px;
background:var(--surface-deep);border:1px solid var(--border)}
</style>"""


def tabs(current: str) -> str:
    links = [("/proposals", "Proposals"), ("/changelog", "Changelog"), ("/monitor", "Monitor")]
    return (
        '<nav class="tabs">'
        + "".join(f'<a href="{h}" class="{"on" if h == current else ""}">{t}</a>' for h, t in links)
        + "</nav>"
    )


def sign_in_card(title: str, what: str) -> str:
    return page(
        title,
        STYLE
        + f"""<div class="card">{tabs("/" + title.lower())}<h1>{E(title)}</h1>
<p class="muted">{E(what)} It is for members of this community.</p>
<p><a class="btn" href="/me/login">Sign in</a></p>
<p class="meta">After signing in, come back to this page.</p></div>""",
    )


# ── proposals ───────────────────────────────────────────────────────────────────────────────
_cache: dict[str, Any] = {"at": 0.0, "data": None}


def load_proposals(svc: Services, fresh: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    if not fresh and _cache["data"] is not None and now - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]
    belfry = belfry_pubkey(svc.settings)
    roots = [
        e
        for e in query(svc, [{"kinds": [1], "#t": [PROPOSAL_TAG], "#p": [belfry], "limit": 300}])
        if not any(t[0] == "e" for t in e.get("tags", []))
    ]
    ids = [e["id"] for e in roots]
    related = query(svc, [{"kinds": [1, 7], "#e": ids, "limit": 2000}]) if ids else []
    comments: dict[str, list] = {i: [] for i in ids}
    support: dict[str, set] = {i: set() for i in ids}
    for e in related:
        es = [t[1] for t in e.get("tags", []) if len(t) > 1 and t[0] == "e"]
        if not es:
            continue
        if e["kind"] == 1:
            root = next(
                (t[1] for t in e["tags"] if t[0] == "e" and len(t) > 3 and t[3] == "root"), es[0]
            )
            if root in comments:
                comments[root].append(e)
        elif (
            e["kind"] == 7
            and es[-1] in support
            and (e.get("content") or "+").strip() in ("+", "👍", "❤️", "")
        ):
            support[es[-1]].add(e["pubkey"])
    authors = {e["pubkey"] for e in roots + related}
    data = {
        "roots": sorted(roots, key=lambda e: e["created_at"], reverse=True),
        "comments": {k: sorted(v, key=lambda e: e["created_at"]) for k, v in comments.items()},
        "support": support,
        "names": names_for(svc, list(authors)) if authors else {},
    }
    _cache.update(at=now, data=data)
    return data


def subject(e: dict[str, Any]) -> str:
    s = next((t[1] for t in e.get("tags", []) if len(t) > 1 and t[0] == "subject"), "")
    return s or (e.get("content") or "").strip().split("\n", 1)[0][:MAX_TITLE] or "(untitled)"


def body_of(e: dict[str, Any]) -> str:
    text = (e.get("content") or "").strip()
    s = subject(e)
    if text.startswith(s):
        text = text[len(s) :]
    return text.replace("#proposal", "").strip()


def proposals_page(data: dict[str, Any], can_post: bool, own_key: bool, msg: str = "") -> str:
    names, support, comments = data["names"], data["support"], data["comments"]
    items = []
    for e in data["roots"]:
        n_support, n_comments = len(support[e["id"]]), len(comments[e["id"]])
        items.append(
            f"""<div class="item"><a href="/proposals/{e["id"]}"><strong>{E(subject(e))}</strong></a>
<div class="meta">by {E(names.get(e["pubkey"], ""))} · {when(e["created_at"])} ·
<span class="pill">{n_support} support</span> <span class="pill">{n_comments} comments</span></div></div>"""
        )
    listing = "".join(items) or '<p class="muted">No proposals yet. Be the first.</p>'
    form = ""
    if can_post:
        form = f"""<h2>Make a proposal</h2>
<form method="post" action="/proposals"><p><input type="text" name="title" maxlength="{MAX_TITLE}" required placeholder="What do you propose?"></p>
<p><textarea name="body" maxlength="{MAX_BODY}" placeholder="Why, how, who is in. Other members can support it and comment."></textarea></p>
<p><button class="btn" type="submit">Publish the proposal</button></p></form>"""
    elif own_key:
        form = """<p class="muted">You hold your own key, so this page can't sign for you. Post from any
Nostr client to this belfry's relay: a note with the tag <code>#proposal</code> that mentions the
belfry's account.</p>"""
    note = f'<p class="ok">{E(msg)}</p>' if msg else ""
    return page(
        "Proposals",
        STYLE
        + f"""<div class="card">{tabs("/proposals")}<h1>Proposals</h1>
<p class="muted">Anything a member wants the community to do or change. Support the ones you back;
comment to improve them. Stored as Nostr notes on this community's relay.</p>{note}{form}
<h2>All proposals</h2>{listing}</div>""",
    )


def proposal_page(e: dict[str, Any], data: dict[str, Any], can_post: bool, me: str | None) -> str:
    names = data["names"]
    supporters = data["support"][e["id"]]
    comments = data["comments"][e["id"]]
    who = ", ".join(E(names.get(p, "")) for p in sorted(supporters)) or "nobody yet"
    thread = (
        "".join(
            f"""<div class="item"><div class="meta">{E(names.get(c["pubkey"], ""))} · {when(c["created_at"])}</div>
<div>{text_html((c.get("content") or "").strip())}</div></div>"""
            for c in comments
        )
        or '<p class="muted">No comments yet.</p>'
    )
    actions = ""
    if can_post:
        backed = me in supporters
        actions = f"""<form method="post" action="/proposals/{e["id"]}/support" class="row">
<button class="btn" type="submit" {"disabled" if backed else ""}>{"You support this" if backed else "Support"}</button></form>
<h2>Comment</h2><form method="post" action="/proposals/{e["id"]}/comment">
<p><textarea name="body" maxlength="{MAX_BODY}" required></textarea></p>
<p><button class="btn" type="submit">Send</button></p></form>"""
    return page(
        subject(e),
        STYLE
        + f"""<div class="card">{tabs("/proposals")}<p><a href="/proposals">← All proposals</a></p>
<h1>{E(subject(e))}</h1><div class="meta">by {E(names.get(e["pubkey"], ""))} · {when(e["created_at"])}</div>
<p>{text_html(body_of(e))}</p>
<p class="meta"><strong>{len(supporters)} support:</strong> {who}</p>{actions}
<h2>Comments</h2>{thread}
<p class="meta">Nostr event <code>{e["id"]}</code></p></div>""",
    )


# ── changelog ───────────────────────────────────────────────────────────────────────────────
def changelog_page(data: dict[str, Any] | None) -> str:
    if not data or not data.get("entries"):
        listing = '<p class="muted">No changelog yet: it is written when the belfry is updated or reconfigured (<code>belfry apply</code>).</p>'
    else:
        days: dict[str, list[str]] = {}
        repo = (data.get("repo") or "").removesuffix(".git")
        for x in data["entries"][:500]:
            try:
                at = dt.datetime.fromisoformat(x["at"])
            except (KeyError, ValueError):
                continue
            by = ", ".join(x.get("by") or [])
            kind = x.get("type", "code")
            ref = x.get("ref", "")
            link = ""
            if kind == "code" and ref and repo.startswith("https://"):
                link = f' <a class="meta" href="{E(repo)}/commit/{E(ref)}"><code>{E(ref[:7])}</code></a>'
            if x.get("proposal"):
                link += (
                    f' · <a class="meta" href="/proposals/{E(x["proposal"])}">from a proposal</a>'
                )
            badge = {"code": "code", "website": "website"}.get(kind, "server")
            days.setdefault(at.strftime("%-d %B %Y"), []).append(
                f"""<div class="item"><span class="pill">{badge}</span> {E(x.get("summary", ""))}{link}
<div class="meta">{("started by " + E(by) + " · ") if by else ""}{at.strftime("%H:%M")} UTC</div></div>"""
            )
        listing = "".join(f"<h2>{E(d)}</h2>{''.join(items)}" for d, items in days.items())
    return page(
        "Changelog",
        STYLE
        + f"""<div class="card">{tabs("/changelog")}<h1>Changelog</h1>
<p class="muted">Every change to this belfry: the code it runs and what was changed on the server,
with who started each change. Settings are named, never their values.</p>{listing}</div>""",
    )


def website_changes(svc: Services) -> list[dict[str, Any]]:
    """The website's versions: the belfry's manifest snapshots (NIP-5A kind 5128) on the relay."""
    belfry = belfry_pubkey(svc.settings)
    if not belfry or svc.relay is None:
        return []
    try:
        snaps = query(svc, [{"kinds": [5128], "authors": [belfry], "limit": 100}])
    except Exception as exc:
        log.info("changelog: website history not readable: %s", exc)
        return []
    return [
        {
            "at": dt.datetime.fromtimestamp(e["created_at"], dt.UTC).isoformat(timespec="seconds"),
            "type": "website",
            "summary": (e.get("content") or "Website updated").strip(),
            "by": [],
            **(
                {"proposal": t[1]}
                if (t := next((t for t in e["tags"] if t[0] == "e"), None))
                else {}
            ),
        }
        for e in snaps
    ]


def full_changelog(svc: Services) -> dict[str, Any]:
    data = read_changelog(svc.settings.changelog_path) or {"entries": []}
    entries = [*data.get("entries", []), *website_changes(svc)]

    def key(e: dict[str, Any]) -> float:
        try:
            return dt.datetime.fromisoformat(e["at"]).timestamp()
        except (KeyError, ValueError):
            return 0.0

    return {**data, "entries": sorted(entries, key=key, reverse=True)}


def read_changelog(path: str) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ── monitor ─────────────────────────────────────────────────────────────────────────────────
MONITOR_JS = """
const $=(id)=>document.getElementById(id);const events=[];const names={};let paused=false,src=null,sel=null;
const kinds=%s;
function label(pk){return names[pk]||pk.slice(0,8)+'…'}
function kindName(k){return kinds[k]?k+' · '+kinds[k]:String(k)}
function render(){const list=$('list');list.textContent='';
 for(const e of events.slice(0,300)){const d=document.createElement('button');d.className='ev'+(sel===e.id?' on':'');
  d.innerHTML='<span class="pill"></span> <strong></strong> <span class="meta"></span><div class="meta txt"></div>';
  d.querySelector('.pill').textContent=kindName(e.kind);d.querySelector('strong').textContent=label(e.pubkey);
  d.querySelector('span.meta').textContent=new Date(e.created_at*1000).toLocaleTimeString();
  d.querySelector('.txt').textContent=(e.kind===1059||e.kind===4)?'(encrypted)':(e.content||'').slice(0,140);
  d.onclick=()=>{sel=e.id;$('inspect').textContent=JSON.stringify(e,null,2);render()};list.appendChild(d)}
 $('count').textContent=events.length+' events'}
const want=new Set();let timer=null;
function askNames(pk){if(names[pk]||want.has(pk))return;want.add(pk);clearTimeout(timer);timer=setTimeout(async()=>{
 const p=[...want];want.clear();try{const r=await fetch('/monitor/names?p='+p.join(','));Object.assign(names,await r.json());render();fillAuthors()}catch{}},400)}
const seenK=new Set(),seenA=new Set();
function fillAuthors(){const a=$('fauthor'),v=a.value;a.textContent='';a.add(new Option('All authors',''));
 for(const pk of [...seenA].sort((x,y)=>label(x).localeCompare(label(y))))a.add(new Option(label(pk),pk));a.value=v}
function fillKinds(){const k=$('fkind'),v=k.value;k.textContent='';k.add(new Option('All kinds',''));
 for(const n of [...seenK].sort((x,y)=>x-y))k.add(new Option(kindName(n),String(n)));k.value=v}
function connect(){if(src)src.close();events.length=0;render();
 const q=new URLSearchParams();if($('fauthor').value)q.set('author',$('fauthor').value);if($('fkind').value)q.set('kind',$('fkind').value);
 src=new EventSource('/monitor/stream?'+q);$('status').textContent='connecting…';
 src.onopen=()=>$('status').textContent='live';src.onerror=()=>$('status').textContent='reconnecting…';
 src.onmessage=(m)=>{const e=JSON.parse(m.data);if(events.some(x=>x.id===e.id))return;
  if(!seenK.has(e.kind)){seenK.add(e.kind);fillKinds()}if(!seenA.has(e.pubkey)){seenA.add(e.pubkey);fillAuthors()}
  askNames(e.pubkey);events.unshift(e);events.sort((a,b)=>b.created_at-a.created_at);if(!paused)render()}}
$('fauthor').onchange=connect;$('fkind').onchange=connect;
$('pause').onclick=()=>{paused=!paused;$('pause').textContent=paused?'Resume':'Pause';if(!paused)render()};
connect();
"""


def monitor_page() -> str:
    return page(
        "Monitor",
        STYLE
        + """<style>main{max-width:1100px}.mon{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px}
@media(max-width:760px){.mon{grid-template-columns:1fr}}
#list{max-height:70vh;overflow:auto}.ev{display:block;width:100%;text-align:left;background:none;color:inherit;
border:0;border-top:1px solid var(--border);padding:8px 4px}.ev.on{background:var(--surface-deep)}
#inspect{white-space:pre-wrap;word-break:break-all;font:12px/1.4 var(--font-mono);max-height:70vh;overflow:auto;margin:0}
.row select{width:auto;flex:1}</style>"""
        + f"""<div class="card">{tabs("/monitor")}<h1>Monitor</h1>
<p class="muted">Events published to this community's relay, live. Select one to inspect it.
<span class="pill" id="status">…</span> <span class="meta" id="count"></span></p>
<div class="row"><select id="fauthor"><option value="">All authors</option></select>
<select id="fkind"><option value="">All kinds</option></select><button class="btn" id="pause" type="button">Pause</button></div>
<div class="mon"><div id="list"></div><pre id="inspect" class="meta">Select an event.</pre></div></div>
<script>{MONITOR_JS % json.dumps(KIND_NAMES)}</script>""",
    )


async def relay_stream(svc: Services, flt: dict[str, Any], request: Request):
    """Server-sent events from the community relay, read with the belfry's key (NIP-42)."""
    from websockets.asyncio.client import connect

    sign = belfry_signer(svc)
    url = svc.settings.relay_url
    req = json.dumps(["REQ", "monitor", flt])
    try:
        async with connect(url, open_timeout=10) as ws:
            await ws.send(req)
            auth_id = None
            while not await request.is_disconnected():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=20)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                msg = json.loads(raw)
                if msg[0] == "AUTH" and auth_id is None:
                    auth = sign(
                        {
                            "kind": 22242,
                            "created_at": int(time.time()),
                            "tags": [["relay", url], ["challenge", msg[1]]],
                            "content": "",
                        }
                    )
                    auth_id = auth["id"]
                    await ws.send(json.dumps(["AUTH", auth]))
                elif msg[0] == "OK" and msg[1] == auth_id and msg[2]:
                    await ws.send(req)
                elif msg[0] == "EVENT" and msg[1] == "monitor":
                    yield "data: " + json.dumps(msg[2], separators=(",", ":")) + "\n\n"
    except Exception as exc:  # the browser's EventSource reconnects
        log.info("monitor stream: %s", exc)
        yield "retry: 3000\n\n"


# ── routes ──────────────────────────────────────────────────────────────────────────────────
def register(app: FastAPI) -> None:
    def svc(request: Request) -> Services:
        return request.app.state.services

    async def partner(request: Request) -> int | None:
        return await run_in_threadpool(
            member_partner, svc(request), request.cookies.get(mypass.SESSION_COOKIE)
        )

    def same_origin(request: Request) -> bool:
        origin = request.headers.get("origin")
        s = svc(request).settings
        allowed = {mypass._origin(s.public_url), mypass._origin(s.site_url)}
        return not origin or origin in allowed

    @app.get("/changelog", response_class=HTMLResponse)
    async def changelog(request: Request):
        data = await run_in_threadpool(full_changelog, svc(request))
        return HTMLResponse(changelog_page(data))

    @app.get("/changelog.json")
    async def changelog_json(request: Request):
        data = await run_in_threadpool(full_changelog, svc(request))
        return JSONResponse(data or {"entries": []})

    @app.get("/monitor", response_class=HTMLResponse)
    async def monitor(request: Request):
        if await partner(request) is None:
            return HTMLResponse(sign_in_card("Monitor", "The community relay, live."))
        return HTMLResponse(monitor_page(), headers={"Cache-Control": "no-store"})

    @app.get("/monitor/stream")
    async def monitor_stream(request: Request, author: str = "", kind: str = ""):
        if await partner(request) is None:
            return JSONResponse({"detail": "members only"}, status_code=401)
        flt: dict[str, Any] = {"limit": 50}
        if len(author) == 64 and all(c in "0123456789abcdef" for c in author):
            flt["authors"] = [author]
        if kind.isdigit():
            flt["kinds"] = [int(kind)]
        return StreamingResponse(
            relay_stream(svc(request), flt, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/monitor/names")
    async def monitor_names(request: Request, p: str = ""):
        if await partner(request) is None:
            return JSONResponse({"detail": "members only"}, status_code=401)
        keys = [k for k in p.split(",") if len(k) == 64 and all(c in "0123456789abcdef" for c in k)]
        return JSONResponse(await run_in_threadpool(names_for, svc(request), keys[:50]))

    async def proposals_context(request: Request):
        pid = await partner(request)
        signer = None
        if pid is not None:
            try:
                signer = await run_in_threadpool(member_signer, svc(request), pid)
            except Exception as exc:
                log.warning("proposals: no signer for partner %s: %s", pid, exc)
        return pid, signer

    @app.get("/proposals", response_class=HTMLResponse)
    async def proposals(request: Request):
        pid, signer = await proposals_context(request)
        if pid is None:
            return HTMLResponse(
                sign_in_card("Proposals", "Proposals from members, with support and comments.")
            )
        data = await run_in_threadpool(load_proposals, svc(request))
        msg = {"sent": "Your proposal is published."}.get(request.query_params.get("msg", ""), "")
        return HTMLResponse(proposals_page(data, signer is not None, signer is None, msg))

    @app.get("/proposals/{event}", response_class=HTMLResponse)
    async def proposal(event: str, request: Request):
        pid, signer = await proposals_context(request)
        if pid is None:
            return HTMLResponse(
                sign_in_card("Proposals", "Proposals from members, with support and comments.")
            )
        data = await run_in_threadpool(load_proposals, svc(request))
        e = next((x for x in data["roots"] if x["id"] == event), None)
        if e is None:
            data = await run_in_threadpool(load_proposals, svc(request), True)
            e = next((x for x in data["roots"] if x["id"] == event), None)
        if e is None:
            return HTMLResponse(
                page(
                    "Not found",
                    '<div class="card"><h1>No such proposal</h1><p><a href="/proposals">All proposals</a></p></div>',
                ),
                status_code=404,
            )
        return HTMLResponse(
            proposal_page(e, data, signer is not None, signer[0] if signer else None)
        )

    async def publish_as_member(
        request: Request, unsigned: dict[str, Any]
    ) -> RedirectResponse | None:
        if not same_origin(request):
            return RedirectResponse("/proposals", status_code=303)
        pid, signer = await proposals_context(request)
        if pid is None or signer is None:
            return RedirectResponse("/proposals", status_code=303)
        _, sign = signer
        services = svc(request)

        def send() -> None:
            event = sign({"created_at": int(time.time()), "content": "", **unsigned})
            services.relay.publish(event, sign)

        await run_in_threadpool(send)
        _cache["data"] = None
        return None

    @app.post("/proposals")
    async def new_proposal(request: Request, title: str = Form(...), body: str = Form("")):
        title, body = title.strip()[:MAX_TITLE], body.strip()[:MAX_BODY]
        if not title:
            return RedirectResponse("/proposals", status_code=303)
        belfry = belfry_pubkey(svc(request).settings)
        refused = await publish_as_member(
            request,
            {
                "kind": 1,
                "content": f"{title}\n\n{body}\n\n#proposal".replace("\n\n\n\n", "\n\n"),
                "tags": [
                    ["t", PROPOSAL_TAG],
                    ["p", belfry],
                    ["subject", title],
                    ["client", "belfry"],
                ],
            },
        )
        return refused or RedirectResponse("/proposals?msg=sent", status_code=303)

    async def root_author(request: Request, event: str) -> str | None:
        data = await run_in_threadpool(load_proposals, svc(request))
        e = next((x for x in data["roots"] if x["id"] == event), None)
        return e["pubkey"] if e else None

    @app.post("/proposals/{event}/support")
    async def support(event: str, request: Request):
        author = await root_author(request, event)
        if author is None:
            return RedirectResponse("/proposals", status_code=303)
        refused = await publish_as_member(
            request,
            {"kind": 7, "content": "+", "tags": [["e", event], ["p", author], ["k", "1"]]},
        )
        return refused or RedirectResponse(f"/proposals/{event}", status_code=303)

    @app.post("/proposals/{event}/comment")
    async def comment(event: str, request: Request, body: str = Form(...)):
        author = await root_author(request, event)
        body = body.strip()[:MAX_BODY]
        if author is None or not body:
            return RedirectResponse(f"/proposals/{event}", status_code=303)
        refused = await publish_as_member(
            request,
            {"kind": 1, "content": body, "tags": [["e", event, "", "root"], ["p", author]]},
        )
        return refused or RedirectResponse(f"/proposals/{event}", status_code=303)
