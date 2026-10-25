"""Feedback from participants and testers: ``/feedback``.

Every app links here (the app bar's "Feedback", the footer of these pages) with ``?from=<page>``.
The form asks what happened and what was expected, with an optional screenshot; it works without
signing in. When the pass page's session cookie is there (mypass.py), the feedback names the
participant, otherwise an optional e-mail address. Stored in Odoo (``belfry.feedback``) and
mailed to every member of the stewards group; stewards read it on the steward pages.
"""

from __future__ import annotations

import base64
import html
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from . import mypass
from .claim import page
from .core import Services

log = logging.getLogger(__name__)
E = html.escape

SCREENSHOT_TYPES = {"image/png", "image/jpeg", "image/webp"}
SCREENSHOT_MAX_BYTES = 5 * 1024 * 1024


class FeedbackError(Exception):
    pass


def _from_url(value: str) -> str:
    value = (value or "").strip()[:500]
    return value if value.startswith(("https://", "http://localhost")) else ""


def form_page(
    from_url: str, signed_in: bool, error: str = "", values: dict[str, str] | None = None
) -> str:
    v = values or {}
    who = (
        '<p class="muted">You are signed in on the pass page, so the stewards see who sent this.</p>'
        if signed_in
        else "<label>Your e-mail (optional, if you want an answer)"
        f'<input type="email" name="email" maxlength="200" value="{E(v.get("email", ""))}"></label>'
    )
    err = f'<p class="bad">{E(error)}</p>' if error else ""
    where = f'<p class="muted small">About: <code>{E(from_url)}</code></p>' if from_url else ""
    body = f"""<div class="card">
<h1>Feedback</h1>
<p class="muted">Something did not work, was confusing, or you liked it? Tell the stewards. A few
words are enough.</p>
{err}
<form method="post" action="/feedback" enctype="multipart/form-data" class="fb">
<input type="hidden" name="from" value="{E(from_url)}">
{where}
<label>What happened? <textarea name="happened" rows="5" maxlength="4000" required>{E(v.get("happened", ""))}</textarea></label>
<label>What did you expect? (optional) <textarea name="expected" rows="3" maxlength="4000">{E(v.get("expected", ""))}</textarea></label>
<label>Screenshot (optional, PNG, JPEG or WebP, up to 5 MB) <input type="file" name="screenshot" accept="image/png,image/jpeg,image/webp"></label>
{who}
<label class="hp" aria-hidden="true">Leave this empty <input type="text" name="website" tabindex="-1" autocomplete="off"></label>
<button class="btn" type="submit">Send feedback</button>
</form>
</div>
<style>
 .fb {{ display: grid; gap: 14px; margin-top: 16px; }}
 .fb label {{ display: grid; gap: 6px; min-width: 0; }}
 .fb textarea, .fb input {{ box-sizing: border-box; width: 100%; max-width: 100%; }}
 .fb textarea, .fb input[type=email], .fb input[type=file] {{ padding: 10px 12px; border-radius: 8px; border: 1px solid var(--osv-border-strong); background: var(--osv-surface-deep); color: var(--osv-foreground); font: inherit; }}
 .fb button {{ border: 0; font: inherit; font-weight: 600; cursor: pointer; justify-self: start; }}
 .hp {{ position: absolute; left: -9999px; }} .small {{ font-size: 0.85rem; }}
</style>"""
    return page("Feedback", body)


def thanks_page(from_url: str) -> str:
    back = (
        f'<p><a class="btn" href="{E(from_url)}">Back to where you were</a></p>' if from_url else ""
    )
    body = f"""<div class="card"><h1>Thank you</h1>
<p>Your feedback went to the stewards.</p>{back}</div>"""
    return page("Feedback sent", body)


def submit(svc: Services, vals: dict[str, Any], shot: bytes | None, shot_type: str) -> int:
    happened = (vals.get("happened") or "").strip()
    if not happened:
        raise FeedbackError("Please write what happened.")
    if shot is not None and (shot_type not in SCREENSHOT_TYPES or len(shot) > SCREENSHOT_MAX_BYTES):
        raise FeedbackError("The screenshot must be a PNG, JPEG or WebP image of at most 5 MB.")
    try:
        stewards = svc.authentik.group_member_emails(svc.settings.steward_group)
    except Exception:  # noqa: BLE001 - feedback counts even when the mail cannot be addressed
        log.exception("feedback: could not list the stewards' e-mail addresses")
        stewards = []
    b64 = base64.b64encode(shot).decode() if shot else False
    feedback_id = svc.odoo.call(
        "belfry.feedback",
        "belfry_feedback_create",
        [{**vals, "happened": happened}, stewards, b64, shot_type if shot else False],
    )
    log.info(
        "feedback %s (partner %s, screenshot %s)", feedback_id, vals.get("partner_id"), bool(shot)
    )
    return int(feedback_id)


def register(app: FastAPI) -> None:
    """Mount /feedback (create_app calls this)."""

    def svc(request: Request) -> Services:
        return request.app.state.services

    def partner_of(request: Request, services: Services) -> int | None:
        if not mypass.enabled(services):
            return None
        sess = mypass._verify(services, request.cookies.get(mypass.SESSION_COOKIE), "session")
        return int(sess["partner"]) if sess else None

    @app.get("/feedback", response_class=HTMLResponse)
    async def feedback_form(request: Request):
        services = svc(request)
        from_url = _from_url(
            request.query_params.get("from", "") or request.headers.get("referer", "")
        )
        return HTMLResponse(form_page(from_url, partner_of(request, services) is not None))

    @app.post("/feedback", response_class=HTMLResponse)
    async def feedback_submit(request: Request):
        services = svc(request)
        form = await request.form()
        text = {
            k: str(form.get(k) or "") for k in ("happened", "expected", "email", "from", "website")
        }
        from_url = _from_url(text["from"])
        if text["website"]:  # the hidden field: only bots fill it in
            log.info("feedback: honeypot filled, dropped")
            return HTMLResponse(thanks_page(from_url))
        partner_id = partner_of(request, services)
        shot, shot_type = None, ""
        upload = form.get("screenshot")
        if upload is not None and hasattr(upload, "read") and getattr(upload, "filename", ""):
            shot = await upload.read(SCREENSHOT_MAX_BYTES + 1)
            shot_type = getattr(upload, "content_type", "") or ""
            if not shot:
                shot = None
        vals = {
            "happened": text["happened"],
            "expected": text["expected"],
            "page_url": from_url,
            "user_agent": request.headers.get("user-agent", "")[:300],
        }
        if partner_id:
            vals["partner_id"] = partner_id
        elif text["email"].strip():
            vals["email"] = text["email"].strip()[:200]
        try:
            await run_in_threadpool(submit, services, vals, shot, shot_type)
        except FeedbackError as exc:
            return HTMLResponse(
                form_page(from_url, partner_id is not None, str(exc), text), status_code=400
            )
        return HTMLResponse(thanks_page(from_url))
