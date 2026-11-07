"""The belfry's own sign-in on ``pass.<domain>/me/login``: e-mail address, then a 6-digit code.

The same steps and look as ``belfry.<domain>/connect`` (connect.py), so a member sees one sign-in
everywhere: the code comes from Odoo (``belfry.key.code``), bound to a throwaway key of this
sign-in. Once the code is right, the browser gets the pass page's session (mypass.SESSION_COOKIE)
and goes on to where the sign-in started (``belfry_pass_next``: the pass, a drop box).

The Open Source Village look keeps authentik's login (with the Village pass from an EUDI wallet):
``own()`` is false there.

A drop box starts it with the address already typed (``start_with_email``): the code is on its
way when the page opens.
"""

from __future__ import annotations

import hmac
import html
import secrets
import threading
import time
from typing import Any

from coincurve import PrivateKey
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from . import claim, connect, mypass, setup, theme
from .connect import ConnectError
from .core import Services

E = html.escape

COOKIE = "belfry_login"
NEXT = "belfry_pass_next"
SECONDS = 1800
TARGETS = ("/me/console?", "/drop/")

_lock = threading.Lock()
FLOWS: dict[str, dict[str, Any]] = {}


def own(svc: Services) -> bool:
    """This belfry signs in with its own page (every look but Open Source Village's)."""
    return theme.current().base != "osv"


def target(request: Request) -> str:
    after = request.cookies.get(NEXT) or ""
    return after if after.startswith(TARGETS) else "/me"


def new_flow() -> dict[str, Any]:
    with _lock:
        now = time.time()
        for k in [k for k, v in FLOWS.items() if v["at"] < now - SECONDS]:
            del FLOWS[k]
        fid = secrets.token_urlsafe(24)
        FLOWS[fid] = {
            "id": fid,
            "csrf": secrets.token_urlsafe(24),
            "session": PrivateKey().public_key_xonly.format().hex(),
            "at": now,
        }
        return FLOWS[fid]


def flow_of(fid: str, csrf: str | None = None) -> dict[str, Any] | None:
    with _lock:
        f = FLOWS.get(fid or "")
    if not f or f["at"] < time.time() - SECONDS:
        return None
    if csrf is not None and not hmac.compare_digest(csrf, f["csrf"]):
        return None
    return f


def set_flow(response: Response, svc: Services, f: dict[str, Any]) -> None:
    response.set_cookie(
        COOKIE,
        f["id"],
        max_age=SECONDS,
        httponly=True,
        secure=mypass._secure(svc),
        samesite="lax",
        path="/me",
    )


def start_with_email(svc: Services, email: str) -> dict[str, Any]:
    """A new sign-in whose code is already sent (raises ConnectError with a message to show)."""
    f = new_flow()
    connect.send_code(svc, f, email)
    return f


def page(svc: Services, f: dict[str, Any]) -> str:
    domain = svc.settings.nip05_domain
    name = theme.current().name
    pending = f.get("pending")
    boxes = "".join(
        ('<span class="sep"></span>' if i == 3 else "")
        + f'<input type="text" inputmode="numeric" pattern="[0-9]*" aria-label="Digit {i + 1}"'
        + (' autocomplete="one-time-code"' if i == 0 else ' autocomplete="off"')
        + ">"
        for i in range(6)
    )
    to = E(pending["email"]) if pending else ""
    return f"""<div class="card wiz" id="wiz" data-base="/me/login" data-flow="{E(f["id"])}" data-csrf="{E(f["csrf"])}" data-start="{"code" if pending else "email"}">
<section data-step="email" hidden>
<h1>Sign in to {E(name)}</h1>
<p class="muted">With your {E(domain)} account. No password: we e-mail you a code.</p>
<form novalidate><label for="c-mail" style="font-weight:500;font-size:14px;display:block;margin-bottom:8px">E-mail</label>
<input id="c-mail" type="email" name="email" autocomplete="email" placeholder="you@example.org" value="{to}">
<p class="err" role="alert"></p><div class="actions"><button class="btn full" type="submit">Continue</button></div></form>
<a href="#" class="quiet center" data-go="other">Other ways to sign in</a></section>

<section data-step="code" hidden>
<h1>Check your inbox</h1>
<p class="muted">Type the 6-digit code we sent to <strong class="to">{to}</strong>.</p>
<form novalidate><div class="otp">{boxes}</div><p class="err" role="alert"></p>
<div class="actions"><button class="btn" type="submit">Verify</button>
<button type="button" class="quiet" data-again>Send a new code</button></div></form>
<a href="#" class="quiet center" data-go="email">Use another e-mail</a></section>

<section data-step="other" hidden>
<a href="#" class="quiet" data-go="email">‹ Back to e-mail</a>
<h1 style="margin-top:16px">Other ways to sign in</h1>
<p class="muted">Not on this sign-in page yet. For now, use your e-mail address.</p>
<div class="opt"><span>Membership pass<small>From your EU digital wallet</small></span></div>
<div class="opt"><span>Nostr signer<small>A browser extension or a bunker:// link</small></span></div>
</section>
</div>{connect.JS}"""


def shell(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        theme.frame(
            "Sign in", setup.WIZARD_STYLE + connect.STYLE + body, claim.TEST_NOTICE, bare=True
        ),
        status_code=status,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def login_page(request: Request) -> HTMLResponse:
    services: Services = request.app.state.services
    f = flow_of(request.cookies.get(COOKIE, "")) or new_flow()
    response = shell(page(services, f))
    set_flow(response, services, f)
    return response


def register(app: FastAPI) -> None:
    def svc(request: Request) -> Services:
        return request.app.state.services

    async def body_of(request: Request) -> tuple[dict[str, Any], dict[str, Any]]:
        origin = request.headers.get("origin")
        if origin and origin != mypass._origin(svc(request).settings.public_url):
            raise ConnectError(403, "Wrong origin.")
        try:
            body = await request.json()
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        f = flow_of(request.cookies.get(COOKIE, ""), str(body.get("csrf", "")))
        if f is None or f["id"] != str(body.get("flow", "")):
            raise ConnectError(410, "This sign-in expired. Reload the page and start again.")
        return f, body

    def fail(exc: ConnectError) -> JSONResponse:
        return JSONResponse({"message": exc.message}, status_code=exc.status)

    @app.post("/me/login/send")
    async def login_send(request: Request):
        try:
            f, body = await body_of(request)
            email = str(body.get("email", ""))[:254]
            return JSONResponse(await run_in_threadpool(connect.send_code, svc(request), f, email))
        except ConnectError as exc:
            return fail(exc)

    @app.post("/me/login/verify")
    async def login_verify(request: Request):
        services = svc(request)
        try:
            f, body = await body_of(request)
            code = "".join(c for c in str(body.get("code", "")) if c.isdigit())[:6]
            member = await run_in_threadpool(connect.verify_code, services, f, code)
        except ConnectError as exc:
            return fail(exc)
        with _lock:
            FLOWS.pop(f["id"], None)
        days = services.settings.pass_page_session_days
        response = JSONResponse({"next": "in", "to": target(request)})
        response.set_cookie(
            mypass.SESSION_COOKIE,
            mypass._sign(services, {"kind": "session", "partner": member["partner"]}, days * 86400),
            max_age=days * 86400,
            httponly=True,
            secure=mypass._secure(services),
            samesite="lax",
            path="/",
        )
        response.delete_cookie(COOKIE, path="/me")
        response.delete_cookie(NEXT, path="/me")
        return response
