# ruff: noqa: E501 — the page's HTML and CSS templates read better unwrapped
"""A channel's public page: anyone opens a thread from the web (Xavier, 2026-11-11).

"In the settings of a channel, admins should be able to enable external people to create a thread
via email, and via the web (which would automatically generate a new page /$channelSlug). Eg. a
/proposals page as a public interface for external people (and members) to make proposals to the
community. It should behave a bit like GitHub issues and show clearly the other proposals and the
membership / role of the author of the proposal and of the comments below. Externals would only see
their thread (like when they send an email to a public channel)."

The e-mail half is the shared inbox (mail.py, ``E-mails from anyone``). This is the web half:

- **The setting.** A channel admin's NIP-78 event on the groups relay: kind 30078, ``d`` =
  ``belfry:web:<channel>``, ``h`` = the channel, ``t`` = ``belfry-web``, content ``{"on": true,
  "slug": "ideas", "visibility": "public" | "members"}``: the channel's visibility, one general
  setting (2026-11-11; older events said ``"list": "public" | "own"``). ``public``: the page lists
  every thread and anyone reads them; ``members``: someone from outside sees only their own (by
  the link they get). ``"periods": [[from, to | null], …]`` (#issues, 2026-11-13) says when it
  was public: the page shows only what was posted then (moving a channel is never retroactive).
  Only the latest setting by an admin of that channel (39001) or of the belfry counts; a slug
  taken by an earlier channel, or one of the service's own paths, is ignored.
  ``"page": "https://<domain>/…"`` (docs/requests.md, 2026-11-19): the community's own website
  renders the list and the threads there, from the JSON of webrequests.py. The belfry then
  answers no HTML at ``/<slug>`` (only the mailed confirmation link, and old thread links move
  to the page), and every link it builds points to ``<page>`` and
  ``<page>?id=<number>#k=<key>``.
- **Edits** (docs/requests.md §4): the author changes the title, the text or a comment of their
  own; a kind 1010 by the key that signed the message, as the chat edits a message.
- **The page** ``<domain>/<slug>`` (two templates, #issues 2026-11-13): a public channel is
  **issues**: like GitHub issues, every thread listed, Open / Closed (the members' label on it in
  the chat), author and their role (admin, member, agent, guest, from outside), replies, last
  activity. A members-only channel is **requests**: like writing to hello@, the form first,
  nobody from outside sees anyone else's. Both offer members to sign in (the chat) and anyone
  else to post as a guest (title, text, name, e-mail).
  ``<domain>/<slug>/<thread id>``: the thread, every comment with its author's role.
- **From outside.** The form mails a confirmation link (the address must be theirs); opening it
  posts the thread in the channel as an e-mail card (groups key, ``mail-from``, ``mail-name``,
  ``subject``, ``client`` = ``belfry-web``), so members see and answer it in the chat like mail
  from outside. The link to the thread (with its key, ``?k=``) is theirs: with it they read it
  (also when the list is not public) and comment; their comments are kind 1111 replies by the
  groups key with ``mail-from``. Replies by members are mailed to them, gathered, at most once an hour.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import threading
import time
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from .core import Services

log = logging.getLogger(__name__)
E = html.escape
D_PREFIX = "belfry:web:"
TAG = "belfry-web"
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
EMAIL = re.compile(r"^[^@\s<>\"]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,}$")
TTL = 60
PENDING_SECONDS = 2 * 86400
LOOP_SECONDS = 120
DIGEST_SECONDS = 3600  # members' replies are mailed at most once an hour, gathered
MAX_THREADS = 100
# first path segments that are never a channel's page (the service's own, and the apex's apps)
RESERVED = {
    "api",
    "admin",
    "apply",
    "apps",
    "bridge",
    "calendar",
    "chat",
    "check",
    "claim",
    "console",
    "connect",
    "door",
    "drop",
    "events",
    "feedback",
    "follow",
    "git",
    "groups",
    "hooks",
    "internal",
    "market",
    "me",
    "mentions",
    "people",
    "photos",
    "programme",
    "shop",
    "signin",
    "skills",
    "static",
    "steward",
    "theme",
    "tokens",
    "vote",
    "wallet",
    "welcome",
    "invite",
    "brand",
    # the service's own pages (pages.py, community.py): a channel can't take them
    "changelog",
    "monitor",
    "privacy",
    "terms",
    "refunds",
    "rules",
    "help",
}

_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "sites": {}}
_pending_mem: dict[str, str] = {}  # without the belfry's own register (tests, Odoo): in memory


# ── the settings ───────────────────────────────────────────────────────────────────────────
def _relay(svc: Services):
    from . import followers

    return followers._relay(svc)


def _groups_pubkey(svc: Services) -> str:
    return svc.groups.key.public_key_xonly.format().hex() if svc.groups is not None else ""


def sites(svc: Services, fresh: bool = False) -> dict[str, dict[str, Any]]:
    """slug → {"channel", "list", "name", "about"}: the channels whose page is on."""
    if not fresh and time.monotonic() - _cache["at"] < TTL:
        return _cache["sites"]
    out: dict[str, dict[str, Any]] = {}
    got = _relay(svc)
    if got is not None:
        try:
            out = _read_sites(svc, *got)
        except Exception as exc:  # noqa: BLE001 — the last known ones stay
            log.warning("webthreads: settings not read: %s", exc)
            out = _cache["sites"]
    with _lock:
        _cache.update(at=time.monotonic(), sites=out)
    return out


def _read_sites(svc: Services, relay: Any, sign: Any) -> dict[str, dict[str, Any]]:
    from . import apps

    events = relay.query([{"kinds": [30078], "#t": [TAG], "limit": 500}], sign) or []
    latest: dict[str, dict[str, Any]] = {}
    for e in sorted(events, key=lambda x: x.get("created_at", 0)):
        d = _tag(e, "d")
        channel = _tag(e, "h")
        if not d.startswith(D_PREFIX) or d[len(D_PREFIX) :] != channel or not channel:
            continue
        latest.setdefault(channel, {})[e["pubkey"]] = e
    if not latest:
        return {}
    belfry_admins = set(apps.admin_pubkeys(svc))
    channels = sorted(latest)
    meta = relay.query(
        [{"kinds": [39000, 39001], "#d": channels, "limit": 2 * len(channels)}], sign
    )
    admins: dict[str, set[str]] = {}
    names: dict[str, tuple[str, str]] = {}
    for m in meta or []:
        ch = _tag(m, "d")
        if m["kind"] == 39001:
            admins[ch] = {t[1] for t in m.get("tags", []) if t[0] == "p" and len(t) > 1}
        else:
            names[ch] = (_tag(m, "name") or ch, _tag(m, "about"))
    picked: list[tuple[int, str, dict[str, Any]]] = []
    for ch, by in latest.items():
        # the channel's admins, the belfry's, or the relay's own key (it made the default channels)
        ok = [
            e
            for k, e in by.items()
            if k in admins.get(ch, set()) or k in belfry_admins or k == _groups_pubkey(svc)
        ]
        if not ok:
            continue
        e = max(ok, key=lambda x: x.get("created_at", 0))
        try:
            conf = json.loads(e.get("content") or "{}")
        except ValueError:
            continue
        slug = str(conf.get("slug") or "").lower()
        if not conf.get("on") or not SLUG.match(slug) or slug in RESERVED:
            continue
        # a nice address (no random end, not the channel's own id) only from a belfry admin
        # (#issues 2026-11-15: no squatting names)
        if (
            slug != ch
            and not RANDOM_END.search(slug)
            and e["pubkey"] not in belfry_admins
            and e["pubkey"] != _groups_pubkey(svc)
            and e.get("created_at", 0) >= SLUG_RULE_SINCE
        ):
            continue
        picked.append(
            (
                e["created_at"],
                ch,
                {
                    "slug": slug,
                    "page": _own_page(svc, ch, conf.get("page")),
                    "list": "own" if _members_only(conf) else "public",
                    "periods": _periods(conf),
                    "posts": conf.get("posts")
                    if conf.get("posts") in ("proposal", "event", "request")
                    else "",
                },
            )
        )
    out: dict[str, dict[str, Any]] = {}
    for _at, ch, conf in sorted(picked):  # the earliest claim to a slug keeps it
        if conf["slug"] in out:
            continue
        name, about = names.get(ch, (ch, ""))
        out[conf["slug"]] = {
            "channel": ch,
            "page": conf["page"],
            "list": conf["list"],
            "periods": conf["periods"],
            "posts": conf["posts"],
            "name": name,
            "about": about,
        }
    return out


_bad_pages: set[tuple[str, str]] = set()  # logged once each


def _own_page(svc: Services, channel: str, raw: Any) -> str:
    """The setting's ``page``: where the community's website shows this channel's threads. Only
    https on the belfry's own domain (the apex), no port, query or fragment; anything else is
    ignored ("": the belfry serves the page itself) and logged once. Checked here, where the
    setting is read: clients write it."""
    if raw in (None, ""):
        return ""
    from . import sales

    url = sales._own_url(svc, raw)
    domain = (svc.settings.nip05_domain or "").lower().strip(".")
    if url is None or urlsplit(url).netloc != domain:
        mark = (channel, hashlib.sha256(repr(raw).encode()).hexdigest()[:12])
        if mark not in _bad_pages:
            _bad_pages.add(mark)
            log.warning("webthreads: %s: its page is not on this domain; ignored", channel)
        return ""
    return url.rstrip("/")


def list_url(svc: Services, slug: str, site: dict[str, Any] | None) -> str:
    """Where the channel's threads are listed: the website's own page when it took them over."""
    return (site or {}).get("page") or f"{_public_base(svc)}/{quote(slug)}"


def thread_number(svc: Services, site: dict[str, Any], root_id: str) -> int | None:
    """The thread's short number ("#42", docs/requests.md §1) when its channel is the requests
    channel: given here as webrequests.py gives it, in the order the threads were made. None
    for another channel, or when it can't be read."""
    from . import sales

    try:
        channel = site["channel"]
        if not sales.own(svc) or sales.settings(svc).get("requests_channel") != channel:
            return None
        roots, _ = _events(svc, channel)
        ids = [e["id"] for e in sorted(roots, key=lambda e: (e["created_at"], e["id"]))]
        if root_id not in ids:
            return None
        return sales.store(svc).request_numbers(channel, ids).get(root_id)
    except Exception as exc:  # noqa: BLE001 — the link then names the thread by its id
        log.warning("webthreads: no number for a thread: %s", type(exc).__name__)
        return None


