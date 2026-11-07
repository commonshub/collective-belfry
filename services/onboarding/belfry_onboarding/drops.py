"""Drop box links: anyone with the link uploads photos and files to one channel.

**The link.** A channel member makes one in the chat (the Files tab): a NIP-78 event, kind 30078,
in the group (``h``), ``d`` = ``drop:<token>``, a NIP-40 ``expiration`` (a week by default) and
``["login"]`` when uploaders must be signed in. Deleting it (kind 5 by its author) ends the link.
The link is ``https://<domain>/drop/<token>`` (the apex sends it to pass.<domain>, where the
member session lives); the token is the only credential, so the event stays in the group.

**An upload.** The files go to the community's Blossom server (as the belfry's key) and arrive in
the channel as one kind-9 message with NIP-92 ``imeta`` tags: signed by the uploader when they are
signed in, else by the groups API key (the relay's owner, shown as "Drop box"). After the upload
an anonymous uploader may add a name (the message is posted again with "From <name>" and the first
one deleted, kind 9005), then sign in to link it to themselves (posted again, signed by their own
key through the signer). The page proves which upload is theirs with an HMAC of its event id.

Pages show fixed text and what the relay or the person's own form says, never text from the URL.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import logging
import mimetypes
import re
import secrets
import time
import urllib.parse
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import segno
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from . import community, followers
from .claim import page
from .connect import ConnectError
from .core import Services
from .homepage import belfry_key, blossom_auth

log = logging.getLogger(__name__)
E = html.escape

KIND = 30078
TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
EVENT_ID = re.compile(r"^[0-9a-f]{64}$")
MAX_FILES = 10
MAX_BYTES = 25 * 1024 * 1024  # Blossom's limit per file
DROP_NAME = "Drop box"


# ── the link ────────────────────────────────────────────────────────────────────────────────
_seen: dict[str, tuple[float, dict[str, Any] | None]] = {}


def find(svc: Services, token: str, fresh: bool = False) -> dict[str, Any] | None:
    """The live drop link of TOKEN: channel, expiry, whether sign-in is required; else None."""
    if not TOKEN.match(token or ""):
        return None
    held = _seen.get(token)
    if held and not fresh and time.monotonic() - held[0] < 30:
        return held[1]
    got = followers._relay(svc)
    if got is None:
        return None
    relay, sign = got
    found = None
    events = relay.query([{"kinds": [KIND], "#d": [f"drop:{token}"]}], sign)
    for e in sorted(events, key=lambda e: e["created_at"], reverse=True):
        tags = e.get("tags", [])
        channel = next((t[1] for t in tags if t[0] == "h" and len(t) > 1), "")
        exp = next((t[1] for t in tags if t[0] == "expiration" and len(t) > 1), "")
        if not channel or not exp.isdigit() or int(exp) <= time.time():
            break
        coord = f"{KIND}:{e['pubkey']}:drop:{token}"
        gone = relay.query([{"kinds": [5], "authors": [e["pubkey"]], "#e": [e["id"]]}], sign)
        gone += relay.query([{"kinds": [5], "authors": [e["pubkey"]], "#a": [coord]}], sign)
        if not gone:
            found = {
                "token": token,
                "channel": channel,
                "expires": int(exp),
                "login": any(t[0] == "login" for t in tags),
                "author": e["pubkey"],
            }
        break
    _seen[token] = (time.monotonic(), found)
    return found


def proof(svc: Services, event_id: str) -> str:
    key = (svc.settings.pass_page_secret or "drop").encode()
    return hmac.new(key, f"drop:{event_id}".encode(), hashlib.sha256).hexdigest()[:32]


def owns(svc: Services, event_id: str, k: str) -> bool:
    return bool(EVENT_ID.match(event_id or "")) and hmac.compare_digest(
        proof(svc, event_id), k or ""
    )


# ── uploads and messages ────────────────────────────────────────────────────────────────────
def upload(svc: Services, files: list[tuple[str, str, bytes]]) -> list[list[str]]:
    """Put each (name, type, body) on Blossom; their NIP-92 imeta tags."""
    key = belfry_key(svc.settings)
    if key is None:
        raise RuntimeError("no belfry key (secret belfry_nsec)")
    base = (getattr(svc.settings, "homepage_upload_url", "") or "http://blossom-gate").rstrip("/")
    public = (getattr(svc.settings, "blossom_public_url", "") or "").rstrip("/")
    tags = []
    with httpx.Client(timeout=120.0) as http:
        for name, ctype, body in files:
            sha = hashlib.sha256(body).hexdigest()
            if http.head(f"{base}/{sha}").status_code != 200:
                r = http.put(
                    f"{base}/upload",
                    content=body,
                    headers={
                        "Authorization": blossom_auth(key.secret, "upload", sha),
                        "Content-Type": ctype,
                        "X-SHA-256": sha,
                    },
                )
                if r.status_code >= 300:
                    raise RuntimeError(f"Blossom refused {name}: {r.status_code}")
            ext = mimetypes.guess_extension(ctype) or ""
            tag = [
                "imeta",
                f"url {public}/{sha}{ext}",
                f"m {ctype}",
                f"x {sha}",
                f"size {len(body)}",
            ]
            if name:
                tag.append(f"alt {name[:120]}")
            tags.append(tag)
    return tags


def _media(event: dict[str, Any]) -> list[list[str]]:
    return [t for t in event.get("tags", []) if t and t[0] == "imeta"]


def _urls(imeta: list[list[str]]) -> str:
    return "\n".join(p[4:] for t in imeta for p in t[1:] if p.startswith("url "))


def post(
    svc: Services,
    channel: str,
    text: str,
    imeta: list[list[str]],
    partner: int | None,
    client: str = "belfry-drop",
) -> dict[str, Any]:
    """The upload as a message in CHANNEL: signed by the member, else by the groups API key."""
    got = followers._relay(svc)
    if got is None:
        raise RuntimeError("no groups relay")
    relay, api_sign = got
    template = {
        "kind": 9,
        "created_at": int(time.time()),
        "content": (text + "\n" + _urls(imeta)).strip(),
        "tags": [["h", channel], *imeta, ["client", client]],
    }
    if partner is not None:
        found = community.member_signer(svc, partner)
        if found is not None:
            _, sign = found
            event = sign(template)
            relay.publish(event, sign)
            return event
    event = api_sign(template)
    relay.publish(event, api_sign)
    return event


def replace(
    svc: Services, channel: str, old_id: str, text: str, partner: int | None
) -> dict[str, Any] | None:
    """Post the upload OLD_ID again (with a name, or as the member) and delete the first one."""
    got = followers._relay(svc)
    if got is None:
        return None
    relay, api_sign = got
    old = relay.query([{"ids": [old_id], "kinds": [9], "#h": [channel]}], api_sign)
    if not old:
        return None
    new = post(svc, channel, text, _media(old[0]), partner)
    relay.publish(
        api_sign(
            {
                "kind": 9005,
                "created_at": int(time.time()),
                "content": "",
                "tags": [["h", channel], ["e", old_id]],
            }
        ),
        api_sign,
    )
    return new


def ensure_profile(svc: Services) -> None:
    """The groups API key's profile on the groups relay: anonymous drops show as "Drop box"."""
    got = followers._relay(svc)
    if got is None or svc.groups is None:
        return
    relay, sign = got
    own = svc.groups.key.public_key_xonly.format().hex()
    if relay.query([{"kinds": [0], "authors": [own]}], sign):
        return
    import json

    content = json.dumps(
        {"name": DROP_NAME, "about": f"Uploads through drop box links on {followers.domain(svc)}"}
    )
    relay.publish(
        sign({"kind": 0, "created_at": int(time.time()), "tags": [], "content": content}), sign
    )


