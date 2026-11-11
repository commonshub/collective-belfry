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
  the link they get).
  Only the latest setting by an admin of that channel (39001) or of the belfry counts; a slug
  taken by an earlier channel, or one of the service's own paths, is ignored.
- **The page** ``<domain>/<slug>``: the channel's name and description, a form to open a thread
  (title, text, name, e-mail), and, when public, the threads like GitHub issues: title, author and
  their role (admin, member, agent, guest, from outside), replies, last activity.
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
        picked.append(
            (
                e["created_at"],
                ch,
                {
                    "slug": slug,
                    "list": "own" if _members_only(conf) else "public",
                    "posts": conf.get("posts")
                    if conf.get("posts") in ("proposal", "event")
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
            "list": conf["list"],
            "posts": conf["posts"],
            "name": name,
            "about": about,
        }
    return out


def _members_only(conf: dict[str, Any]) -> bool:
    """The channel's visibility (2026-11-11: "public" or "members"); older settings said "list":
    "own"."""
    v = conf.get("visibility")
    return v == "members" if v else conf.get("list") == "own"


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
def _events(svc: Services, channel: str) -> tuple[list[dict], list[dict]]:
    """(root messages, replies) of CHANNEL, without deleted ones."""
    got = _relay(svc)
    if got is None:
        return [], []
    relay, sign = got
    evs = relay.query([{"kinds": [9, 1111, 5], "#h": [channel], "limit": 2000}], sign) or []
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
    return roots, replies


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


def _form(slug: str, error: str = "", values: dict[str, str] | None = None) -> str:
    v = values or {}
    err = f'<p class="error" role="alert">{E(error)}</p>' if error else ""
    return f"""<h2 id="new">Open a thread</h2>{err}
<form class="wt-form" method="post" action="/{E(slug)}">
<label for="t">Title</label><input id="t" name="title" maxlength="200" required value="{E(v.get("title", ""))}">
<label for="x">What is it about?</label><textarea id="x" name="text" rows="6" maxlength="8000" required>{E(v.get("text", ""))}</textarea>
<label for="n">Your name</label><input id="n" name="name" maxlength="100" required value="{E(v.get("name", ""))}">
<label for="m">Your e-mail</label><input id="m" name="email" type="email" maxlength="200" required value="{E(v.get("email", ""))}">
<p class="wt-note">We mail you a link to confirm: the thread appears once you open it. Answers come by e-mail too.</p>
<input type="text" name="website" value="" tabindex="-1" autocomplete="off" style="position:absolute;left:-9999px" aria-hidden="true">
<button class="btn" type="submit">Send</button></form>"""


def list_page(
    svc: Services,
    slug: str,
    site: dict[str, Any],
    error: str = "",
    values: dict[str, str] | None = None,
) -> HTMLResponse:
    from . import followers

    head = f'<div class="wt-head"><h1>{E(site["name"])}</h1>'
    if site["about"]:
        head += f"<p>{followers.text_html(site['about'])}</p>"
    head += "</div>"
    body = head
    if site["list"] == "public":
        roots, replies = _events(svc, site["channel"])
        count: dict[str, int] = {}
        last: dict[str, int] = {}
        for r in replies:
            root = _root_of(r)
            count[root] = count.get(root, 0) + 1
            last[root] = max(last.get(root, 0), r["created_at"])
        if site.get("posts") == "proposal":
            roots = [e for e in roots if _tag(e, "proposal")]
        roots.sort(key=lambda e: max(e["created_at"], last.get(e["id"], 0)), reverse=True)
        roots = roots[:MAX_THREADS]
        names, roles = _who(svc, roots)
        items = []
        for e in roots:
            who, role = _author(svc, e, names, roles)
            n = count.get(e["id"], 0)
            items.append(
                f'<li><a href="/{E(slug)}/{E(e["id"])}">{E(_title(e))}</a>'
                f'<div class="wt-meta">{E(who)}{_role(role)} · {_ago(e["created_at"])}'
                f"{f' · {n} {chr(0x1F4AC)}' if n else ''}</div></li>"
            )
        if site.get("posts") == "proposal":  # proposals only: members make them in the chat
            body += "<h2>Proposals</h2>" + (
                f'<ul class="wt-list">{"".join(items)}</ul>'
                if items
                else "<p>No proposals yet.</p>"
            )
        else:
            body += '<p><a class="btn" href="#new">Open a thread</a></p><h2>Threads</h2>' + (
                f'<ul class="wt-list">{"".join(items)}</ul>'
                if items
                else "<p>No threads yet: be the first.</p>"
            )
    else:
        body += '<p class="wt-note">Your thread is between you and the members: you get its link by e-mail.</p>'
    if not site.get("posts"):
        body += _form(slug, error, values)
    return _page(site["name"], body, 400 if error else 200)


def thread_page(
    svc: Services, slug: str, site: dict[str, Any], root_id: str, k: str = "", note: str = ""
) -> HTMLResponse:
    from . import followers

    roots, replies = _events(svc, site["channel"])
    root = next((e for e in roots if e["id"] == root_id), None)
    if root is None:
        return _page("Not found", "<h1>No such thread</h1>", 404)
    owner = _owner(svc, root)
    mine = bool(owner) and hmac.compare_digest(k or "", thread_key(svc, root_id, owner))
    if site["list"] != "public" and not mine:
        return _page(
            "Private thread",
            "<h1>This thread is private</h1><p>Open it with the link we mailed you.</p>",
            403,
        )
    comments = sorted((r for r in replies if _root_of(r) == root_id), key=lambda e: e["created_at"])
    names, roles = _who(svc, [root, *comments])

    def card(e: dict[str, Any]) -> str:
        who, role = _author(svc, e, names, roles)
        return (
            f'<div class="wt-c"><div class="h"><b>{E(who)}</b>{_role(role)} · {_ago(e["created_at"])}</div>'
            f'<div class="b">{followers.text_html(_body(e))}</div></div>'
        )

    body = (
        f'<p class="wt-note"><a href="/{E(slug)}">← {E(site["name"])}</a></p><h1>{E(_title(root))}</h1>'
        + (f'<p class="notice" role="status">{E(note)}</p>' if note else "")
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
    from . import mail

    if site.get("posts"):  # #proposals, #events: one type of post, made in the chat
        return _page("Not here", "<h1>This page takes no threads</h1>", 403)
    values = {k: (form.get(k) or "").strip() for k in ("title", "text", "name", "email")}
    if form.get("website"):  # the field people don't see: a bot filled it
        return _page("Check your e-mail", "<h1>Check your e-mail</h1>")
    email = values["email"].lower()
    if not all(values.values()):
        return list_page(svc, slug, site, "Fill in every field.", values)
    if not EMAIL.match(email):
        return list_page(svc, slug, site, "That e-mail address does not look right.", values)
    if mail._limited("web-ip:" + ip, 10, 3600) or mail._limited("web-mail:" + email, 5, 3600):
        return list_page(
            svc, slug, site, "Too many threads in a short time: try again in an hour.", values
        )
    token = secrets.token_urlsafe(18)
    pending = {
        "slug": slug,
        "channel": site["channel"],
        "title": values["title"][:200],
        "text": values["text"][:8000],
        "name": values["name"][:100],
        "email": email,
        "at": int(time.time()),
    }
    _set_param(svc, "belfry.web.pending." + token, json.dumps(pending))
    link = f"{_public_base(svc)}/{slug}/confirm/{token}"
    _mailer(svc).send(
        email,
        f"Confirm your thread: {values['title'][:120]}",
        [
            f"Hello {values['name']},",
            f"To open your thread “{values['title']}” in {site['name']}, confirm it is your address:",
        ],
        (link, "Confirm and open the thread"),
        "If you did not ask for this, ignore this mail: nothing is posted without your confirmation.",
    )
    return _page(
        "Check your e-mail",
        f"<h1>Check your e-mail</h1><p>We sent a link to <b>{E(email)}</b>. "
        "Your thread opens when you click it.</p>",
    )


def confirm(svc: Services, slug: str, token: str) -> Response:
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
        return RedirectResponse(
            f"/{slug}/{p['posted']}?k={thread_key(svc, p['posted'], owner)}", 303
        )
    if time.time() - p.get("at", 0) > PENDING_SECONDS:
        return _page(
            "Link too old", "<h1>This link is too old</h1><p>Send the thread again.</p>", 410
        )
    text = f"**{p['title']}**\n\n{p['text']}"
    me = web_identity(svc, p["channel"], p["name"], p["email"])
    if me is not None:  # as themselves, an account like everyone's
        subject = [["subject", p["title"][:300]]]
        event = drops.post(svc, p["channel"], text, subject, me["partner"], client=TAG)
    else:  # no account could be made: a card by the groups key, as before
        extra = [
            ["subject", p["title"][:300]],
            ["mail-from", p["email"]],
            ["mail-name", p["name"][:100]],
            ["belfry-auth", "pass"],  # the address answered the link
        ]
        event = drops.post(svc, p["channel"], text, extra, None, client=TAG)
    owner = event["pubkey"] if event["pubkey"] != _groups_pubkey(svc) else p["email"]
    p["posted"], p["owner"] = event["id"], owner
    _set_param(svc, "belfry.web.pending." + token, json.dumps(p))
    k = thread_key(svc, event["id"], owner)
    url = f"{_public_base(svc)}/{slug}/{event['id']}?k={k}"
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
    return RedirectResponse(f"/{slug}/{event['id']}?k={k}&new=1", 303)


def comment(
    svc: Services, slug: str, site: dict[str, Any], root_id: str, k: str, text: str
) -> Response:
    from . import community, mail

    roots, _ = _events(svc, site["channel"])
    root = next((e for e in roots if e["id"] == root_id), None)
    owner = _owner(svc, root)
    if not owner or not hmac.compare_digest(k or "", thread_key(svc, root_id, owner)):
        return _page("Not allowed", "<h1>Only the thread's author comments here</h1>", 403)
    text = (text or "").strip()[:8000]
    if not text:
        return RedirectResponse(f"/{slug}/{root_id}?k={k}", 303)
    if mail._limited("web-comment:" + owner, 20, 3600):
        return _page("Slow down", "<h1>Too many comments in a short time</h1>", 429)
    relay, api_sign = _relay(svc)
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
    sign = api_sign
    if owner == author:  # their own account: the comment is theirs, signed by the signer
        pid = _partner_of_key(svc, author)
        found = community.member_signer(svc, pid) if pid else None
        if found is None:
            return _page(
                "Not now", "<h1>Something went wrong</h1><p>Try again in a minute.</p>", 503
            )
        sign = found[1]
    else:  # an older card: the groups key writes for the address
        tags += [["mail-from", owner], ["mail-name", _tag(root, "mail-name")]]
    event = sign({"kind": 1111, "created_at": int(time.time()), "content": text, "tags": tags})
    relay.publish(event, sign)
    return RedirectResponse(f"/{slug}/{root_id}?k={k}", 303)


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
        roots, replies = _events(svc, site["channel"])
        by_id = {e["id"]: e for e in roots if _tag(e, "client") == TAG}
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
                link = f"{_public_base(svc)}/{slug}/{root['id']}?k={thread_key(svc, root['id'], _owner(svc, root))}"
                lines.append(f"Open it: {link}")
        slug, site, root, rs = items[0]
        url = f"{_public_base(svc)}/{slug}/{root['id']}?k={thread_key(svc, root['id'], _owner(svc, root))}"
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
        if home.netloc and host != home.netloc.lower():
            if request.method not in ("GET", "HEAD"):
                return await call_next(request)
            q = ("?" + request.url.query) if request.url.query else ""
            return RedirectResponse(f"{home.scheme}://{home.netloc}{request.url.path}{q}", 301)
        ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
            request.client.host if request.client else ""
        )
        m = request.method
        try:
            if len(parts) == 1 and m in ("GET", "HEAD"):
                return await run_in_threadpool(list_page, s, slug, site)
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
    return f"{_public_base(svc)}/{quote(slug)}"