def thread_url(
    svc: Services, slug: str, site: dict[str, Any] | None, root_id: str, key: str = ""
) -> str:
    """A thread's address, with its author's KEY when it is their link. On the website's own
    page it is the list's page with the request's number (its id when it has none), and the key
    rides in the fragment: never sent to a server, never in a log."""
    page = (site or {}).get("page")
    if page:
        ref = thread_number(svc, site or {}, root_id) or root_id
        return f"{page}?id={ref}" + (f"#k={key}" if key else "")
    return f"{_public_base(svc)}/{slug}/{root_id}" + (f"?k={key}" if key else "")


RANDOM_END = re.compile(r"-[a-z0-9]{4}$")
# settings made before the rule keep their address
SLUG_RULE_SINCE = 1_794_740_000  # 2026-11-15


def _members_only(conf: dict[str, Any]) -> bool:
    """The channel's visibility (2026-11-11: "public" or "members"); older settings said "list":
    "own"."""
    v = conf.get("visibility")
    return v == "members" if v else conf.get("list") == "own"


def _periods(conf: dict[str, Any]) -> list[tuple[int, int | None]]:
    """When the channel was public (#issues, 2026-11-13): ``"periods": [[from, to | null], …]``
    in seconds. Moving a channel is never retroactive: what was posted while it was members only
    stays private, what was posted while public stays public. A setting without periods is public
    since ever when it says public, else never."""
    raw = conf.get("periods")
    if not isinstance(raw, list):
        return [] if _members_only(conf) else [(0, None)]
    out: list[tuple[int, int | None]] = []
    for p in raw[-100:]:
        if (
            isinstance(p, list)
            and len(p) == 2
            and isinstance(p[0], int)
            and (p[1] is None or isinstance(p[1], int))
        ):
            out.append((p[0], p[1]))
    return sorted(out)


def public_at(site: dict[str, Any], at: int) -> bool:
    """Was something posted at AT public on the page?"""
    return any(a <= at and (b is None or at < b) for a, b in site.get("periods") or [])


def _tag(e: dict[str, Any], k: str) -> str:
    return next((t[1] for t in e.get("tags", []) if t and t[0] == k and len(t) > 1), "")


# ── keys, pending submissions ─────────────────────────────────────────────────────────────
def _secret(svc: Services) -> bytes:
    return (svc.settings.pass_page_secret or svc.settings.hook_token or "").encode()


def thread_key(svc: Services, root: str, email: str) -> str:
    return hmac.new(
        _secret(svc), f"web:{root}:{email.lower()}".encode(), hashlib.sha256
    ).hexdigest()[:24]


def _param(svc: Services, key: str) -> str:
    if hasattr(svc.odoo, "param"):
        return svc.odoo.param(key, "")
    return _pending_mem.get(key, "")


def _set_param(svc: Services, key: str, value: str) -> None:
    if hasattr(svc.odoo, "set_param"):
        svc.odoo.set_param(key, value)
    else:
        _pending_mem[key] = value


def _public_base(svc: Services) -> str:
    return (svc.settings.site_url or svc.settings.public_url or "").rstrip("/")


def _mailer(svc: Services):
    from . import mailer

    return getattr(svc.odoo, "mailer", None) or mailer.Mailer(svc.settings)


# ── reading the channel ────────────────────────────────────────────────────────────────────
EDIT_KIND = 1010  # the chat's message edits: ``e`` = the message, content = the new text


def _events(svc: Services, channel: str) -> tuple[list[dict], list[dict]]:
    """(root messages, replies) of CHANNEL, without deleted ones, as they were first written."""
    roots, replies, _edits = _read(svc, channel)
    return roots, replies


def _read(svc: Services, channel: str) -> tuple[list[dict], list[dict], dict[str, list[dict]]]:
    """(root messages, replies, edits) of CHANNEL, without deleted ones. Edits: per message, its
    author's kind 1010 events, oldest first (the chat's rule: only the key that signed a message
    edits it, the latest counts)."""
    got = _relay(svc)
    if got is None:
        return [], [], {}
    relay, sign = got
    evs = (
        relay.query([{"kinds": [9, 1111, 5, EDIT_KIND], "#h": [channel], "limit": 2000}], sign)
        or []
    )
    gone = {
        t[1] for e in evs if e["kind"] == 5 for t in e.get("tags", []) if t[0] == "e" and len(t) > 1
    }
    # NIP-29 deletions are 9005 by admins; the relay drops those events itself
    roots = [e for e in evs if e["kind"] == 9 and e["id"] not in gone and not _tag(e, "e")]
    replies = [e for e in evs if e["kind"] == 1111 and e["id"] not in gone]
    # an admin's e-mail answer to a card (mail.sent_card): kind 9 with e = the card
    replies += [
        e for e in evs if e["kind"] == 9 and _tag(e, "e") and _tag(e, "client") == "belfry-mail-out"
    ]
    by_id = {e["id"]: e for e in [*roots, *replies]}
    edits: dict[str, list[dict]] = {}
    for x in sorted(evs, key=lambda e: (e["created_at"], e["id"])):
        if x["kind"] != EDIT_KIND or x["id"] in gone:
            continue
        target = by_id.get(_tag(x, "e"))
        if target is not None and target["pubkey"] == x["pubkey"]:
            edits.setdefault(target["id"], []).append(x)
    return roots, replies, edits


