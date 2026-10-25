"""The pass on the phone without a wallet (decided 2026-10-24): a signed-in participant opens
/me and shows a QR code at the door; door staff scan it with any phone camera and /check/<token>
says whether the pass is valid. Like the wallet door check it tells only the tier and the
validity, never a name.

Sign-in: Authentik application belfry-pass (blueprint belfry-pass-app, group
belfry-participants, implicit consent), authorization code + PKCE, code and userinfo server to
server as on the steward pages. The partner comes from the "keycast" scope's claim
keycast_vine_id (odoo-partner-<id>). A participant already signed in to Authentik passes without
a click. The session is a signed cookie on /me (PASS_PAGE_SECRET).

The QR is a URL with a short-lived token, <pass id>.<expiry>.<HMAC>, renewed every
QR_REFRESH_SECONDS by the page and valid for QR_TTL_SECONDS, so a screenshot is useless soon
after. /check reads the pass from Odoo at scan time (a revoke counts at once) and remembers
scanned tokens until they expire, so a second scan of the same code says when it was first
scanned. The memory is per process; the onboarding service runs as one.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import html
import logging
import secrets
import threading
import time
import urllib.parse
from typing import Any

import httpx
import jwt
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .claim import TIER_NAMES, page, qr_svg
from .clients.odoo import OdooError
from .core import Services, odoo_date

log = logging.getLogger(__name__)

SESSION_COOKIE = "belfry_pass"
FLOW_COOKIE = "belfry_pass_flow"
FLOW_SECONDS = 600
QR_TTL_SECONDS = 90
QR_REFRESH_SECONDS = 30
PASS_STATE_ORDER = {"active": 0, "pending": 1, "expired": 2, "revoked": 3}
E = html.escape


class PassPageError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def enabled(svc: Services) -> bool:
    s = svc.settings
    return bool(s.pass_page_oidc_client_secret and s.pass_page_secret)


def redirect_uri(svc: Services) -> str:
    return svc.settings.public_url.rstrip("/") + "/me/callback"


def _secure(svc: Services) -> bool:
    return svc.settings.public_url.startswith("https://")


def _sign(svc: Services, claims: dict[str, Any], seconds: int) -> str:
    now = int(time.time())
    return jwt.encode(
        {**claims, "iat": now, "exp": now + seconds}, svc.settings.pass_page_secret, "HS256"
    )


def _verify(svc: Services, token: str | None, kind: str) -> dict[str, Any] | None:
    if not token:
        return None
    try:
        claims = jwt.decode(token, svc.settings.pass_page_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    return claims if claims.get("kind") == kind else None


# -- the QR token ----------------------------------------------------------------------------------


def _mac(svc: Services, payload: str) -> str:
    key = hashlib.sha256(b"belfry-pass-qr\0" + svc.settings.pass_page_secret.encode()).digest()
    digest = hmac.new(key, payload.encode(), hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def qr_token(svc: Services, pass_id: int, now: int | None = None) -> tuple[str, int]:
    """(token, expiry): <pass id>.<expiry>.<HMAC-SHA256, 128 bits>."""
    exp = int(now if now is not None else time.time()) + QR_TTL_SECONDS
    payload = f"{pass_id}.{exp}"
    return f"{payload}.{_mac(svc, payload)}", exp


def read_qr_token(svc: Services, token: str, now: int | None = None) -> int:
    """The pass id of a genuine, unexpired token; PassPageError otherwise."""
    parts = token.split(".")
    if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
        raise PassPageError(400, "This is not an Open Source Village pass code.")
    payload = f"{parts[0]}.{parts[1]}"
    if not hmac.compare_digest(parts[2], _mac(svc, payload)):
        raise PassPageError(400, "This is not an Open Source Village pass code.")
    if int(parts[1]) < int(now if now is not None else time.time()):
        raise PassPageError(410, "This code has expired. Ask for the pass page to be shown live.")
    return int(parts[0])


class ScanLog:
    """Tokens scanned before, until they expire: token -> first scan time."""

    def __init__(self) -> None:
        self._seen: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def first_scan(self, token: str, exp: int, now: float | None = None) -> float | None:
        """Record a scan; the time of an earlier scan of the same token, or None."""
        now = time.time() if now is None else now
        with self._lock:
            self._seen = {t: v for t, v in self._seen.items() if v[1] >= now}
            earlier = self._seen.get(token)
            if earlier is None:
                self._seen[token] = (now, exp)
                return None
            return earlier[0]


# -- the pass --------------------------------------------------------------------------------------


def partner_pass(svc: Services, partner_id: int) -> dict[str, Any] | None:
    """The partner's pass to show: the active one if any, else the newest."""
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["partner_id", "=", partner_id]],
        ["state", "tier", "date_start", "date_end"],
        order="id desc",
    )
    return min(rows, key=lambda r: PASS_STATE_ORDER.get(r.get("state"), 9)) if rows else None


