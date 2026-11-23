"""Announcements: messages a channel's admins flag as such, followed by e-mail like an account.

**Announcements.** Any channel, group or event (a NIP-29 group on the groups relay) can have them:
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
mailed together once each is ``ASAP_DELAY`` old, so a slip deleted right away is never mailed),
weekly (the default since 2026-11-23: Mondays, after ``DIGEST_HOUR`` local time in
``DIGEST_TZ``) or monthly (on the 1st, same hour); the first worker to claim the day sends.
Weekly and monthly are a **summary** of the channel (``summary_mail``): its new messages of the
period, newest first, at most ``SUMMARY_MAX`` with "and N more", and how many people started
following it. A follower who can't read the channel in the chat (someone who follows ``general``
from the website, with no pass that reads it) gets only its announcements, in the same format.
Private groups get no summary at all. Nothing new, no mail.

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
from .core import Services, m2o_id
from .groups_members import signed
from .relay import RelayPublisher

log = logging.getLogger(__name__)
E = html.escape

GENERAL = "general"
TAG = "announcement"  # the t tag of an announcement
FOLLOWS = "/api/me/follows"
CHANNEL = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MODEL = "belfry.follower"
CHOICES = ("asap", "weekly", "monthly")
DEFAULT = "weekly"  # what a follow is when nobody picked (the owner, 2026-11-23)
SUMMARIES = ("weekly", "monthly")  # a summary of the channel, not only its announcements
SUMMARY_MAX = 20  # messages a summary shows; "and N more" links to the channel
SUMMARY_SNIPPET = 200  # characters of each
# how far back a summary looks at most (a follower nothing was mailed to for months)
WINDOW = {"weekly": 8 * 86400, "monthly": 32 * 86400}
ASAP_DELAY = 300  # seconds a post waits before it is mailed
LOOP_SECONDS = 300
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
URL = re.compile(r"https?://[^\s<>\"']+")
IMAGE = re.compile(r"\.(png|jpe?g|gif|webp|avif)(\?.*)?$", re.I)

# Fixed messages, picked by key (a POST's outcome rides the flash cookie).
LABEL = {"asap": "as soon as possible", "weekly": "weekly", "monthly": "monthly"}
SAID = {"asap": "as soon as possible", "weekly": "once a week", "monthly": "once a month"}
WHEN = {
    "asap": "a few minutes after each post",
    "weekly": "every Monday, when there is something new",
    "monthly": "on the 1st of each month, when there is something new",
}
WHAT = {"asap": "the announcements", "weekly": "a summary", "monthly": "a summary"}
REACHES = {
    "asap": "Announcements reach",
    "weekly": "A summary reaches",
    "monthly": "A summary reaches",
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


# channels every new belfry gets (Xavier, 2026-11-11). The channel says who sees what is in it,
# no switch per item: 🌐 #events (public, the events page) and #proposals (members discuss and
# vote). #bookings and #requests were defaults too until 2026-11-14 (Xavier: "bookings and requests
# channels shouldn't be created by default"): belfries that have them keep them as they are (their
# posts, their 30078 web setting and the /requests page, webthreads.py), none are made any more.
DEFAULTS: list[dict[str, Any]] = [
    {"id": GENERAL, "about": "Everything about {domain}. Say hi 👋"},
    {
        "id": "events",
        "about": "Public events at {domain}: anyone sees them, members and people from outside post them.",
        "web": {"on": True, "slug": "events", "visibility": "public", "posts": "event"},
    },
    {
        "id": "proposals",
        "about": "Proposals members discuss and vote on. Members only.",
        "web": {"on": False, "slug": "proposals", "visibility": "members", "posts": "proposal"},
    },
]
# the ones a new belfry gets: #general alone (Xavier, 2026-11-17: "by default don't add
# fairground, scriptorium, assembly", i.e. #events, #website, #proposals). Belfries that have the
# others keep them (and their descriptions and web settings stay up to date below); an admin can
# delete them when empty or archive them (checkin.delete_group, /group-meta archived).
MADE = {GENERAL}
# descriptions an earlier version wrote: replaced by today's when nobody changed them since
_OLD_ABOUT = {
    "proposals": "Proposals for {domain}: one per thread, discussed and voted on.",
    "events": "What's on at {domain}: one event per thread.",
}
# what an earlier version set up and nobody changed since: brought to today's defaults
_EARLIER = {
    "proposals": [{"on": True, "slug": "proposals", "visibility": "public", "posts": "proposal"}]
}


def _web_setting(relay: Any, sign: Any, cid: str, conf: dict[str, Any]) -> None:
    relay.publish(
        sign(
            {
                "kind": 30078,
                "created_at": int(time.time()),
                "content": json.dumps(conf),
                "tags": [["d", f"belfry:web:{cid}"], ["h", cid], ["t", "belfry-web"]],
            }
        ),
        sign,
    )


def ensure_channels(svc: Services) -> dict[str, Any]:
    """Create the default channels when missing. Best effort: logged, retried on the next round."""
    got = _relay(svc)
    if got is None:
        return {}
    relay, sign = got
    made = []
    for ch in DEFAULTS:
        cid = ch["id"]
        meta = relay.query([{"kinds": [39000], "#d": [cid]}], sign)
        if meta:
            about = next(
                (t[1] for t in meta[0].get("tags", []) if t[0] == "about" and len(t) > 1), ""
            )
            if cid in _OLD_ABOUT and about == _OLD_ABOUT[cid].format(domain=domain(svc)):
                name = next(
                    (t[1] for t in meta[0].get("tags", []) if t[0] == "name" and len(t) > 1), cid
                )
                new_about = ch["about"].format(domain=domain(svc))
                _publish(
                    relay, sign, 9002, [["h", cid], ["name", name], ["about", new_about], ["open"]]
                )
                log.info("announcements: %s gets today's description", cid)
            if cid in _EARLIER:  # set up by an earlier default, untouched since: today's
                mine = sign({"kind": 1, "created_at": 0, "tags": [], "content": ""})["pubkey"]
                evs = relay.query([{"kinds": [30078], "#d": [f"belfry:web:{cid}"]}], sign) or []
                last = max(evs, key=lambda e: e.get("created_at", 0), default=None)
                try:
                    conf = json.loads(last["content"]) if last else None
                except ValueError:
                    conf = None
                if last and last["pubkey"] == mine and conf in _EARLIER[cid]:
                    _web_setting(relay, sign, cid, ch["web"])
                    log.info("announcements: %s brought to today's defaults", cid)
            continue
        if cid not in MADE:  # kept up to date where it exists, never made any more
            continue
        _publish(relay, sign, 9007, [["h", cid]])
        _publish(
            relay,
            sign,
            9002,
            [
                ["h", cid],
                ["name", cid],
                ["about", ch["about"].format(domain=domain(svc))],
                ["open"],
            ],
        )
        if ch.get("web"):  # who sees it, its page on the web, its type of post (webthreads.py)
            _web_setting(relay, sign, cid, ch["web"])
        log.info("announcements: made %s", cid)
        made.append(cid)
    return {"made": made}


_names: dict[str, tuple[float, str]] = {}


def chat_link(svc: Services, channel: str) -> str:
    """Where a channel opens in the community's chat (chat.<domain>, ``#/c/<id>``)."""
    if svc.settings.osv_apps:  # Open Source Village's chat (Coracle) on chat.<domain>
        return svc.settings.chat_public_url or ""
    host = svc.settings.nip05_domain or domain(svc)
    return f"https://chat.{host}/#/c/{urllib.parse.quote(channel, safe='')}"