BOLD_LINE = re.compile(r"^\*\*([^\n]+?)\*\*[ \t]*(?:\n|$)")


def _apply(e: dict[str, Any], x: dict[str, Any]) -> dict[str, Any]:
    """E as its edit X says it: X's text, and X's title (its ``subject`` tag; an edit made in
    the chat has none: the bold first line then, as the thread was posted). Every other tag
    stays the original's (whose thread, which offer). ``edited_at``: when."""
    content = x.get("content") or ""
    was = _tag(e, "subject")
    subject = _tag(x, "subject")
    if was and not subject:
        m = BOLD_LINE.match(content)
        subject = m.group(1).strip() if m else was
    tags = [["subject", subject] if t[:1] == ["subject"] and subject else t for t in e["tags"]]
    if subject and not was:
        tags.append(["subject", subject])
    return {**e, "content": content, "tags": tags, "edited_at": x["created_at"]}


def edited(
    e: dict[str, Any],
    edits: dict[str, list[dict]],
    site: dict[str, Any] | None = None,
    mine: bool = True,
) -> dict[str, Any]:
    """E as its author's latest edit says it (``edited_at`` set), or E itself. For anyone but
    the author (MINE false) only the edits made while the channel was public count, as for
    everything else on the page."""
    out = e
    for x in edits.get(e["id"], []):
        if mine or site is None or public_at(site, x["created_at"]):
            out = _apply(out, x)
    return out


def _root_of(e: dict[str, Any]) -> str:
    return _tag(e, "E") if e["kind"] == 1111 else _tag(e, "e")


def _who(svc: Services, events: list[dict]) -> tuple[dict[str, str], dict[str, str]]:
    """(names, roles) of the authors: admin / member / agent / guest; the groups key is whoever
    its mail-from says (from outside)."""
    from . import apps, mentions, whois

    keys = {e["pubkey"] for e in events}
    names = mentions._names(svc, keys)
    roles: dict[str, str] = {}
    try:
        kinds = whois.kinds(svc)
    except Exception:  # noqa: BLE001
        kinds = {}
    admins = set(apps.admin_pubkeys(svc))
    for k in keys:
        roles[k] = "admin" if k in admins else kinds.get(k, "")
    return names, roles


def _author(
    svc: Services, e: dict[str, Any], names: dict[str, str], roles: dict[str, str]
) -> tuple[str, str]:
    """(name, role) of an event's author as the page shows it."""
    if e["pubkey"] == _groups_pubkey(svc):
        if _tag(e, "client") == "belfry-mail-out":
            return "the channel", "member"
        mail_from = _tag(e, "mail-from")
        if mail_from:
            return (_tag(e, "mail-name") or mail_from.split("@")[0]), "external"
        return "Belfry", ""
    return names.get(e["pubkey"]) or "Someone", roles.get(e["pubkey"], "")


def _title(e: dict[str, Any]) -> str:
    subject = _tag(e, "subject")
    if subject:
        return subject
    text = (e.get("content") or "").strip().split("\n", 1)[0]
    text = re.sub(r"nostr:\S+", "", text).replace("**", "").strip()
    text = re.sub(r"^📝 Proposal: ", "", text)  # a proposal's card in the chat
    return (text[:100] + "…") if len(text) > 100 else (text or "(no title)")


def _body(e: dict[str, Any]) -> str:
    text = e.get("content") or ""
    subject = _tag(e, "subject")
    if subject and text.startswith(f"**{subject}**"):
        text = text[len(subject) + 4 :].lstrip()
    return text


# ── pages ──────────────────────────────────────────────────────────────────────────────────
STYLE = """<style>
.wt-head p{color:var(--muted,#71717a);margin:.3em 0 1em}
.wt-list{list-style:none;padding:0;margin:0 0 24px;border:1px solid var(--border,#e4e4e7);border-radius:10px;overflow:hidden}
.wt-list li{padding:12px 14px;border-top:1px solid var(--border,#e4e4e7)}.wt-list li:first-child{border-top:0}
.wt-list a{font-weight:600;text-decoration:none;color:inherit;font-size:16px}
.wt-meta{font-size:13px;color:var(--muted,#71717a);margin-top:2px}
.wt-role{display:inline-block;font-size:11px;font-weight:600;padding:1px 7px;border-radius:99px;border:1px solid var(--border,#e4e4e7);margin-left:4px;vertical-align:1px;text-transform:lowercase}
.wt-role.admin{background:#09090b;color:#fff;border-color:#09090b}.wt-role.member{background:#dcfce7;border-color:#bbf7d0;color:#166534}
.wt-role.external{background:#fef3c7;border-color:#fde68a;color:#92400e}
.wt-c{border:1px solid var(--border,#e4e4e7);border-radius:10px;margin:0 0 12px}
.wt-c .h{padding:8px 12px;border-bottom:1px solid var(--border,#e4e4e7);font-size:14px;background:var(--surface,#fafafa);border-radius:10px 10px 0 0}
.wt-c .b{padding:10px 12px;overflow-wrap:anywhere}
.wt-form label{display:block;margin:10px 0 4px;font-weight:600;font-size:14px}
.wt-form input,.wt-form textarea{width:100%;box-sizing:border-box;font:inherit;font-size:16px;padding:10px 12px;border:1px solid var(--border,#e4e4e7);border-radius:8px;background:var(--surface,#fff);color:inherit}
.wt-form button{margin-top:12px}
.wt-note{font-size:13px;color:var(--muted,#71717a)}
.wt-lead{font-size:16px;margin:0 0 8px}
.wt-tabs{display:flex;gap:16px;margin:-4px 0 10px;font-size:14px}.wt-tabs a{text-decoration:none;color:var(--muted,#71717a)}
.wt-tabs a[aria-current]{color:inherit;font-weight:600}
.wt-state{display:inline-block;font-size:11px;font-weight:600;padding:1px 8px;border-radius:99px;margin-right:8px;vertical-align:2px}
.wt-state.open{background:#dcfce7;color:#166534}.wt-state.closed{background:#ede9fe;color:#5b21b6}
</style>"""


def _role(role: str) -> str:
    label = {"external": "from outside"}.get(role, role)
    return f'<span class="wt-role {E(role)}">{E(label)}</span>' if role else ""


def _ago(ts: int) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%-d %b %Y")


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    from . import theme

    return HTMLResponse(
        theme.frame(title, STYLE + body), status_code=status, headers={"Cache-Control": "no-store"}
    )


