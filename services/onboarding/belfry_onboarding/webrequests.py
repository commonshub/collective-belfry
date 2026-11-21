"""The public requests and an event's public activity, as JSON (docs/requests.md, 2026-11-19).

A community's own website (the nsite on the apex) renders three pages itself, in its own look:
the list of requests (like GitHub issues), one request's thread with its approval, and an
event's activity log. The data is on the groups relay, which wants authentication even for
public channels, so a browser can't read it: the belfry serves it here, read only, plus the
one thing an author does from the web, a comment under their own request.

- ``GET /api/v1/requests``: the threads of the sales requests channel (sales.requests_site).
- ``GET /api/v1/requests/<id or number>``: one thread with its timeline; ``me`` for its author.
- ``POST /api/v1/requests/<id or number>/comments``: the author's comment.
- ``POST /api/v1/requests/<id or number>/edit``: the author's edit of the title, the text or
  one of their own comments (a kind 1010, as the chat edits a message).
- ``POST /api/v1/requests/<id or number>/react``: an emoji on the request or on a comment, by
  someone logged in (a kind 7 signed as the person, as the chat reacts to a message).
- ``GET /api/v1/events/<link>/activity``: tickets bought and requests, newest first.

Reading and permissions are webthreads.py's (the HTML pages of the same channel): what was
posted while the channel was public, the thread's own key for its author. Names only: never a
key, an npub or an e-mail address in an answer. A buyer's or requester's name shows in the
activity only with their ``public_name`` consent (sales.py).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import events, retries, sales, webthreads
from .core import Services
from .places import fold

log = logging.getLogger(__name__)

API = "/api"
CACHE_SECONDS = 15  # how long what was read from the relay is kept (at most 20, the contract)
LIMIT_DEFAULT = 30
LIMIT_MAX = 100
Q_MAX = 100
CHILDREN_MAX = 60
STATES = ("open", "approved", "closed", "all")
TYPES = ("tickets", "requests", "program")
ID_RE = re.compile(r"^[0-9a-f]{64}$")
NUMBER_RE = re.compile(r"^[1-9][0-9]{0,8}$")
KEY_RE = re.compile(r"^[0-9a-f]{24}$")
# what a name never is: a key, an npub, an e-mail address (someone typed one as their name)
NOT_A_NAME = re.compile(r"npub1[0-9a-z]{20,}|[0-9a-f]{64}|[^@\s]+@[^@\s]+\.[^@\s]+", re.I)
SHORT = "public, max-age=15"
NO_STORE = "no-store"

PER_IP = sales.Limiter(120, 60)  # per address, a minute: a page asks two or three times

_lock = threading.Lock()
_cache: dict[str, tuple[float, Any]] = {}


def forget() -> None:
    """Something changed here (a comment, an offer posted or decided): read again."""
    with _lock:
        _cache.clear()


def _cached(key: str, make: Any) -> Any:
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    value = make()
    with _lock:
        if len(_cache) > 200:
            _cache.clear()
        _cache[key] = (time.monotonic(), value)
    return value


def _name(raw: Any) -> str:
    from .community import UNKNOWN

    name = " ".join(str(raw or "").split())[:80]
    return name if name and not NOT_A_NAME.search(name) else UNKNOWN


def _unix(stamp: Any) -> int | None:
    try:
        return int(sales.parse_iso(str(stamp)).timestamp())
    except (ValueError, TypeError):
        return None


# ── the requests channel, read once for every answer ────────────────────────────────────────
def _site(svc: Services) -> tuple[str, dict[str, Any]]:
    from .holder_invites import ApiError

    found = sales.requests_site(svc) if sales.own(svc) else None
    if found is None:
        raise ApiError(404, "none", "This belfry has no public requests.")
    return found


def _read(svc: Services, slug: str, site: dict[str, Any]) -> dict[str, Any]:
    channel = site["channel"]
    roots, replies, edits = webthreads._read(svc, channel)
    labels = webthreads._labels(svc, channel)
    roots.sort(key=lambda e: (e["created_at"], e["id"]))
    st = sales.store(svc)
    # numbers live in sales.sqlite (table request_numbers): given once, in creation order
    numbers = st.request_numbers(channel, [e["id"] for e in roots])
    offers = {f["card"]: f for f in st.offers() if f.get("card") and f.get("channel") == channel}
    labellers = [{"pubkey": who} for labs in labels.values() for (_at, _lab, who) in labs]
    names, roles = webthreads._who(svc, [*roots, *replies, *labellers])
    names = {**_account_names(svc, {e["pubkey"] for e in [*roots, *replies]} - set(names)), **names}
    by_root: dict[str, list[dict]] = {}
    for r in sorted(replies, key=lambda e: (e["created_at"], e["id"])):
        by_root.setdefault(webthreads._root_of(r), []).append(r)
    gk = webthreads._groups_pubkey(svc)
    reactions = _reactions(svc, channel)
    threads = []
    for root in roots:
        threads.append(
            {
                "root": root,
                "number": numbers[root["id"]],
                "offer": offers.get(root["id"]),
                "replies": by_root.get(root["id"], []),
                "edits": edits,
                "labels": labels.get(root["id"], []),
                "public": webthreads.public_at(site, root["created_at"]),
            }
        )
    return {
        "slug": slug,
        "site": site,
        "threads": threads,
        "names": names,
        "roles": roles,
        "gk": gk,
        "reactions": reactions,
    }


# ── reactions: kind 7 on the thread's message or on a comment, as the chat makes them ───────
REACTIONS_PER_HOUR = 60  # per person, through the endpoint
EMOJI_BYTES = 32
_ASCII = re.compile(r"[A-Za-z0-9\s]")
LIKES = {"+": "👍", "-": "👎"}


def emoji_of(raw: Any, one: bool = False) -> str | None:
    """A reaction's content as the answers show it: "+" is 👍 and "-" is 👎 (NIP-25; so is an
    empty one, when read); else an emoji: at most 32 UTF-8 bytes, no ASCII letter, digit or
    whitespace, no control character, something that is not ASCII in it. ONE (what the
    endpoint takes): exactly one emoji sequence. None for anything else."""
    if not isinstance(raw, str):
        return None
    if raw in LIKES:
        return LIKES[raw]
    if raw == "":
        return None if one else LIKES["+"]
    if len(raw.encode("utf-8", "replace")) > EMOJI_BYTES or _ASCII.search(raw):
        return None
    if all(ord(ch) < 128 for ch in raw):
        return None
    if any(unicodedata.category(ch) in ("Cc", "Cs", "Co", "Cn", "Zs", "Zl", "Zp") for ch in raw):
        return None
    if one and _sequences(raw) != 1:
        return None
    return raw


def _sequences(text: str) -> int:
    """How many emoji sequences TEXT holds, near enough: what a zero-width joiner ties, a
    variation selector, a skin tone, a keycap, a tag or a combining mark belongs to the emoji
    before it; two regional indicators make one flag."""
    n = flags = 0
    joined = False
    for ch in text:
        o = ord(ch)
        if (
            o in (0xFE0F, 0xFE0E, 0x20E3)
            or 0x1F3FB <= o <= 0x1F3FF
            or 0xE0020 <= o <= 0xE007F
            or unicodedata.category(ch).startswith("M")
        ):
            continue
        if o == 0x200D:
            joined = True
            continue
        if 0x1F1E6 <= o <= 0x1F1FF:
            flags += 1
            if flags % 2 == 1 and not joined:
                n += 1
        elif not joined:
            n += 1
        joined = False
    return n


def _reactions(svc: Services, channel: str) -> dict[str, list[dict[str, Any]]]:
    """Per message, the reactions on it that stand, oldest first: every kind 7 in the channel
    whose content is an emoji (whoever made it, in the chat or here), but those their own
    author deleted (kind 5). The message is the reaction's last ``e`` tag (NIP-25)."""
    got = webthreads._relay(svc)
    if got is None:
        return {}
    relay, sign = got
    try:
        evs = relay.query([{"kinds": [7, 5], "#h": [channel], "limit": 5000}], sign) or []
    except Exception as exc:  # noqa: BLE001 — the requests read without their reactions
        retries.soft(exc)  # an API call: 503 relay, never "no reactions"
        log.warning("webrequests: reactions not read: %s", type(exc).__name__)
        return {}
    gone = {
        (e["pubkey"], t[1])
        for e in evs
        if e["kind"] == 5
        for t in e.get("tags", [])
        if isinstance(t, list) and len(t) > 1 and t[0] == "e"
    }
    out: dict[str, list[dict[str, Any]]] = {}
    for e in sorted(evs, key=lambda e: (e["created_at"], e["id"])):
        if e["kind"] != 7 or (e["pubkey"], e["id"]) in gone:
            continue
        emoji = emoji_of(e.get("content"))
        target = next(
            (
                t[1]
                for t in reversed(e.get("tags", []))
                if isinstance(t, list) and len(t) > 1 and t[0] == "e"
            ),
            "",
        )
        if emoji and target:
            out.setdefault(target, []).append(
                {"id": e["id"], "emoji": emoji, "pubkey": e["pubkey"], "at": e["created_at"]}
            )
    return out