def check_pass(svc: Services, pass_id: int, today: dt.date | None = None) -> dict[str, Any]:
    """The door's answer for a pass id, from Odoo now: valid or not, tier and dates only."""
    rows = svc.odoo.read("belfry.pass", [pass_id], ["state", "tier", "date_start", "date_end"])
    if not rows:
        return {"valid": False, "reason": "No such pass."}
    row = rows[0]
    today = today or dt.datetime.now(dt.UTC).date()
    start, end = odoo_date(row.get("date_start")), odoo_date(row.get("date_end"))
    out = {
        "tier": row.get("tier"),
        "tier_name": TIER_NAMES.get(row.get("tier"), row.get("tier")),
        "date_start": start.isoformat() if start else None,
        "date_end": end.isoformat() if end else None,
    }
    if row.get("state") != "active":
        return {**out, "valid": False, "reason": f"The pass is {row.get('state')}."}
    if start and today < start:
        return {**out, "valid": False, "reason": f"The pass starts on {start:%d %B %Y}."}
    if end and today > end:
        return {**out, "valid": False, "reason": f"The pass ended on {end:%d %B %Y}."}
    return {**out, "valid": True}


# -- sign-in ---------------------------------------------------------------------------------------


def start_login(svc: Services) -> tuple[str, str]:
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    params = {
        "response_type": "code",
        "client_id": svc.settings.pass_page_oidc_client_id,
        "redirect_uri": redirect_uri(svc),
        "scope": "openid keycast",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = (
        svc.settings.authentik_public_url.rstrip("/")
        + "/application/o/authorize/?"
        + urllib.parse.urlencode(params)
    )
    return url, _sign(svc, {"kind": "flow", "state": state, "verifier": verifier}, FLOW_SECONDS)


def finish_login(svc: Services, code: str, state: str, flow_cookie: str | None) -> str:
    """Exchange the code, read the partner id; returns the session cookie."""
    flow = _verify(svc, flow_cookie, "flow")
    if not flow or not state or state != flow.get("state"):
        raise PassPageError(400, "The sign-in took too long or was started elsewhere. Try again.")
    if not code:
        raise PassPageError(400, "The sign-in returned no code. Try again.")
    s = svc.settings
    base = s.authentik_url.rstrip("/")
    token = httpx.post(
        f"{base}/application/o/token/",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(svc),
            "code_verifier": flow["verifier"],
        },
        auth=(s.pass_page_oidc_client_id, s.pass_page_oidc_client_secret),
        timeout=15,
    )
    if token.status_code != 200:
        log.warning("pass page login: token exchange %s %s", token.status_code, token.text[:200])
        raise PassPageError(502, "The sign-in failed. Try again.")
    info = httpx.get(
        f"{base}/application/o/userinfo/",
        headers={"Authorization": f"Bearer {token.json().get('access_token')}"},
        timeout=15,
    )
    if info.status_code != 200:
        log.warning("pass page login: userinfo %s %s", info.status_code, info.text[:200])
        raise PassPageError(502, "The sign-in failed. Try again.")
    vine = info.json().get("keycast_vine_id") or ""
    if not vine.startswith("odoo-partner-") or not vine.removeprefix("odoo-partner-").isdigit():
        raise PassPageError(403, "This account has no pass.")
    partner_id = int(vine.removeprefix("odoo-partner-"))
    return _sign(svc, {"kind": "session", "partner": partner_id}, s.pass_page_session_days * 86400)


# -- pages -----------------------------------------------------------------------------------------