def _form(
    slug: str,
    error: str = "",
    values: dict[str, str] | None = None,
    heading: str = "Open a thread",
    button: str = "Send",
) -> str:
    v = values or {}
    err = f'<p class="error" role="alert">{E(error)}</p>' if error else ""
    return f"""<h2 id="new">{E(heading)}</h2>{err}
<form class="wt-form" method="post" action="/{E(slug)}">
<label for="t">Title</label><input id="t" name="title" maxlength="200" required value="{E(v.get("title", ""))}">
<label for="x">What is it about?</label><textarea id="x" name="text" rows="6" maxlength="8000" required>{E(v.get("text", ""))}</textarea>
<label for="n">Your name</label><input id="n" name="name" maxlength="100" required value="{E(v.get("name", ""))}">
<label for="m">Your e-mail</label><input id="m" name="email" type="email" maxlength="200" required value="{E(v.get("email", ""))}">
<p class="wt-note">We mail you a link to confirm: the thread appears once you open it. Answers come by e-mail too.</p>
<input type="text" name="website" value="" tabindex="-1" autocomplete="off" style="position:absolute;left:-9999px" aria-hidden="true">
<button class="btn" type="submit">{E(button)}</button></form>"""


def _labels(svc: Services, channel: str) -> dict[str, list[tuple[int, str, str]]]:
    """Per thread, the members' open / closed labels on it in the chat, oldest first, as
    (when, label, who): NIP-32, namespace ``belfry/inbox``, as e-mail cards have."""
    got = _relay(svc)
    if got is None:
        return {}
    relay, sign = got
    try:
        evs = relay.query([{"kinds": [1985], "#h": [channel], "limit": 2000}], sign) or []
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, list[tuple[int, str, str]]] = {}
    for e in sorted(evs, key=lambda e: e["created_at"]):
        if ["L", "belfry/inbox"] not in [t[:2] for t in e.get("tags", [])]:
            continue
        lab = next(
            (t[1] for t in e["tags"] if t[:1] == ["l"] and len(t) > 2 and t[2] == "belfry/inbox"),
            "",
        )
        card = _tag(e, "e")
        if lab in ("open", "closed") and card:
            out.setdefault(card, []).append((e["created_at"], lab, e["pubkey"]))
    return out


def _states(svc: Services, channel: str) -> dict[str, str]:
    """Each thread's state, open or closed: the latest label on it."""
    return {card: labs[-1][1] for card, labs in _labels(svc, channel).items()}


def _chat_link(svc: Services, channel: str) -> str:
    return f"https://chat.{svc.settings.nip05_domain}/#/c/{quote(channel, safe='')}"


def list_page(
    svc: Services,
    slug: str,
    site: dict[str, Any],
    error: str = "",
    values: dict[str, str] | None = None,
    state: str = "open",
) -> HTMLResponse:
    """Two templates (#issues, 2026-11-13). **Issues**, a public channel: like GitHub issues,
    everyone reads every thread, Open / Closed. **Requests**, a members-only channel: like
    writing to hello@, the form first, and nobody from outside sees anyone else's. Both: members
    sign in and post from the chat; anyone else posts as a guest with a name and an e-mail."""
    from . import followers

    head = f'<div class="wt-head"><h1>{E(site["name"])}</h1>'
    if site["about"]:
        head += f"<p>{followers.text_html(site['about'])}</p>"
    head += "</div>"
    body = head
    proposals = site.get("posts") == "proposal"
    requests = site.get("posts") == "request"
    public = site["list"] == "public"
    signin = (
        f'<p class="wt-note wt-signin">Have an account here? <a href="{E(_chat_link(svc, site["channel"]))}">Sign in</a>'
        " and post from the chat. No account? Post as a guest: your name and e-mail are enough.</p>"
    )
    if not public and not proposals:
        body += (
            '<p class="wt-lead">Write to the members of this channel. Only they read what you send, '
            "and their answers come to you by e-mail.</p>"
        )
        if site.get("posts") in ("", None, "request"):
            body += signin + _form(slug, error, values, "Send a request", "Send")
    if site.get("periods"):
        roots, replies, edits = _read(svc, site["channel"])
        # only what was posted while the channel was public (never retroactive)
        roots = [edited(e, edits, site, False) for e in roots if public_at(site, e["created_at"])]
        replies = [r for r in replies if public_at(site, r["created_at"])]
        count: dict[str, int] = {}
        last: dict[str, int] = {}
        for r in replies:
            root = _root_of(r)
            count[root] = count.get(root, 0) + 1
            last[root] = max(last.get(root, 0), r["created_at"])
        if proposals:
            roots = [e for e in roots if _tag(e, "proposal")]
        states = {} if proposals else _states(svc, site["channel"])
        closed = [e for e in roots if states.get(e["id"]) == "closed"]
        opened = [e for e in roots if states.get(e["id"]) != "closed"]
        state = "closed" if state == "closed" and not proposals else "open"
        shown = closed if state == "closed" else (roots if proposals else opened)
        shown.sort(key=lambda e: max(e["created_at"], last.get(e["id"], 0)), reverse=True)
        shown = shown[:MAX_THREADS]
        names, roles = _who(svc, shown)
        items = []
        for e in shown:
            who, role = _author(svc, e, names, roles)
            n = count.get(e["id"], 0)
            mark = (
                ""
                if proposals
                else (
                    '<span class="wt-state closed">Closed</span>'
                    if states.get(e["id"]) == "closed"
                    else '<span class="wt-state open">Open</span>'
                )
            )
            items.append(
                f'<li>{mark}<a href="/{E(slug)}/{E(e["id"])}">{E(_title(e))}</a>'
                f'<div class="wt-meta">{E(who)}{_role(role)} · {_ago(e["created_at"])}'
                f"{f' · {n} {chr(0x1F4AC)}' if n else ''}</div></li>"
            )
        word = "Proposals" if proposals else ("Requests" if requests else "Issues")
        if not proposals and public:
            body += f'<p><a class="btn" href="#new">{"Make a request" if requests else "Open an issue"}</a></p>'
        if not public:
            body += '<p class="wt-note">These were posted while the channel was public.</p>'
        tabs = ""
        if not proposals:
            tabs = (
                '<nav class="wt-tabs">'
                f'<a href="/{E(slug)}"{" aria-current=page" if state == "open" else ""}>Open <b>{len(opened)}</b></a>'
                f'<a href="/{E(slug)}?state=closed"{" aria-current=page" if state == "closed" else ""}>Closed <b>{len(closed)}</b></a>'
                "</nav>"
            )
        empty = (
            "No proposals yet."
            if proposals
            else (
                "Nothing closed yet."
                if state == "closed"
                else ("Nothing open: be the first." if public else "Nothing open.")
            )
        )
        body += f"<h2>{word}</h2>{tabs}" + (
            f'<ul class="wt-list">{"".join(items)}</ul>' if items else f"<p>{empty}</p>"
        )
    elif not public and not proposals:
        pass
    if public and site.get("posts") in ("", None, "request"):
        body += signin + _form(
            slug, error, values, "Make a request" if requests else "Open an issue"
        )
    return _page(site["name"], body, 400 if error else 200)


