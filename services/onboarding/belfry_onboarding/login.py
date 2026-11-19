"""Belfry's login page and logout (replaces Authentik's flows, 2026-11-19).

    GET  /login?next=…&fresh=1    e-mail address, or "Sign in with your pass"
    POST /login/email             Odoo mails a one-time link and a 6-digit code (belfry.login.code)
    POST /login/code              the code, typed in this browser -> session -> next
    GET  /login/link/<secret>     the mail's link: a page whose script POSTs (mail scanners
    POST /login/link/<secret>     open links without scripts) -> session -> next
    GET  /login/pass …            the pass from a wallet (passlogin.py)
    GET  /logout?return_to=…      ends the session; also /api/auth/oidc/logout (the apps' path)
    /oidc/*                       the credential issuer's sign-in (oidc.py)

``next`` is a path here or a URL on one of Belfry's own origins (session.safe_next). Only
members with an active pass and stewards are mailed; the answer is the same for any address.
"""

from __future__ import annotations

import html
import logging
import urllib.parse
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from . import oidc, passlogin, session
from .claim import page
from .clients.odoo import OdooError
from .core import Services

log = logging.getLogger(__name__)
E = html.escape

FORM_CSS = """<style>
 .fb { display: grid; gap: 14px; margin-top: 16px; }
 .fb label { display: grid; gap: 6px; min-width: 0; }
 .fb input[type=text], .fb input[type=email] { box-sizing: border-box; width: 100%; max-width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid var(--osv-border-strong); background: var(--osv-surface-deep); color: var(--osv-foreground); font: inherit; }
 .fb input.code { font-size: 1.6rem; letter-spacing: 0.3em; max-width: 12ch; }
 .fb button, button.btn { border: 0; font: inherit; font-weight: 600; cursor: pointer; justify-self: start; }
 .or { margin: 20px 0 12px; } .small { font-size: 0.85rem; }
</style>"""


def no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _next_field(next_url: str) -> str:
    return f'<input type="hidden" name="next" value="{E(next_url)}">'


def login_page(
    next_url: str, message: str = "", fresh: bool = False, pass_login: bool = True
) -> str:
    lead = (
        "For your safety, sign in again to continue."
        if fresh
        else "Sign in with the e-mail address of your pass. You get a link and a code by mail; "
        "there is no password."
    )
    note = f'<p class="bad">{E(message)}</p>' if message else ""
    pass_button = (
        f"""<p class="or muted">Or, with the pass in a wallet app:</p>
<p><a class="btn" href="/login/pass?{urllib.parse.urlencode({"next": next_url})}">Sign in with your pass</a></p>"""
        if pass_login
        else ""
    )
    body = f"""<div class="card">
<h1>Sign in to Open Source Village</h1>
<p class="muted">{E(lead)}</p>
{note}
<form method="post" action="/login/email" class="fb">
{_next_field(next_url)}
<label>Your e-mail address <input type="email" name="email" required autocomplete="email" maxlength="254"></label>
<button class="btn" type="submit">Send me a sign-in link</button>
</form>
{pass_button}
</div>{FORM_CSS}"""
    return page("Sign in", body)


def code_page(email: str, next_url: str, message: str = "") -> str:
    note = f'<p class="bad">{E(message)}</p>' if message else ""
    body = f"""<div class="card">
<h1>Check your mail</h1>
<p>If <strong>{E(email)}</strong> belongs to a pass, a sign-in link and a code are on their way.
Press the button in the mail, or type the code here. Both work for 10 minutes.</p>
{note}
<form method="post" action="/login/code" class="fb">
{_next_field(next_url)}
<input type="hidden" name="email" value="{E(email)}">
<label>Code <input class="code" type="text" name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9 ]*" maxlength="8" required autofocus></label>
<button class="btn" type="submit">Sign in</button>
</form>
<p class="muted small"><a href="/login?{urllib.parse.urlencode({"next": next_url})}">Use another address</a></p>
</div>{FORM_CSS}"""
    return page("Check your mail", body)


def link_page(secret: str) -> str:
    body = f"""<div class="card">
<h1>Signing you in</h1>
<p class="muted">One moment, you are being signed in to Open Source Village.</p>
<form method="post" action="/login/link/{E(secret)}" id="go">
<button class="btn" type="submit">Sign in</button>
</form>
</div>
<script>document.getElementById("go").submit()</script>"""
    return page("Signing you in", body)


def problem_page(text: str) -> str:
    body = f"""<div class="card">
<h1>Sign in to Open Source Village</h1>
<p>{E(text)}</p>
<p><a class="btn" href="/login">Sign in</a></p>
</div>"""
    return page("Sign in", body)


