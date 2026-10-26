"""One-time sign-in links and the page after paying: ``/signin/<secret>``, ``/welcome/<secret>``.

The activation mail's button and the page the payer lands on after paying both sign the browser
in to Open Source Village. authentik keeps one recovery token per user, so the two cannot both
carry an authentik link: a second link would make the first one useless. Each carries a secret of
Belfry's own instead (Odoo ``belfry.pass``: ``signin_token``, ``welcome_token``, models/
belfry_welcome.py), spent once; spending it mints a short-lived authentik link (core.
authentik_signin_url) and redirects there. A sign-in that no app asked for ends on the apps page
(the brand's default application, blueprint belfry-keycast-provider).

Mail scanners open links without running scripts, so the GET only shows a page whose script
sends the POST that spends the secret (a button without scripts).
"""

from __future__ import annotations

import html
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from .claim import page
from .core import Services, authentik_signin_url

log = logging.getLogger(__name__)
E = html.escape

MESSAGES = {
    "used": "This sign-in link has been used already.",
    "expired": "This sign-in link has expired.",
    "not_found": "This sign-in link is not valid.",
    "not_active": "This pass is not active.",
    "no_user": "Your account is not ready yet. Try again in a minute.",
}


def _apps_button(svc: Services) -> str:
    apps = svc.settings.apps_public_url or ""
    return f'<p><a class="btn" href="{E(apps)}">Go to the apps</a></p>' if apps else ""


def _later_hint(svc: Services) -> str:
    return (
        "<p>Sign in with your e-mail address instead: you get a new sign-in link by mail.</p>"
        + _apps_button(svc)
    )


def spend_page(action: str, title: str, lead: str, button: str) -> str:
    body = f"""<div class="card">
<h1>{E(title)}</h1>
<p class="muted">{E(lead)}</p>
<form method="post" action="{E(action)}" id="go">
<button class="btn" type="submit">{E(button)}</button>
</form>
</div>
<script>document.getElementById("go").submit()</script>"""
    return page(title, body)


def problem_page(svc: Services, result: str) -> str:
    body = f"""<div class="card">
<h1>Sign in to Open Source Village</h1>
<p>{E(MESSAGES.get(result, MESSAGES["not_found"]))}</p>
{_later_hint(svc)}
</div>"""
    return page("Sign in", body)


def welcome_page(svc: Services, token: str, info: dict[str, Any]) -> str:
    name = info.get("first_name") or ""
    hello = f"Welcome, {E(name)}!" if name else "Welcome!"
    public = svc.settings.public_url.rstrip("/")
    if info.get("state") == "active":
        dates = (
            f" from {E(info['date_start'])} to {E(info['date_end'])}"
            if info.get("date_start") and info.get("date_end")
            else ""
        )
        if info.get("signin_available"):
            go = f"""<form method="post" action="/welcome/{E(token)}">
<button class="btn" type="submit">Open the apps</button></form>
<p class="muted small">Signs you in on this device. On another device, use the link in the
mail we sent you.</p>"""
        else:
            go = (
                "<p>Sign in with the button in the mail we sent you, or with your e-mail address "
                "on the apps page.</p>" + _apps_button(svc)
            )
        wallet = (
            f' · <a href="{E(public)}/claim/{E(info["claim_token"])}">Add it to a wallet app</a>'
            if info.get("claim_token")
            else ""
        )
        body = f"""<div class="card">
<h1>{hello}</h1>
<p>Your <strong>{E(info.get("tier_name") or "")}</strong> pass is active{dates}.</p>
{go}
<p class="muted small">At the door:
<a href="{E(public)}/me">show your pass on your phone</a>{wallet}.</p>
</div>"""
        return page("Welcome to Open Source Village", body)
    if info.get("payment") == "paid":
        text = "Payment received. Your pass is being set up; this page updates by itself."
        refresh = '<meta http-equiv="refresh" content="3">'
    elif info.get("payment") == "processing":
        text = (
            "Your payment is being processed by your payment provider (a bank transfer can take "
            "a few days). You get a mail as soon as your pass is ready."
        )
        refresh = '<meta http-equiv="refresh" content="30">'
    else:
        text = "We have not received your payment yet. Use the payment link of your invite to pay."
        refresh = ""
    body = f"""{refresh}<div class="card">
<h1>{hello}</h1>
<p>{E(text)}</p>
</div>"""
    return page("Welcome to Open Source Village", body)


def register(app: FastAPI) -> None:
    """Mount /signin and /welcome (create_app calls this)."""

    def svc(request: Request) -> Services:
        return request.app.state.services

    def no_store(response: Response) -> Response:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    async def spend(services: Services, kind: str, token: str) -> Response:
        result = await run_in_threadpool(
            services.odoo.call, "belfry.pass", "belfry_use_signin", [kind, token]
        )
        if result.get("result") != "ok":
            log.info("%s link refused: %s", kind, result.get("result"))
            return no_store(
                HTMLResponse(problem_page(services, str(result.get("result"))), status_code=410)
            )
        url = await run_in_threadpool(
            authentik_signin_url, services, int(result["authentik_user_id"])
        )
        log.info("%s link used for pass %s", kind, result.get("pass_id"))
        return no_store(RedirectResponse(url, status_code=303))

    @app.get("/signin/{token}", response_class=HTMLResponse)
    async def signin_page(token: str):
        return no_store(
            HTMLResponse(
                spend_page(
                    f"/signin/{token}",
                    "Signing you in",
                    "One moment, you are being signed in to Open Source Village.",
                    "Sign in",
                )
            )
        )

    @app.post("/signin/{token}")
    async def signin(request: Request, token: str):
        return await spend(svc(request), "signin", token)

    @app.get("/welcome/{token}", response_class=HTMLResponse)
    async def welcome(request: Request, token: str):
        services = svc(request)
        info = await run_in_threadpool(
            services.odoo.call, "belfry.pass", "belfry_welcome_info", [token]
        )
        if info.get("result") != "ok":
            return no_store(HTMLResponse(problem_page(services, "not_found"), status_code=404))
        return no_store(HTMLResponse(welcome_page(services, token, info)))

    @app.post("/welcome/{token}")
    async def welcome_signin(request: Request, token: str):
        return await spend(svc(request), "welcome", token)
