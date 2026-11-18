"""Applying to join: ``/apply`` (decision 2026-10-26).

Besides invites, people can ask to join: name, e-mail, a few words of motivation, and whether
they want to volunteer. Stored in Odoo (``belfry.application``), confirmed to the applicant and
mailed to every steward; stewards answer on the steward pages with an invite (mailed to the
applicant, who then picks and pays a pass) or a friendly no. Honeypot and the proxy's rate limit
like the feedback form.

When the belfry has a charter (charter.py), the form lists its agreements with a checkbox each
and takes the request only with every one ticked; the ticked ids are kept on the application
(``agreed``, the belfry's own register only) and signed for the person once they are a member
(guestapply.after_invite).
"""

from __future__ import annotations

import html
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
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
    agreements: list[dict[str, Any]] | None = None,
    ticked: list[str] | None = None,
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
    ticked = ticked or []
    rules = "".join(
        f'<label class="check"><input type="checkbox" name="agreed" value="{E(r["id"])}" required'
        f"{' checked' if r['id'] in ticked else ''}><span><b>{E(r['title'])}</b>"
        + (f'<br><span class="muted">{E(r["about"])}</span>' if r.get("about") else "")
        + "</span></label>"
        for r in agreements or []
    )
    charter = (
        f'<fieldset class="charter"><legend>Our charter: tick each agreement</legend>{rules}'
        "</fieldset>"
        if rules
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
{charter}
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
 .fb fieldset.charter {{ display: grid; gap: 10px; border: 1px solid var(--osv-border-strong); border-radius: 8px; padding: 12px 14px; margin: 0; }}
 /* round ticks, like the chat's to-dos (Xavier, 2026-11-18) */
 .fb fieldset.charter input[type=checkbox] {{ appearance: none; -webkit-appearance: none; flex: none; width: 22px; height: 22px; margin: 1px 0 0; border-radius: 50%; border: 2px solid var(--osv-border-strong); background: transparent; cursor: pointer; display: grid; place-items: center; }}
 .fb fieldset.charter input[type=checkbox]:checked {{ background: var(--osv-foreground); border-color: var(--osv-foreground); }}
 .fb fieldset.charter input[type=checkbox]:checked::after {{ content: ""; width: 10px; height: 5px; border: solid var(--osv-surface-deep, #fff); border-width: 0 0 2px 2px; transform: translateY(-1px) rotate(-45deg); }}
 .hp {{ position: absolute; left: -9999px; }} .small {{ font-size: 0.85rem; }}
</style>"""
    return page(f"Apply to join {community or 'this community'}", body)


def thanks_page() -> str:
    body = """<div class="card"><h1>Thank you</h1>
<p>Your request is in. You get a confirmation by mail now, and the answer by mail too.</p></div>"""
    return page("Application sent", body)


def submit(svc: Services, vals: dict[str, Any]) -> int:
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


def _agreements(svc: Services) -> list[dict[str, Any]]:
    """The charter's agreements, or none when the relay can't say (the form works without)."""
    from . import charter

    try:
        return charter.agreements(svc)
    except Exception as exc:  # noqa: BLE001
        log.info("apply: charter not readable: %s", exc)
        return []


def _ctx(svc: Services, agreements: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    from . import invite

    return {
        "community": svc.settings.community_name or "",
        "volunteers": not invite.own(svc),
        "agreements": agreements or [],
    }


MISSING = "Please fill in your name, e-mail and motivation."
MISSING_API = "Please give your e-mail and a few words on why you would like to join."
API = "/api/apply"
UNTICKED = "Please tick each agreement of the charter: joining means agreeing to them."


def register(app: FastAPI) -> None:
    """Mount /apply (create_app calls this)."""

    def svc(request: Request) -> Services:
        return request.app.state.services

    @app.get("/apply", response_class=HTMLResponse)
    async def apply_form(request: Request):
        s = svc(request)
        rules = await run_in_threadpool(_agreements, s)
        return HTMLResponse(form_page(**_ctx(s, rules)))

    @app.post("/apply", response_class=HTMLResponse)
    async def apply_submit(request: Request):
        from . import invite

        form = await request.form()
        text = {k: str(form.get(k) or "") for k in ("name", "email", "motivation", "website")}
        ticked = [str(v) for v in form.getlist("agreed")][:100]
        if text["website"]:  # the hidden field: only bots fill it in
            log.info("apply: honeypot filled, dropped")
            return HTMLResponse(thanks_page())
        vals = {
            "name": text["name"].strip(),
            "email": text["email"].strip(),
            "motivation": text["motivation"].strip(),
            "kind": "volunteer" if form.get("kind") == "volunteer" else "visitor",
        }
        s = svc(request)
        rules = await run_in_threadpool(_agreements, s)
        ctx = {**_ctx(s, rules), "ticked": ticked}
        if not vals["name"] or not vals["email"] or not vals["motivation"]:
            return HTMLResponse(form_page(MISSING, vals, **ctx), status_code=400)
        if {r["id"] for r in rules} - set(ticked):
            return HTMLResponse(form_page(UNTICKED, vals, **ctx), status_code=400)
        sent: dict[str, Any] = dict(vals)
        if rules and invite.own(s):  # Odoo's add-on keeps no ticks
            known = {r["id"] for r in rules}
            sent["agreed"] = [t for t in ticked if t in known]
        try:
            await run_in_threadpool(submit, s, sent)
        except ApplyError as exc:
            return HTMLResponse(form_page(str(exc), vals, **ctx), status_code=400)
        return HTMLResponse(thanks_page())

    @app.post(API)
    async def apply_api(request: Request) -> JSONResponse:
        """The chat's own form (chat.<domain>/apply; Xavier, 2026-11-18): e-mail, why, the
        charter's ticks. The name is optional: the part of the address before the @ otherwise."""
        from . import invite

        def no(error: str, message: str, status: int = 400) -> JSONResponse:
            return JSONResponse(
                {"error": error, "message": message},
                status_code=status,
                headers={"Cache-Control": "no-store"},
            )

        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return no("bad_request", "Send JSON.")
        text = {
            k: str(body.get(k) or "").strip() for k in ("name", "email", "motivation", "website")
        }
        raw = body.get("agreed")
        ticked = [str(v) for v in raw][:100] if isinstance(raw, list) else []
        if text["website"]:  # the hidden field: only bots fill it in
            log.info("apply: honeypot filled, dropped")
            return JSONResponse({"ok": True})
        if not text["email"] or not text["motivation"]:
            return no("missing", MISSING_API)
        vals = {
            "name": (text["name"] or text["email"].split("@")[0])[:200],
            "email": text["email"][:200],
            "motivation": text["motivation"][:4000],
            "kind": "volunteer" if body.get("kind") == "volunteer" else "visitor",
        }
        s = svc(request)
        rules = await run_in_threadpool(_agreements, s)
        if {r["id"] for r in rules} - set(ticked):
            return no("unticked", UNTICKED)
        sent: dict[str, Any] = dict(vals)
        if rules and invite.own(s):
            known = {r["id"] for r in rules}
            sent["agreed"] = [t for t in ticked if t in known]
        try:
            await run_in_threadpool(submit, s, sent)
        except ApplyError as exc:
            return no("refused", str(exc))
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