def channel_label(svc: Services, channel: str) -> str:
    """How mails and pages name a channel: ``#general``, or a group's or event's own name."""
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
    if frequency in SUMMARIES:
        return (
            f"You receive this e-mail because you follow {E(whose(svc, channel))}."
            "<br>Receive it " + " · ".join(parts)
        )
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


def confirm_mail(
    svc: Services, token: str, channel: str = GENERAL, choice: str = ""
) -> tuple[str, str]:
    """The confirmation link; CHOICE (a frequency the form picked) rides in it."""
    where = whose(svc, channel)
    what = f"{WHAT[choice]} of {E(where)} {SAID[choice]}" if choice in CHOICES else ""
    what = what or f"the announcements of {E(where)}"
    body = f"""<p style="margin:0 0 12px">Hello,</p>
<p style="margin:0 0 12px">Someone (we hope you) asked to receive {what} at this address.</p>
<p style="margin:20px 0"><a href="{E(link(svc, token, choice if choice in CHOICES else ""))}" style="display:inline-block;background:#a4532b;color:#ffffff;font-weight:600;padding:11px 20px;border-radius:8px;text-decoration:none">Confirm</a></p>
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
    if frequency in SUMMARIES:
        return send_summaries(svc, frequency, until)
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


# ── weekly and monthly: a summary of the channel ─────────────────────────────────────────────
class _Named(dict):
    """Authors' names; anyone without a profile reads "Somebody" (never a key)."""

    def get(self, key: Any, default: Any = None) -> str:  # type: ignore[override]
        return super().get(key) or "Somebody"