# ── pages ───────────────────────────────────────────────────────────────────────────────────
STYLE = """<style>
.drop h1 a{color:inherit;text-decoration:underline;text-decoration-thickness:2px;text-underline-offset:4px}
.drop input[type=text],.drop input[type=email]{width:100%;font:inherit;padding:10px 12px;border:1px solid var(--border);border-radius:8px;background:var(--surface);color:var(--fg);box-sizing:border-box}
.drop .zone{position:relative;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;box-sizing:border-box;width:100%;max-width:100%;min-height:120px;margin:16px 0;padding:18px;border:2px dashed var(--border-strong,var(--border));border-radius:12px;background:var(--surface-deep,transparent);text-align:center;cursor:pointer;overflow:hidden}
.drop .zone input{position:absolute;inset:0;width:100%;height:100%;opacity:0;cursor:pointer}
.drop .zone b{font-weight:600}
.drop .zone small{color:var(--fg-muted);font-size:.85rem;overflow-wrap:anywhere}
.drop .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.drop .row input{flex:1 1 160px;min-width:0}
.drop button{font:inherit;cursor:pointer}
.drop .full{display:block;width:100%;box-sizing:border-box;text-align:center}
.drop .done{font-size:2.2rem;line-height:1;margin:0 0 6px}
.drop .step{border-top:1px solid var(--border);margin-top:20px;padding-top:16px}
.drop .or{display:flex;align-items:center;gap:12px;color:var(--fg-muted);font-size:.85rem;margin:18px 0}
.drop .or:before,.drop .or:after{content:"";flex:1;border-top:1px solid var(--border)}
.drop .ghost{background:transparent;color:var(--fg);border:1px solid var(--border);padding:9px 16px;border-radius:8px;text-decoration:none;display:inline-block;box-sizing:border-box}
.drop label.l{display:block;font-weight:500;font-size:14px;margin:0 0 8px}
</style>
<script>
document.addEventListener('change', (e) => {
  const i = e.target; if (!i.matches || !i.matches('.zone input')) return;
  const n = i.files ? i.files.length : 0, s = i.closest('.zone').querySelector('b');
  s.textContent = n === 0 ? 'Choose photos or files' : n === 1 ? i.files[0].name : n + ' files chosen';
});
</script>"""


