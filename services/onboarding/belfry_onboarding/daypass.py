"""Free day passes for week-pass holders (decided 2026-11-04). On the pass page (/me) a
participant with an active week pass picks an event day and gets a day pass for it, free, at
most one per day (belfry_pass models/belfry_day_pass.py holds the rules). The day pass is the
holder's own: same account and key, valid that day only, its own wallet credential and status
bit, no tokens (the week pass has them) and no activation mail (the holder is signed in on /me).
On its day /me shows it instead of the week pass (mypass.partner_pass). Revoking the week pass
revokes its day passes (core._close_pass)."""

from __future__ import annotations

import datetime as dt
import html
import logging
import threading
from typing import Any

from . import core
from .clients.odoo import OdooError
from .core import Services

log = logging.getLogger(__name__)

E = html.escape
PARTNER_FIELDS = [
    "name",
    "email",
    "belfry_member_number",
    "belfry_authentik_user_id",
    "belfry_npub",
    "belfry_key_version",
    "belfry_key_custody",
]

_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


class DayPassError(Exception):
    pass


# Odoo's refusals (belfry_day_pass.py) the page may show; anything else reads as GENERIC.
REFUSALS = {
    "already": "You have a day pass for that day already.",
    "over": "That day is over.",
    "day": "Pick a day of the event.",
    "week": "Free day passes come with an active week pass.",
}
GENERIC = "That did not work. Try again, or ask a steward."


def refusal_code(message: str) -> str:
    return next((code for code, text in REFUSALS.items() if text == message), "other")


def day_passes(svc: Services, partner_id: int) -> dict[str, Any] | None:
    """Odoo's view of the partner's free day passes, or None when Odoo refuses."""
    try:
        return svc.odoo.call("belfry.pass", "belfry_free_day_passes", [partner_id])
    except OdooError as exc:
        log.warning("free day passes for partner %s: %s", partner_id, exc)
        return None


def take(svc: Services, partner_id: int, day: str) -> int:
    """Create the free day pass for ``day`` and activate it. Returns the pass id; raises
    DayPassError with Odoo's reason when the day cannot be taken. A pending pass for that day
    (an activation that failed) is activated instead."""
    info = day_passes(svc, partner_id) or {}
    held = next((d for d in info.get("days") or [] if d.get("date") == day), None)
    if held and held.get("state") == "taken" and held.get("pass_state") == "pending":
        result = activate(svc, held["pass_id"])
        log.info("free day pass %s of partner %s finished: %s", held["pass_id"], partner_id, result)
        return held["pass_id"]
    try:
        pass_id = svc.odoo.call("belfry.pass", "belfry_free_day_pass_create", [partner_id, day])
    except OdooError as exc:
        text = (exc.data.get("message") or exc.message or "").strip()
        raise DayPassError(text.splitlines()[0][:300] if text else "Odoo refused.") from exc
    result = activate(svc, pass_id)
    log.info("free day pass %s for partner %s on %s: %s", pass_id, partner_id, day, result)
    return pass_id


def activate(svc: Services, pass_id: int) -> str:
    """Activate a pending free day pass. Returns ``activated``, ``already_active``,
    ``already_<state>``, ``not_found`` or ``not_free``. ``reconcile`` does not know these
    passes (no invoice): a failed activation is retried by taking the day again, which finds
    the pending pass (the page shows it as taken and offers to activate it)."""
    with _locks_guard:
        lock = _locks.setdefault(pass_id, threading.Lock())
    with lock:
        row = svc.odoo.read_one("belfry.pass", pass_id, core.PASS_FIELDS)
        if row is None:
            return "not_found"
        if row.get("tier") != "day" or not core.m2o_id(row.get("free_of_pass_id")):
            return "not_free"
        if row.get("state") == "active":
            return "already_active"
        if row.get("state") != "pending":
            return f"already_{row.get('state')}"
        partner_id = core.m2o_id(row.get("partner_id"))
        partner = svc.odoo.read("res.partner", [partner_id], PARTNER_FIELDS)[0]
        day = core.odoo_date(row.get("date_start"))
        if day is None:
            return "not_free"
        return core._finish_activation(
            svc, row, partner, "day", 1, day, day, {}, mint_tokens=False, login_link=False
        )


def section(info: dict[str, Any] | None, claim_urls: dict[int, str], msg: str = "") -> str:
    """The "Free day passes" card on /me; empty for participants without a week pass."""
    if not info or not info.get("eligible"):
        return ""
    rows = []
    for d in info.get("days") or []:
        day = dt.date.fromisoformat(d["date"])
        label = f"{day:%A %d %B}"
        if d["state"] == "taken":
            url = claim_urls.get(d.get("pass_id") or 0)
            if d.get("pass_state") == "pending":
                action = (
                    f'<form method="post" action="/me/day-passes"><input type="hidden" '
                    f'name="day" value="{E(d["date"])}"><button class="small-btn" type="submit">'
                    "Finish</button></form>"
                )
            elif url:
                action = f'<a class="small" href="{E(url)}">Add to your wallet</a>'
            else:
                action = ""
            rows.append(
                f'<li><span>{E(label)}</span><span class="ok small">Day pass</span>{action}</li>'
            )
        elif d["state"] == "available":
            rows.append(
                f'<li><span>{E(label)}</span><form method="post" action="/me/day-passes">'
                f'<input type="hidden" name="day" value="{E(d["date"])}">'
                '<button class="small-btn" type="submit">Get a free day pass</button></form></li>'
            )
        else:
            rows.append(
                f'<li class="muted"><span>{E(label)}</span><span class="small">over</span></li>'
            )
    flash = f'<p class="ok">{E(msg)}</p>' if msg else ""
    return f"""<div class="card day-passes" id="day-passes">
<h2>Free day passes</h2>
<p class="muted">Your week pass comes with a free day pass for any day of the village, one per
day. On that day this page shows the day pass at the door.</p>
{flash}<ul class="day-list">{"".join(rows)}</ul>
</div>"""


STYLE = """<style>
 .day-list { list-style: none; padding: 0; margin: 12px 0 0; display: grid; gap: 10px; }
 .day-list li { display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
   border-top: 1px solid var(--osv-border); padding-top: 10px; }
 .day-list li > span:first-child { flex: 1 1 160px; }
 .day-list form { margin: 0; }
</style>"""