def _dates(p: dict[str, Any]) -> str:
    """'25 January 2027' or '25 January – 31 January 2027' from Odoo or ISO dates."""
    s, e = odoo_date(p.get("date_start")), odoo_date(p.get("date_end"))
    if not s:
        return ""
    e = e or s
    return f"{s:%d %B %Y}" if s == e else f"{s:%d %B} – {e:%d %B %Y}"


STYLE = """<style>
 .pass-qr { display: flex; flex-direction: column; align-items: center; gap: 12px; }
 .pass-qr img { width: min(86vw, 360px); height: auto; background: #fff; padding: 16px; border-radius: 16px; box-sizing: border-box; }
 .live { display: flex; align-items: center; gap: 8px; font-variant-numeric: tabular-nums; font-size: 1.1rem; }
 .live .dot { width: 12px; height: 12px; border-radius: 50%; background: var(--osv-accent); animation: pulse 1.2s ease-in-out infinite; }
 @keyframes pulse { 50% { opacity: 0.2; transform: scale(0.7); } }
 .tier { font-size: 1.4rem; font-weight: 600; margin: 0; }
 .verdict { font-size: 2rem; font-weight: 700; margin: 0 0 8px; }
 .verdict.ok { color: var(--osv-accent); } .verdict.bad { color: var(--osv-accent-2); }
 .warn { color: #ffd166; font-weight: 600; }
 .row { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 16px; }
 button.link { background: none; border: 0; color: var(--osv-accent); font: inherit; padding: 0; cursor: pointer; text-decoration: underline; }
 .card + .card { margin-top: 16px; } h2 { font-size: 1.15rem; margin: 0 0 8px; }
 .invites form.row input { box-sizing: border-box; min-width: 0; flex: 1 1 200px; padding: 10px 12px; border-radius: 8px; border: 1px solid var(--osv-border-strong); background: var(--osv-surface-deep); color: var(--osv-foreground); font: inherit; }
 .invites button.btn { border: 0; font: inherit; font-weight: 600; cursor: pointer; }
 .invite-list { list-style: none; padding: 0; margin: 16px 0 0; display: grid; gap: 12px; }
 .invite-list li { border-top: 1px solid var(--osv-border); padding-top: 12px; }
 .invite-list .row { margin-top: 8px; align-items: center; } .invite-list code { display: block; margin-top: 8px; }
 .small-btn { background: var(--osv-surface-deep); color: var(--osv-foreground); border: 1px solid var(--osv-border-strong); border-radius: 8px; padding: 6px 12px; font: inherit; cursor: pointer; }
 .small { font-size: 0.85rem; }
</style>"""


def invites_section(invites: dict[str, Any] | None, msg: str = "") -> str:
    """ "Invite friends": the participant's invite links (belfry_friends.py in Odoo)."""
    if not invites:
        return ""
    left, limit = invites.get("left", 0), invites.get("limit", 0)
    rows = []
    for inv in invites.get("invites") or []:
        note = f"<strong>{E(inv['note'])}</strong> · " if inv.get("note") else ""
        if inv["state"] == "open":
            tools = (
                f'<div class="row"><button class="small-btn" type="button" '
                f'data-copy="{E(inv["url"])}">Copy link</button>'
                f'<button class="small-btn" type="button" data-share="{E(inv["url"])}">'
                f'Share</button><form method="post" action="/me/invites/{inv["id"]}/revoke">'
                '<button class="link" type="submit">Revoke</button></form></div>'
                f'<code class="small">{E(inv["url"])}</code>'
            )
            state = f"open until {E((inv.get('expires_at') or '')[:10])}"
        elif inv["state"] == "used":
            tools, state = "", f"used by {E(inv.get('used_by') or 'a friend')}"
        else:
            tools, state = "", E(inv["state"])
        rows.append(f'<li>{note}<span class="muted">{state}</span>{tools}</li>')
    form = (
        '<form method="post" action="/me/invites" class="row">'
        '<input type="text" name="note" maxlength="200" '
        'placeholder="Who is it for? (only you see this)">'
        '<button class="btn" type="submit">Create an invite link</button></form>'
        if left
        else '<p class="muted">You have no invites left. Revoke an unused one, or ask a steward.</p>'
    )
    flash = f'<p class="ok">{E(msg)}</p>' if msg else ""
    plural = "" if left == 1 else "s"
    return f"""<div class="card invites" id="invite">
<h2>Invite friends</h2>
<p class="muted">The village grows through people who know each other. You can invite {limit}
friends; {left} invite{plural} left. Each link works once and expires after two weeks; an unused
one can be revoked and gives the invite back.</p>
{flash}{form}
<ul class="invite-list">{"".join(rows)}</ul>
</div>
<script>
document.addEventListener('click', e => {{
  const c = e.target.closest('[data-copy]'), s = e.target.closest('[data-share]');
  if (c) navigator.clipboard.writeText(c.dataset.copy).then(() => {{
    c.textContent = 'Copied'; setTimeout(() => c.textContent = 'Copy link', 1500); }});
  if (s) {{
    const data = {{ title: 'Open Source Village', text: 'Join me at Open Source Village:', url: s.dataset.share }};
    if (navigator.share) navigator.share(data).catch(() => {{}});
    else navigator.clipboard.writeText(s.dataset.share);
  }}
}});
</script>"""