def _when(svc: Services, ts: int) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo(svc.settings.digest_tz)).strftime("%-d %B %Y, %H:%M")


def _shell(body: str) -> HTMLResponse:
    return HTMLResponse(
        page("Drop box", STYLE + f'<div class="card drop">{body}</div>'),
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def channel(svc: Services, ch: str) -> str:
    """The channel's name, linked to it in the chat."""
    label = E(followers.channel_label(svc, ch))
    href = followers.chat_link(svc, ch)
    return f'<a href="{E(href)}">{label}</a>' if href else label


def gone_page() -> HTMLResponse:
    return _shell(
        """<h1>This drop link does not work</h1>
<p class="muted">It has expired or was ended. Ask whoever shared it for a new one.</p>"""
    )


def member_name(svc: Services, partner: int) -> str:
    row = (svc.odoo.read("res.partner", [partner], ["name"]) or [{}])[0]
    return row.get("name") or "you"


def signin_form(svc: Services, t: str, button: str, u: str = "", k: str = "") -> str:
    """The e-mail address, right here; the code comes next on the sign-in page."""
    from . import passlogin

    q = urllib.parse.urlencode({"u": u, "k": k}) if u else ""
    if not passlogin.own(svc):
        return f'<p><a class="btn full" href="/drop/{t}/signin{"?" + E(q) if q else ""}">{E(button)}</a></p>'
    hidden = (
        f'<input type="hidden" name="u" value="{E(u)}"><input type="hidden" name="k" value="{E(k)}">'
        if u
        else ""
    )
    return f"""<form method="post" action="/drop/{t}/signin">{hidden}
<label class="l" for="d-mail">E-mail</label>
<input id="d-mail" type="email" name="email" required autocomplete="email" placeholder="you@example.org">
<p style="margin:12px 0 0"><button class="btn full">{E(button)}</button></p>
<p class="muted" style="font-size:.85rem">No password: we e-mail you a code.</p></form>"""


def form_page(
    svc: Services,
    d: dict[str, Any],
    partner: int | None,
    error: str = "",
    guest: bool = False,
) -> HTMLResponse:
    ch = channel(svc, d["channel"])
    where = E(followers.domain(svc))
    t = E(d["token"])
    err = f'<p class="bad" role="alert">{E(error)}</p>' if error else ""
    until = f'<p class="muted" style="font-size:.85rem">This link works until {E(_when(svc, d["expires"]))}.</p>'
    if partner is None and (d["login"] or not guest):
        if d["login"]:
            body = f"""<h1>Send files to {ch}</h1>
<p>On {where}. This drop box takes files from signed-in members only.</p>{err}
{signin_form(svc, t, "Sign in to upload")}{until}"""
        else:
            body = f"""<h1>Send files to {ch}</h1>
<p>Photos, videos or files go straight to {ch} on {where}. Sign in to post them as yourself.</p>{err}
{signin_form(svc, t, "Sign in")}
<div class="or">or</div>
<a class="ghost full" href="/drop/{t}?as=guest">Upload as a guest</a>
<p class="muted" style="font-size:.85rem">No account needed. You can add your name afterwards.</p>{until}"""
        return _shell(body)
    who = (
        f'<p class="muted">Signed in as {E(member_name(svc, partner))}: your files are posted as you.</p>'
        if partner is not None
        else '<p class="muted">As a guest. You can add your name afterwards.</p>'
    )
    body = f"""<h1>Send files to {ch}</h1>
<p>Photos, videos or files go straight to {ch} on {where}.</p>{err}
<form method="post" action="/drop/{t}" enctype="multipart/form-data">
<label class="zone"><input type="file" name="files" multiple required aria-label="Photos or files">
<b>Choose photos or files</b><small>Up to {MAX_FILES} files of 25 MB each</small></label>
<button class="btn full">Upload</button>
</form>{who}{until}"""
    return _shell(body)


def done_page(
    svc: Services,
    d: dict[str, Any],
    event: dict[str, Any],
    count: int,
    partner: int | None,
    name: str = "",
) -> HTMLResponse:
    ch = channel(svc, d["channel"])
    t, eid, k = E(d["token"]), E(event["id"]), E(proof(svc, event["id"]))
    what = f"{count} file{'s' if count != 1 else ''}"
    head = f"""<p class="done">✓</p><h1>Done</h1><p>{what} sent to {ch}.</p>"""
    if partner is not None:
        body = head + f'<p class="muted">Posted as {E(member_name(svc, partner))}.</p>'
    else:
        step_name = (
            f"<p>Thanks, {E(name)}: your name is on it.</p>"
            if name
            else f"""<p><strong>Want credit?</strong> Add your name (optional).</p>
<form method="post" action="/drop/{t}/name" class="row"><input type="hidden" name="u" value="{eid}"><input type="hidden" name="k" value="{k}">
<input type="text" name="name" maxlength="80" placeholder="Your name" aria-label="Your name" autocomplete="name"><button class="ghost">Add my name</button></form>"""
        )
        body = (
            head
            + f'<div class="step">{step_name}</div>'
            + '<div class="step"><p><strong>A member here?</strong> Sign in to link this upload to your identity.</p>'
            + signin_form(svc, t, "Sign in and link it to me", event["id"], proof(svc, event["id"]))
            + "</div>"
        )
    href = followers.chat_link(svc, d["channel"])
    label = E(followers.channel_label(svc, d["channel"]))
    end = f'<a class="btn full" href="{E(href)}">Open {label}</a>' if href else ""
    again = f"/drop/{t}" + ("" if partner is not None else "?as=guest")
    body += f'<div class="step">{end}<p style="text-align:center"><a href="{again}">Send more</a></p></div>'
    return _shell(body)


def register(app: FastAPI) -> None:
    def svc(request: Request) -> Services:
        return request.app.state.services

    def me(request: Request) -> int | None:
        from .mypass import SESSION_COOKIE

        return community.member_partner(svc(request), request.cookies.get(SESSION_COOKIE))

    def same_origin(request: Request) -> bool:
        s = svc(request).settings
        origin = request.headers.get("origin")
        allowed = {u.rstrip("/") for u in (s.public_url, s.site_url) if u}
        return not origin or origin.rstrip("/") in allowed

    @app.get("/drop/{token}", response_class=HTMLResponse)
    async def drop_page(token: str, request: Request, as_: str = Query("", alias="as")):
        d = await run_in_threadpool(find, svc(request), token)
        if d is None:
            return gone_page()
        return await run_in_threadpool(
            form_page,
            svc(request),
            d,
            await run_in_threadpool(me, request),
            "",
            as_ == "guest",
        )

    @app.get("/drop/{token}/qr.svg")
    async def drop_qr(token: str, request: Request):
        s = svc(request)
        d = await run_in_threadpool(find, s, token)
        if d is None:
            return Response(status_code=404)
        url = f"{followers.base_url(s)}/drop/{d['token']}"
        svg = segno.make(url, error="m").svg_inline(
            scale=8, border=2, dark="#09090b", light="#ffffff"
        )
        return Response(
            svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=3600"}
        )

    @app.post("/drop/{token}")
    async def drop_upload(token: str, request: Request):
        s = svc(request)
        d = await run_in_threadpool(find, s, token, True)
        if d is None:
            return gone_page()
        partner = await run_in_threadpool(me, request)
        if (d["login"] and partner is None) or not same_origin(request):
            return RedirectResponse(f"/drop/{d['token']}", status_code=303)
        form = await request.form(max_files=MAX_FILES + 1)
        items = [f for f in form.getlist("files") if isinstance(f, UploadFile) and f.filename]
        if not items:
            return await run_in_threadpool(
                form_page, s, d, partner, "Choose at least one file.", True
            )
        if len(items) > MAX_FILES:
            return await run_in_threadpool(
                form_page, s, d, partner, f"At most {MAX_FILES} files at once.", True
            )
        files = []
        for f in items:
            body = await f.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                return await run_in_threadpool(
                    form_page, s, d, partner, f"{f.filename} is over 25 MB.", True
                )
            ctype = (
                f.content_type
                or mimetypes.guess_type(f.filename or "")[0]
                or "application/octet-stream"
            )
            files.append(((f.filename or "")[:120], ctype, body))
        try:
            imeta = await run_in_threadpool(upload, s, files)
            text = "" if partner is not None else "📥 Dropped anonymously"
            event = await run_in_threadpool(post, s, d["channel"], text, imeta, partner)
        except Exception as exc:  # noqa: BLE001 — the person sees a retry, the log the reason
            log.warning("drop: upload to %s failed: %s", d["channel"], exc)
            return await run_in_threadpool(
                form_page, s, d, partner, "The upload did not go through. Try again.", True
            )
        log.info(
            "drop: %d files to %s (%s)", len(files), d["channel"], "member" if partner else "guest"
        )
        return await run_in_threadpool(done_page, s, d, event, len(files), partner)

    @app.post("/drop/{token}/name")
    async def drop_name(token: str, request: Request):
        s = svc(request)
        d = await run_in_threadpool(find, s, token)
        form = await request.form()
        u, k, name = (
            str(form.get("u") or ""),
            str(form.get("k") or ""),
            " ".join(str(form.get("name") or "").split())[:80],
        )
        if d is None or not owns(s, u, k) or not same_origin(request):
            return (
                gone_page()
                if d is None
                else RedirectResponse(f"/drop/{d['token']}", status_code=303)
            )
        if not name:
            return await run_in_threadpool(done_page, s, d, {"id": u}, 1, None)
        new = await run_in_threadpool(replace, s, d["channel"], u, f"📥 From {name}", None)
        if new is None:
            return gone_page()
        return await run_in_threadpool(done_page, s, d, new, len(_media(new)), None, name)

    @app.get("/drop/{token}/signin")
    async def drop_signin(token: str, request: Request, u: str = "", k: str = ""):
        """Sign in on the pass page, then come back: to the drop box, or to link an upload."""
        s = svc(request)
        d = await run_in_threadpool(find, s, token)
        if d is None:
            return gone_page()
        after = f"/drop/{d['token']}"
        if owns(s, u, k):
            after += "/link?" + urllib.parse.urlencode({"u": u, "k": k})
        response = RedirectResponse("/me/login", status_code=303)
        response.set_cookie(
            "belfry_pass_next", after, max_age=900, httponly=True, samesite="lax", path="/me"
        )
        return response

    @app.post("/drop/{token}/signin")
    async def drop_signin_email(token: str, request: Request):
        """The address typed on the drop page: the code is sent, the sign-in page asks for it."""
        from . import passlogin

        s = svc(request)
        d = await run_in_threadpool(find, s, token)
        if d is None:
            return gone_page()
        form = await request.form()
        u, k = str(form.get("u") or ""), str(form.get("k") or "")
        if not passlogin.own(s) or not same_origin(request):
            return RedirectResponse(f"/drop/{d['token']}/signin", status_code=303)
        try:
            f = await run_in_threadpool(
                passlogin.start_with_email, s, str(form.get("email") or "")[:254]
            )
        except ConnectError as exc:
            if owns(s, u, k):
                return await run_in_threadpool(done_page, s, d, {"id": u}, 1, None)
            return await run_in_threadpool(form_page, s, d, None, exc.message)
        after = f"/drop/{d['token']}"
        if owns(s, u, k):
            after += "/link?" + urllib.parse.urlencode({"u": u, "k": k})
        response = RedirectResponse("/me/login", status_code=303)
        passlogin.set_flow(response, s, f)
        response.set_cookie(
            "belfry_pass_next", after, max_age=900, httponly=True, samesite="lax", path="/me"
        )
        return response

    @app.get("/drop/{token}/link", response_class=HTMLResponse)
    async def drop_link_page(token: str, request: Request, u: str = "", k: str = ""):
        s = svc(request)
        d = await run_in_threadpool(find, s, token)
        partner = await run_in_threadpool(me, request)
        if d is None or not owns(s, u, k):
            return gone_page()
        if partner is None:
            return RedirectResponse(
                f"/drop/{d['token']}/signin?" + urllib.parse.urlencode({"u": u, "k": k}),
                status_code=303,
            )
        who = E(await run_in_threadpool(member_name, s, partner))
        return _shell(
            f"""<h1>Link your upload</h1>
<p>Post your upload to {channel(s, d["channel"])} as {who}?</p>
<form method="post" action="/drop/{E(d["token"])}/link"><input type="hidden" name="u" value="{E(u)}"><input type="hidden" name="k" value="{E(k)}">
<button class="btn">Yes, post it as me</button></form>"""
        )

    @app.post("/drop/{token}/link")
    async def drop_link(token: str, request: Request):
        s = svc(request)
        d = await run_in_threadpool(find, s, token)
        form = await request.form()
        u, k = str(form.get("u") or ""), str(form.get("k") or "")
        partner = await run_in_threadpool(me, request)
        if d is None or not owns(s, u, k) or partner is None or not same_origin(request):
            return gone_page()
        new = await run_in_threadpool(replace, s, d["channel"], u, "", partner)
        if new is None:
            return gone_page()
        return await run_in_threadpool(done_page, s, d, new, len(_media(new)), partner)


def new_token() -> str:
    return secrets.token_urlsafe(16)


__all__ = ["register", "find", "ensure_profile", "proof", "new_token"]
