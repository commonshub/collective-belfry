"""Volunteers and their shifts (decision 2026-10-26): ``/shifts/<secret>`` and ``/admin/shifts``.

A volunteer (accepted from an application or added by a steward) gets a pending volunteer pass in
Odoo and a mail with a personal link to the shift page. There they sign up for shifts; with the
minimum (``belfry.volunteer_min_shifts``, 3) the pass activates like a paid one (accounts, key,
sign-in mail), without an invoice. Shifts live in Odoo (models/belfry_volunteer.py), never on the
public relay: volunteers and stewards see them, visitors do not.

``/admin/shifts`` serves the programme app's Shifts tab (village server, ``PASS_INFO_TOKEN``):
the board for a partner with the role ``volunteer`` (an active volunteer pass), ``steward``
(e-mail in the stewards group) or none.
"""

from __future__ import annotations

import datetime as dt
import hmac
import html
import logging
import threading
import urllib.parse
from collections.abc import Iterable
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from . import core
from .claim import page
from .clients.odoo import OdooError
from .core import Services

log = logging.getLogger(__name__)
E = html.escape

_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def _message(exc: OdooError) -> str:
    text = (exc.data.get("message") or exc.message or "").strip()
    return text.splitlines()[0][:300] if text else "Odoo refused the request."


# -- activation ----------------------------------------------------------------------------------


def activate_volunteer(svc: Services, pass_id: int) -> str:
    """Activate a pending volunteer pass that has its minimum of shifts. Returns ``activated``,
    ``already_active``, ``not_ready``, ``not_found`` or ``not_volunteer``."""
    with _locks_guard:
        lock = _locks.setdefault(pass_id, threading.Lock())
    with lock:
        return _activate_volunteer(svc, pass_id)


def _activate_volunteer(svc: Services, pass_id: int) -> str:
    row = svc.odoo.read_one("belfry.pass", pass_id, core.PASS_FIELDS)
    if row is None:
        return "not_found"
    if row.get("tier") != "volunteer":
        return "not_volunteer"
    if row.get("state") == "active":
        return "already_active"
    if row.get("state") != "pending":
        return f"already_{row.get('state')}"
    partner_id = core.m2o_id(row.get("partner_id"))
    info = svc.odoo.call("belfry.shift", "belfry_volunteer_of_partner", [partner_id])
    if not info or info["count"] < info["min_shifts"]:
        return "not_ready"
    partner = svc.odoo.read(
        "res.partner",
        [partner_id],
        [
            "name",
            "email",
            "belfry_member_number",
            "belfry_authentik_user_id",
            "belfry_npub",
            "belfry_key_version",
            "belfry_key_custody",
        ],
    )[0]
    date_start = core.odoo_date(row.get("date_start")) or svc.settings.event_start
    date_end = core.odoo_date(row.get("date_end")) or (
        date_start + dt.timedelta(days=svc.settings.volunteer_days - 1)
    )
    result = core._finish_activation(svc, row, partner, "volunteer", None, date_start, date_end, {})
    log.info("volunteer pass %s activated after %s shifts", pass_id, info["count"])
    return result


# -- shift page ----------------------------------------------------------------------------------


def _local(svc: Services, value: str) -> dt.datetime:
    utc = dt.datetime.fromisoformat(value).replace(tzinfo=dt.UTC)
    return utc.astimezone(ZoneInfo(svc.settings.programme_timezone))