def thread_page(
    svc: Services, slug: str, site: dict[str, Any], root_id: str, k: str = "", note: str = ""
) -> HTMLResponse:
    from . import followers

    roots, replies, edits = _read(svc, site["channel"])
    root = next((e for e in roots if e["id"] == root_id), None)
    if root is None:
        return _page("Not found", "<h1>No such thread</h1>", 404)
    owner = _owner(svc, root)
    mine = bool(owner) and hmac.compare_digest(k or "", thread_key(svc, root_id, owner))
    root = edited(root, edits, site, mine)
    if not public_at(site, root["created_at"]) and not mine:
        return _page(
            "Private thread",
            "<h1>This thread is private</h1><p>Open it with the link we mailed you.</p>",
            403,
        )
    comments = sorted(
        (
            edited(r, edits, site, mine)
            for r in replies
            if _root_of(r) == root_id and (mine or public_at(site, r["created_at"]))
        ),
        key=lambda e: e["created_at"],
    )
    names, roles = _who(svc, [root, *comments])

    def card(e: dict[str, Any]) -> str:
        who, role = _author(svc, e, names, roles)
        mark = ' · <span class="wt-edited">edited</span>' if e.get("edited_at") else ""
        return (
            f'<div class="wt-c"><div class="h"><b>{E(who)}</b>{_role(role)} · {_ago(e["created_at"])}{mark}</div>'
            f'<div class="b">{followers.text_html(_body(e))}</div></div>'
        )

    body = (
        f'<p class="wt-note"><a href="/{E(slug)}">← {E(site["name"])}</a></p><h1>{E(_title(root))}</h1>'
        + (f'<p class="notice" role="status">{E(note)}</p>' if note else "")
        + _offer_details(root)
        + card(root)
        + "".join(card(c) for c in comments)
    )
    if mine:
        body += f"""<form class="wt-form" method="post" action="/{E(slug)}/{E(root_id)}?k={E(k)}">
<label for="x">Add a comment</label><textarea id="x" name="text" rows="4" maxlength="8000" required></textarea>
<button class="btn" type="submit">Comment</button></form>"""
    else:
        body += '<p class="wt-note">Members answer in the chat. Opened this thread? Comment through the link we mailed you.</p>'
    return _page(_title(root), body)


OFFER_LABELS = {"volunteer": "Volunteer", "food": "Food", "workshop": "Workshop", "other": "Other"}


def _offer_details(root: dict[str, Any]) -> str:
    """What an offer in kind says besides its text (sales.py's tags on the message): the kind
    of help and the days."""
    import datetime as dt

    label = OFFER_LABELS.get(_tag(root, "offer-label"), "")
    days = []
    for t in root.get("tags", []):
        if t[:1] == ["offer-days"]:
            for d in t[1:32]:
                with contextlib.suppress(ValueError, TypeError):
                    days.append(dt.date.fromisoformat(d).strftime("%-d %b %Y"))
    parts = [x for x in (label, ", ".join(days)) if x]
    return f'<p class="wt-note wt-offer">{E(" · ".join(parts))}</p>' if parts else ""


# ── who writes from the web: an account like everyone's ───────────────────────────────────
GUEST_DAYS = 90


def web_identity(svc: Services, channel: str, name: str, email: str) -> dict[str, Any] | None:
    """The account behind a proven address (Xavier, 2026-11-11: "create an identity for them on
    the belfry / bunker so that we can reuse all the same pipes"): a member posts as themselves;
    anyone else becomes a guest of the channel with a key in the signer, like a guest who joined
    from an invite, with this address saved. ``{"partner", "pubkey"}``; None when it can't be
    made (no register, the signer is down): the page then posts the old way, as a card."""
    from . import core, guests, mentions

    make = getattr(svc.odoo, "web_guest", None)
    if make is None:
        return None
    try:
        got = make(channel, name, email, GUEST_DAYS)
        pid = int(got["partner_id"])
        if got["kind"] == "new":
            row = svc.odoo.read_one("res.partner", pid, ["name"]) or {}
            partner = {"id": pid, "name": row.get("name") or name}
            pubkey, npub, _ucan = core._ensure_keycast_user(svc, partner, f"guest-{pid}")
            svc.odoo.write("res.partner", [pid], {"belfry_npub": npub})
            svc.odoo.write(
                "belfry.pass", [got["pass_id"]], {"keycast_pubkey": pubkey, "state": "active"}
            )
            # their thread's replies come by the hourly thread mail, not also the daily digest
            mentions.save(svc, pid, f="off")
            got["pubkey"] = pubkey
        if got["kind"] in ("new", "guest"):
            guests.admitted(svc, got["pass_id"])
        return {"partner": pid, "pubkey": got["pubkey"]}
    except Exception as exc:  # noqa: BLE001
        log.warning("webthreads: no account for a web author: %r", exc)
        return None


def _partner_of_key(svc: Services, pubkey: str) -> int | None:
    from .core import m2o_id

    rows = svc.odoo.search_read(
        "belfry.pass", [["keycast_pubkey", "=", pubkey], ["state", "=", "active"]], ["partner_id"]
    )
    return m2o_id(rows[0].get("partner_id")) if rows else None


def _email_of_key(svc: Services, pubkey: str) -> str:
    pid = _partner_of_key(svc, pubkey)
    row = svc.odoo.read_one("res.partner", pid, ["email"]) if pid else None
    return ((row or {}).get("email") or "").lower()


def _owner(svc: Services, root: dict[str, Any] | None) -> str:
    """Whose thread it is, for its link's key: the author's key for a thread opened from the
    web by an account, the address for the older cards (posted by the groups key)."""
    if not root or _tag(root, "client") != TAG:
        return ""
    if root["pubkey"] == _groups_pubkey(svc):
        return _tag(root, "mail-from")
    return root["pubkey"]


# ── actions ────────────────────────────────────────────────────────────────────────────────
def submit(
    svc: Services, slug: str, site: dict[str, Any], form: dict[str, str], ip: str
) -> HTMLResponse:

    if site.get("posts") in ("proposal", "event"):  # one type of post, made in the chat
        return _page("Not here", "<h1>This page takes no threads</h1>", 403)
    values = {k: (form.get(k) or "").strip() for k in ("title", "text", "name", "email")}
    if form.get("website"):  # the field people don't see: a bot filled it
        return _page("Check your e-mail", "<h1>Check your e-mail</h1>")
    email = values["email"].lower()
    if not all(values.values()):
        return list_page(svc, slug, site, "Fill in every field.", values)
    if not EMAIL.match(email):
        return list_page(svc, slug, site, "That e-mail address does not look right.", values)
    if start_thread(svc, slug, site, values["title"], values["text"], values["name"], email, ip) is None:
        return list_page(
            svc, slug, site, "Too many threads in a short time: try again in an hour.", values
        )
    return _page(
        "Check your e-mail",
        f"<h1>Check your e-mail</h1><p>We sent a link to <b>{E(email)}</b>. "
        "Your thread opens when you click it.</p>",
    )


def start_thread(
    svc: Services,
    slug: str,
    site: dict[str, Any],
    title: str,
    text: str,
    name: str,
    email: str,
    ip: str,
    extra: list[list[str]] | None = None,
    hook: dict[str, str] | None = None,
    public_note: bool = False,
    subject: str = "",
) -> str | None:
    """A thread from outside, waiting for its author's address: the confirmation link is mailed;
    nothing is posted before it is pressed. EXTRA: more tags on the message (sales.py's offers);
    HOOK ``{"kind": "offer", "id"}``: told when the thread is posted. PUBLIC_NOTE: the mail says
    the thread and the name will be public, the address never. SUBJECT: the confirmation mail's own
    subject (sales.py's requests). Returns the token, or None when the limits say no."""
    from . import mail

    email = email.strip().lower()
    if mail._limited("web-ip:" + ip, 10, 3600) or mail._limited("web-mail:" + email, 5, 3600):
        return None
    token = secrets.token_urlsafe(18)
    pending = {
        "slug": slug,
        "channel": site["channel"],
        "title": title[:200],
        "text": text[:8000],
        "name": name[:100],
        "email": email,
        "at": int(time.time()),
        "extra": extra or [],
        "hook": hook or {},
    }
    _set_param(svc, "belfry.web.pending." + token, json.dumps(pending))
    link = f"{_public_base(svc)}/{slug}/confirm/{token}"
    lines = [
        f"Hello {name},",
        f"To open your thread “{title}” in {site['name']}, confirm it is your address:",
    ]
    if public_note:
        lines.append(
            f"Your request and your name will be public on {list_url(svc, slug, site)}, like "
            "the others there. Your e-mail address is never shown on the public page."
        )
    _mailer(svc).send(
        email,
        subject or f"Confirm your thread: {title[:120]}",
        lines,
        (link, "Confirm and open the thread"),
        "If you did not ask for this, ignore this mail: nothing is posted without your confirmation.",
    )
    return token


