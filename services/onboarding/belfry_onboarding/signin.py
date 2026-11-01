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

A pass ordered by an app with a ``return_url`` (Odoo's pass API) comes back to the app instead
(2026-11-01): the welcome page waits for the activation, signs in by script and the sign-in link
carries ``belfry_return``, which the account-setup flow turns into its final redirect (blueprint
belfry-account-setup-flow). Then the app's "Sign in with Open Source Village" finds a session.
When signing in is not possible (the secret used or stale, the pass not active within two
minutes, a payment still processing), the payer goes back to the app without a session.
"""

from __future__ import annotations

import html
import logging
import urllib.parse
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


# the welcome page reloads every 3 s while the pass is set up; with a return_url the payer goes
# back to the app after this many rounds even without a session
WELCOME_WAIT_ROUNDS = 40


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc or url


def welcome_next(info: dict[str, Any], wait: int) -> str:
    """For a pass with a return_url: ``signin`` (spend the secret, then the app), ``wait`` (the
    pass is being set up) or ``go`` (back to the app now, without signing in)."""
    if info.get("state") == "active":
        return "signin" if info.get("signin_available") else "go"
    if info.get("payment") == "paid" and wait < WELCOME_WAIT_ROUNDS:
        return "wait"
    return "go"


def waiting_page(token: str, target: str, wait: int) -> str:
    host = E(_host(target))
    body = f"""<meta http-equiv="refresh" content="3;url=/welcome/{E(token)}?wait={wait + 1}">
<div class="card">
<h1>Payment received</h1>
<p>Your pass is being set up; then you go back to {host}, signed in.</p>
<p class="muted small"><a href="{E(target)}">Back to {host} now</a></p>
</div>"""
    return page("Welcome to Open Source Village", body)


def with_return(link: str, target: str) -> str:
    """The authentik sign-in link, ending at the app (policy belfry-account-setup-return)."""
    return link + ("&" if "?" in link else "?") + urllib.parse.urlencode({"belfry_return": target})


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
        target = result.get("return_url") or ""
        if result.get("result") != "ok":
            log.info("%s link refused: %s", kind, result.get("result"))
            if target:  # back to the app that ordered, without a session
                return no_store(RedirectResponse(target, status_code=303))
            return no_store(
                HTMLResponse(problem_page(services, str(result.get("result"))), status_code=410)
            )
        url = await run_in_threadpool(
            authentik_signin_url, services, int(result["authentik_user_id"])
        )
        log.info("%s link used for pass %s", kind, result.get("pass_id"))
        return no_store(RedirectResponse(with_return(url, target) if target else url, 303))

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
    async def welcome(request: Request, token: str, wait: int = 0):
        services = svc(request)
        info = await run_in_threadpool(
            services.odoo.call, "belfry.pass", "belfry_welcome_info", [token]
        )
        if info.get("result") != "ok":
            return no_store(HTMLResponse(problem_page(services, "not_found"), status_code=404))
        target = info.get("return_url")
        if target:
            step = welcome_next(info, wait)
            if step == "go":
                return no_store(RedirectResponse(target, status_code=303))
            if step == "wait":
                return no_store(HTMLResponse(waiting_page(token, target, wait)))
            return no_store(
                HTMLResponse(
                    spend_page(
                        f"/welcome/{token}",
                        "Signing you in",
                        f"One moment: you are signed in and go back to {_host(target)}.",
                        "Continue",
                    )
                )
            )
        return no_store(HTMLResponse(welcome_page(services, token, info)))

    @app.post("/welcome/{token}")
    async def welcome_signin(request: Request, token: str):
        return await spend(svc(request), "welcome", token)