def me_page(
    pass_row: dict[str, Any] | None,
    qr: dict[str, Any] | None,
    claim_url: str = "",
    invites: dict[str, Any] | None = None,
    msg: str = "",
) -> str:
    if pass_row is None:
        body = """<div class="card"><h1>No pass</h1>
<p class="muted">This account has no Open Source Village pass. If you paid for one, ask a steward.</p></div>"""
        return page("Your pass", body + STYLE)
    tier = TIER_NAMES.get(pass_row.get("tier"), pass_row.get("tier") or "Pass")
    dates = _dates(pass_row)
    if qr is None:
        body = f"""<div class="card"><h1>Your pass</h1>
<p class="tier">{E(tier)}</p><p class="muted">{E(dates)}</p>
<p class="bad">This pass is {E(pass_row.get("state") or "not active")}; it cannot be shown at the door.</p></div>"""
        return page("Your pass", body + STYLE)
    wallet = (
        f'<p class="muted small">Prefer a wallet? <a href="{E(claim_url)}">Add the pass to your wallet</a>.</p>'
        if claim_url
        else ""
    )
    body = f"""<div class="card pass-qr" id="pass" data-refresh="{QR_REFRESH_SECONDS}">
<h1>Your pass</h1>
<p class="tier">{E(tier)}</p><p class="muted">{E(dates)}</p>
<img id="qr" alt="Your pass as a QR code" src="{qr["svg"]}">
<p class="live"><span class="dot"></span><span id="clock"></span></p>
<p class="muted">Show this code at the door. It renews itself every {QR_REFRESH_SECONDS} seconds, so a
screenshot does not work. Turn the brightness up.</p>
{wallet}
<form method="post" action="/me/logout"><button class="link" type="submit">Sign out of this page</button></form>
</div>
{invites_section(invites, msg)}
<script>
const box = document.getElementById('pass'), img = document.getElementById('qr'), clock = document.getElementById('clock');
const tick = () => clock.textContent = new Date().toLocaleTimeString([], {{hour: '2-digit', minute: '2-digit', second: '2-digit'}});
tick(); setInterval(tick, 1000);
async function refresh() {{
  try {{
    const r = await fetch('/me/qr', {{cache: 'no-store'}});
    if (r.status === 401) {{ location.reload(); return; }}
    const j = await r.json();
    if (j.svg) img.src = j.svg; else location.reload();
  }} catch (e) {{ /* offline: keep the last code; it expires on its own */ }}
}}
setInterval(refresh, Number(box.dataset.refresh) * 1000);
document.addEventListener('visibilitychange', () => {{ if (!document.hidden) {{ refresh(); lock(); }} }});
// keep the screen on while the code is shown (where the browser allows it)
let wake = null;
async function lock() {{ try {{ wake = await navigator.wakeLock?.request('screen'); }} catch (e) {{}} }}
lock();
</script>"""
    return page("Your pass", body + STYLE)


