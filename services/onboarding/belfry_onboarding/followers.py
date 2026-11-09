"""Announcements: messages a channel's admins flag as such, followed by e-mail like an account.

**Announcements.** Any channel, pod or event (a NIP-29 group on the groups relay) can have them:
a kind-9 message tagged ``["t", "announcement"]`` (NIP-24) by one of the group's admins (its
creator, kind 9007, and the keys of its kind 39001). The relay lets any member tag a message;
only the admins' count, here and in the chat. ``general`` is made at start when missing (by the
groups API key, the relay's owner): its announcements are the community's public ones.

**Followers.** Members follow any channel from the chat (``/api/me/follows``, NIP-98: their own
address, no confirmation); anyone may follow ``general`` from the website, which posts the
address to ``/follow`` (on the apex, the homepage's own origin). Odoo keeps a contact for it
(``belfry.follower``, one per contact and channel) and the belfry mails a link to confirm. The
token in that link and in every mail's footer is the follow's only credential. Opening the
confirmation link confirms (monthly unless the follower picks otherwise);
``/follow?t=…[&f=asap|monthly|stop]`` shows where they stand and a button for the change (a
button, not the link itself: mail scanners open every link of a message). Mails carry RFC 8058
one-click ``List-Unsubscribe``.

**Mails.** Followers choose "as soon as possible" (the announcements of the last few minutes,
mailed together once each is ``ASAP_DELAY`` old, so a slip deleted right away is never mailed) or
monthly (on the 1st, after ``DIGEST_HOUR`` local time in ``DIGEST_TZ``; the first worker to claim
the day sends). Nothing new, no mail.

Pages show fixed text only: never text taken from their address (flash.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import html
import json
import logging
import re
import time
import urllib.parse
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from . import flash
from .claim import page
from .clients.odoo import OdooError
from .core import Services
from .groups_members import signed
from .relay import RelayPublisher

log = logging.getLogger(__name__)
E = html.escape

GENERAL = "general"
TAG = "announcement"  # the t tag of an announcement
FOLLOWS = "/api/me/follows"
CHANNEL = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MODEL = "belfry.follower"
CHOICES = ("asap", "monthly")
ASAP_DELAY = 300  # seconds a post waits before it is mailed
LOOP_SECONDS = 300
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
URL = re.compile(r"https?://[^\s<>\"']+")
IMAGE = re.compile(r"\.(png|jpe?g|gif|webp|avif)(\?.*)?$", re.I)

# Fixed messages, picked by key (a POST's outcome rides the flash cookie).
LABEL = {"asap": "as soon as possible", "monthly": "monthly"}
SAID = {"asap": "as soon as possible", "monthly": "once a month"}
WHEN = {
    "asap": "a few minutes after each post",
    "monthly": "on the 1st of each month, when there is something new",
}


# ── who and where ───────────────────────────────────────────────────────────────────────────
def domain(svc: Services) -> str:
    site = svc.settings.site_url or svc.settings.public_url
    return httpx.URL(site).host or svc.settings.nip05_domain


def base_url(svc: Services) -> str:
    """Where /follow lives: the apex (the website's own origin) when there is one."""
    return (svc.settings.site_url or svc.settings.public_url).rstrip("/")


def name(svc: Services) -> str:
    from . import theme

    return theme.current().name or domain(svc)


def link(svc: Services, token: str, choice: str = "") -> str:
    q = {"t": token} | ({"f": choice} if choice else {})
    return f"{base_url(svc)}/follow?{urllib.parse.urlencode(q)}"


def odoo(svc: Services, method: str, *args: Any) -> Any:
    return svc.odoo.call(MODEL, method, list(args))


# ── the groups relay ────────────────────────────────────────────────────────────────────────
def _relay(svc: Services) -> tuple[RelayPublisher, Any] | None:
    url = svc.settings.groups_relay_public_url
    if svc.groups is None or not url:
        return None
    key = svc.groups.key
    return RelayPublisher(url), (lambda template: signed(key, template))


def _publish(relay: RelayPublisher, sign: Any, kind: int, tags: list[list[str]]) -> None:
    relay.publish(
        sign({"kind": kind, "created_at": int(time.time()), "tags": tags, "content": ""}), sign
    )


def ensure_channels(svc: Services) -> dict[str, Any]:
    """Create ``general`` when missing. Best effort: logged, retried on the next round."""
    got = _relay(svc)
    if got is None:
        return {}
    relay, sign = got
    if relay.query([{"kinds": [39000], "#d": [GENERAL]}], sign):
        return {"made": []}
    _publish(relay, sign, 9007, [["h", GENERAL]])
    _publish(
        relay,
        sign,
        9002,
        [
            ["h", GENERAL],
            ["name", GENERAL],
            ["about", f"Everything about {domain(svc)}. Say hi 👋"],
            ["open"],
        ],
    )
    log.info("announcements: made %s", GENERAL)
    return {"made": [GENERAL]}


_names: dict[str, tuple[float, str]] = {}


def chat_link(svc: Services, channel: str) -> str:
    """Where a channel opens in the community's chat (chat.<domain>, ``#/c/<id>``)."""
    if svc.settings.osv_apps:  # Open Source Village's chat (Coracle) on chat.<domain>
        return svc.settings.chat_public_url or ""
    host = svc.settings.nip05_domain or domain(svc)
    return f"https://chat.{host}/#/c/{urllib.parse.quote(channel, safe='')}"


def channel_label(svc: Services, channel: str) -> str:
    """How mails and pages name a channel: ``#general``, or a pod's or event's own name."""
    held = _names.get(channel)
    if held and time.monotonic() - held[0] < 600:
        return held[1]
    label = f"#{channel}"
    got = _relay(svc)
    if got is not None:
        relay, sign = got
        try:
            for e in relay.query([{"kinds": [39000], "#d": [channel]}], sign):
                n = next((t[1] for t in e.get("tags", []) if t[0] == "name" and len(t) > 1), "")
                if n:
                    plain = not channel.startswith(("ws-", "ev-", "pod-", "prop-"))
                    label = f"#{n}" if plain else n
        except Exception as exc:  # noqa: BLE001 — the id is a fine fallback
            log.warning("announcements: no name for %s: %s", channel, exc)
    _names[channel] = (time.monotonic(), label[:80])
    return label[:80]


def whose(svc: Services, channel: str) -> str:
    """ "belfry.sh" for general (the community's public announcements), else "#x on belfry.sh"."""
    return domain(svc) if channel == GENERAL else f"{channel_label(svc, channel)} on {domain(svc)}"


def announcements(
    svc: Services, channel: str, since: int, until: int
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The admins' announcements in CHANNEL in (since, until], oldest first, without deleted
    ones; and the authors' names."""
    got = _relay(svc)
    if got is None:
        return [], {}
    relay, sign = got
    posts = relay.query(
        [
            {
                "kinds": [9],
                "#h": [channel],
                "#t": [TAG],
                "since": since + 1,
                "until": until,
                "limit": 300,
            }
        ],
        sign,
    )
    if not posts:
        return [], {}
    admins = {e["pubkey"] for e in relay.query([{"kinds": [9007], "#h": [channel]}], sign)}
    for e in relay.query([{"kinds": [39001], "#d": [channel]}], sign):
        admins |= {t[1] for t in e.get("tags", []) if len(t) > 1 and t[0] == "p"}
    gone = {
        t[1]
        for e in relay.query([{"kinds": [5, 9005], "#h": [channel], "since": since + 1}], sign)
        for t in e.get("tags", [])
        if len(t) > 1 and t[0] == "e"
    }
    posts = sorted(
        (
            e
            for e in posts
            if e["pubkey"] in admins
            and e["id"] not in gone
            and since < e["created_at"] <= until
            and e.get("content", "").strip()
        ),
        key=lambda e: e["created_at"],
    )
    names: dict[str, str] = {}
    authors = sorted({e["pubkey"] for e in posts})
    if authors:
        for e in relay.query([{"kinds": [0], "authors": authors}], sign):
            try:
                c = json.loads(e.get("content") or "{}")
                names[e["pubkey"]] = str(c.get("display_name") or c.get("name") or "")[:80]
            except ValueError:
                pass
    return posts, names


# ── mails ───────────────────────────────────────────────────────────────────────────────────
INK, MUTED, LINE, BG = "#1c1917", "#6b6460", "#e7e2d9", "#f4f0e8"


def _shell(svc: Services, body: str, footer: str) -> str:
    return f"""<div style="margin:0;padding:28px 12px;background:{BG};font-family:-apple-system,'Segoe UI',Inter,Arial,sans-serif;font-size:15px;line-height:1.55;color:{INK}">
<div style="max-width:560px;margin:0 auto;background:#ffffff;border:1px solid {LINE};border-radius:12px;padding:28px">
<p style="margin:0 0 20px;font:600 20px/1.2 Georgia,'Source Serif 4',serif;color:{INK}"><a href="{E(base_url(svc))}/" style="color:{INK};text-decoration:none">{E(name(svc))}</a></p>
{body}
</div>
<p style="max-width:560px;margin:16px auto 0;text-align:center;font-size:12px;color:{MUTED}">{footer}</p>
</div>"""


def footer(svc: Services, token: str, frequency: str, channel: str = GENERAL) -> str:
    """The digest's footer: the current frequency in bold, the others and unsubscribe as links."""
    parts = []
    for choice in CHOICES:
        if choice == frequency:
            parts.append(f"<strong>{LABEL[choice]}</strong>")
        else:
            parts.append(
                f'<a href="{E(link(svc, token, choice))}" style="color:{MUTED}">{LABEL[choice]}</a>'
            )
    parts.append(f'<a href="{E(link(svc, token, "stop"))}" style="color:{MUTED}">unsubscribe</a>')
    return (
        f"You receive this e-mail because you subscribed to the announcements of "
        f"{E(whose(svc, channel))}.<br>Receive such announcements " + " · ".join(parts)
    )


def headers(svc: Services, token: str) -> dict[str, str]:
    one_click = f"{base_url(svc)}/follow/unsubscribe?{urllib.parse.urlencode({'t': token})}"
    return {
        "List-Unsubscribe": f"<{one_click}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


MD_INLINE = (
    (
        re.compile(r"`([^`\n]+)`"),
        r'<code style="background:#f4f4f5;padding:1px 4px;border-radius:4px">\1</code>',
    ),
    (re.compile(r"\*\*([^*\n]+)\*\*|__([^_\n]+)__"), None),
    (re.compile(r"~~([^~\n]+)~~"), r"<s>\1</s>"),
    (re.compile(r"(^|[\s(])\*([^*\s][^*\n]*)\*"), r"\1<em>\2</em>"),
    (re.compile(r"(^|[\s(])_([^_\s][^_\n]*)_"), r"\1<em>\2</em>"),
)


def md_inline(escaped: str) -> str:
    """Basic Markdown on text that is HTML-escaped already: `code`, **bold**, ~~strike~~,
    *italic* (the chat's subset; links are made by text_html)."""
    for pat, rep in MD_INLINE:
        if rep is None:
            escaped = pat.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", escaped)
        else:
            escaped = pat.sub(rep, escaped)
    return escaped


def text_html(text: str) -> str:
    """A message as HTML: escaped, basic Markdown, links clickable, image links shown, line
    breaks kept."""
    out, last = [], 0
    for m in URL.finditer(text):
        out.append(md_inline(E(text[last : m.start()])))
        url = m.group(0).rstrip(".,;:!?)")
        rest = m.group(0)[len(url) :]
        if IMAGE.search(url):
            out.append(
                f'<br><img src="{E(url)}" alt="" style="max-width:100%;height:auto;border-radius:8px;margin:8px 0"><br>'
            )
        else:
            out.append(f'<a href="{E(url)}" style="color:#a4532b">{E(url)}</a>')
        out.append(md_inline(E(rest)))
        last = m.end()
    out.append(md_inline(E(text[last:])))
    return "".join(out).strip().replace("\n", "<br>")


def confirm_mail(svc: Services, token: str, channel: str = GENERAL) -> tuple[str, str]:
    where = whose(svc, channel)
    body = f"""<p style="margin:0 0 12px">Hello,</p>
<p style="margin:0 0 12px">Someone (we hope you) asked to receive the announcements of {E(where)} at this address.</p>
<p style="margin:20px 0"><a href="{E(link(svc, token))}" style="display:inline-block;background:#a4532b;color:#ffffff;font-weight:600;padding:11px 20px;border-radius:8px;text-decoration:none">Confirm</a></p>
<p style="margin:0;color:{MUTED}">If you did not ask for this, ignore this mail: nothing is sent without your confirmation.</p>"""
    return f"Confirm the announcements of {where}", _shell(
        svc, body, f"{E(name(svc))} · {E(domain(svc))}"
    )


def digest_mail(
    svc: Services, f: dict[str, Any], posts: list[dict[str, Any]], names: dict[str, str]
) -> tuple[str, str]:
    channel = f.get("channel") or GENERAL
    where = whose(svc, channel)
    items = []
    for e in posts:
        when = dt.datetime.fromtimestamp(e["created_at"], ZoneInfo(svc.settings.digest_tz))
        who = names.get(e["pubkey"]) or ""
        meta = when.strftime("%a %-d %b, %H:%M") + (f" · {E(who)}" if who else "")
        items.append(
            f'<div style="border-top:1px solid {LINE};padding:16px 0 4px">'
            f'<p style="margin:0 0 6px;font-size:13px;color:{MUTED}">{meta}</p>'
            f'<p style="margin:0">{text_html(e["content"])}</p></div>'
        )
    first = posts[-1]["content"].strip().splitlines()[0][:80] if posts else ""
    subject = f"{where}: {first}" if len(posts) == 1 else f"{len(posts)} announcements from {where}"
    title = "New announcement" if len(posts) == 1 else f"{len(posts)} new announcements"
    body = f'<h1 style="margin:0 0 4px;font-size:20px">{title}</h1>' + "".join(items)
    return subject, _shell(svc, body, footer(svc, f["token"], f["frequency"], channel))


def send_digests(svc: Services, frequency: str, until: int) -> dict[str, int]:
    due = odoo(svc, "belfry_follow_due", frequency)
    if not due:
        return {"due": 0, "sent": 0}
    by_channel: dict[str, list[dict[str, Any]]] = {}
    for f in due:
        by_channel.setdefault(f.get("channel") or GENERAL, []).append(f)
    found: dict[str, tuple[list[dict[str, Any]], dict[str, str]]] = {}
    for channel, fs in by_channel.items():
        try:
            found[channel] = announcements(
                svc, channel, min(int(f["since"] or 0) for f in fs), until
            )
        except Exception as exc:  # noqa: BLE001 — that channel waits for the next round
            log.warning("announcements: %s not read: %s", channel, exc)
    sent = 0
    for f in due:
        posts, names = found.get(f.get("channel") or GENERAL, ([], {}))
        mine = [e for e in posts if e["created_at"] > int(f["since"] or 0)]
        if not mine:
            continue
        subject, body = digest_mail(svc, f, mine, names)
        try:
            odoo(svc, "belfry_follow_mail", f["id"], subject, body, until, headers(svc, f["token"]))
            sent += 1
        except OdooError as exc:
            log.warning("announcements: digest for follower %s not queued: %s", f["id"], exc)
    total = sum(len(p) for p, _ in found.values())
    if sent or total:
        log.info(
            "announcements: %s, %d posts, %d of %d followers mailed",
            frequency,
            total,
            sent,
            len(due),
        )
    return {"due": len(due), "sent": sent, "posts": total}


def morning(svc: Services, now: dt.datetime | None = None) -> dict[str, Any] | None:
    """On the 1st, once the morning hour has come, the monthly digests (once that day)."""
    tz = ZoneInfo(svc.settings.digest_tz)
    local = (now or dt.datetime.now(tz)).astimezone(tz)
    if local.day != 1 or local.hour < svc.settings.digest_hour:
        return None
    if not odoo(svc, "belfry_follow_claim_day", local.date().isoformat()):
        return None
    return send_digests(svc, "monthly", int(local.timestamp()))


def soon(svc: Services, now: float | None = None) -> dict[str, int]:
    """The "as soon as possible" followers: posts at least ASAP_DELAY old they did not get yet."""
    return send_digests(svc, "asap", int((now or time.time()) - ASAP_DELAY))


def tick(svc: Services) -> None:
    from .drops import ensure_profile

    for job in (ensure_channels, ensure_profile, soon, morning):
        try:
            job(svc)
        except Exception as exc:  # noqa: BLE001 — retried on the next round
            log.warning("announcements: %s failed: %s", job.__name__, exc)


@contextlib.asynccontextmanager
async def running(app: FastAPI):
    async def loop() -> None:
        while True:
            await run_in_threadpool(tick, app.state.services)
            await asyncio.sleep(LOOP_SECONDS)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


# ── pages ───────────────────────────────────────────────────────────────────────────────────
STYLE = """<style>
.follow .opts{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0 6px}
.follow .opts form{margin:0}
.follow button{font:inherit;cursor:pointer}
.follow .ghost{background:transparent;color:var(--fg);border:1px solid var(--border);padding:9px 16px;border-radius:8px}
.follow .ghost.on{border-color:var(--accent);color:var(--accent);font-weight:600}
.follow .stop{background:none;border:0;padding:0;color:var(--fg-muted);text-decoration:underline}
.follow .msg{border-left:3px solid var(--accent);padding:2px 0 2px 12px;margin:0 0 16px}
</style>"""


def _button(token: str, choice: str, label: str, cls: str) -> str:
    return (
        f'<form method="post" action="/follow/set"><input type="hidden" name="t" value="{E(token)}">'
        f'<input type="hidden" name="f" value="{choice}"><button class="{cls}">{label}</button></form>'
    )


def state_page(svc: Services, token: str, state: dict[str, Any], want: str, msg: str) -> str:
    where = E(domain(svc))
    note = f'<p class="msg">{E(msg)}</p>' if msg else ""
    s, freq = state.get("state"), state.get("frequency") or "monthly"
    if s == "unknown":
        body = f"""<h1>This link does not work</h1>
<p class="muted">It may be cut off, or the subscription is gone. Subscribe again on the
<a href="{E(base_url(svc))}/">website of {where}</a>.</p>"""
        return page("Announcements", f'<div class="card follow">{body}</div>')
    where = E(whose(svc, state.get("channel") or GENERAL))
    if s == "active":
        top = ""
        if want in CHOICES and want != freq:
            top = f'<div class="opts">{_button(token, want, f"Receive them {SAID[want]} instead", "btn")}</div>'
        elif want == "stop":
            top = f'<div class="opts">{_button(token, "stop", "Unsubscribe", "btn")}</div>'
        opts = "".join(
            _button(token, c, LABEL[c].capitalize(), "ghost on" if c == freq else "ghost")
            for c in CHOICES
        )
        body = f"""<h1>You follow {where}</h1>
<p>Announcements reach {E(state.get("email") or "you")} {WHEN[freq]}.</p>{top}
<p class="muted" style="margin:18px 0 0">How often</p><div class="opts">{opts}</div>
<p style="margin:14px 0 0">{"" if want == "stop" else _button(token, "stop", "Unsubscribe", "stop")}</p>"""
    else:
        opts = "".join(
            _button(token, c, f"Subscribe again, {SAID[c]}", "btn" if c == freq else "ghost")
            for c in CHOICES
        )
        body = f"""<h1>Unsubscribed</h1>
<p>{E(state.get("email") or "This address")} receives no more announcements from {where}.</p>
<div class="opts">{opts}</div>"""
    return page("Announcements", STYLE + f'<div class="card follow">{note}{body}</div>')


def sent_page(svc: Services) -> str:
    return page(
        "Check your inbox",
        f"""<div class="card"><h1>Check your inbox</h1>
<p>We sent you a link to confirm. Nothing is mailed until you do.</p>
<p class="muted">No mail after a few minutes? Look in your spam folder, or
<a href="{E(base_url(svc))}/">try again</a>.</p></div>""",
    )


def register(app: FastAPI) -> None:
    def svc(request: Request) -> Services:
        return request.app.state.services

    @app.post("/follow")
    async def follow(request: Request, email: str = Form(""), website: str = Form("")):
        s = svc(request)
        email = email.strip()
        if website:  # the honeypot field people never see
            return RedirectResponse("/follow/sent", status_code=303)
        if len(email) > 254 or not EMAIL.match(email):
            return HTMLResponse(
                page(
                    "Announcements",
                    f"""<div class="card"><h1>That is not an e-mail address</h1>
<p><a href="{E(base_url(s))}/">Go back</a> and try again.</p></div>""",
                ),
                status_code=422,
            )
        try:
            got = await run_in_threadpool(odoo, s, "belfry_follow_request", email, GENERAL)
            if got.get("state") == "send":
                subject, body = confirm_mail(s, got["token"], GENERAL)
                await run_in_threadpool(
                    odoo,
                    s,
                    "belfry_follow_mail",
                    got["id"],
                    subject,
                    body,
                    0,
                    headers(s, got["token"]),
                )
        except (OdooError, httpx.HTTPError) as exc:
            log.warning("follow: not recorded: %s", exc)
            return HTMLResponse(
                page(
                    "Announcements",
                    """<div class="card"><h1>That did not work</h1>
<p>The belfry could not record your address just now. Try again in a moment.</p></div>""",
                ),
                status_code=503,
            )
        return RedirectResponse("/follow/sent", status_code=303)

    @app.get("/follow/sent", response_class=HTMLResponse)
    async def follow_sent(request: Request):
        return HTMLResponse(sent_page(svc(request)))

    @app.get("/follow", response_class=HTMLResponse)
    async def follow_page(request: Request, t: str = "", f: str = ""):
        s = svc(request)
        state = (
            await run_in_threadpool(odoo, s, "belfry_follow_get", t) if t else {"state": "unknown"}
        )
        msg, _ = flash.peek(request, s)
        want = f if f in (*CHOICES, "stop") else ""
        if state.get("state") == "pending":
            # the confirmation link: opening it confirms, monthly unless the link says otherwise
            choice = want if want in CHOICES else "monthly"
            state = await run_in_threadpool(odoo, s, "belfry_follow_set", t, choice)
            msg, want = f"Confirmed. You will receive the announcements {SAID[choice]}.", ""
        body = state_page(s, t, state, want, msg)
        headers_ = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        return flash.clear(HTMLResponse(body, headers=headers_), request)

    @app.post("/follow/set")
    async def follow_set(request: Request, t: str = Form(""), f: str = Form("")):
        s = svc(request)
        if f not in (*CHOICES, "stop"):
            return RedirectResponse("/follow?" + urllib.parse.urlencode({"t": t}), status_code=303)
        before = await run_in_threadpool(odoo, s, "belfry_follow_get", t)
        state = await run_in_threadpool(odoo, s, "belfry_follow_set", t, f)
        if state.get("state") == "unknown":
            msg = ""
        elif f == "stop":
            msg = "Done: you are unsubscribed."
        elif before.get("state") == "stopped":
            msg = f"Welcome back. You will receive the announcements {SAID[f]}."
        else:
            msg = f"Saved. You will receive the announcements {SAID[f]}."
        return flash.redirect(s, "/follow?" + urllib.parse.urlencode({"t": t}), msg=msg)

    async def member(request: Request, method: str, body: bytes) -> int:
        from .holder_invites import authenticate, holder

        s = svc(request)
        pubkey = authenticate(
            s,
            request.app.state.api_seen,
            request.headers.get("authorization"),
            method,
            FOLLOWS,
            body,
        )
        return (await run_in_threadpool(holder, s, pubkey))["partner_id"]

    @app.get(FOLLOWS)
    async def api_follows(request: Request) -> JSONResponse:
        """The signed-in member's follows (NIP-98): ``{"email", "follows": [{channel,
        frequency, state}]}``."""
        partner = await member(request, "GET", b"")
        s = svc(request)
        rows = await run_in_threadpool(odoo, s, "belfry_follow_member_list", partner)
        email = (s.odoo.read("res.partner", [partner], ["email"]) or [{}])[0].get("email") or ""
        return JSONResponse(
            {"email": email, "follows": rows}, headers={"Cache-Control": "no-store"}
        )

    @app.post(FOLLOWS)
    async def api_follow(request: Request) -> JSONResponse:
        """Follow a channel's announcements: ``{"channel", "frequency": "asap"|"monthly"|"stop"}``."""
        from .holder_invites import ApiError

        body = await request.body()
        partner = await member(request, "POST", body)
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_json", "Send a JSON object.")
        channel, choice = str(data.get("channel") or ""), str(data.get("frequency") or "")
        if not CHANNEL.match(channel) or choice not in (*CHOICES, "stop"):
            raise ApiError(422, "invalid", "channel: a group id; frequency: asap, monthly or stop")
        try:
            row = await run_in_threadpool(
                odoo, svc(request), "belfry_follow_member_set", partner, channel, choice
            )
        except OdooError as exc:
            raise ApiError(422, "refused", "The belfry could not save this.") from exc
        return JSONResponse(row)

    @app.post("/follow/unsubscribe")
    async def follow_unsubscribe(request: Request, t: str = ""):
        """RFC 8058 one-click unsubscribe from the mail client."""
        await run_in_threadpool(odoo, svc(request), "belfry_follow_set", t, "stop")
        return Response(status_code=200)


__all__ = ["register", "running", "ensure_channels", "send_digests", "morning", "soon", "footer"]