def board_html(svc: Services, shifts: Iterable[dict[str, Any]], action: str) -> str:
    """The shifts by day; ``action`` is the form path prefix (``/shifts/<token>``)."""
    days: dict[str, list[str]] = {}
    for s in shifts:
        start, end = _local(svc, s["start"]), _local(svc, s["end"])
        day = start.strftime("%A %-d %B")
        left = s["capacity"] - s["taken"]
        people = ", ".join(s.get("people") or []) or "nobody yet"
        if s.get("mine"):
            button = (
                f'<form method="post" action="{E(action)}/{s["id"]}/leave">'
                '<button class="link" type="submit">Leave</button></form>'
            )
            badge = '<span class="mine">Yours</span>'
        elif left > 0:
            button = (
                f'<form method="post" action="{E(action)}/{s["id"]}/signup">'
                '<button class="small-btn" type="submit">Sign up</button></form>'
            )
            badge = f'<span class="muted">{left} of {s["capacity"]} free</span>'
        else:
            button, badge = "", '<span class="muted">full</span>'
        place = f" · {E(s['place'])}" if s.get("place") else ""
        desc = f'<p class="muted small">{E(s["description"])}</p>' if s.get("description") else ""
        days.setdefault(day, []).append(
            f'<li class="shift"><div><strong>{start:%H:%M}–{end:%H:%M} {E(s["title"])}</strong>'
            f"{place}<br>{badge} · <span class='muted small'>{E(people)}</span>{desc}</div>"
            f"{button}</li>"
        )
    if not days:
        return '<p class="muted">No shifts yet. The stewards add them; come back soon.</p>'
    return "".join(
        f'<h2 class="day">{E(day)}</h2><ul class="shifts">{"".join(items)}</ul>'
        for day, items in days.items()
    )


STYLE = """<style>
 .shifts { list-style: none; padding: 0; margin: 0 0 16px; display: grid; gap: 10px; }
 .shift { display: flex; gap: 12px; justify-content: space-between; align-items: flex-start; background: var(--osv-surface); border: 1px solid var(--osv-border); border-radius: 10px; padding: 12px 14px; }
 .shift form { flex: none; } .day { font-size: 1.05rem; margin: 22px 0 8px; }
 .small-btn { border: 0; border-radius: 8px; padding: 8px 12px; font: inherit; font-weight: 600; background: var(--osv-accent); color: var(--osv-accent-foreground, #1a231d); cursor: pointer; }
 .link { background: none; border: 0; color: var(--osv-accent); font: inherit; cursor: pointer; padding: 8px 0; }
 .mine { color: var(--osv-accent); font-weight: 600; } .small { font-size: 0.85rem; }
 .progress { font-size: 1.1rem; }
</style>"""

MESSAGES = {
    "signup": "You are signed up.",
    "leave": "You left the shift.",
    "active": "That was your last needed shift: your volunteer pass is active. Your sign-in link "
    "is on its way by mail.",
}


def shift_page(
    svc: Services, token: str, info: dict[str, Any], shifts: list[dict], msg: str, err: str
) -> str:
    count, minimum = info["count"], info["min_shifts"]
    if info["state"] == "active":
        status = (
            f'<p class="progress">Your volunteer pass is <strong>active</strong>. You have '
            f"{count} shift{'s' if count != 1 else ''}.</p>"
        )
    else:
        need = max(minimum - count, 0)
        status = (
            f'<p class="progress"><strong>{count} of {minimum}</strong> shifts chosen. '
            f"{f'{need} more and' if need else 'With that'} your volunteer pass becomes active.</p>"
        )
    note = f'<p class="ok">{E(MESSAGES.get(msg, ""))}</p>' if msg in MESSAGES else ""
    bad = f'<p class="bad">{E(err)}</p>' if err else ""
    body = f"""<div class="card">
<h1>Your shifts, {E(info["name"].split(" ")[0] if info["name"] else "")}</h1>
{status}
<p class="muted small">You can leave a shift until {info["change_hours"]} hours before it starts.
Times are in Brussels time. Keep this page's link private: it is your shift page.</p>
{note}{bad}
</div>
{board_html(svc, shifts, f"/shifts/{token}")}
{STYLE}"""
    return page("Your volunteer shifts", body)


# -- API -----------------------------------------------------------------------------------------


def role_of(svc: Services, partner_id: int) -> tuple[str | None, dict[str, Any] | None]:
    """``volunteer`` (active volunteer pass), ``steward`` (e-mail in the stewards group) or None."""
    info = svc.odoo.call("belfry.shift", "belfry_volunteer_of_partner", [partner_id])
    if info and info.get("state") == "active":
        return "volunteer", info
    partner = svc.odoo.read("res.partner", [partner_id], ["email"])
    email = (partner[0].get("email") or "").lower() if partner else ""
    if email:
        try:
            stewards = {
                e.lower() for e in svc.authentik.group_member_emails(svc.settings.steward_group)
            }
        except Exception:  # noqa: BLE001 - no steward role when authentik does not answer
            log.exception("shifts: could not list the stewards")
            stewards = set()
        if email in stewards:
            return "steward", info
    return None, info