def post_thread(
    svc: Services,
    slug: str,
    site: dict[str, Any],
    title: str,
    text: str,
    name: str,
    email: str,
    ip: str,
    partner: int,
    extra: list[list[str]] | None = None,
) -> dict[str, str]:
    """A thread by someone logged in on the website (sitesessions.py): their address is proven
    already, so nothing waits for a link; it is posted now, by their own key, as ``confirm``
    posts it. PARTNER: their account. ``{"id", "url", "my_url"}``; ApiError 429 ``too_many``
    (the limits of ``start_thread``), 409 ``own_key`` (they hold their own key: the belfry can't
    sign for them), 503 ``unavailable``."""
    from . import drops, mail, sitesessions
    from .holder_invites import ApiError

    email = email.strip().lower()
    if mail._limited("web-ip:" + ip, 10, 3600) or mail._limited("web-mail:" + email, 5, 3600):
        raise ApiError(429, "too_many", "Too many requests in a short time: try in an hour.")
    # a guest of something else becomes a guest of this channel too; a member posts as is
    me = web_identity(svc, site["channel"], name, email)
    if me is None or int(me["partner"]) != int(partner):
        log.warning("webthreads: no account to post a thread from the website as")
        raise ApiError(503, "unavailable", "That did not work just now. Try again.")
    acct = sitesessions.account(svc, int(partner))
    if acct is None:
        raise ApiError(503, "unavailable", "That did not work just now. Try again.")
    sitesessions.signer(svc, acct)  # 409 own_key, 503 signer: before anything is posted
    tags = [["subject", title[:300]], *[t for t in extra or [] if isinstance(t, list) and t]]
    try:
        event = drops.post(
            svc, site["channel"], f"**{title[:200]}**\n\n{text[:8000]}", tags, int(partner), TAG
        )
    except Exception as exc:  # noqa: BLE001 — the signer or the relay
        log.warning("webthreads: a thread from the website was not posted: %s", type(exc).__name__)
        raise ApiError(503, "unavailable", "That did not work just now. Try again.") from exc
    k = thread_key(svc, event["id"], event["pubkey"])
    url = thread_url(svc, slug, site, event["id"])
    my_url = thread_url(svc, slug, site, event["id"], k)
    try:
        _mailer(svc).send(
            email,
            f"Your thread: {title[:120]}",
            [
                f"Hello {name},",
                "Your thread is open. This link is yours: read the answers and comment there.",
            ],
            (my_url, "Open your thread"),
            "Keep this mail: the link is how you come back.",
        )
    except Exception as exc:  # noqa: BLE001 — the thread is posted; they are logged in
        log.warning("webthreads: a thread's own link was not mailed: %s", type(exc).__name__)
    from . import webrequests

    webrequests.forget()
    return {"id": event["id"], "url": url, "my_url": my_url}


def confirm(svc: Services, slug: str, token: str, site: dict[str, Any] | None = None) -> Response:
    """SITE: the channel's setting when its page lives on the community's website (``page``):
    the thread's links then point there."""
    from . import drops

    raw = (
        _param(svc, "belfry.web.pending." + token)
        if re.match(r"^[A-Za-z0-9_-]{10,60}$", token)
        else ""
    )
    try:
        p = json.loads(raw) if raw else None
    except ValueError:
        p = None
    if not p or p.get("slug") != slug:
        return _page("Link used", "<h1>This link was used already or is too old</h1>", 404)
    if p.get("posted"):
        owner = p.get("owner") or p["email"]
        k = thread_key(svc, p["posted"], owner)
        if (site or {}).get("page"):
            return RedirectResponse(thread_url(svc, slug, site, p["posted"], k), 303)
        return RedirectResponse(f"/{slug}/{p['posted']}?k={k}", 303)
    if time.time() - p.get("at", 0) > PENDING_SECONDS:
        return _page(
            "Link too old", "<h1>This link is too old</h1><p>Send the thread again.</p>", 410
        )
    text = f"**{p['title']}**\n\n{p['text']}"
    more = [t for t in p.get("extra") or [] if isinstance(t, list) and t]
    me = web_identity(svc, p["channel"], p["name"], p["email"])
    if me is not None:  # as themselves, an account like everyone's
        subject = [["subject", p["title"][:300]], *more]
        event = drops.post(svc, p["channel"], text, subject, me["partner"], client=TAG)
    else:  # no account could be made: a card by the groups key, as before
        extra = [
            ["subject", p["title"][:300]],
            ["mail-from", p["email"]],
            ["mail-name", p["name"][:100]],
            ["belfry-auth", "pass"],  # the address answered the link
            *more,
        ]
        event = drops.post(svc, p["channel"], text, extra, None, client=TAG)
    owner = event["pubkey"] if event["pubkey"] != _groups_pubkey(svc) else p["email"]
    p["posted"], p["owner"] = event["id"], owner
    _set_param(svc, "belfry.web.pending." + token, json.dumps(p))
    k = thread_key(svc, event["id"], owner)
    url = thread_url(svc, slug, site, event["id"], k)
    hook = p.get("hook") or {}
    if hook.get("kind") == "offer":
        from . import sales

        try:
            sales.offer_posted(svc, hook["id"], event, thread_url(svc, slug, site, event["id"]), url)
        except Exception:  # noqa: BLE001 — the thread is posted; the console shows the offer
            log.exception("webthreads: offer %s not marked posted", hook.get("id"))
    _mailer(svc).send(
        p["email"],
        f"Your thread: {p['title'][:120]}",
        [
            f"Hello {p['name']},",
            "Your thread is open. This link is yours: read the answers and comment there.",
        ],
        (url, "Open your thread"),
        "Keep this mail: the link is how you come back.",
    )
    if (site or {}).get("page"):
        return RedirectResponse(url, 303)
    return RedirectResponse(f"/{slug}/{event['id']}?k={k}&new=1", 303)


def comment(
    svc: Services, slug: str, site: dict[str, Any], root_id: str, k: str, text: str
) -> Response:
    got = add_comment(svc, site, root_id, (text or "").strip()[:COMMENT_MAX], k)
    if got == "not_author":
        return _page("Not allowed", "<h1>Only the thread's author comments here</h1>", 403)
    if got == "too_many":
        return _page("Slow down", "<h1>Too many comments in a short time</h1>", 429)
    if got == "unavailable":
        return _page("Not now", "<h1>Something went wrong</h1><p>Try again in a minute.</p>", 503)
    return RedirectResponse(f"/{slug}/{root_id}?k={k}", 303)


COMMENT_MAX = 8000


def is_author(svc: Services, root: dict[str, Any] | None, k: str) -> bool:
    """Whether K is the key of this thread's own link (the one mailed to its author)."""
    owner = _owner(svc, root)
    return bool(owner and root) and hmac.compare_digest(
        (k or "").encode(), thread_key(svc, root["id"], owner).encode()
    )