def _meta_tags(svc: Services, channel: str) -> set[str] | None:
    """The flags of the channel's latest metadata (39000): ``private``, ``closed``…; None when
    the channel has none (deleted, never made)."""
    got = _relay(svc)
    if got is None:
        return None
    relay, sign = got
    meta = [
        e
        for e in relay.query([{"kinds": [39000], "#d": [channel]}], sign)
        if any(t[:2] == ["d", channel] for t in e.get("tags", []))
    ]
    if not meta:
        return None
    last = max(meta, key=lambda e: e.get("created_at", 0))
    return {str(t[0]) for t in last.get("tags", []) if t}


def channel_messages(svc: Services, channel: str, since: int, until: int) -> list[dict[str, Any]]:
    """The messages people wrote in CHANNEL in (since, until], newest first: kind 9, without
    deleted ones, the chat's cards (events, proposals, new groups), e-mail cards and the belfry's
    own lines; each with its author's latest edit applied."""
    from .eventchat import _plain

    got = _relay(svc)
    if got is None:
        return []
    relay, sign = got
    found = relay.query(
        [
            {"kinds": [9], "#h": [channel], "since": since + 1, "until": until, "limit": 500},
            {"kinds": [5, 9005, 1010], "#h": [channel], "since": since + 1, "limit": 1000},
        ],
        sign,
    )
    found = [e for e in found if any(t[:2] == ["h", channel] for t in e.get("tags", []))]
    posts = {e["id"]: e for e in found if e.get("kind") == 9}
    gone: set[str] = set()
    edits: dict[str, dict[str, Any]] = {}
    for x in sorted(found, key=lambda e: (e.get("created_at", 0), e.get("id", ""))):
        targets = [str(t[1]) for t in x.get("tags", []) if t[:1] == ["e"] and len(t) > 1]
        if x.get("kind") == 9005:
            gone |= set(targets)
        elif x.get("kind") == 5:
            gone |= {t for t in targets if t in posts and posts[t]["pubkey"] == x["pubkey"]}
        elif x.get("kind") == 1010 and targets and targets[0] in posts:
            if posts[targets[0]]["pubkey"] == x["pubkey"]:  # only its author edits a message
                edits[targets[0]] = x
    out = []
    for e in posts.values():
        if e["id"] in gone or not (since < int(e.get("created_at") or 0) <= until):
            continue
        if not _plain(e) or any(t[:2] == ["card", "1"] for t in e.get("tags", [])):
            continue
        if e["id"] in edits:
            e = {**e, "content": edits[e["id"]].get("content") or ""}
        if (e.get("content") or "").strip():
            out.append(e)
    return sorted(out, key=lambda e: (e["created_at"], e["id"]), reverse=True)


def readers(svc: Services, partners: set[int], channel: str) -> set[int]:
    """Which of these contacts read CHANNEL in the chat: an active pass that is not a guest's
    (members read every channel that is not private), or a guest pass of that channel."""
    from .core import pass_fields

    if not partners:
        return set()
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["partner_id", "in", sorted(partners)]],
        pass_fields(svc, "tier", "guest_channel", "partner_id"),
    )
    out: set[int] = set()
    for r in rows:
        pid = m2o_id(r.get("partner_id"))
        if pid is None:
            continue
        if r.get("tier") != "guest" or r.get("guest_channel") == channel:
            out.add(pid)
    return out