def signup(svc: Services, partner_id: int, shift_id: int) -> dict[str, Any]:
    info = svc.odoo.call("belfry.shift", "belfry_shift_signup", [partner_id, shift_id])
    if info.get("ready"):
        info["activation"] = activate_volunteer(svc, info["pass_id"])
    return info


def leave(svc: Services, partner_id: int, shift_id: int) -> dict[str, Any]:
    try:
        stewards = svc.authentik.group_member_emails(svc.settings.steward_group)
    except Exception:  # noqa: BLE001 - the mail to the stewards is best effort
        stewards = []
    return svc.odoo.call("belfry.shift", "belfry_shift_leave", [partner_id, shift_id, stewards])


def register(app: FastAPI) -> None:
    """Mount /shifts and /admin/shifts (create_app calls this)."""

    def svc(request: Request) -> Services:
        return request.app.state.services

    def volunteer_of(services: Services, token: str) -> dict[str, Any]:
        info = services.odoo.call("belfry.shift", "belfry_volunteer_by_token", [token])
        if not info:
            raise HTTPException(status_code=404, detail="no such shift page")
        return info

    @app.get("/shifts/{token}", response_class=HTMLResponse)
    async def shifts(request: Request, token: str, msg: str = "", err: str = ""):
        services = svc(request)
        info = await run_in_threadpool(volunteer_of, services, token)
        board = await run_in_threadpool(
            services.odoo.call, "belfry.shift", "belfry_shift_board", [info["partner_id"]]
        )
        response = HTMLResponse(shift_page(services, token, info, board, msg, err))
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.post("/shifts/{token}/{shift_id}/{action}")
    async def shift_action(request: Request, token: str, shift_id: int, action: str):
        if action not in ("signup", "leave"):
            raise HTTPException(status_code=404, detail="unknown action")
        services = svc(request)
        info = await run_in_threadpool(volunteer_of, services, token)
        run = signup if action == "signup" else leave
        try:
            result = await run_in_threadpool(run, services, info["partner_id"], shift_id)
        except OdooError as exc:
            query = urllib.parse.urlencode({"err": _message(exc)})
            return RedirectResponse(f"/shifts/{token}?{query}", status_code=303)
        msg = "active" if result.get("activation") == "activated" else action
        return RedirectResponse(f"/shifts/{token}?msg={msg}", status_code=303)

    def require_token(request: Request) -> None:
        expected = svc(request).settings.pass_info_token
        if not expected:
            raise HTTPException(status_code=503, detail="pass info not configured")
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not given or not hmac.compare_digest(given, expected):
            raise HTTPException(status_code=401, detail="invalid token")

    @app.get("/admin/shifts")
    async def admin_shifts(request: Request, partner: int) -> dict[str, Any]:
        """The Shifts tab: shifts with who signed up, for volunteers and stewards only."""
        require_token(request)
        services = svc(request)
        role, info = await run_in_threadpool(role_of, services, partner)
        if role is None:
            return {"role": None, "shifts": []}
        board = await run_in_threadpool(
            services.odoo.call, "belfry.shift", "belfry_shift_board", [partner]
        )
        return {
            "role": role,
            "count": info["count"] if info else 0,
            "min_shifts": info["min_shifts"] if info else None,
            "change_hours": info["change_hours"] if info else None,
            "shifts": board,
        }

    @app.post("/admin/shifts/{shift_id}/{action}")
    async def admin_shift_action(
        request: Request, shift_id: int, action: str, partner: int
    ) -> dict[str, Any]:
        """Sign up or leave from the Shifts tab (volunteers with an active pass)."""
        require_token(request)
        if action not in ("signup", "leave"):
            raise HTTPException(status_code=404, detail="unknown action")
        services = svc(request)
        run = signup if action == "signup" else leave
        try:
            info = await run_in_threadpool(run, services, partner, shift_id)
        except OdooError as exc:
            return {"result": "refused", "message": _message(exc)}
        return {"result": "ok", "count": info["count"], "min_shifts": info["min_shifts"]}