def _summary(rows: list[dict[str, Any]], me: str | None = None) -> list[dict[str, Any]]:
    """``[{"emoji", "count", "mine"?}]``: each person once per emoji; most used first, then
    first used. ``mine`` only for ME (who signed the request), never otherwise."""
    seen: dict[str, dict[str, Any]] = {}
    for i, r in enumerate(rows):
        seen.setdefault(r["emoji"], {"first": (r["at"], i), "who": set()})["who"].add(r["pubkey"])
    out = []
    for emoji, d in sorted(seen.items(), key=lambda kv: (-len(kv[1]["who"]), kv[1]["first"])):
        row: dict[str, Any] = {"emoji": emoji, "count": len(d["who"])}
        if me and me in d["who"]:
            row["mine"] = True
        out.append(row)
    return out


def _account_names(svc: Services, keys: set[str]) -> dict[str, str]:
    """The name on the account of each **guest** among KEYS, who has no profile to read a name
    from (someone who wrote from the website and never set a profile: the name they gave for
    it). A member with no profile stays "Somebody": the name on a member's contact is not
    theirs to see in public unless they put it in their profile."""
    from .core import m2o_id

    out: dict[str, str] = {}
    try:
        for k in sorted(keys)[:200]:
            rows = svc.odoo.search_read(
                "belfry.pass",
                [["keycast_pubkey", "=", k], ["state", "=", "active"]],
                ["partner_id", "tier"],
            )
            if not rows or any(r.get("tier") != "guest" for r in rows):
                continue
            pid = m2o_id(rows[0].get("partner_id"))
            row = svc.odoo.read_one("res.partner", pid, ["name"]) if pid else None
            name = " ".join(str((row or {}).get("name") or "").split())[:80]
            if name:
                out[k] = name
    except Exception as exc:  # noqa: BLE001 — "Somebody" then
        log.info("webrequests: account names not read: %s", type(exc).__name__)
    return out


def snapshot(svc: Services) -> dict[str, Any]:
    slug, site = _site(svc)
    key = f"requests:{svc.settings.register_path}:{site['channel']}:{site.get('page') or ''}"
    return _cached(key, lambda: _read(svc, slug, site))


ROLES = {"admin": "organiser", "member": "member", "agent": "agent"}


def _person(svc: Services, snap: dict[str, Any], e: dict[str, Any]) -> dict[str, str]:
    """An event's author as the answers show them: a name and organiser / member / guest /
    agent (someone from outside, with or without an account, is a guest)."""
    name, role = webthreads._author(svc, e, snap["names"], snap["roles"])
    if e["pubkey"] != snap["gk"] and not snap["names"].get(e["pubkey"]):
        name = ""  # no profile: "Somebody", as everywhere in the API
    if e["pubkey"] == snap["gk"] and not webthreads._tag(e, "mail-from"):
        return {"name": _name(name), "role": "organiser"}  # the channel, or the belfry itself
    return {"name": _name(name), "role": ROLES.get(role, "guest")}


def _same_author(snap: dict[str, Any], root: dict[str, Any], e: dict[str, Any]) -> bool:
    if e["pubkey"] != root["pubkey"]:
        return False
    if root["pubkey"] != snap["gk"]:
        return True
    a, b = webthreads._tag(root, "mail-from"), webthreads._tag(e, "mail-from")
    return bool(a) and a.lower() == b.lower()  # an older card: the groups key wrote for both