def new_followers(svc: Services, channel: str, since: int) -> int:
    """How many started following CHANNEL since SINCE (0 when the register can't say)."""
    from .register import stamp

    after = stamp(dt.datetime.fromtimestamp(since, dt.UTC).replace(tzinfo=None))
    try:
        rows = svc.odoo.search_read(
            MODEL,
            [["channel", "=", channel], ["state", "=", "active"], ["confirmed_at", ">", after]],
            ["id"],
        )
    except Exception as exc:  # noqa: BLE001 — a summary without the count
        log.info("followers: no count for %s: %s", channel, type(exc).__name__)
        return 0
    return len(rows or [])


def summary_mail(
    svc: Services,
    f: dict[str, Any],
    posts: list[dict[str, Any]],
    names: dict[str, str],
    joined: int = 0,
) -> tuple[str, str]:
    """A weekly or monthly summary: POSTS newest first, at most SUMMARY_MAX shown, the rest as
    "and N more" with a link to the channel; JOINED: how many started following it."""
    from . import mentions

    channel = f.get("channel") or GENERAL
    where = whose(svc, channel)
    label = channel_label(svc, channel)
    period = "week" if f["frequency"] == "weekly" else "month"
    named = _Named(names)
    tz = ZoneInfo(svc.settings.digest_tz)
    items = []
    for e in posts[:SUMMARY_MAX]:
        when = dt.datetime.fromtimestamp(e["created_at"], tz)
        meta = when.strftime("%a %-d %b, %H:%M") + f" · {E(named.get(e['pubkey']))}"
        text = mentions._html_text(svc, e, named, "#a4532b", limit=SUMMARY_SNIPPET)
        items.append(
            f'<div style="border-top:1px solid {LINE};padding:14px 0 4px">'
            f'<p style="margin:0 0 4px;font-size:13px;color:{MUTED}">{meta}</p>'
            f'<p style="margin:0 0 4px">{text}</p>'
            f'<p style="margin:0;font-size:13px"><a href="{E(mentions.link(svc, e))}" '
            f'style="color:#a4532b">Open in the chat</a></p></div>'
        )
    n = len(posts)
    more = ""
    if n > SUMMARY_MAX:
        rest = n - SUMMARY_MAX
        more = (
            f'<p style="margin:16px 0 0"><a href="{E(chat_link(svc, channel))}" '
            f'style="color:#a4532b">and {rest} more in {E(label)}</a></p>'
        )
    followers_line = ""
    if joined > 0:
        who = "person" if joined == 1 else "people"
        followers_line = (
            f'<p style="margin:0 0 8px;color:{MUTED}">{joined} new {who} started following '
            f"{E(label)} this {period}.</p>"
        )
    count = f"{n} new {'message' if n == 1 else 'messages'}"
    body = (
        f'<h1 style="margin:0 0 4px;font-size:20px">This {period} in {E(label)}</h1>'
        f'<p style="margin:0 0 12px;color:{MUTED}">{count}</p>'
        + followers_line
        + "".join(items)
        + more
    )
    subject = f"{where}: {count} this {period}"
    return subject, _shell(svc, body, footer(svc, f["token"], f["frequency"], channel))


