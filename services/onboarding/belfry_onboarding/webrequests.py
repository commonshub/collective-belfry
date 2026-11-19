"""The public requests and an event's public activity, as JSON (docs/requests.md, 2026-11-19).

A community's own website (the nsite on the apex) renders three pages itself, in its own look:
the list of requests (like GitHub issues), one request's thread with its approval, and an
event's activity log. The data is on the groups relay, which wants authentication even for
public channels, so a browser can't read it: the belfry serves it here, read only, plus the
one thing an author does from the web, a comment under their own request.

- ``GET /api/v1/requests``: the threads of the sales requests channel (sales.requests_site).
- ``GET /api/v1/requests/<id or number>``: one thread with its timeline; ``me`` for its author.
- ``POST /api/v1/requests/<id>/comments``: the author's comment.
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
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import events, sales, webthreads
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
    roots, replies = webthreads._events(svc, channel)
    labels = webthreads._labels(svc, channel)
    roots.sort(key=lambda e: (e["created_at"], e["id"]))
    st = sales.store(svc)
    # numbers live in sales.sqlite (table request_numbers): given once, in creation order
    numbers = st.request_numbers(channel, [e["id"] for e in roots])
    offers = {f["card"]: f for f in st.offers() if f.get("card") and f.get("channel") == channel}
    labellers = [{"pubkey": who} for labs in labels.values() for (_at, _lab, who) in labs]
    names, roles = webthreads._who(svc, [*roots, *replies, *labellers])
    by_root: dict[str, list[dict]] = {}
    for r in sorted(replies, key=lambda e: (e["created_at"], e["id"])):
        by_root.setdefault(webthreads._root_of(r), []).append(r)
    gk = webthreads._groups_pubkey(svc)
    threads = []
    for root in roots:
        threads.append(
            {
                "root": root,
                "number": numbers[root["id"]],
                "offer": offers.get(root["id"]),
                "replies": by_root.get(root["id"], []),
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
    }


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


def _comments(snap: dict[str, Any], t: dict[str, Any], mine: bool = False) -> list[dict]:
    site = snap["site"]
    return [
        r
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


def item(svc: Services, snap: dict[str, Any], t: dict[str, Any], mine: bool = False) -> dict:
    root, f = t["root"], t["offer"]
    comments = _comments(snap, t, mine)
    state = _state(t)
    decided = _unix(f.get("decided_at")) if f and state in ("approved", "declined") else None
    return {
        "id": root["id"],
        "number": t["number"],
        "title": webthreads._title(root),
        # an offer says which it is: in kind, or a sponsored pass asked for (sales.OFFER_KINDS)
        "kind": sales.offer_kind(f) if f else "other",
        "label": f.get("label") if f else None,
        "days": (f.get("days") or None) if f else None,
        "percent_asked": int(f["percent"]) if f else None,
        "state": state,
        "percent_approved": int(f.get("decided_percent") or 0) if state == "approved" else None,
        "approved_by": {"name": _name(f.get("decided_by"))} if state == "approved" else None,
        "author": _person(svc, snap, root),
        "created_at": root["created_at"],
        "comments": len(comments),
        "last_at": max([root["created_at"], decided or 0, *(c["created_at"] for c in comments)]),
    }


def _timeline(svc: Services, snap: dict[str, Any], t: dict[str, Any], mine: bool) -> list[dict]:
    root, f = t["root"], t["offer"]
    out: list[dict[str, Any]] = []
    for c in _comments(snap, t, mine):
        who = _person(svc, snap, c)
        if _same_author(snap, root, c):
            who["role"] = "author"
        out.append(
            {"type": "comment", "author": who, "at": c["created_at"], "text": webthreads._body(c)}
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


def listing(svc: Services, state: str, q: str, before: int | None, limit: int) -> dict[str, Any]:
    snap = snapshot(svc)
    needle = fold(q)
    rows = []
    for t in snap["threads"]:
        if not t["public"]:
            continue
        root = t["root"]
        if needle and needle not in fold(webthreads._title(root) + "\n" + webthreads._body(root)):
            continue
        rows.append(item(svc, snap, t))
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


def detail(svc: Services, ref: str, signer: str | None, thread_key: str) -> dict[str, Any]:
    from .holder_invites import ApiError

    snap = snapshot(svc)
    t = _find(snap, ref)
    mine = t is not None and _proven(svc, t, signer, thread_key)
    if t is None or not (t["public"] or mine):
        raise ApiError(404, "unknown", "No such request.")
    out = item(svc, snap, t, mine)
    out["body"] = webthreads._body(t["root"])
    out["timeline"] = _timeline(svc, snap, t, mine)
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


def comment(svc: Services, ref: str, signer: str | None, thread_key: str, text: Any) -> None:
    from .holder_invites import ApiError

    snap = snapshot(svc)
    t = _find(snap, ref) if ID_RE.match(ref) else None
    if t is None:
        raise ApiError(404, "unknown", "No such request.")
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
        title = webthreads._title(root)
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
    """FN in a thread; whatever goes wrong with the relay is 503, never a trace."""
    from .holder_invites import ApiError

    try:
        return await run_in_threadpool(fn, *args)
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
        before, limit = _paging(request)
        s = request.app.state.services
        out = await _run(listing, s, state, text, before, limit)
        return answer(out, SHORT)

    @app.get(API + "/requests/{ref}")
    async def requests_one(request: Request, ref: str) -> JSONResponse:
        _allow(request)
        s = request.app.state.services
        # who asks, when they say: the offer's key (NIP-98) or the thread's own key, in a
        # header. Never from the query string (query strings end up in logs)
        signer = events._viewer(request, request.url.path)
        key = request.headers.get("x-thread-key") or ""
        out = await _run(detail, s, ref, signer, key)
        return answer(out, NO_STORE)  # the same address answers its author with more

    @app.post(API + "/requests/{ref}/comments")
    async def requests_comment(request: Request, ref: str) -> JSONResponse:
        _allow(request)
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
        key = request.headers.get("x-thread-key") or ""
        await _run(comment, s, ref, signer, key, data.get("text"))
        return answer({"ok": True}, NO_STORE, 201)

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


__all__ = ["register", "forget", "snapshot", "listing", "detail", "comment", "activity"]