def check_page(result: dict[str, Any], scanned_before: float | None = None) -> str:
    now = dt.datetime.now().strftime("%H:%M:%S")
    if result.get("valid"):
        dates = _dates(result)
        again = (
            f'<p class="warn">This code was already scanned at '
            f"{dt.datetime.fromtimestamp(scanned_before):%H:%M:%S}. Make sure it is the same person.</p>"
            if scanned_before
            else ""
        )
        body = f"""<div class="card"><p class="verdict ok">Valid pass</p>
<p class="tier">{E(result.get("tier_name") or "")}</p><p class="muted">{E(dates)}</p>{again}
<p class="muted small">Checked at {now}.</p></div>"""
    else:
        body = f"""<div class="card"><p class="verdict bad">Not valid</p>
<p>{E(result.get("reason") or "")}</p><p class="muted small">Checked at {now}.</p></div>"""
    return page("Pass check", body + STYLE)


def error_page(exc: PassPageError) -> str:
    body = f'<div class="card"><h1>Your pass</h1><p class="bad">{E(exc.message)}</p></div>'
    return page("Your pass", body + STYLE)


# -- routes ----------------------------------------------------------------------------------------


def register(app: FastAPI) -> None:
    """Mount /me and /check (create_app calls this)."""
    app.state.scans = ScanLog()

    def svc(request: Request) -> Services:
        return request.app.state.services

    def guard(request: Request) -> tuple[Services, dict[str, Any] | None]:
        services = svc(request)
        if not enabled(services):
            raise PassPageError(503, "The pass page is not configured on this server.")
        return services, _verify(services, request.cookies.get(SESSION_COOKIE), "session")

    def qr_for(services: Services, pass_id: int) -> dict[str, Any]:
        token, exp = qr_token(services, pass_id)
        url = services.settings.public_url.rstrip("/") + "/check/" + token
        return {"svg": qr_svg(url), "expires": exp}

    @app.exception_handler(PassPageError)
    async def pass_page_error(_request: Request, exc: PassPageError):
        return HTMLResponse(error_page(exc), status_code=exc.status)

    @app.get("/me", response_class=HTMLResponse)
    async def me(request: Request):
        services, sess = guard(request)
        if sess is None:
            return RedirectResponse("/me/login", status_code=303)
        row = await run_in_threadpool(partner_pass, services, sess["partner"])
        active = row is not None and row.get("state") == "active"
        claim_url = ""
        if active:
            info = await run_in_threadpool(_claim_url, services, row["id"])
            claim_url = info or ""
        qr = qr_for(services, row["id"]) if active else None
        invites = None
        if active:
            invites = await run_in_threadpool(participant_invites, services, sess["partner"])
        msg = INVITE_MESSAGES.get(request.query_params.get("msg", ""), "")
        response = HTMLResponse(me_page(row, qr, claim_url, invites, msg))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/me/qr")
    async def me_qr(request: Request):
        services, sess = guard(request)
        if sess is None:
            return JSONResponse({"error": "signed out"}, status_code=401)
        row = await run_in_threadpool(partner_pass, services, sess["partner"])
        if row is None or row.get("state") != "active":
            return JSONResponse({"error": "no active pass"}, status_code=409)
        return JSONResponse(qr_for(services, row["id"]), headers={"Cache-Control": "no-store"})

    @app.get("/me/login")
    async def me_login(request: Request):
        services, _sess = guard(request)
        url, flow = start_login(services)
        response = RedirectResponse(url, status_code=303)
        response.set_cookie(
            FLOW_COOKIE,
            flow,
            max_age=FLOW_SECONDS,
            httponly=True,
            secure=_secure(services),
            samesite="lax",
            path="/me",
        )
        return response

    @app.get("/me/callback")
    async def me_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        services, _sess = guard(request)
        if error:
            raise PassPageError(
                403, "You were not signed in. Only participants with a pass can open this page."
            )
        cookie = await run_in_threadpool(
            finish_login, services, code, state, request.cookies.get(FLOW_COOKIE)
        )
        response = RedirectResponse("/me", status_code=303)
        response.delete_cookie(FLOW_COOKIE, path="/me")
        response.set_cookie(
            SESSION_COOKIE,
            cookie,
            max_age=services.settings.pass_page_session_days * 86400,
            httponly=True,
            secure=_secure(services),
            samesite="lax",
            path="/",  # the feedback page knows who sends it
        )
        return response

    def same_origin(request: Request, services: Services) -> None:
        """Forms post only from this site (the session cookie is SameSite=Lax as well)."""
        origin = request.headers.get("origin")
        allowed = {_origin(services.settings.public_url), _origin(services.settings.site_url)}
        if origin and origin not in allowed:
            raise PassPageError(403, "This form can only be sent from the pass page.")

    @app.post("/me/invites")
    async def me_create_invite(request: Request):
        services, sess = guard(request)
        if sess is None:
            return RedirectResponse("/me/login", status_code=303)
        same_origin(request, services)
        note = str((await request.form()).get("note") or "")
        try:
            await run_in_threadpool(create_participant_invite, services, sess["partner"], note)
        except OdooError as exc:
            raise PassPageError(409, _odoo_message(exc)) from exc
        return RedirectResponse("/me?msg=created#invite", status_code=303)

    @app.post("/me/invites/{invite_id}/revoke")
    async def me_revoke_invite(invite_id: int, request: Request):
        services, sess = guard(request)
        if sess is None:
            return RedirectResponse("/me/login", status_code=303)
        same_origin(request, services)
        try:
            await run_in_threadpool(revoke_participant_invite, services, sess["partner"], invite_id)
        except OdooError as exc:
            raise PassPageError(409, _odoo_message(exc)) from exc
        return RedirectResponse("/me?msg=revoked#invite", status_code=303)

    @app.post("/me/logout")
    async def me_logout(request: Request):
        guard(request)
        body = """<div class="card"><h1>Signed out</h1><p>This page is signed out.</p>
<p><a class="btn" href="/me">Show my pass</a></p></div>"""
        response = HTMLResponse(page("Signed out", body))
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(SESSION_COOKIE, path="/me")  # sessions from before 2026-10-25
        return response

    @app.get("/check/{token}", response_class=HTMLResponse)
    async def check(token: str, request: Request):
        services = svc(request)
        if not enabled(services):
            raise PassPageError(503, "Pass codes are not configured on this server.")
        try:
            pass_id = read_qr_token(services, token)
        except PassPageError as exc:
            result = {"valid": False, "reason": exc.message}
            return HTMLResponse(
                check_page(result), status_code=200, headers={"Cache-Control": "no-store"}
            )
        result = await run_in_threadpool(check_pass, services, pass_id)
        before = None
        if result.get("valid"):
            before = request.app.state.scans.first_scan(token, int(token.split(".")[1]))
        log.info(
            "pass check: pass %s valid=%s again=%s", pass_id, result.get("valid"), bool(before)
        )
        accept = request.headers.get("accept", "")
        if "application/json" in accept and "text/html" not in accept:
            return JSONResponse(
                {**result, "scanned_before": before}, headers={"Cache-Control": "no-store"}
            )
        return HTMLResponse(check_page(result, before), headers={"Cache-Control": "no-store"})


