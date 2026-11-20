"""Which e-mails someone gets, set from the website (docs/website-accounts.md §3).

``GET`` / ``POST /api/site/notifications`` for a site session (sitesessions.py), and the page
behind the "Change or stop these e-mails" link of the mails below
(``belfry.<domain>/notifications/<token>``: buttons, a POST changes it; fixed text only).

An item is listed only when the belfry really sends that mail to THAT account, and each has
one store, the one its sender reads:

- ``replies``: answers in the threads someone opened from the web, or commented on from the
  website (webthreads.notify, at most once an hour). Param ``belfry.notify.<partner>.replies``:
  asap (the default: what authors got before there was a setting) | daily | weekly | off.
- ``mentions``: mentions.py's own choice (``belfry.mentions.<partner>.f``), which its digest's
  footer page changes too.
- ``requests``: new requests in the requests channel, for whoever has that channel. Param
  ``belfry.notify.<partner>.requests``: off (the default) | asap (once an hour) | daily.
- ``events``: the announcements of the events someone holds a ticket for. These are follows
  (followers.py, one per event's group, made when the ticket is issued): the switch stops or
  makes them, and ``belfry.notify.<partner>.events`` = ``off`` keeps new tickets from following.

Daily and weekly ride the mentions' morning (``daily``, called by mentions.morning). No mail
goes to an address nobody proved: every recipient is a contact's own address with an active
pass. Nothing here logs an address.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import html
import logging
import time
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from . import flash, theme
from .core import Services
from .holder_invites import ApiError

log = logging.getLogger(__name__)
E = html.escape
API = "/api/site/notifications"
PREFIX = "belfry.notify."
NO_STORE = {"Cache-Control": "no-store"}
MAX_PER_MAIL = 25
DAY, WEEK = 2 * 86400, 8 * 86400  # how far back a daily / weekly mail looks at most

REPLIES = ("asap", "daily", "weekly", "off")
REQUESTS = ("asap", "daily", "off")
EVENTS = ("asap", "off")
DEFAULTS = {"replies": "asap", "requests": "off", "events": "asap"}
CHOICES = {"replies": REPLIES, "requests": REQUESTS, "events": EVENTS}


def own(svc: Services) -> bool:
    return hasattr(svc.odoo, "params_under")


def _key(partner: int, name: str) -> str:
    return f"{PREFIX}{int(partner)}.{name}"


def pref(svc: Services, partner: int | None, name: str) -> str:
    """The choice of a contact for ``replies``, ``requests`` or ``events`` (its default when
    they never chose, when there is no contact, or without the belfry's own register)."""
    if not partner or not own(svc):
        return DEFAULTS[name]
    v = svc.odoo.param(_key(partner, name), "")
    return v if v in CHOICES[name] else DEFAULTS[name]


def _stamp(svc: Services, partner: int, name: str) -> int:
    v = svc.odoo.param(_key(partner, name), "")
    return int(v) if v.isdigit() else 0


def _chosen(svc: Services, name: str) -> dict[int, str]:
    """contact → their stored value of NAME (only those who have one)."""
    out: dict[int, str] = {}
    if not own(svc):
        return out
    for k, v in svc.odoo.params_under(PREFIX).items():
        pid, _, what = k[len(PREFIX) :].partition(".")
        if what == name and pid.isdigit() and v:
            out[int(pid)] = v
    return out


# ── replies: what webthreads.notify asks ────────────────────────────────────────────────────
def catchups(svc: Services) -> dict[int, int]:
    """Contacts who went back to "asap" from a daily or weekly mail: the time their next
    hourly mail starts from (what was waiting for their next digest is not lost)."""
    return {p: int(v) for p, v in _chosen(svc, "replies_from").items() if v.isdigit()}


def caught_up(svc: Services, partners: Any) -> None:
    for p in partners:
        svc.odoo.set_param(_key(p, "replies_from"), "")


def settings_url(svc: Services, partner: int) -> str:
    base = (svc.settings.console_url or svc.settings.public_url or "").rstrip("/")
    return f"{base}/notifications/{token(svc, partner)}"


def token(svc: Services, partner: int) -> str:
    secret = (svc.settings.pass_page_secret or svc.settings.hook_token or "").encode()
    mac = hmac.new(secret, f"notify:{int(partner)}".encode(), hashlib.sha256).hexdigest()[:24]
    return f"{int(partner)}.{mac}"


def partner_of(svc: Services, tok: str) -> int | None:
    pid, _, _mac = (tok or "").partition(".")
    if not pid.isdigit():
        return None
    return int(pid) if hmac.compare_digest(token(svc, int(pid)), tok) else None


# ── what an account has ─────────────────────────────────────────────────────────────────────
def items(svc: Services, acct: dict[str, Any]) -> list[dict[str, Any]]:
    """The settings of an account (sitesessions.account): only what the belfry really mails
    to it. Without a proven address: nothing."""
    from . import mentions, sitesessions, webthreads

    if not own(svc) or not acct.get("email"):
        return []
    partner = acct["partner"]
    out: list[dict[str, Any]] = []

    def add(name: str, value: str, default: str, choices: tuple[str, ...]) -> None:
        out.append({"id": name, "value": value, "default": default, "choices": list(choices)})

    try:
        pages = bool(webthreads.sites(svc))
    except Exception:  # noqa: BLE001 — the relay can't say: not listed this time
        pages = False
    if pages:  # some channel takes threads from the web: their answers are mailed
        add("replies", pref(svc, partner, "replies"), DEFAULTS["replies"], REPLIES)
    if mentions.own(svc):
        add("mentions", mentions.prefs(svc, partner)["f"], "daily", mentions.CHOICES)
    channel = sitesessions.requests_channel(svc)
    try:
        has = bool(channel) and sitesessions.has_channel(svc, acct, channel)
    except Exception:  # noqa: BLE001
        has = False
    if has:
        add("requests", pref(svc, partner, "requests"), DEFAULTS["requests"], REQUESTS)
    add("events", pref(svc, partner, "events"), DEFAULTS["events"], EVENTS)
    return out


def change(svc: Services, acct: dict[str, Any], body: dict[str, Any]) -> list[dict[str, Any]]:
    """Set some of the account's settings ({id: choice}); 422 ``invalid`` for an id the account
    doesn't have or a choice the id doesn't take (nothing is changed then)."""
    from . import mentions

    have = {i["id"]: i for i in items(svc, acct)}
    if not body or any(
        k not in have or not isinstance(v, str) or v not in have[k]["choices"]
        for k, v in body.items()
    ):
        raise ApiError(422, "invalid", "Send settings you have, each with one of its choices.")
    partner = acct["partner"]
    for name, value in body.items():
        was = have[name]["value"]
        if value == was:
            continue
        if name == "mentions":
            mentions.save(svc, partner, f=value)
        elif name == "events":
            _set_events(svc, acct, value)
        elif name == "replies":
            _set_replies(svc, partner, was, value)
        else:  # requests: a daily mail starts from now
            svc.odoo.set_param(_key(partner, "requests_last"), str(int(time.time())))
            svc.odoo.set_param(_key(partner, name), value)
        log.info("notifications: partner %s set %s to %s", partner, name, value)
    return items(svc, acct)


def _set_replies(svc: Services, partner: int, was: str, value: str) -> None:
    from . import webthreads

    digest = ("daily", "weekly")
    if value in digest and was not in digest:
        # their first digest starts where the hourly mails stopped
        last = webthreads._param(svc, "belfry.web.notified")
        start = int(last) if last.isdigit() else int(time.time())
        svc.odoo.set_param(_key(partner, "replies_last"), str(start))
    if value == "asap" and was in digest:
        svc.odoo.set_param(
            _key(partner, "replies_from"), str(_stamp(svc, partner, "replies_last") or "")
        )
    elif value != "asap":
        svc.odoo.set_param(_key(partner, "replies_from"), "")
    svc.odoo.set_param(_key(partner, "replies"), value)


def _ticket_groups(svc: Services, key: str) -> list[str]:
    """The groups of the events KEY holds a ticket for."""
    from . import events, sitesessions

    found = events.groups(svc, [{"kinds": [9000], "#p": [key]}])
    ds = sorted(
        {
            str(events.tag(e, "h"))[3:]
            for e in found
            if str(events.tag(e, "h") or "").startswith("ev-") and events.tag(e, "ticket")
        }
    )
    out = []
    for d in ds[: sitesessions.TICKETS_MAX]:
        ev = events.load(svc, d)
        if ev is not None and key in events.people(svc, ev).holders:
            out.append(ev.group)
    return out


def _set_events(svc: Services, acct: dict[str, Any], value: str) -> None:
    partner = acct["partner"]

    def follow(channel: str, choice: str) -> None:
        svc.odoo.call("belfry.follower", "belfry_follow_member_set", [partner, channel, choice])

    try:
        if value == "off":
            rows = svc.odoo.call("belfry.follower", "belfry_follow_member_list", [partner])
            for f in rows:
                if str(f.get("channel") or "").startswith("ev-") and f.get("state") == "active":
                    follow(f["channel"], "stop")
        else:
            for group in _ticket_groups(svc, acct["pubkey"]):
                follow(group, "asap")
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 — nothing is stored: the switch reads as before
        log.warning("notifications: events not set: %s", type(exc).__name__)
        raise ApiError(503, "unavailable", "That did not work just now. Try again.") from exc
    svc.odoo.set_param(_key(partner, "events"), value)


# ── new requests ────────────────────────────────────────────────────────────────────────────
def _new_requests(svc: Services, since: int, until: int) -> tuple[dict[str, Any], list[dict]]:
    """(the requests channel's snapshot, its public threads opened in (since, until])."""
    from . import webrequests

    snap = webrequests.snapshot(svc)
    new = [
        t for t in snap["threads"] if t["public"] and since < int(t["root"]["created_at"]) <= until
    ]
    return snap, sorted(new, key=lambda t: t["number"])


def _mail_requests(svc: Services, partners: dict[int, int], until: int, often: str) -> int:
    """One mail per contact of PARTNERS (contact → since) with the requests opened since then
    that are not their own. Only to someone who has the requests channel now."""
    from . import sitesessions, webrequests, webthreads

    channel = sitesessions.requests_channel(svc)
    if not channel or not partners:
        return 0
    snap, new = _new_requests(svc, min(partners.values()), until)
    if not new:
        return 0
    list_url = webthreads.list_url(svc, snap["slug"], snap["site"])
    sent = 0
    for partner, since in partners.items():
        acct = sitesessions.account(svc, partner)
        if acct is None or not acct["email"] or not sitesessions.has_channel(svc, acct, channel):
            continue
        mine = [
            t
            for t in new
            if int(t["root"]["created_at"]) > since and not webrequests.is_mine(svc, t, acct)
        ]
        if not mine:
            continue
        lines: list[Any] = [
            "A new request:" if len(mine) == 1 else f"{len(mine)} new requests:",
        ]
        for t in mine[-MAX_PER_MAIL:]:
            root = webrequests._root(snap, t)
            who = webrequests._person(svc, snap, root)["name"]
            url = sitesessions._request_url(svc, snap, t)
            lines.append([f"{who}: ", (webthreads._title(root)[:150], url)])
        first = webthreads._title(webrequests._root(snap, mine[0]))[:150]
        url = settings_url(svc, partner)
        webthreads._mailer(svc).send(
            acct["email"],
            f"New request: {first}" if len(mine) == 1 else f"{len(mine)} new requests",
            lines,
            (list_url, "Open the requests"),
            f"You asked to hear about new requests ({often}).",
            headers={"List-Unsubscribe": f"<{url}>"},
            after=[[("Change or stop these e-mails", url)]],
        )
        sent += 1
    return sent


def hourly(svc: Services, since: int, until: int) -> int:
    """With webthreads.notify's round: new requests, to those who chose "asap"."""
    if not own(svc):
        return 0
    asap = {p: since for p, v in _chosen(svc, "requests").items() if v == "asap"}
    n = _mail_requests(svc, asap, until, "gathered, at most once an hour")
    if n:
        log.info("notifications: %d mail(s) about new requests", n)
    return n


# ── the daily round (mentions.morning) ──────────────────────────────────────────────────────
def daily(svc: Services, now: dt.datetime) -> dict[str, int]:
    """Once a day at the digest hour: the replies of those who chose daily (weekly: on
    Mondays), and the new requests of those who chose daily."""
    from . import webthreads

    if not own(svc):
        return {"replies": 0, "requests": 0}
    ts = int(now.timestamp())
    due: dict[int, tuple[int, str]] = {}  # contact → (since, daily | weekly)
    for p, v in _chosen(svc, "replies").items():
        if v == "daily" or (v == "weekly" and now.weekday() == 0):
            window = WEEK if v == "weekly" else DAY
            due[p] = (max(_stamp(svc, p, "replies_last"), ts - window), v)
    out = {"replies": 0, "requests": 0}
    if due:
        news = webthreads.reply_news(svc, min(s for s, _ in due.values()), ts)
        names = webthreads.reply_names(svc, news)
        for email, rec in news.items():
            partner = rec["partner"]
            if partner not in due:
                continue
            since, often = due[partner]
            said = "once a day" if often == "daily" else "once a week, on Mondays"
            if webthreads.send_replies(svc, email, rec, since, names, said):
                out["replies"] += 1
        for p in due:
            svc.odoo.set_param(_key(p, "replies_last"), str(ts))
    asked = {
        p: max(_stamp(svc, p, "requests_last"), ts - DAY)
        for p, v in _chosen(svc, "requests").items()
        if v == "daily"
    }
    if asked:
        out["requests"] = _mail_requests(svc, asked, ts, "once a day")
        for p in asked:
            svc.odoo.set_param(_key(p, "requests_last"), str(ts))
    if any(out.values()):
        log.info(
            "notifications: daily, %(replies)d about replies, %(requests)d about requests", out
        )
    return out


# ── the API and the page ────────────────────────────────────────────────────────────────────
TITLE = {
    "replies": "Answers to your requests and to the threads you commented on",
    "mentions": "When someone mentions you in the chat",
    "requests": "New requests",
    "events": "Announcements of the events you have a ticket for",
}
LABEL = {
    "asap": "As they come",
    "daily": "Once a day",
    "weekly": "Once a week",
    "off": "Never",
}
EVENTS_NOTE = (
    "Whatever you choose, you are told when an event you have a ticket for is cancelled, "
    "and you get its reminder the day before."
)


def register(app: FastAPI) -> None:
    from . import sitesessions

    async def session(request: Request, method: str) -> tuple[Services, dict, dict]:
        import json

        from .holder_invites import authenticate

        s: Services = request.app.state.services
        raw = await request.body() if method == "POST" else b""
        key = authenticate(
            s, request.app.state.api_seen, request.headers.get("authorization"), method, API, raw
        )
        if not sitesessions.own(s):
            raise ApiError(409, "unsupported", "Website accounts need the belfry's own register.")
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_json", "Send a JSON object.")
        acct = await run_in_threadpool(sitesessions.site_account, s, key)
        if acct is None:
            raise ApiError(401, "no_session", "Log in again.")
        return s, acct, body

    @app.get(API)
    async def site_notifications(request: Request) -> JSONResponse:
        s, acct, _ = await session(request, "GET")
        return JSONResponse({"items": await run_in_threadpool(items, s, acct)}, headers=NO_STORE)

    @app.post(API)
    async def site_notifications_set(request: Request) -> JSONResponse:
        s, acct, body = await session(request, "POST")
        return JSONResponse(
            {"items": await run_in_threadpool(change, s, acct, body)}, headers=NO_STORE
        )

    def page(title: str, body: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(
            theme.frame(title, body),
            status_code=status,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    def account_of(s: Services, tok: str) -> dict[str, Any] | None:
        partner = partner_of(s, tok) if own(s) else None
        return sitesessions.account(s, partner) if partner else None

    gone = "<h1>E-mails</h1><p>This link does not work any more.</p>"

    @app.get("/notifications/{tok}", response_class=HTMLResponse)
    async def notifications_page(request: Request, tok: str):
        s = request.app.state.services
        acct = await run_in_threadpool(account_of, s, tok)
        have = await run_in_threadpool(items, s, acct) if acct else []
        if not have:
            return page("E-mails", gone, 404)
        msg, _ = flash.peek(request, s)
        parts = [
            "<style>.pick{display:inline-block;margin:0 8px 8px 0}</style>",
            "<h1>E-mails</h1>",
            f'<p class="ok">{E(msg)}</p>' if msg else "",
        ]
        for it in have:
            parts.append(f'<h2 style="font-size:1rem;margin:22px 0 8px">{E(TITLE[it["id"]])}</h2>')
            for c in it["choices"]:
                cls = "btn" if c == it["value"] else "btn ghost"
                parts.append(
                    f'<form method="post" action="/notifications/{E(tok)}" class="pick">'
                    f'<input type="hidden" name="id" value="{E(it["id"])}">'
                    f'<input type="hidden" name="value" value="{E(c)}">'
                    f'<button class="{cls}" type="submit">{LABEL[c]}</button></form>'
                )
            if it["id"] == "events":
                parts.append(f'<p class="muted">{E(EVENTS_NOTE)}</p>')
        return flash.clear(page("E-mails", "".join(parts)), request)

    @app.post("/notifications/{tok}")
    async def notifications_set(
        request: Request, tok: str, id: str = Form(""), value: str = Form("")
    ):
        s = request.app.state.services
        acct = await run_in_threadpool(account_of, s, tok)
        if acct is None:
            return Response(status_code=404)
        try:
            await run_in_threadpool(change, s, acct, {id: value})
        except ApiError as exc:
            if exc.status == 422:
                return Response(status_code=404)
            return flash.redirect(s, f"/notifications/{tok}", msg="That did not work. Try again.")
        return flash.redirect(s, f"/notifications/{tok}", msg="Saved.")


__all__ = ["register", "items", "change", "pref", "hourly", "daily", "settings_url"]