def add_comment(
    svc: Services, site: dict[str, Any], root_id: str, text: str, k: str = "", proven: bool = False
) -> str:
    """The author's comment under their own thread: by the key of their link (K), or PROVEN
    another way by the caller (webrequests.py: the key that made the offer). "ok", "empty",
    "not_author", "too_many" or "unavailable" (the signer)."""
    from . import mail

    roots, _ = _events(svc, site["channel"])
    root = next((e for e in roots if e["id"] == root_id), None)
    owner = _owner(svc, root)
    if not owner or not (proven or is_author(svc, root, k)):
        return "not_author"
    if not text:
        return "empty"
    if mail._limited("web-comment:" + owner, 20, 3600):
        return "too_many"
    relay, _api_sign = _relay(svc)
    author = root["pubkey"]
    tags = [
        ["h", site["channel"]],
        ["E", root_id, "", author],
        ["K", "9"],
        ["P", author],
        ["e", root_id, "", author],
        ["k", "9"],
        ["p", author],
        ["client", TAG],
    ]
    sign = _author_sign(svc, root, owner)
    if sign is None:
        return "unavailable"
    if owner != author:  # an older card: the groups key writes for the address
        tags += [["mail-from", owner], ["mail-name", _tag(root, "mail-name")]]
    event = sign({"kind": 1111, "created_at": int(time.time()), "content": text, "tags": tags})
    relay.publish(event, sign)
    from . import webrequests

    webrequests.forget()  # the JSON of this thread is kept a few seconds: not across this
    return "ok"


def add_reply(
    svc: Services, site: dict[str, Any], root_id: str, text: str, pubkey: str, sign: Any
) -> str:
    """A comment under a thread by someone who has the channel, signed with their own key (SIGN:
    the signer, for PUBKEY): a member, a steward or a guest of the channel logged in on the
    website (sitesessions.py; the caller checked that they may). "ok", "unknown" (no such
    thread) or "empty"."""
    roots, _ = _events(svc, site["channel"])
    root = next((e for e in roots if e["id"] == root_id), None)
    if root is None:
        return "unknown"
    if not text:
        return "empty"
    author = root["pubkey"]
    tags = [
        ["h", site["channel"]],
        ["E", root_id, "", author],
        ["K", "9"],
        ["P", author],
        ["e", root_id, "", author],
        ["k", "9"],
        ["p", author],
        ["client", TAG],
    ]
    event = sign({"kind": 1111, "created_at": int(time.time()), "content": text, "tags": tags})
    if event.get("pubkey") != pubkey:
        raise RuntimeError("the signer signed for another key")
    relay, _api_sign = _relay(svc)
    relay.publish(event, sign)
    from . import webrequests

    webrequests.forget()
    return "ok"


def _author_sign(svc: Services, root: dict[str, Any], owner: str) -> Any:
    """What signs for the thread's author: the signer for their own account (the thread is by
    their key), the groups key for an older card (it wrote the card for their address). None
    when the signer can't."""
    from . import community

    if owner != root["pubkey"]:
        return _relay(svc)[1]
    pid = _partner_of_key(svc, root["pubkey"])
    found = community.member_signer(svc, pid) if pid else None
    return found[1] if found is not None else None


def own_comment(svc: Services, root: dict[str, Any], c: dict[str, Any]) -> bool:
    """Whether the reply C is by the thread's author: signed by the key that signed the thread
    and, on an older card (the groups key writes for everyone from outside), for the same
    address."""
    owner = _owner(svc, root)
    if not owner or c["kind"] != 1111 or c["pubkey"] != root["pubkey"]:
        return False
    return owner == root["pubkey"] or _tag(c, "mail-from").lower() == owner.lower()


EDITS_PER_HOUR = 20
# tags an edit never carries over: the three it sets itself, the title (set again), and the
# buttons (docs/actions.md: an edit never changes them)
EDIT_DROPS = ("h", "e", "k", "subject", "button")