def _is_decision(snap: dict[str, Any], e: dict[str, Any]) -> bool:
    """The belfry's own reply that says the decision (sales._reply_in_thread): the timeline
    shows it as the decision, not as a comment."""
    return e["pubkey"] == snap["gk"] and webthreads._tag(e, "client") == "belfry-sales"


def _root(snap: dict[str, Any], t: dict[str, Any], mine: bool = False) -> dict[str, Any]:
    """The thread's opening message as it reads now (its author's latest edit)."""
    return webthreads.edited(t["root"], t["edits"], snap["site"], mine)


def _comments(snap: dict[str, Any], t: dict[str, Any], mine: bool = False) -> list[dict]:
    """The thread's comments as they read now, oldest first."""
    site = snap["site"]
    return [
        webthreads.edited(r, t["edits"], site, mine)
        for r in t["replies"]
        if not (t["offer"] and _is_decision(snap, r))
        and (mine or webthreads.public_at(site, r["created_at"]))
    ]


def _state(t: dict[str, Any]) -> str:
    f = t["offer"]
    if f and f["state"] == "accepted":
        return "approved"
    if f and f["state"] == "declined":
        return "declined"
    return "closed" if t["labels"] and t["labels"][-1][1] == "closed" else "open"


def _asked(svc: Services, f: dict[str, Any] | None) -> dict[str, Any]:
    none = {"ticket": None, "ticket_title": None, "event_link": None, "event_title": None}
    if not f or not f.get("ticket"):
        return none
    from . import ticketsales

    try:
        link = ticketsales.link(svc, f["event"])
    except Exception as exc:  # noqa: BLE001 — the event's own id opens it too
        retries.soft(exc)
        link = f.get("event")
    return {
        "ticket": f["ticket"],
        "ticket_title": str(f.get("ticket_title") or "")[:200] or None,
        "event_link": link or None,
        "event_title": str(f.get("event_title") or "")[:200] or None,
    }


def item(
    svc: Services,
    snap: dict[str, Any],
    t: dict[str, Any],
    mine: bool = False,
    me: str | None = None,
) -> dict:
    """ME: the key that signed the request, if any: its own reactions are marked."""
    root, f = _root(snap, t, mine), t["offer"]
    comments = _comments(snap, t, mine)
    state = _state(t)
    decided = _unix(f.get("decided_at")) if f and state in ("approved", "declined") else None
    asks = f if f and not sales.plain_offer(f) else None  # an offer: a pass or a ticket
    return {
        "id": root["id"],
        "number": t["number"],
        "title": webthreads._title(root),
        # what the request is (sales.REQUEST_TYPES): enquiry, talk, workshop, in_kind…
        "type": sales.message_type(t["root"], f),
        # an offer says which it is: in kind, or a sponsored pass asked for (sales.OFFER_KINDS)
        "kind": sales.offer_kind(asks) if asks else "other",
        "label": f.get("label") if f else None,
        "days": (f.get("days") or None) if f else None,
        "percent_asked": int(asks["percent"]) if asks else None,
        # which ticket of which event is asked for (null for a product's offer or a plain thread)
        **_asked(svc, f),
        "state": state,
        "percent_approved": int(f.get("decided_percent") or 0) if state == "approved" else None,
        "approved_by": {"name": _name(f.get("decided_by"))} if state == "approved" else None,
        "author": _person(svc, snap, root),
        "created_at": root["created_at"],
        "edited_at": root.get("edited_at"),  # the title or the text, by its author
        "comments": len(comments),
        "last_at": max([root["created_at"], decided or 0, *(c["created_at"] for c in comments)]),
        "reactions": _summary(snap["reactions"].get(root["id"], []), me),
    }


def _timeline(
    svc: Services,
    snap: dict[str, Any],
    t: dict[str, Any],
    mine: bool,
    viewer: dict[str, Any] | None = None,
    me: str | None = None,
) -> list[dict]:
    root, f = t["root"], t["offer"]
    site = snap["site"]
    out: list[dict[str, Any]] = []
    author = {"name": _person(svc, snap, root)["name"]}

    def versions(e: dict[str, Any]) -> Any:
        """(the edit, E before it, E after it) for each edit of E that shows."""
        was = e
        for x in t["edits"].get(e["id"], []):
            if mine or webthreads.public_at(site, x["created_at"]):
                now = webthreads._apply(was, x)
                yield x, was, now
                was = now

    for x, was, now in versions(root):
        if webthreads._title(was) != webthreads._title(now):
            out.append(
                {
                    "type": "edited",
                    "field": "title",
                    "by": author,
                    "at": x["created_at"],
                    "title": webthreads._title(now),
                }
            )
        if webthreads._body(was) != webthreads._body(now):
            out.append({"type": "edited", "field": "body", "by": author, "at": x["created_at"]})
    first = {r["id"]: r for r in t["replies"]}  # as they were first written
    for c in _comments(snap, t, mine):
        who = _person(svc, snap, c)
        if _same_author(snap, root, c):
            who["role"] = "author"
        entry = {
            "type": "comment",
            "id": c["id"],
            "author": who,
            "at": c["created_at"],
            "edited_at": c.get("edited_at"),
            "text": webthreads._body(c),
            "reactions": _summary(snap["reactions"].get(c["id"], []), me),
        }
        if (mine and webthreads.own_comment(svc, root, c)) or (
            viewer and c["kind"] == 1111 and c["pubkey"] == viewer["pubkey"]
        ):
            entry["mine"] = True  # they may edit it (POST …/edit)
        out.append(entry)
        for x, was, now in versions(first[c["id"]]):
            if was.get("content") != now.get("content"):
                out.append(
                    {
                        "type": "edited",
                        "field": "comment",
                        "by": {"name": who["name"]},
                        "at": x["created_at"],
                        "comment": c["id"],
                    }
                )
    if f and f["state"] in ("accepted", "declined"):
        entry: dict[str, Any] = {
            "type": "approved" if f["state"] == "accepted" else "declined",
            "by": {"name": _name(f.get("decided_by"))},
            "at": _unix(f.get("decided_at")) or root["created_at"],
        }
        if f["state"] == "accepted":
            entry["percent"] = int(f.get("decided_percent") or 0)
        if f.get("note"):
            entry["note"] = f["note"]  # the admin's public note, as the thread's reply says it
        out.append(entry)
    was = "open"
    for at, lab, who in t["labels"]:
        if lab == was:
            continue
        was = lab
        name = "Belfry" if who == snap["gk"] else snap["names"].get(who)
        out.append(
            {
                "type": "closed" if lab == "closed" else "reopened",
                "by": {"name": _name(name)},
                "at": at,
            }
        )
    out.sort(key=lambda x: x["at"])
    return out