LINK_PROBLEMS = {
    "used": "This sign-in link has been used already. Ask for a new one.",
    "expired": "This sign-in link has expired. Ask for a new one.",
    "not_found": "This sign-in link is not valid.",
    "not_active": "This pass is not active.",
}


def signed_in(svc: Services, target: str, partner_id: int, method: str) -> Response:
    response = RedirectResponse(session.safe_next(svc.settings, target), status_code=303)
    session.set_cookie(response, svc.settings, partner_id, method)
    log.info("partner %s signed in (%s)", partner_id, method)
    return no_store(response)


def logout_target(svc: Services, return_to: str | None, app_origins: set[str] = frozenset()) -> str:
    return session.safe_next(svc.settings, return_to, "/login?logged_out=1", app_origins)


def register(app: FastAPI) -> None:
    """Mount /login, /logout and /oidc (create_app calls this)."""
    app.state.passlogin = passlogin.PassLoginStore()
    app.state.oidc = oidc.OidcStore()

    def svc(request: Request) -> Services:
        return request.app.state.services

    @app.get("/login", response_class=HTMLResponse)
    async def login(request: Request, next: str = "", fresh: int = 0, logged_out: int = 0):
        services = svc(request)
        target = session.safe_next(services.settings, next)
        current = session.current(request)
        if current and not fresh and not logged_out:
            # already signed in: straight on (a fresh sign-in is asked for explicitly)
            return no_store(RedirectResponse(target, status_code=303))
        message = "You are signed out." if logged_out else ""
        return no_store(
            HTMLResponse(
                login_page(
                    target, message, fresh=bool(fresh), pass_login=services.verifier is not None
                )
            )
        )

    @app.post("/login/email", response_class=HTMLResponse)
    async def login_email(request: Request):
        services = svc(request)
        form = await request.form()
        email = str(form.get("email") or "").strip()
        target = session.safe_next(services.settings, str(form.get("next") or ""))
        try:
            result = await run_in_threadpool(
                services.odoo.call, "belfry.login.code", "belfry_login_send", [email, target]
            )
        except OdooError as exc:
            log.info("login mail refused: %s", exc)
            return no_store(
                HTMLResponse(login_page(target, "Type a valid e-mail address."), status_code=422)
            )
        if result.get("state") == "too_many":
            return no_store(
                HTMLResponse(
                    login_page(
                        target,
                        "Too many sign-in mails for this address. Try again in half an hour.",
                    ),
                    status_code=429,
                )
            )
        return no_store(HTMLResponse(code_page(email, target)))

    @app.post("/login/code", response_class=HTMLResponse)
    async def login_code(request: Request):
        services = svc(request)
        form = await request.form()
        email = str(form.get("email") or "").strip()
        target = session.safe_next(services.settings, str(form.get("next") or ""))
        result = await run_in_threadpool(
            services.odoo.call,
            "belfry.login.code",
            "belfry_login_verify",
            [email, str(form.get("code") or "")],
        )
        state = result.get("state")
        if state == "ok":
            return signed_in(services, target, int(result["partner_id"]), "email")
        if state == "wrong_code":
            left = int(result.get("tries_left") or 0)
            msg = (
                f"That code is not right. {left} tries left." if left else "That code is not right."
            )
            return no_store(HTMLResponse(code_page(email, target, msg), status_code=422))
        msg = (
            "This pass is not active."
            if state == "not_active"
            else "The code has expired. Ask for a new one."
        )
        return no_store(HTMLResponse(login_page(target, msg), status_code=410))

    @app.get("/login/link/{secret}", response_class=HTMLResponse)
    async def login_link_page(secret: str):
        return no_store(HTMLResponse(link_page(secret)))

    @app.post("/login/link/{secret}")
    async def login_link(request: Request, secret: str):
        services = svc(request)
        result = await run_in_threadpool(
            services.odoo.call, "belfry.login.code", "belfry_login_use_link", [secret]
        )
        state = result.get("state")
        if state == "ok":
            return signed_in(
                services, str(result.get("next_url") or ""), int(result["partner_id"]), "link"
            )
        log.info("sign-in link refused: %s", state)
        return no_store(
            HTMLResponse(
                problem_page(LINK_PROBLEMS.get(state, LINK_PROBLEMS["not_found"])), status_code=410
            )
        )

    # -- the pass from a wallet ---------------------------------------------------------------

    @app.get("/login/pass", response_class=HTMLResponse)
    async def login_pass(request: Request, next: str = ""):
        services = svc(request)
        target = session.safe_next(services.settings, next)
        try:
            attempt = await run_in_threadpool(
                passlogin.start, services, request.app.state.passlogin, target
            )
        except passlogin.PassLoginError as exc:
            return no_store(HTMLResponse(passlogin.error_page(exc), status_code=exc.status))
        response = HTMLResponse(passlogin.page_html(attempt))
        response.set_cookie(
            passlogin.COOKIE,
            attempt.browser,
            max_age=passlogin.AUTH_TTL,
            path="/login/pass",
            httponly=True,
            secure=services.settings.public_url.startswith("https://"),
            samesite="lax",
        )
        return no_store(response)

    @app.get("/login/pass/poll/{sid}")
    async def login_pass_poll(request: Request, sid: str) -> JSONResponse:
        try:
            return JSONResponse(
                await run_in_threadpool(
                    passlogin.poll, svc(request), request.app.state.passlogin, sid
                )
            )
        except passlogin.PassLoginError as exc:
            return JSONResponse(
                {"error": exc.error, "error_description": exc.description}, status_code=exc.status
            )

    @app.get("/login/pass/done/{sid}")
    async def login_pass_done(request: Request, sid: str):
        services = svc(request)
        try:
            partner_id, target = await run_in_threadpool(
                passlogin.finish,
                services,
                request.app.state.passlogin,
                sid,
                request.cookies.get(passlogin.COOKIE),
            )
        except passlogin.PassLoginError as exc:
            return no_store(HTMLResponse(passlogin.error_page(exc), status_code=exc.status))
        response = signed_in(services, target, partner_id, "pass")
        response.delete_cookie(passlogin.COOKIE, path="/login/pass")
        return response

    # -- logout -------------------------------------------------------------------------------

    async def do_logout(request: Request, return_to: str | None) -> Response:
        services = svc(request)
        origins = getattr(request.app.state, "app_origins", None)
        target = logout_target(services, return_to, origins.current() if origins else set())
        response = RedirectResponse(target, status_code=303)
        session.clear_cookie(response, services.settings)
        # the pass page's and the steward pages' own cookies end with the sign-in
        response.delete_cookie("belfry_pass", path="/")
        response.delete_cookie("belfry_steward", path="/steward")
        return no_store(response)

    @app.get("/logout")
    async def logout(request: Request, return_to: str = ""):
        return await do_logout(request, return_to)

    @app.get("/api/auth/oidc/logout")
    async def app_logout(request: Request, return_to: str = ""):
        """The apps' logout (the path Keycast had, so the app builds did not change): ends the
        sign-in everywhere and goes back to the app."""
        return await do_logout(request, return_to)

    # -- the credential issuer's sign-in ------------------------------------------------------

    def oidc_error(exc: oidc.OidcError) -> JSONResponse:
        return JSONResponse(
            {"error": exc.error, "error_description": exc.description}, status_code=exc.status
        )

    @app.get("/oidc/.well-known/openid-configuration")
    async def oidc_discovery(request: Request) -> JSONResponse:
        return JSONResponse(oidc.discovery(svc(request)))

    @app.get("/oidc/jwks")
    async def oidc_jwks(request: Request) -> JSONResponse:
        key = svc(request).bridge_jwk
        return JSONResponse({"keys": [oidc.public_jwk(key)] if key else []})

    @app.get("/oidc/authorize")
    async def oidc_authorize(request: Request):
        services = svc(request)
        params = {k: str(v) for k, v in request.query_params.items()}
        try:
            oidc.check_request(services, params)
        except oidc.OidcError as exc:
            return no_store(
                HTMLResponse(problem_page(exc.description or exc.error), status_code=exc.status)
            )
        current = session.current(request)
        if current is None:
            here = "/oidc/authorize?" + urllib.parse.urlencode(params)
            return no_store(
                RedirectResponse("/login?" + urllib.parse.urlencode({"next": here}), 303)
            )
        try:
            target = await run_in_threadpool(
                oidc.authorize, services, request.app.state.oidc, params, current.partner_id
            )
        except oidc.OidcError as exc:
            return no_store(
                HTMLResponse(problem_page(exc.description or exc.error), status_code=exc.status)
            )
        return no_store(RedirectResponse(target, status_code=302))

    @app.post("/oidc/token")
    async def oidc_token(request: Request) -> JSONResponse:
        form = {k: str(v) for k, v in (await request.form()).items()}
        try:
            tokens = await run_in_threadpool(
                oidc.exchange_code,
                svc(request),
                request.app.state.oidc,
                form,
                request.headers.get("authorization"),
            )
        except oidc.OidcError as exc:
            return oidc_error(exc)
        return JSONResponse(tokens, headers={"Cache-Control": "no-store"})

    @app.get("/oidc/userinfo")
    async def oidc_userinfo(request: Request) -> JSONResponse:
        try:
            return JSONResponse(
                oidc.userinfo(request.app.state.oidc, request.headers.get("authorization"))
            )
        except oidc.OidcError as exc:
            return oidc_error(exc)


__all__: list[Any] = ["register", "login_page", "code_page"]
