"""Applying to join: ``/apply`` (decision 2026-10-26).

Besides invites, people can ask to join: name, e-mail, a few words of motivation, and whether
they want to volunteer. Stored in Odoo (``belfry.application``), confirmed to the applicant and
mailed to every steward; stewards answer on the steward pages with an invite (mailed to the
applicant, who then picks and pays a pass) or a friendly no. Honeypot and the proxy's rate limit
like the feedback form.
"""

from __future__ import annotations

import html
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from . import mypass
from .claim import page
from .clients.odoo import OdooError
from .core import Services

log = logging.getLogger(__name__)
E = html.escape


class ApplyError(Exception):
    pass


def form_page(
    error: str = "",
    values: dict[str, str] | None = None,
    community: str = "",
    volunteers: bool = True,
) -> str:
    name = E(community or "this community")
    v = values or {}
    err = f'<p class="bad">{E(error)}</p>' if error else ""
    volunteer = " checked" if v.get("kind") == "volunteer" else ""
    # volunteer shifts are the Odoo add-on's (OSV); a belfry on its own register has none
    shifts = (
        f'<label class="check"><input type="checkbox" name="kind" value="volunteer"{volunteer}>'
        "I would like to volunteer (volunteers get a two-week pass for at least three shifts)"
        "</label>"
        if volunteers
        else ""
    )
    body = f"""<div class="card">
<h1>Apply to join</h1>
<p class="muted">{name} grows through people who know each other. No invite yet? Tell its members
who you are and why you would like to join; you get the answer by mail, with an invite if they
say yes.</p>
{err}
<form method="post" action="/apply" class="fb">
<label>Your name <input type="text" name="name" maxlength="200" required value="{E(v.get("name", ""))}"></label>
<label>Your e-mail <input type="email" name="email" maxlength="200" required value="{E(v.get("email", ""))}"></label>
<label>Why would you like to join? <textarea name="motivation" rows="6" maxlength="4000" required>{E(v.get("motivation", ""))}</textarea></label>
{shifts}
<label class="hp" aria-hidden="true">Leave this empty <input type="text" name="website" tabindex="-1" autocomplete="off"></label>
<p class="muted small">The admins (and, when the community accepts people by vouching, its
members) see your name and what you write here; your e-mail address only the admins.
See <a href="/privacy">privacy</a>.</p>
<button class="btn" type="submit">Send application</button>
</form>
</div>
<style>
 .fb {{ display: grid; gap: 14px; margin-top: 16px; }}
 .fb label {{ display: grid; gap: 6px; min-width: 0; }}
 .fb label.check {{ display: flex; gap: 10px; align-items: flex-start; }}
 .fb textarea, .fb input[type=text], .fb input[type=email] {{ box-sizing: border-box; width: 100%; max-width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid var(--osv-border-strong); background: var(--osv-surface-deep); color: var(--osv-foreground); font: inherit; }}
 .fb input[type=checkbox] {{ margin-top: 4px; }}
 .fb button {{ border: 0; font: inherit; font-weight: 600; cursor: pointer; justify-self: start; }}
 .hp {{ position: absolute; left: -9999px; }} .small {{ font-size: 0.85rem; }}
</style>"""
    return page(f"Apply to join {community or 'this community'}", body)


def thanks_page() -> str:
    body = """<div class="card"><h1>Thank you</h1>
<p>Your request is in. You get a confirmation by mail now, and the answer by mail too.</p></div>"""
    return page("Application sent", body)


def submit(svc: Services, vals: dict[str, str]) -> int:
    try:
        from . import console

        stewards = sorted(console.admin_emails(svc))
    except Exception:  # noqa: BLE001 - the application counts even when the mail cannot be addressed
        log.exception("apply: could not list the stewards' e-mail addresses")
        stewards = []
    try:
        app_id = svc.odoo.call("belfry.application", "belfry_application_create", [vals, stewards])
    except OdooError as exc:
        # Odoo's UserError text is meant for the applicant (missing name, bad address)
        raise ApplyError(mypass._odoo_message(exc)) from exc
    log.info("application %s (%s)", app_id, vals.get("kind"))
    return int(app_id)


def _ctx(svc: Services) -> dict[str, Any]:
    from . import invite

    return {"community": svc.settings.community_name or "", "volunteers": not invite.own(svc)}


def register(app: FastAPI) -> None:
    """Mount /apply (create_app calls this)."""

    def svc(request: Request) -> Services:
        return request.app.state.services

    @app.get("/apply", response_class=HTMLResponse)
    async def apply_form(request: Request):
        return HTMLResponse(form_page(**_ctx(svc(request))))

    @app.post("/apply", response_class=HTMLResponse)
    async def apply_submit(request: Request):
        form = await request.form()
        text = {k: str(form.get(k) or "") for k in ("name", "email", "motivation", "website")}
        if text["website"]:  # the hidden field: only bots fill it in
            log.info("apply: honeypot filled, dropped")
            return HTMLResponse(thanks_page())
        vals = {
            "name": text["name"].strip(),
            "email": text["email"].strip(),
            "motivation": text["motivation"].strip(),
            "kind": "volunteer" if form.get("kind") == "volunteer" else "visitor",
        }
        if not vals["name"] or not vals["email"] or not vals["motivation"]:
            return HTMLResponse(
                form_page(
                    "Please fill in your name, e-mail and motivation.", vals, **_ctx(svc(request))
                ),
                status_code=400,
            )
        try:
            await run_in_threadpool(submit, svc(request), vals)
        except ApplyError as exc:
            return HTMLResponse(
                form_page(str(exc), vals, **_ctx(svc(request))),
                status_code=400,
            )
        return HTMLResponse(thanks_page())