def send_summaries(svc: Services, frequency: str, until: int) -> dict[str, int]:
    """The weekly or monthly summaries due: one mail per follower with something new for them."""
    from . import mentions

    due = odoo(svc, "belfry_follow_due", frequency)
    if not due:
        return {"due": 0, "sent": 0}
    floor = until - WINDOW[frequency]
    by_channel: dict[str, list[dict[str, Any]]] = {}
    for f in due:
        by_channel.setdefault(f.get("channel") or GENERAL, []).append(f)
    sent = total = 0
    for channel, fs in by_channel.items():
        since = max(floor, min(int(f.get("since") or 0) for f in fs))
        try:
            tags = _meta_tags(svc, channel)
            if tags is None or "private" in tags:
                continue  # gone, or a private group: nothing the belfry may summarise
            posts = channel_messages(svc, channel, since, until)
            if not posts:
                continue
            partners = {int(p) for f in fs if (p := m2o_id(f.get("partner_id")))}
            can_read = readers(svc, partners, channel)
            outsiders = any(m2o_id(f.get("partner_id")) not in can_read for f in fs)
            public: set[str] = set()
            if outsiders:  # for those, the announcements only, as before
                public = {e["id"] for e in announcements(svc, channel, since, until)[0]}
            names = mentions._names(svc, {e["pubkey"] for e in posts})
            joined = new_followers(svc, channel, since)
        except Exception as exc:  # noqa: BLE001 — that channel waits for the next round
            log.warning("followers: %s not read: %s", channel, exc)
            continue
        total += len(posts)
        for f in fs:
            mine = max(floor, int(f.get("since") or 0))
            shown = [e for e in posts if e["created_at"] > mine]
            if m2o_id(f.get("partner_id")) not in can_read:
                shown = [e for e in shown if e["id"] in public]
            if not shown:
                continue
            subject, body = summary_mail(svc, f, shown, names, joined)
            try:
                odoo(
                    svc,
                    "belfry_follow_mail",
                    f["id"],
                    subject,
                    body,
                    until,
                    headers(svc, f["token"]),
                )
                sent += 1
            except OdooError as exc:
                log.warning("followers: summary for follower %s not queued: %s", f["id"], exc)
    if sent or total:
        log.info(
            "followers: %s summaries, %d messages, %d of %d followers mailed",
            frequency,
            total,
            sent,
            len(due),
        )
    return {"due": len(due), "sent": sent, "posts": total}


def follow(svc: Services, partner: int, channel: str, choice: str = DEFAULT) -> bool:
    """PARTNER (whose address is proven) follows CHANNEL by e-mail at CHOICE. Best effort: the
    card or the guest account it comes with is made either way."""
    if choice not in CHOICES or not CHANNEL.match(channel or ""):
        return False
    try:
        odoo(svc, "belfry_follow_member_set", int(partner), channel, choice)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("followers: %s not followed: %s", channel, type(exc).__name__)
        return False


def summary_choice(data: dict[str, Any]) -> str:
    """The ``summary`` of a request body: ``weekly`` (also when missing) or ``monthly``; 422
    ``invalid`` (field ``summary``) for anything else."""
    from .holder_invites import ApiError

    raw = data.get("summary")
    if raw in (None, ""):
        return DEFAULT
    if raw not in SUMMARIES:
        exc = ApiError(422, "invalid", 'summary: "weekly" or "monthly".')
        exc.extra["field"] = "summary"
        raise exc
    return str(raw)


def morning(svc: Services, now: dt.datetime | None = None) -> dict[str, Any] | None:
    """On the 1st, once the morning hour has come, the monthly digests (once that day)."""
    tz = ZoneInfo(svc.settings.digest_tz)
    local = (now or dt.datetime.now(tz)).astimezone(tz)
    if local.day != 1 or local.hour < svc.settings.digest_hour:
        return None
    if not odoo(svc, "belfry_follow_claim_day", local.date().isoformat()):
        return None
    return send_digests(svc, "monthly", int(local.timestamp()))


def monday(svc: Services, now: dt.datetime | None = None) -> dict[str, Any] | None:
    """On Mondays, once the morning hour has come, the weekly summaries (once that day)."""
    tz = ZoneInfo(svc.settings.digest_tz)
    local = (now or dt.datetime.now(tz)).astimezone(tz)
    if local.weekday() != 0 or local.hour < svc.settings.digest_hour:
        return None
    if not odoo(svc, "belfry_follow_claim_day", local.date().isoformat(), "weekly"):
        return None
    return send_digests(svc, "weekly", int(local.timestamp()))


def soon(svc: Services, now: float | None = None) -> dict[str, int]:
    """The "as soon as possible" followers: posts at least ASAP_DELAY old they did not get yet."""
    return send_digests(svc, "asap", int((now or time.time()) - ASAP_DELAY))


EXPIRE_SECONDS = 3600
_expired_at = float("-inf")  # the first tick after a start runs (monotonic time starts at boot)