def _page(rows: list[dict], at: Any, before: int | None, limit: int) -> tuple[list[dict], Any]:
    """ROWS newest first, those before BEFORE, LIMIT of them (a few more when several share the
    last one's second, so the next page misses none), and the next ``before``."""
    rows = [r for r in rows if before is None or at(r) < before]
    page = rows[:limit]
    while len(page) < len(rows) and at(rows[len(page)]) == at(page[-1]):
        page.append(rows[len(page)])
    return page, (at(page[-1]) if len(page) < len(rows) else None)


def listing(
    svc: Services,
    state: str,
    q: str,
    before: int | None,
    limit: int,
    viewer: dict[str, Any] | None = None,
    rtype: str = "",
    me: str | None = None,
) -> dict[str, Any]:
    """VIEWER: the account of a website session that signed the request: its own requests
    carry ``"mine": true`` (the list itself is the public one, for everyone). RTYPE: only the
    requests of that type. ME: the key that signed, if any (its own reactions are marked)."""
    snap = snapshot(svc)
    needle = fold(q)
    rows = []
    for t in snap["threads"]:
        if not t["public"]:
            continue
        root = _root(snap, t)
        if needle and needle not in fold(webthreads._title(root) + "\n" + webthreads._body(root)):
            continue
        row = item(svc, snap, t, me=me)
        if rtype and row["type"] != rtype:
            continue
        if is_mine(svc, t, viewer):
            row["mine"] = True
        rows.append(row)
    counts = {"open": 0, "approved": 0, "closed": 0}
    for r in rows:
        counts["closed" if r["state"] == "declined" else r["state"]] += 1
    if state != "all":
        want = ("closed", "declined") if state == "closed" else (state,)
        rows = [r for r in rows if r["state"] in want]
    rows.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
    page, nxt = _page(rows, lambda r: r["created_at"], before, limit)
    return {
        "slug": snap["slug"],
        "channel": snap["site"].get("name") or snap["slug"],
        "counts": counts,
        "items": page,
        "next": nxt,
    }


def _find(snap: dict[str, Any], ref: str) -> dict[str, Any] | None:
    if ID_RE.match(ref):
        return next((t for t in snap["threads"] if t["root"]["id"] == ref), None)
    if NUMBER_RE.match(ref):
        return next((t for t in snap["threads"] if t["number"] == int(ref)), None)
    return None


def _proven(svc: Services, t: dict[str, Any], signer: str | None, thread_key: str) -> bool:
    """The thread's author: the key that made the offer signed the request, or the request
    carries the key of the thread's own link."""
    f = t["offer"]
    if signer and f and f.get("key") == signer:
        return True
    return bool(KEY_RE.match(thread_key or "")) and webthreads.is_author(svc, t["root"], thread_key)


def is_mine(svc: Services, t: dict[str, Any], acct: dict[str, Any] | None) -> bool:
    """Whether the thread is the account's own (ACCT: someone logged in on the website,
    sitesessions.py): they made its offer, they signed it, or it is an older card written for
    their proven address."""
    if not acct:
        return False
    f, root = t["offer"], t["root"]
    if f and f.get("key") == acct["pubkey"]:
        return True
    owner = webthreads._owner(svc, root)
    if not owner:
        return False
    return owner == acct["pubkey"] or bool(acct["email"]) and owner.lower() == acct["email"]


def may_comment(svc: Services, snap: dict[str, Any], acct: dict[str, Any] | None) -> bool:
    """Whether the account has the requests channel (sitesessions.has_channel)."""
    from . import sitesessions

    if not acct:
        return False
    try:
        return sitesessions.has_channel(svc, acct, snap["site"]["channel"])
    except Exception as exc:  # noqa: BLE001 — the relay can't say: no
        retries.soft(exc)  # an API call: 503 relay, never "you may not comment"
        return False


def ticket_url(svc: Services, f: dict[str, Any] | None) -> str | None:
    """Where an approved author goes on for their pass. GET /offers/<id> never gives the code
    (it is only in the private mail), so neither does this: only the address of the checkout
    page the console names ("Where people buy tickets"), when it is one page for everything
    (no ``{link}`` / ``{id}`` to fill: those are an event ticket's)."""
    if not f or f["state"] != "accepted":
        return None
    if f.get("ticket"):
        # an offer for an event ticket: the template filled for it, with the offer's id (the
        # page sends it back as POST /checkout's "offer")
        from . import ticketsales

        return ticketsales.offer_url(svc, f) or None
    url = sales.settings(svc).get("checkout_url") or ""
    return url if url and "{" not in url else None


def _redeemed(svc: Services, f: dict[str, Any]) -> bool:
    """Whether an order holds the offer's approval (its code's one use): made, paid or not. An
    order that ends unpaid gives it back."""
    c = sales.store(svc).code(f["code"]) if f.get("code") else None
    return bool(c and len(c["used"]) >= int(c["uses"]))