INVITE_MESSAGES = {
    "created": "Invite link created: copy it or share it with your friend.",
    "revoked": "Invite revoked.",
}


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url or "")
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def _odoo_message(exc: OdooError) -> str:
    """Odoo's UserError text (shown to the participant), without the traceback."""
    text = (exc.data.get("message") or exc.message or "").strip()
    return text.splitlines()[0][:300] if text else "Odoo refused the request."


def participant_invites(svc: Services, partner_id: int) -> dict[str, Any] | None:
    try:
        return svc.odoo.call("belfry.invite", "belfry_participant_invites", [partner_id])
    except OdooError as exc:
        log.warning("participant invites for partner %s: %s", partner_id, exc)
        return None


def create_participant_invite(svc: Services, partner_id: int, note: str) -> dict[str, Any]:
    row = svc.odoo.call(
        "belfry.invite", "belfry_participant_create", [partner_id, note.strip()[:200] or False]
    )
    log.info("partner %s created invite %s", partner_id, row.get("id"))
    return row


def revoke_participant_invite(svc: Services, partner_id: int, invite_id: int) -> dict[str, Any]:
    return svc.odoo.call("belfry.invite", "belfry_participant_revoke", [partner_id, invite_id])


def _claim_url(svc: Services, pass_id: int) -> str | None:
    rows = svc.odoo.read("belfry.pass", [pass_id], ["claim_token"])
    token = rows[0].get("claim_token") if rows else None
    return svc.settings.public_url.rstrip("/") + "/claim/" + token if token else None