def expire_passes(svc: Services) -> dict[str, Any] | None:
    """Close the passes whose last day has passed (a guest's 30 days), about once an hour. Only
    with the belfry's own register: nothing else does it there (with Odoo, the host's cron runs
    ``python -m belfry_onboarding expire``, docs/runbook.md)."""
    global _expired_at
    from . import core, invite

    if not invite.own(svc) or time.monotonic() - _expired_at < EXPIRE_SECONDS:
        return None
    _expired_at = time.monotonic()
    result = core.expire(svc)
    if result["expired"]:
        log.info("passes expired: %s", result["expired"])
    return result


_round_from: float | None = None


def membership_round(svc: Services) -> None:
    """Requests accepted by vouches: groups (vouches.watch) and the belfry (membership.settle).
    Not in the first minute after start: the register is busy enough then (and short-lived test
    apps never run it)."""
    global _round_from
    from .membership import settle
    from .vouches import watch

    now = time.monotonic()
    if _round_from is None:
        _round_from = now
    if now - _round_from < 60:
        return
    watch(svc)
    settle(svc)


def tick(svc: Services) -> None:
    from .drops import ensure_profile
    from .events import watch as events_watch
    from .mailcheck import tick as mailcheck
    from .sales import tick as sales_tick
    from .welcome import tick as welcome_mails

    for job in (
        expire_passes,
        ensure_channels,
        ensure_profile,
        soon,
        morning,
        monday,
        events_watch,
        membership_round,
        sales_tick,
        welcome_mails,
        mailcheck,  # port 25 from outside, aside in its own thread (mailcheck.py)
    ):
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
    s, freq = state.get("state"), state.get("frequency") or DEFAULT
    freq = freq if freq in CHOICES else DEFAULT
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
<p>{REACHES[freq]} {E(state.get("email") or "you")} {WHEN[freq]}.</p>{top}
<p class="muted" style="margin:18px 0 0">How often</p><div class="opts">{opts}</div>
<p style="margin:14px 0 0">{"" if want == "stop" else _button(token, "stop", "Unsubscribe", "stop")}</p>"""
    else:
        opts = "".join(
            _button(token, c, f"Subscribe again, {SAID[c]}", "btn" if c == freq else "ghost")
            for c in CHOICES
        )
        body = f"""<h1>Unsubscribed</h1>
<p>{E(state.get("email") or "This address")} receives no more e-mails from {where}.</p>
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
    async def follow(
        request: Request,
        email: str = Form(""),
        website: str = Form(""),
        frequency: str = Form(""),
    ):
        s = svc(request)
        email = email.strip()
        choice = frequency if frequency in CHOICES else DEFAULT
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
                subject, body = confirm_mail(s, got["token"], GENERAL, choice)
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
            # the confirmation link: opening it confirms, weekly unless the link says otherwise
            choice = want if want in CHOICES else DEFAULT
            state = await run_in_threadpool(odoo, s, "belfry_follow_set", t, choice)
            msg, want = f"Confirmed. You will receive {WHAT[choice]} {SAID[choice]}.", ""
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
            msg = f"Welcome back. You will receive {WHAT[f]} {SAID[f]}."
        else:
            msg = f"Saved. You will receive {WHAT[f]} {SAID[f]}."
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
        """Follow a channel by e-mail: ``{"channel", "frequency": "asap"|"weekly"|"monthly"|
        "stop"}`` (no frequency: weekly). Weekly and monthly are a summary of the channel."""
        from .holder_invites import ApiError

        body = await request.body()
        partner = await member(request, "POST", body)
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_json", "Send a JSON object.")
        channel, choice = str(data.get("channel") or ""), str(data.get("frequency") or DEFAULT)
        if not CHANNEL.match(channel) or choice not in (*CHOICES, "stop"):
            raise ApiError(
                422, "invalid", "channel: a group id; frequency: asap, weekly, monthly or stop"
            )
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


__all__ = [
    "register",
    "running",
    "ensure_channels",
    "send_digests",
    "send_summaries",
    "morning",
    "monday",
    "soon",
    "footer",
    "follow",
]