def edit(
    svc: Services,
    site: dict[str, Any],
    root_id: str,
    k: str = "",
    proven: bool = False,
    title: str | None = None,
    body: str | None = None,
    comment_id: str = "",
    text: str | None = None,
    actor: tuple[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """The author's edit of their thread (TITLE and / or BODY) or of one of their own comments
    (COMMENT_ID, TEXT), proven as for a comment. Published as the chat reads edits: a kind 1010
    in the channel by the key that signed the message, ``e`` = the message, ``k`` = its kind,
    the new text as content; the message's other tags ride along, the thread's title in
    ``subject`` and as the bold first line (where the page and the chat read it). The first
    version and every edit stay on the relay. → ("ok", {"title", "body"} as they are now),
    ("same", …) when nothing changes (no event), or "not_author", "unknown", "not_yours",
    "too_many", "unavailable".

    ACTOR ``(pubkey, sign)``: someone logged in on the website (sitesessions.py) who is not
    the thread's author edits a comment **they** signed (COMMENT_ID, TEXT); nothing else."""
    roots, replies, edits = _read(svc, site["channel"])
    root = next((e for e in roots if e["id"] == root_id), None)
    owner = _owner(svc, root)
    if actor is not None:
        if root is None:
            return "unknown", {}
        if not comment_id:
            return "not_author", {}
    elif root is None or not owner or not (proven or is_author(svc, root, k)):
        return "not_author", {}
    now_root = edited(root, edits)
    if comment_id:
        target = next(
            (r for r in replies if r["id"] == comment_id and _root_of(r) == root_id), None
        )
        if target is None:
            return "unknown", {}
        if actor is not None:
            if target["kind"] != 1111 or target["pubkey"] != actor[0]:
                return "not_yours", {}
        elif not own_comment(svc, root, target):
            return "not_yours", {}
        content = text or ""
        same = edited(target, edits).get("content") == content
        subject = ""
    else:
        target = root
        subject = title if title is not None else _tag(now_root, "subject")
        new_body = body if body is not None else _body(now_root)
        same = subject == _tag(now_root, "subject") and new_body == _body(now_root)
        content = f"**{subject}**\n\n{new_body}" if subject else new_body
    now = {"title": subject, "body": new_body} if not comment_id else {}
    if same:
        return "same", now
    from . import mail

    # a commenter's edits count apart: they never use up the author's
    if mail._limited(
        "web-edit:" + root_id + (":" + actor[0] if actor is not None else ""), EDITS_PER_HOUR, 3600
    ):
        return "too_many", {}
    sign = actor[1] if actor is not None else _author_sign(svc, root, owner)
    if sign is None:
        return "unavailable", {}
    tags = [
        ["h", site["channel"]],
        ["e", target["id"]],
        ["k", str(target["kind"])],
        *([["subject", subject]] if subject else []),
        *(t for t in target.get("tags", []) if t and t[0] not in EDIT_DROPS),
    ]
    # after the message and after its last edit, whatever the clocks: the latest counts
    last = max([target["created_at"], *(x["created_at"] for x in edits.get(target["id"], []))])
    event = sign(
        {
            "kind": EDIT_KIND,
            "created_at": max(int(time.time()), last + 1),
            "content": content,
            "tags": tags,
        }
    )
    relay, _api_sign = _relay(svc)
    relay.publish(event, sign)
    from . import webrequests

    webrequests.forget()
    return "ok", now


# ── members' replies, mailed to the author from outside ───────────────────────────────────
def notify(svc: Services, now: int | None = None) -> int:
    """Mail each author from outside the replies by members since the last round: at most once an
    hour (Xavier, 2026-11-11: "Email isn't supposed to be real time"), one mail per address that
    gathers every thread of theirs with news. Returns how many mails went out."""
    now = now or int(time.time())
    last = int(_param(svc, "belfry.web.notified") or 0) or now - DIGEST_SECONDS
    if now - last < DIGEST_SECONDS:
        return 0
    gk = _groups_pubkey(svc)
    # address → [(slug, site, root, replies)]
    news: dict[str, list[tuple[str, dict[str, Any], dict[str, Any], list[dict]]]] = {}
    for slug, site in sites(svc).items():
        roots, replies, edits = _read(svc, site["channel"])
        # the thread by the title it has now (its author may have edited it)
        by_id = {e["id"]: edited(e, edits) for e in roots if _tag(e, "client") == TAG}
        new: dict[str, list[dict]] = {}
        for r in replies:
            root = by_id.get(_root_of(r))
            if root is None or not last < r["created_at"] <= now or r["pubkey"] == gk:
                continue
            if r["pubkey"] == root["pubkey"]:  # their own comment
                continue
            new.setdefault(root["id"], []).append(r)
        for root_id, rs in new.items():
            root = by_id[root_id]
            owner = _owner(svc, root)
            email = _email_of_key(svc, owner) if owner == root["pubkey"] else owner.lower()
            if email:
                news.setdefault(email, []).append((slug, site, root, rs))
    if news:
        everyone = [r for items in news.values() for (_s, _site, _root, rs) in items for r in rs]
        names, _roles = _who(svc, everyone)
    sent = 0
    for email, items in news.items():
        lines: list[str] = []
        for slug, site, root, rs in items:
            if len(items) > 1:
                lines.append(f"“{_title(root)}” ({site['name']}):")
            for r in sorted(rs, key=lambda x: x["created_at"]):
                lines.append(f"{names.get(r['pubkey']) or 'A member'}: {_body(r)[:600]}")
            if len(items) > 1:
                link = thread_url(svc, slug, site, root["id"], thread_key(svc, root["id"], _owner(svc, root)))
                lines.append(f"Open it: {link}")
        slug, site, root, rs = items[0]
        url = thread_url(svc, slug, site, root["id"], thread_key(svc, root["id"], _owner(svc, root)))
        n = sum(len(x[3]) for x in items)
        if len(items) == 1:
            subject = f"Re: {_title(root)[:150]}"
            head = f"New in your thread “{_title(root)}” ({site['name']}):"
        else:
            subject = f"{n} new replies in your threads"
            head = f"New in your {len(items)} threads:"
        _mailer(svc).send(
            email,
            subject,
            [head, *lines],
            (url, "Open the thread"),
            "You get this because you opened a thread. Replies are gathered and sent at most once an hour.",
        )
        sent += 1  # built and handed to the mail provider (kept in tests)
    _set_param(svc, "belfry.web.notified", str(now))
    return sent


# ── wiring ─────────────────────────────────────────────────────────────────────────────────
def _route_taken(app: FastAPI, scope: dict) -> bool:
    from starlette.routing import Match

    return any(
        route.matches(scope)[0] != Match.NONE
        for route in app.router.routes
        if getattr(route, "path", "/") != "/"
    )


def install(app: FastAPI) -> None:
    """The middleware: call it after the apex homepage's, so it runs first and a channel's page
    is answered before the homepage takes the path."""

    @app.middleware("http")
    async def channel_pages(request: Request, call_next):
        parts = request.url.path.strip("/").split("/")
        slug = parts[0].lower() if parts else ""
        if not SLUG.match(slug) or slug in RESERVED or len(parts) > 3:
            return await call_next(request)
        s = request.app.state.services
        try:
            found = await run_in_threadpool(sites, s)
        except Exception:  # noqa: BLE001
            return await call_next(request)
        site = found.get(slug)
        if site is None or _route_taken(request.app, request.scope):
            return await call_next(request)
        # one address: the public website's (<domain>/<slug>); the service's hosts send there
        home = urlsplit(s.settings.site_url or "")
        host = request.headers.get("host", "").lower()
        if site.get("page") and host == f"preview.{s.settings.nip05_domain}".lower():
            # handed to the website (docs/requests.md): a proposed version of the site shows
            # its own page for this channel on the preview host too
            return await call_next(request)
        if home.netloc and host != home.netloc.lower():
            if request.method not in ("GET", "HEAD"):
                return await call_next(request)
            q = ("?" + request.url.query) if request.url.query else ""
            return RedirectResponse(f"{home.scheme}://{home.netloc}{request.url.path}{q}", 301)
        ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
            request.client.host if request.client else ""
        )
        m = request.method
        if site.get("page"):
            # the community's website shows this channel itself (docs/requests.md): no HTML
            # from here. Two links people hold in a mail still work: the confirmation link
            # (it posts the thread), and an older thread link, which moves to the page
            if len(parts) == 3 and parts[1] == "confirm" and m in ("GET", "HEAD"):
                try:
                    return await run_in_threadpool(confirm, s, slug, parts[2], site)
                except Exception as exc:  # noqa: BLE001
                    log.warning("webthreads: confirm on %s failed: %r", slug, exc, exc_info=True)
                    return _page(
                        "Not now", "<h1>Something went wrong</h1><p>Try again in a minute.</p>", 503
                    )
            if len(parts) == 2 and re.match(r"^[0-9a-f]{64}$", parts[1]) and m in ("GET", "HEAD"):
                k = request.query_params.get("k", "")
                k = k if re.match(r"^[0-9a-f]{24}$", k) else ""
                there = await run_in_threadpool(thread_url, s, slug, site, parts[1], k)
                return RedirectResponse(there, 302)
            return await call_next(request)
        try:
            if len(parts) == 1 and m in ("GET", "HEAD"):
                st = "closed" if request.query_params.get("state") == "closed" else "open"
                return await run_in_threadpool(list_page, s, slug, site, "", None, st)
            if len(parts) == 1 and m == "POST":
                form = {k: str(v) for k, v in (await request.form()).items()}
                return await run_in_threadpool(submit, s, slug, site, form, ip)
            if len(parts) == 3 and parts[1] == "confirm" and m in ("GET", "HEAD"):
                return await run_in_threadpool(confirm, s, slug, parts[2])
            if len(parts) == 2 and re.match(r"^[0-9a-f]{64}$", parts[1]):
                k = request.query_params.get("k", "")
                if m == "POST":
                    form = await request.form()
                    return await run_in_threadpool(
                        comment, s, slug, site, parts[1], k, str(form.get("text") or "")
                    )
                note = (
                    "Your thread is open. Keep the mail we sent: its link is how you come back."
                    if request.query_params.get("new")
                    else ""
                )
                return await run_in_threadpool(thread_page, s, slug, site, parts[1], k, note)
        except Exception as exc:  # noqa: BLE001
            log.warning("webthreads: %s %s failed: %r", m, request.url.path, exc, exc_info=True)
            return _page(
                "Not now", "<h1>Something went wrong</h1><p>Try again in a minute.</p>", 503
            )
        return await call_next(request)


@contextlib.asynccontextmanager
async def running(app: FastAPI):
    async def loop() -> None:
        while True:
            await asyncio.sleep(LOOP_SECONDS)
            try:
                await run_in_threadpool(notify, app.state.services)
            except Exception as exc:  # noqa: BLE001
                log.warning("webthreads: notify failed: %r", exc)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()


def page_url(svc: Services, slug: str) -> str:
    try:
        site = sites(svc).get(slug)
    except Exception:  # noqa: BLE001
        site = None
    return list_url(svc, slug, site)
