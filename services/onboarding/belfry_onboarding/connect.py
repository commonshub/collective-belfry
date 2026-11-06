"""``Sign in with <domain>`` for apps: ``belfry.<domain>/connect``.

The same OAuth 2.0 code + PKCE exchange apps already use with the signer (Belfry skill: "Sign in
with …"), so an app only changes the address it sends people to:

    GET  https://belfry.<domain>/connect/authorize?client_id&redirect_uri&state&code_challenge
         &code_challenge_method=S256[&prompt=none]
    POST https://belfry.<domain>/connect/token {grant_type, code, client_id, redirect_uri,
         code_verifier}  →  {"bunker_url": "bunker://…", "pubkey": …}

The person signs in with their e-mail address and a 6-digit code (Odoo's ``belfry.key.code``),
or with one tap when this browser is already signed in to ``belfry.<domain>``. The bunker URL
opens a session on their own key in the signer (Keycast): what the app signs is signed as them.

Only apps an admin approved get a session (apps.py; requests and approvals are Nostr events on
the community relay). For an app nobody approved yet, a member's sign-in sends the admins a
request, and an admin's sign-in offers to approve it.

The page never shows text from its URL: an app is shown by its address (where the code goes),
or by the name an admin gave it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import logging
import secrets
import threading
import time
import urllib.parse
from typing import Any

from coincurve import PrivateKey
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from . import apps, claim, community, console, mypass, nostr, setup, theme
from .clients.odoo import OdooError
from .core import Services

log = logging.getLogger(__name__)
E = html.escape

FLOW_SECONDS = 1800
CODE_SECONDS = 300

_lock = threading.Lock()
FLOWS: dict[str, dict[str, Any]] = {}
CODES: dict[str, dict[str, Any]] = {}


def _sweep() -> None:
    now = time.time()
    for store, ttl in ((FLOWS, FLOW_SECONDS), (CODES, CODE_SECONDS)):
        for k in [k for k, v in store.items() if v["at"] < now - ttl]:
            del store[k]


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def back_to(uri: str, **params: str) -> str:
    return uri + ("&" if "?" in uri else "?") + urllib.parse.urlencode(params)


class ConnectError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ── the steps, without HTTP ──────────────────────────────────────────────────────────────────
def start(svc: Services, q: dict[str, str]) -> dict[str, Any]:
    if q.get("code_challenge_method", "S256") != "S256" or len(q.get("code_challenge", "")) < 43:
        raise ConnectError(400, "This app's sign-in link is incomplete (PKCE S256 is required).")
    try:
        uri = apps.app_uri(q.get("redirect_uri", ""))
    except ValueError as exc:
        raise ConnectError(400, "This app's sign-in link has no valid return address.") from exc
    with _lock:
        _sweep()
        fid = secrets.token_urlsafe(24)
        FLOWS[fid] = {
            "id": fid,
            "csrf": secrets.token_urlsafe(24),
            "client_id": q.get("client_id", "")[:120],
            "uri": uri,
            "redirect": q.get("redirect_uri", "").strip(),
            "state": q.get("state", "")[:500],
            "challenge": q.get("code_challenge", "")[:128],
            "session": PrivateKey().public_key_xonly.format().hex(),  # the code is for this key
            "member": None,
            "at": time.time(),
        }
    return FLOWS[fid]


def flow_of(fid: str, csrf: str) -> dict[str, Any]:
    with _lock:
        f = FLOWS.get(fid or "")
    if not f or f["at"] < time.time() - FLOW_SECONDS:
        raise ConnectError(410, "This sign-in expired. Go back to the app and start again.")
    if not hmac.compare_digest(csrf or "", f["csrf"]):
        raise ConnectError(403, "This page expired. Go back to the app and start again.")
    return f


def issue(f: dict[str, Any], member: dict[str, Any]) -> str:
    """The authorization code for an approved app: where to send the browser."""
    code = secrets.token_urlsafe(32)
    with _lock:
        CODES[code] = {
            "uri": f["uri"],
            "redirect": f["redirect"],
            "client_id": f["client_id"],
            "challenge": f["challenge"],
            "partner": member["partner"],
            "pubkey": member["pubkey"],
            "at": time.time(),
        }
        FLOWS.pop(f["id"], None)
    log.info("connect: %s opens a session with %s", member["pubkey"][:12], f["uri"])
    return back_to(f["redirect"], code=code, state=f["state"])


def outcome(svc: Services, f: dict[str, Any], member: dict[str, Any]) -> dict[str, Any]:
    """After the person is known: in (approved app), approve (an admin), or pending (a request
    went to the admins)."""
    f["member"] = member
    if apps.is_approved(svc, f["uri"]):
        return {"next": "in", "to": issue(f, member)}
    if apps.is_admin(svc, member):
        return {"next": "approve"}
    asked = apps.request(svc, member, f["uri"], "")
    others = max(asked["count"] - 1, 0)
    return {"next": "pending", "others": others}


def send_code(svc: Services, f: dict[str, Any], email: str) -> dict[str, Any]:
    member = apps.member_by_email(svc, email)
    if member is None:
        raise ConnectError(
            404,
            f"No member of {svc.settings.nip05_domain} has this address. Check it, or ask "
            "to join first.",
        )
    try:
        row = svc.odoo.call(
            "belfry.key.code", "belfry_key_code_send", [member["email"], f["session"], False]
        )
    except OdooError as exc:
        raise ConnectError(502, "The code could not be sent. Try again in a moment.") from exc
    if row.get("state") == "too_many":
        raise ConnectError(429, "Too many codes for this address. Try again in half an hour.")
    f["pending"] = member
    return {"to": member["email"]}


def verify_code(svc: Services, f: dict[str, Any], code: str) -> dict[str, Any]:
    member = f.get("pending")
    if not member:
        raise ConnectError(409, "Type your e-mail address first.")
    try:
        row = svc.odoo.call(
            "belfry.key.code",
            "belfry_key_code_verify",
            [member["email"], f["session"], code, False],
        )
    except OdooError as exc:
        raise ConnectError(502, "The code could not be checked. Try again.") from exc
    state = row.get("state")
    if state == "wrong_code":
        left = int(row.get("tries_left") or 0)
        raise ConnectError(
            422, f"That code is not right ({left} {'try' if left == 1 else 'tries'} left)."
        )
    if state == "expired":
        raise ConnectError(410, "That code expired. Send a new one.")
    # the right code for an address whose member has their own key: Odoo answers other_key
    if state != "other_key" or row.get("npub") != nostr.hex_to_npub(member["pubkey"]):
        raise ConnectError(409, "This address does not belong to a member's account.")
    return member


def token(svc: Services, body: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        _sweep()
        c = CODES.pop(str(body.get("code", "")), None)
    if not c:
        raise ConnectError(400, "invalid_grant")
    verifier = str(body.get("code_verifier", ""))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    if (
        body.get("grant_type", "authorization_code") != "authorization_code"
        or not hmac.compare_digest(challenge, c["challenge"])
        or str(body.get("redirect_uri", "")).strip() != c["redirect"]
        or str(body.get("client_id", ""))[:120] != c["client_id"]
    ):
        raise ConnectError(400, "invalid_grant")
    if not apps.is_approved(svc, c["uri"]):
        raise ConnectError(403, "app_not_approved")
    found = community.member_token(svc, c["partner"])
    if found is None or found[0] != c["pubkey"]:
        raise ConnectError(409, "own_key")
    bunker = svc.keycast.create_bunker(found[1], apps.display_name(svc, c["uri"]))
    return {
        "bunker_url": bunker["bunker_url"],
        "token_type": "bunker",
        "pubkey": c["pubkey"],
        "npub": nostr.hex_to_npub(c["pubkey"]),
    }


# ── the page ─────────────────────────────────────────────────────────────────────────────────
STYLE = """<style>
.wiz .chip{display:flex;align-items:center;gap:12px;border:1px solid var(--border);border-radius:12px;padding:12px;margin:0 0 24px}
.wiz .chip .ic{width:40px;height:40px;border-radius:10px;background:var(--surface-deep);display:grid;place-items:center;flex:none}
.wiz .chip b{display:block;font-size:15px;font-weight:600;word-break:break-all}
.wiz .chip small{display:block;font-size:13px;color:var(--fg-muted)}
.wiz .badge{margin-left:auto;font-size:12px;font-weight:500;border-radius:999px;padding:3px 8px;white-space:nowrap;border:1px solid var(--border)}
.wiz .badge.ok{color:var(--ok);border-color:currentColor}
.wiz .badge.wait{color:#92400e;border-color:#fde68a;background:#fffbeb}
.wiz .who{display:flex;align-items:center;gap:12px;border:1px solid var(--border);border-radius:12px;padding:12px;margin:0 0 18px}
.wiz .av{width:36px;height:36px;border-radius:999px;background:var(--surface-deep);display:grid;place-items:center;font-weight:600}
.wiz .opt{display:flex;align-items:center;gap:12px;border:1px solid var(--border);border-radius:10px;padding:14px;margin:0 0 10px;color:var(--fg-muted)}
.wiz .opt small{display:block;font-size:13px}
.wiz .full{width:100%;justify-content:center}
.wiz .center{text-align:center;display:block;margin-top:20px}
</style>"""

APP_ICON = (
    '<svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="3" width="18" '
    'height="18" rx="4" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M8 9h8M8 13h5" '
    'stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>'
)

JS = """<script>
(() => {
  const W = document.getElementById('wiz'); if (!W) return;
  const fid = W.dataset.flow, csrf = W.dataset.csrf;
  const sec = (s) => W.querySelector('section[data-step="' + s + '"]');
  const err = (s, t) => { const e = sec(s).querySelector('.err'); if (e) e.textContent = t || ''; };
  function show(step) {
    W.querySelectorAll('section').forEach((x) => { x.hidden = x.dataset.step !== step; });
    const f = sec(step).querySelector('input, button.btn'); if (f) setTimeout(() => f.focus(), 40);
  }
  const post = (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin', body: JSON.stringify(Object.assign({ flow: fid, csrf }, body)) })
    .then(async (r) => { let d = {}; try { d = await r.json(); } catch (_) {}
      if (!r.ok) throw new Error(d.message || 'Something went wrong. Try again.'); return d; });
  function after(d) {
    if (d.next === 'in') { location.href = d.to; return; }
    if (d.next === 'approve') show('approve');
    else if (d.next === 'pending') {
      sec('pending').querySelector('.others').textContent = d.others ? 'you and ' + d.others + ' other member' + (d.others > 1 ? 's' : '') : 'you';
      show('pending');
    }
  }
  function step(name, run) {
    const sc = sec(name); if (!sc) return; const f = sc.querySelector('form'); if (!f) return;
    f.addEventListener('submit', async (e) => {
      e.preventDefault(); const b = f.querySelector('button[type=submit]'); if (b.disabled) return;
      err(name, ''); b.disabled = true;
      try { await run(f, b); } catch (x) { err(name, x.message); } finally { b.disabled = false; }
    });
  }
  W.querySelectorAll('[data-go]').forEach((b) => b.addEventListener('click', (e) => { e.preventDefault(); show(b.dataset.go); }));
  step('known', async () => after(await post('/connect/continue', {})));
  step('email', async (f) => {
    const d = await post('/connect/send', { email: f.elements.email.value.trim() });
    sec('code').querySelector('.to').textContent = d.to; show('code');
  });
  const boxes = [...sec('code').querySelectorAll('.otp input')], codeForm = sec('code').querySelector('form');
  const full = () => boxes.every((x) => /^[0-9]$/.test(x.value));
  function fill(t, from) { const d = (t || '').replace(/[^0-9]/g, '').slice(0, 6); if (!d) return;
    const s = d.length === 6 ? 0 : from; [...d].forEach((c, k) => { if (boxes[s + k]) boxes[s + k].value = c; });
    boxes[Math.min(s + d.length, 5)].focus(); if (full()) codeForm.requestSubmit(); }
  boxes.forEach((x, i) => {
    x.addEventListener('focus', () => x.select());
    x.addEventListener('paste', (e) => { e.preventDefault(); fill((e.clipboardData || window.clipboardData).getData('text'), i); });
    x.addEventListener('input', () => { const d = x.value.replace(/[^0-9]/g, '');
      if (d.length > 1) { x.value = ''; fill(d, i); return; } x.value = d;
      if (d && i < 5) boxes[i + 1].focus(); if (full()) codeForm.requestSubmit(); });
    x.addEventListener('keydown', (e) => {
      if (e.key === 'Backspace' && !x.value && i > 0) { e.preventDefault(); boxes[i - 1].value = ''; boxes[i - 1].focus(); }
      else if (e.key === 'ArrowLeft' && i > 0) { e.preventDefault(); boxes[i - 1].focus(); }
      else if (e.key === 'ArrowRight' && i < 5) { e.preventDefault(); boxes[i + 1].focus(); } });
  });
  step('code', async () => {
    if (!full()) throw new Error('Type the 6 digits from the e-mail.');
    try { after(await post('/connect/verify', { code: boxes.map((x) => x.value).join('') })); }
    catch (x) { boxes.forEach((y) => { y.value = ''; }); boxes[0].focus(); throw x; }
  });
  const again = sec('code').querySelector('[data-again]');
  again.addEventListener('click', async () => { again.disabled = true; err('code', '');
    try { await post('/connect/send', { email: W.querySelector('input[name=email]').value.trim() }); again.textContent = 'A new code is on its way.'; }
    catch (x) { err('code', x.message); again.disabled = false; } });
  step('approve', async () => after(await post('/connect/approve', {})));
  show(W.dataset.start);
})();
</script>"""


def page(svc: Services, f: dict[str, Any], known: dict[str, Any] | None) -> str:
    domain = svc.settings.nip05_domain
    a = apps.state(svc).get(f["uri"]) or {}
    approved = bool(a.get("approved"))
    name = apps.tag(a["decided"], "client") if approved and a.get("decided") else ""
    label = apps.app_name(name, f["uri"])
    addr = apps.address(f["uri"])
    badge = (
        '<span class="badge ok">Approved</span>'
        if approved
        else '<span class="badge wait">Not approved yet</span>'
    )
    chip = (
        f'<div class="chip"><span class="ic">{APP_ICON}</span><span style="min-width:0">'
        f"<b>{E(label)}</b>{f'<small>{E(addr)}</small>' if label != addr else ''}</span>{badge}</div>"
    )
    if approved:
        intro = (
            f'<h1>Sign in to {E(label)}</h1><p class="muted">Open a session in this app with your '
            f"{E(domain)} account. No password: we e-mail you a code.</p>"
        )
    else:
        intro = (
            '<h1>This app isn\'t approved yet</h1><p class="muted">Only the admins of '
            f"{E(domain)} can approve a new app. Once it is approved, you open a session in it "
            "with your own account. Sign in so they know who is asking, or to approve it yourself "
            "if you are an admin.</p>"
        )
    boxes = "".join(
        ('<span class="sep"></span>' if i == 3 else "")
        + f'<input type="text" inputmode="numeric" pattern="[0-9]*" aria-label="Digit {i + 1}"'
        + (' autocomplete="one-time-code"' if i == 0 else ' autocomplete="off"')
        + ">"
        for i in range(6)
    )
    known_html = ""
    if known:
        initial = (known["name"] or "?")[:1].upper()
        known_html = f"""<section data-step="known" hidden>{chip}
<h1>Continue to {E(label)}</h1>
<p class="muted">This browser is signed in to {E(domain)} as</p>
<div class="who"><span class="av">{E(initial)}</span><span><b>{E(known["name"])}</b><br>
<small class="muted">{E(known["email"])}</small></span></div>
<form><p class="err" role="alert"></p><div class="actions"><button class="btn full" type="submit">Continue as {E(known["name"].split(" ")[0] or known["name"])}</button></div></form>
<a href="#" class="quiet center" data-go="email">Use another account</a></section>"""
    back = E(back_to(f["redirect"], error="access_denied", state=f["state"]))
    return f"""<div class="card wiz" id="wiz" data-flow="{E(f["id"])}" data-csrf="{E(f["csrf"])}" data-start="{"known" if known else "email"}">
{known_html}
<section data-step="email" hidden>{chip}{intro}
<form novalidate><label for="c-mail" style="font-weight:500;font-size:14px;display:block;margin-bottom:8px">E-mail</label>
<input id="c-mail" type="email" name="email" autocomplete="email" placeholder="you@example.org">
<p class="err" role="alert"></p><div class="actions"><button class="btn full" type="submit">Continue</button></div></form>
<a href="#" class="quiet center" data-go="other">Other ways to sign in</a></section>

<section data-step="code" hidden>
<h1>Check your inbox</h1>
<p class="muted">Type the 6-digit code we sent to <strong class="to"></strong>.</p>
<form novalidate><div class="otp">{boxes}</div><p class="err" role="alert"></p>
<div class="actions"><button class="btn" type="submit">Verify</button>
<button type="button" class="quiet" data-again>Send a new code</button></div></form>
<a href="#" class="quiet center" data-go="email">Use another e-mail</a></section>

<section data-step="pending" hidden>
<h1>Waiting for an admin</h1>
<p class="muted">We asked the admins of {E(domain)} to approve {E(label)}. Asked by <span class="others">you</span>.
Once it is approved, sign in again from the app.</p>
<p class="muted" style="font-size:13px">The request is a signed event on the community relay: admins see it in
belfry.{E(domain)} › Apps.</p>
<div class="actions"><a class="btn full" href="{back}" style="text-align:center;text-decoration:none">Back to the app</a></div></section>

<section data-step="approve" hidden>{chip}
<h1>Approve this app?</h1>
<p class="muted">You are an admin of {E(domain)}. Once approved, any member can open a session in
{E(addr)} with their own account. What the app signs in that session is signed as them.</p>
<form><p class="err" role="alert"></p>
<div class="actions" style="flex-direction:column;align-items:stretch;gap:10px">
<button class="btn full" type="submit">Approve for everyone</button>
<a class="quiet center" href="{back}" style="margin-top:4px">Not now</a></div></form>
<p class="muted" style="font-size:13px">Your approval is published as a signed event. You can withdraw it in belfry.{E(domain)} › Apps.</p></section>

<section data-step="other" hidden>
<a href="#" class="quiet" data-go="email">‹ Back to e-mail</a>
<h1 style="margin-top:16px">Other ways to sign in</h1>
<p class="muted">Not on this sign-in page yet. For now, use your e-mail address.</p>
<div class="opt"><span>Membership pass<small>From your EU digital wallet</small></span></div>
<div class="opt"><span>Nostr signer<small>A browser extension or a bunker:// link</small></span></div>
</section>
</div>{JS}"""


def error_page(message: str) -> str:
    return f'<div class="card wiz"><section><h1>Can\'t sign in</h1><p class="muted">{E(message)}</p></section></div>'


def register(app: FastAPI) -> None:
    def svc(request: Request) -> Services:
        return request.app.state.services

    def html_(request: Request, body: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(
            theme.frame("Sign in", setup.WIZARD_STYLE + STYLE + body, claim.TEST_NOTICE, bare=True),
            status_code=status,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    def known_member(request: Request) -> dict[str, Any] | None:
        services = svc(request)
        sess = console.read_session(services, request.cookies.get(console.COOKIE))
        if not sess:
            return None
        return apps.member_by_partner(services, int(sess["partner"]))

    def same_origin(request: Request) -> bool:
        origin = request.headers.get("origin")
        return not origin or origin == mypass._origin(console.console_url(svc(request).settings))

    def sign_in(response: Response, request: Request, member: dict[str, Any]) -> None:
        """This browser is now signed in to belfry.<domain> (the console's session)."""
        services = svc(request)
        days = services.settings.pass_page_session_days
        response.set_cookie(
            console.COOKIE,
            mypass._sign(
                services,
                {
                    "kind": "console",
                    "partner": member["partner"],
                    "csrf": secrets.token_urlsafe(24),
                },
                days * 86400,
            ),
            max_age=days * 86400,
            httponly=True,
            secure=console.console_url(services.settings).startswith("https://"),
            samesite="lax",
            path="/",
        )

    def fail(exc: ConnectError) -> JSONResponse:
        return JSONResponse({"message": exc.message}, status_code=exc.status)

    @app.get("/console/connect/authorize", response_class=HTMLResponse)
    async def authorize(request: Request):
        services = svc(request)
        q = {k: v for k, v in request.query_params.items()}
        try:
            f = await run_in_threadpool(start, services, q)
        except ConnectError as exc:
            return html_(request, error_page(exc.message), exc.status)
        known = await run_in_threadpool(known_member, request)
        if q.get("prompt") == "none":
            # silent sign-in: only for a known browser and an approved app
            if known and await run_in_threadpool(apps.is_approved, services, f["uri"]):
                return RedirectResponse(issue(f, known), status_code=302)
            return RedirectResponse(
                back_to(f["redirect"], error="login_required", state=f["state"]), status_code=302
            )
        body = await run_in_threadpool(page, services, f, known)
        return html_(request, body)

    async def body_of(request: Request) -> tuple[dict[str, Any], dict[str, Any]]:
        if not same_origin(request):
            raise ConnectError(403, "Wrong origin.")
        try:
            body = await request.json()
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        return flow_of(str(body.get("flow", "")), str(body.get("csrf", ""))), body

    @app.post("/console/connect/send")
    async def connect_send(request: Request):
        try:
            f, body = await body_of(request)
            return JSONResponse(
                await run_in_threadpool(
                    send_code, svc(request), f, str(body.get("email", ""))[:254]
                )
            )
        except ConnectError as exc:
            return fail(exc)

    @app.post("/console/connect/verify")
    async def connect_verify(request: Request):
        services = svc(request)
        try:
            f, body = await body_of(request)
            code = "".join(c for c in str(body.get("code", "")) if c.isdigit())[:6]
            member = await run_in_threadpool(verify_code, services, f, code)
            result = await run_in_threadpool(outcome, services, f, member)
        except ConnectError as exc:
            return fail(exc)
        except ValueError as exc:
            return JSONResponse({"message": str(exc)}, status_code=409)
        response = JSONResponse(result)
        sign_in(response, request, member)
        return response

    @app.post("/console/connect/continue")
    async def connect_continue(request: Request):
        services = svc(request)
        try:
            f, _ = await body_of(request)
            member = await run_in_threadpool(known_member, request)
            if member is None:
                raise ConnectError(401, "Sign in with your e-mail address.")
            return JSONResponse(await run_in_threadpool(outcome, services, f, member))
        except ConnectError as exc:
            return fail(exc)
        except ValueError as exc:
            return JSONResponse({"message": str(exc)}, status_code=409)

    @app.post("/console/connect/approve")
    async def connect_approve(request: Request):
        services = svc(request)
        try:
            f, _ = await body_of(request)
            member = f.get("member")
            if not member:
                raise ConnectError(401, "Sign in first.")
            await run_in_threadpool(apps.decide, services, member, f["uri"], "", True)
            return JSONResponse({"next": "in", "to": issue(f, member)})
        except ConnectError as exc:
            return fail(exc)
        except PermissionError as exc:
            return JSONResponse({"message": str(exc)}, status_code=403)
        except ValueError as exc:
            return JSONResponse({"message": str(exc)}, status_code=409)

    def cors(request: Request, response: Response) -> Response:
        origin = request.headers.get("origin")
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        return response

    @app.options("/console/connect/token")
    @app.options("/console/connect/revoke")
    async def connect_preflight(request: Request):
        return cors(request, Response(status_code=204))

    @app.post("/console/connect/token")
    async def connect_token(request: Request):
        try:
            if "json" in request.headers.get("content-type", ""):
                body = await request.json()
            else:
                body = dict(await request.form())
            body = body if isinstance(body, dict) else {}
            result = await run_in_threadpool(token, svc(request), body)
            return cors(request, JSONResponse(result, headers={"Cache-Control": "no-store"}))
        except ConnectError as exc:
            return cors(request, JSONResponse({"error": exc.message}, status_code=exc.status))
        except ValueError:
            return cors(request, JSONResponse({"error": "invalid_request"}, status_code=400))

    @app.post("/console/connect/revoke")
    async def connect_revoke(request: Request):
        # The app forgets its bunker URL; the connection itself is removed on the account page.
        return cors(request, JSONResponse({"state": "ok"}))

    @app.get("/console/connect/logout")
    async def connect_logout(request: Request, return_to: str = ""):
        services = svc(request)
        to = "/"
        try:
            uri = apps.app_uri(return_to)
            if await run_in_threadpool(apps.is_approved, services, uri):
                to = return_to
        except ValueError:
            pass
        response = RedirectResponse(to, status_code=303)
        response.delete_cookie(console.COOKIE, path="/")
        return response