def detail(
    svc: Services,
    ref: str,
    signer: str | None,
    thread_key: str,
    viewer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """VIEWER: the account of a website session that signed the request (SIGNER is then that
    account's key): the author of its own threads, and ``viewer`` in the answer says whether
    it may comment."""
    from .holder_invites import ApiError

    snap = snapshot(svc)
    t = _find(snap, ref)
    mine = t is not None and (_proven(svc, t, signer, thread_key) or is_mine(svc, t, viewer))
    if t is None or not (t["public"] or mine):
        raise ApiError(404, "unknown", "No such request.")
    out = item(svc, snap, t, mine, signer)
    out["body"] = webthreads._body(_root(snap, t, mine))
    out["timeline"] = _timeline(svc, snap, t, mine, viewer, signer)
    if viewer:
        out["viewer"] = {
            "role": viewer["role"],
            "author": mine,
            "can_comment": mine or may_comment(svc, snap, viewer),
        }
    if mine:
        out["me"] = {"author": True}
        f = t["offer"]
        if f and f.get("ticket"):
            from . import ticketsales

            out["me"].update(
                offer=f["id"],
                ticket=f["ticket"],
                event=ticketsales.link(svc, f["event"]),
                state=f["state"],
                redeemed=_redeemed(svc, f),
            )
        url = ticket_url(svc, t["offer"])
        if url:
            out["me"]["ticket_url"] = url
    return out


COMMENTS_PER_HOUR = 20  # per account, for a website session


def _site_comment(
    svc: Services, snap: dict[str, Any], t: dict[str, Any], viewer: dict[str, Any], text: str
) -> None:
    """A comment by someone logged in on the website, signed with their own key through the
    signer: the author of the thread, or anyone who has the requests channel."""
    from . import checkin, mail, sitesessions
    from .holder_invites import ApiError

    mine = is_mine(svc, t, viewer)
    if not (t["public"] or mine):
        raise ApiError(404, "unknown", "No such request.")
    if not (mine or may_comment(svc, snap, viewer)):
        raise ApiError(403, "not_allowed", "Only people who have this channel comment here.")
    checkin.refuse_archived(svc, snap["site"]["channel"])
    if mine and t["root"]["pubkey"] == snap["gk"]:
        # their older card, written by the belfry's posting key for their address: the comment
        # goes the way the card's own link posts it, so it reads as the author's
        if mail._limited(f"site-comment:{viewer['partner']}", COMMENTS_PER_HOUR, 3600):
            raise ApiError(429, "too_many", "Too many comments in a short time.")
        got = webthreads.add_comment(svc, snap["site"], t["root"]["id"], text, "", True)
        if got == "too_many":
            raise ApiError(429, "too_many", "Too many comments in a short time.")
        if got != "ok":
            raise ApiError(503, "unavailable", "That did not work just now. Try again.")
        return
    sign = sitesessions.signer(svc, viewer)  # 409 own_key, 503 signer
    if mail._limited(f"site-comment:{viewer['partner']}", COMMENTS_PER_HOUR, 3600):
        raise ApiError(429, "too_many", "Too many comments in a short time.")
    try:
        got = webthreads.add_reply(svc, snap["site"], t["root"]["id"], text, viewer["pubkey"], sign)
    except Exception as exc:  # noqa: BLE001 — the signer or the relay
        log.warning("webrequests: a comment was not posted: %s", type(exc).__name__)
        retries.check(exc)  # the relay: 503 relay
        raise ApiError(503, "unavailable", "That did not work just now. Try again.") from exc
    if got != "ok":
        raise ApiError(404, "unknown", "No such request.")


def comment(
    svc: Services,
    ref: str,
    signer: str | None,
    thread_key: str,
    text: Any,
    viewer: dict[str, Any] | None = None,
) -> None:
    from .holder_invites import ApiError

    snap = snapshot(svc)
    t = _find(snap, ref)
    if t is None:
        raise ApiError(404, "unknown", "No such request.")
    if viewer is not None:
        text = text.strip() if isinstance(text, str) else ""
        if not text or len(text) > webthreads.COMMENT_MAX:
            raise ApiError(422, "invalid", f"text: 1 to {webthreads.COMMENT_MAX} characters.")
        _site_comment(svc, snap, t, viewer, text)
        return
    by_offer = bool(signer and t["offer"] and t["offer"].get("key") == signer)
    if not by_offer and not _proven(svc, t, None, thread_key):
        raise ApiError(403, "not_author", "Only the request's author comments here.")
    text = text.strip() if isinstance(text, str) else ""
    if not text or len(text) > webthreads.COMMENT_MAX:
        raise ApiError(422, "invalid", f"text: 1 to {webthreads.COMMENT_MAX} characters.")
    got = webthreads.add_comment(
        svc, snap["site"], t["root"]["id"], text, thread_key if not by_offer else "", by_offer
    )
    if got == "not_author":
        raise ApiError(403, "not_author", "Only the request's author comments here.")
    if got == "too_many":
        raise ApiError(429, "too_many", "Too many comments in a short time.")
    if got != "ok":
        raise ApiError(503, "unavailable", "That did not work just now. Try again.")


def edit(
    svc: Services,
    ref: str,
    signer: str | None,
    thread_key: str,
    data: dict[str, Any],
    viewer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST /requests/<id or number>/edit: the author changes the title and / or the text of
    their request, or one of their own comments. VIEWER (a website session's account): the
    author of their own threads; under anyone else's, only a comment they signed."""
    from . import checkin
    from .holder_invites import ApiError

    snap = snapshot(svc)
    t = _find(snap, ref)
    if t is None:
        raise ApiError(404, "unknown", "No such request.")
    by_offer = bool(signer and t["offer"] and t["offer"].get("key") == signer)
    by_site = is_mine(svc, t, viewer)  # proven by the session: no thread key needed
    actor = None
    if viewer is not None and not by_site:
        # not their thread: one of their own comments, when the thread is one they can see
        if not t["public"] or "comment" not in data:
            raise ApiError(403, "not_author", "Only the request's author edits it.")
        from . import sitesessions

        actor = (viewer["pubkey"], sitesessions.signer(svc, viewer))
    elif not by_offer and not by_site and not _proven(svc, t, None, thread_key):
        raise ApiError(403, "not_author", "Only the request's author edits it.")
    by_offer = by_offer or by_site
    title = body = text = None
    comment_id = ""
    if "comment" in data or "text" in data:
        comment_id, text = data.get("comment"), data.get("text")
        if set(data) - {"comment", "text"}:
            raise ApiError(422, "invalid", "Send the title and the body, or a comment: not both.")
        if not isinstance(comment_id, str) or not ID_RE.match(comment_id):
            raise ApiError(404, "unknown", "No such comment.")
        text = text.strip() if isinstance(text, str) else ""
        if not text or len(text) > webthreads.COMMENT_MAX:
            raise ApiError(422, "invalid", f"text: 1 to {webthreads.COMMENT_MAX} characters.")
    else:
        if data.get("title") is not None:
            title = sales.request_title(data["title"])
        if data.get("body") is not None:
            body = data["body"].strip() if isinstance(data["body"], str) else ""
            if not body or len(body) > webthreads.COMMENT_MAX:
                raise ApiError(422, "invalid", f"body: 1 to {webthreads.COMMENT_MAX} characters.")
        if title is None and body is None:
            raise ApiError(422, "invalid", "Send a title, a body, or a comment and its text.")
    checkin.refuse_archived(svc, snap["site"]["channel"])
    got, now = webthreads.edit(
        svc,
        snap["site"],
        t["root"]["id"],
        thread_key if not by_offer else "",
        by_offer,
        title,
        body,
        comment_id or "",
        text,
        actor,
    )
    if got == "not_author":
        raise ApiError(403, "not_author", "Only the request's author edits it.")
    if got == "unknown":
        raise ApiError(404, "unknown", "No such comment.")
    if got == "not_yours":
        raise ApiError(403, "not_yours", "Only your own comments can be edited here.")
    if got == "too_many":
        raise ApiError(429, "too_many", "Too many edits in a short time.")
    if got not in ("ok", "same"):
        raise ApiError(503, "unavailable", "That did not work just now. Try again.")
    if got == "ok" and t["offer"] and not comment_id:
        # the offer says what its thread says now (the console, the mails' subjects)
        sales.offer_edited(svc, t["offer"]["id"], now["title"] if title is not None else None, body)
    return {"ok": True, "changed": got == "ok"}


def _reactor(
    svc: Services, t: dict[str, Any], signer: str | None, thread_key: str
) -> dict[str, Any]:
    """The account behind a key that signed and is no website session: the account's own key
    (a member's chat session signs with it). No account: 403 ``not_allowed`` for the thread's
    author by its own key or the offer's (a reaction is signed as a person, through the
    signer: it needs an account), else 401 ``no_session``."""
    from . import sitesessions
    from .holder_invites import ApiError

    acct = None
    if signer and sitesessions.own(svc):
        pid = webthreads._partner_of_key(svc, signer)
        acct = sitesessions.account(svc, pid) if pid else None
        if acct is not None and acct["pubkey"] != signer:
            acct = None
    if acct is not None:
        return acct
    if (KEY_RE.match(thread_key or "") and t is not None) or (
        t is not None and _proven(svc, t, signer, "")
    ):
        raise ApiError(403, "not_allowed", "Log in to react: a thread's own link is not enough.")
    raise ApiError(401, "no_session", "Log in first.")


def react(
    svc: Services,
    ref: str,
    signer: str | None,
    thread_key: str,
    data: dict[str, Any],
    viewer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST /requests/<id or number>/react: ``{"emoji", "comment"?, "remove"?}`` by someone
    logged in who may comment (VIEWER: a website session's account; else the account whose own
    key signed). A kind 7 signed as the person through the signer, with the tags the chat
    writes for a message in a group; ``remove``: a kind 5 by the person on their own
    reaction(s) with that emoji. → ``{"reactions": [...]}`` of the request or the comment."""
    from . import checkin, mail, sitesessions
    from .holder_invites import ApiError

    snap = snapshot(svc)
    t = _find(snap, ref)
    acct = viewer if viewer is not None else _reactor(svc, t, signer, thread_key)
    mine = t is not None and is_mine(svc, t, acct)
    if t is None or not (t["public"] or mine):
        raise ApiError(404, "unknown", "No such request.")
    if not (mine or may_comment(svc, snap, acct)):
        raise ApiError(403, "not_allowed", "Only people who have this channel react here.")
    emoji = emoji_of(data.get("emoji"), one=True)
    if emoji is None:
        raise ApiError(422, "invalid", "emoji: one emoji.")
    remove = data.get("remove", False)
    if not isinstance(remove, bool):
        raise ApiError(422, "invalid", "remove: true or false.")
    target = t["root"]
    if data.get("comment") is not None:
        cid = data["comment"]
        target = next(
            (c for c in _comments(snap, t, mine) if isinstance(cid, str) and c["id"] == cid), None
        )
        if target is None:
            raise ApiError(422, "invalid", "comment: no such comment under this request.")
    channel = snap["site"]["channel"]
    checkin.refuse_archived(svc, channel)
    me = acct["pubkey"]
    rows = list(snap["reactions"].get(target["id"], []))
    own = [r for r in rows if r["pubkey"] == me and r["emoji"] == emoji]
    if bool(own) != remove:  # there already, or nothing to take away: nothing changes
        return {"reactions": _summary(rows, me)}
    sign = sitesessions.signer(svc, acct)  # 409 own_key, 503 signer
    if mail._limited(f"site-react:{acct['partner']}", REACTIONS_PER_HOUR, 3600):
        raise ApiError(429, "too_many", "Too many reactions in a short time.")
    now = int(time.time())
    if remove:
        draft = {
            "kind": 5,
            "created_at": now,
            "content": "",
            "tags": [["h", channel], *[["e", r["id"]] for r in own], ["k", "7"]],
        }
    else:
        draft = {
            "kind": 7,
            "created_at": now,
            "content": emoji,
            "tags": [
                ["h", channel],
                ["e", target["id"]],
                ["p", target["pubkey"]],
                ["k", str(target["kind"])],
            ],
        }
    try:
        event = sign(draft)
        if event.get("pubkey") != me:
            raise RuntimeError("the signer signed for another key")
        relay, _api_sign = webthreads._relay(svc)
        relay.publish(event, sign)
    except Exception as exc:  # noqa: BLE001 — the signer or the relay
        log.warning("webrequests: a reaction was not posted: %s", type(exc).__name__)
        retries.check(exc)  # the relay: 503 relay
        raise ApiError(503, "unavailable", "That did not work just now. Try again.") from exc
    forget()
    if remove:
        rows = [r for r in rows if r not in own]
    else:
        rows.append({"id": event.get("id"), "emoji": emoji, "pubkey": me, "at": now})
    return {"reactions": _summary(rows, me)}


# ── an event's public activity ──────────────────────────────────────────────────────────────
def _family(svc: Services, ev: events.Event) -> tuple[list[events.Event], bool]:
    """(EV and the events that point to it with an ``a`` tag (a village event's day events),
    whether EV is where the requests belong). The requests belong to the event other events
    point to; when no event is pointed to, to every public event."""
    since = int(time.time()) - 400 * 86400
    found = events.community(svc, [{"kinds": list(events.KINDS), "since": since}])
    latest: dict[str, dict[str, Any]] = {}
    for e in found:
        d = events.tag(e, "d") or ""
        if events.D_RE.match(d) and e["created_at"] >= latest.get(d, e)["created_at"]:
            latest[d] = e
    coords = {f"{k}:{ev.creator}:{ev.d}" for k in events.KINDS}
    pointed: set[str] = set()
    kids: list[str] = []
    for d, e in sorted(latest.items()):
        for t in e.get("tags", []):
            if t[:1] != ["a"] or len(t) < 2:
                continue
            parts = str(t[1]).split(":")
            if len(parts) != 3 or parts[0] not in ("31922", "31923") or parts[2] == d:
                continue
            pointed.add(parts[2])
            if t[1] in coords:
                kids.append(d)
    family = [ev]
    for d in kids[:CHILDREN_MAX]:
        child = events.load(svc, d)
        if child is not None and any(
            t[:1] == ["a"] and len(t) > 1 and t[1] in coords for t in child.version.get("tags", [])
        ):
            family.append(child)
    return family, (ev.d in pointed or not pointed)


def _ticket_rows(svc: Services, ev: events.Event, family: list[events.Event]) -> list[dict]:
    """Live orders of a ticket of the event (or of its day events) that are paid."""
    ds = {e.d for e in family}
    return [
        o
        for o in sales.store(svc).orders(("paid", "active"))
        if o.get("ticket")
        and o.get("event") in ds
        and sales.env_of(o) == "live"  # a tester's test order never shows
        and "refunded_admitted" not in (o.get("flags") or [])
    ]


def _read_activity(svc: Services, ev: events.Event) -> dict[str, Any]:
    family, owns_requests = _family(svc, ev)
    over = ev.ended()
    items: list[dict[str, Any]] = []
    stats = {"villagers": 0, "open_requests": 0, "in_kind_passes": 0, "sponsored_passes": 0}
    ds = {e.d for e in family}
    if sales.own(svc):
        orders = _ticket_rows(svc, ev, family)
        stats["villagers"] = len(
            {o.get("ticket_pubkey") or o.get("partner_id") or o["email"].lower() for o in orders}
        )
        for o in orders:
            if o.get("erase_after_event") and over:
                continue  # they asked to be erased once the event is over
            at = _unix((o.get("paid") or {}).get("at")) or _unix(o.get("activated"))
            items.append(
                {
                    "type": "ticket",
                    "at": at or _unix(o.get("created")) or 0,
                    # the buyer's name only when they agreed to it (``public_name``)
                    "who": {"name": _name(o.get("name")) if o.get("public_name") else None},
                    "ticket": {"title": str(o.get("ticket_title") or "Ticket")[:200]},
                }
            )
    snap = None
    if sales.own(svc) and sales.requests_site(svc) is not None:
        snap = snapshot(svc)
    for t in snap["threads"] if snap else []:
        if not t["public"]:
            continue
        f, root = t["offer"], t["root"]
        # an offer for a ticket belongs to that ticket's event (and to the event its day
        # events point to); every other request to the event that owns the requests
        if not (f["event"] in ds if f and f.get("event") else owns_requests):
            continue
        state = _state(t)
        sponsored = bool(f) and sales.offer_kind(f) == "sponsored"
        stats["open_requests"] += state == "open"
        stats["sponsored_passes" if sponsored else "in_kind_passes"] += state == "approved"
        if f and f.get("erase_after_event") and over:
            continue
        consent = bool(f and f.get("public_name"))
        title = webthreads._title(_root(snap, t))
        if f and not consent and title.startswith(f"{f['name']}: "):
            title = title[len(f["name"]) + 2 :]  # an offer's title starts with its author's name
        ref = {
            "id": root["id"],
            "number": t["number"],
            "title": title,
            "label": f.get("label") if f else None,
        }
        requester = {"name": _name(f["name"]) if consent else None}
        items.append(
            {"type": "request_opened", "at": root["created_at"], "who": requester, "request": ref}
        )
        for c in _comments(snap, t):
            if _same_author(snap, root, c):
                who = requester
            else:
                # members, organisers and agents answer under their own name, as the thread
                # shows them; anyone from outside stays unnamed here
                p = _person(svc, snap, c)
                who = {"name": p["name"] if p["role"] != "guest" else None}
            items.append(
                {"type": "request_comment", "at": c["created_at"], "who": who, "request": ref}
            )
        if state == "approved":
            items.append(
                {
                    "type": "request_approved",
                    "at": _unix(f.get("decided_at")) or root["created_at"],
                    "who": requester,
                    "request": ref,
                    "percent": int(f.get("decided_percent") or 0),
                }
            )
    # "program" (a talk or a workshop added to the event's programme) would be read here: no
    # source for it exists yet, so there are no such items
    items.sort(key=lambda x: x["at"], reverse=True)
    return {"stats": stats, "items": items}


KIND_OF = {"tickets": ("ticket",), "program": ("program",)}


def activity(
    svc: Services, link: str, kind: str | None, before: int | None, limit: int
) -> dict[str, Any]:
    from .holder_invites import ApiError

    try:
        ev, _moved = events.resolve(svc, link)
    except Exception as exc:  # noqa: BLE001 — the relay
        log.warning("webrequests: the event could not be read: %s", type(exc).__name__)
        retries.check(exc)  # 503 relay
        raise ApiError(503, "unavailable", "The event can't be read just now.") from exc
    # only a public event has a public activity: members-only and by-link events answer as
    # an event that does not exist
    if ev is None or ev.visibility != "public":
        raise ApiError(404, "not_found", "No such event.")
    got = _cached(f"activity:{svc.settings.register_path}:{ev.d}", lambda: _read_activity(svc, ev))
    rows = got["items"]
    if kind:
        want = KIND_OF.get(kind)
        rows = [
            r for r in rows if (r["type"] in want if want else r["type"].startswith("request_"))
        ]
    page, nxt = _page(rows, lambda r: r["at"], before, limit)
    return {"stats": got["stats"], "items": page, "next": nxt}


# ── the API ─────────────────────────────────────────────────────────────────────────────────
def _int(raw: str | None, name: str, default: int | None, low: int, high: int) -> int | None:
    from .holder_invites import ApiError

    if raw in (None, ""):
        return default
    try:
        value = int(str(raw))
    except ValueError:
        raise ApiError(422, "invalid", f"{name}: a number.") from None
    if not low <= value <= high:
        raise ApiError(422, "invalid", f"{name}: {low} to {high}.")
    return value


def _paging(request: Request) -> tuple[int | None, int]:
    q = request.query_params
    before = _int(q.get("before"), "before", None, 0, 2**40)
    limit = _int(q.get("limit"), "limit", LIMIT_DEFAULT, 1, LIMIT_MAX)
    return before, int(limit or LIMIT_DEFAULT)


def _allow(request: Request) -> None:
    from .holder_invites import ApiError

    if not PER_IP.allow(sales.client_ip(request)):
        raise ApiError(429, "too_many", "Too many requests from here. Try again in a minute.")


async def _run(fn: Any, *args: Any) -> Any:
    """FN in a thread. A relay that can't be read or written is 503 ``relay`` (retries.py:
    never an empty answer); anything else that goes wrong is 503 ``unavailable``, never a
    trace."""
    from .holder_invites import ApiError

    try:
        return await retries.run(fn, *args)
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("webrequests: %s failed: %s", fn.__name__, type(exc).__name__, exc_info=True)
        raise ApiError(503, "unavailable", "That can't be read just now. Try again.") from exc


def register(app: FastAPI) -> None:
    from .holder_invites import ApiError, authenticate

    def answer(data: Any, cache: str, status: int = 200) -> JSONResponse:
        return JSONResponse(data, status_code=status, headers={"Cache-Control": cache})

    @app.get(API + "/requests")
    async def requests_list(request: Request) -> JSONResponse:
        _allow(request)
        q = request.query_params
        state = q.get("state") or "all"
        if state not in STATES:
            raise ApiError(422, "invalid", "state: " + ", ".join(STATES) + ".")
        text = q.get("q") or ""
        if len(text) > Q_MAX:
            raise ApiError(422, "invalid", f"q: at most {Q_MAX} characters.")
        rtype = q.get("type") or ""
        if rtype and rtype not in sales.REQUEST_TYPES:
            raise ApiError(422, "invalid", "type: " + ", ".join(sales.REQUEST_TYPES) + ".")
        before, limit = _paging(request)
        s = request.app.state.services
        # signed by a website session: the same list, the account's own requests marked
        key, viewer = await _acting(request, events._viewer(request, _signed_path(request)))
        out = await _run(listing, s, state, text, before, limit, viewer, rtype, key)
        return answer(out, NO_STORE if key else SHORT)

    @app.get(API + "/requests/{ref}")
    async def requests_one(request: Request, ref: str) -> JSONResponse:
        _allow(request)
        s = request.app.state.services
        # who asks, when they say: the offer's key (NIP-98) or the thread's own key, in a
        # header. Never from the query string (query strings end up in logs)
        signer, viewer = await _acting(request, events._viewer(request, request.url.path))
        key = request.headers.get("x-thread-key") or ""
        out = await _run(detail, s, ref, signer, key, viewer)
        return answer(out, NO_STORE)  # the same address answers its author with more

    def _signed_path(request: Request) -> str:
        """The path a GET with a query was signed for (NIP-98 signs the whole address)."""
        return request.url.path + ("?" + request.url.query if request.url.query else "")

    async def _acting(request: Request, signer: str | None) -> tuple[str | None, Any]:
        """(the key to act as, the account when SIGNER is a website session, else None)."""
        from . import sitesessions

        if not signer:
            return signer, None
        return await run_in_threadpool(sitesessions.acting, request.app.state.services, signer)

    async def _posted(request: Request) -> tuple[Any, str | None, str, dict[str, Any]]:
        """(services, the NIP-98 signer if any, the thread key if any, the JSON body)."""
        s = request.app.state.services
        body = await request.body()
        signer = None
        if request.headers.get("authorization"):
            signer = authenticate(
                s,
                request.app.state.api_seen,
                request.headers.get("authorization"),
                "POST",
                request.url.path,
                body,
            )
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_json", "Send a JSON object.")
        return s, signer, request.headers.get("x-thread-key") or "", data

    def _retry(signer: str | None, key: str, data: dict[str, Any]) -> tuple[str, str | None]:
        """(whose retry this is, the body's ``client_id`` taken out of it): the account the
        call acts as; for an author with no account, the thread's own key they hold. With
        neither there is nobody to remember an answer for."""
        cid = retries.client_id(data)
        who = signer or (f"thread-key:{key}" if KEY_RE.match(key) else "")
        return who, (cid if who else None)

    @app.post(API + "/requests/{ref}/comments")
    async def requests_comment(request: Request, ref: str) -> JSONResponse:
        _allow(request)
        s, signer, key, data = await _posted(request)
        signer, viewer = await _acting(request, signer)

        async def make() -> JSONResponse:
            await _run(comment, s, ref, signer, key, data.get("text"), viewer)
            return answer({"ok": True}, NO_STORE, 201)

        return await retries.once(request, *_retry(signer, key, data), make)

    @app.post(API + "/requests/{ref}/edit")
    async def requests_edit(request: Request, ref: str) -> JSONResponse:
        _allow(request)
        s, signer, key, data = await _posted(request)
        signer, viewer = await _acting(request, signer)
        who, cid = _retry(signer, key, data)

        async def make() -> JSONResponse:
            return answer(await _run(edit, s, ref, signer, key, data, viewer), NO_STORE)

        return await retries.once(request, who, cid, make)

    @app.post(API + "/requests/{ref}/react")
    async def requests_react(request: Request, ref: str) -> JSONResponse:
        _allow(request)
        s, signer, key, data = await _posted(request)
        signer, viewer = await _acting(request, signer)
        who, cid = _retry(signer, key, data)

        async def make() -> JSONResponse:
            return answer(await _run(react, s, ref, signer, key, data, viewer), NO_STORE)

        return await retries.once(request, who, cid, make)

    @app.get(API + "/events/{link}/activity")
    async def event_activity(request: Request, link: str) -> JSONResponse:
        _allow(request)
        kind = request.query_params.get("type") or None
        if kind is not None and kind not in TYPES:
            raise ApiError(422, "invalid", "type: " + ", ".join(TYPES) + ".")
        before, limit = _paging(request)
        s = request.app.state.services
        out = await _run(activity, s, link, kind, before, limit)
        return answer(out, SHORT)


__all__ = [
    "register",
    "forget",
    "snapshot",
    "listing",
    "detail",
    "comment",
    "edit",
    "react",
    "activity",
]
