"""Belfry events: a Luma alternative built into the belfry.

Protocol: work/events-design/PROTOCOL.md (the belfry agent's notes).

An event is three things that point at each other:

1. a NIP-52 calendar event (kind 31923 timed, 31922 all day) on the community relay, ``d`` =
   ``<year>-<6 of [a-z0-9]>``, with our tags ``visibility`` (members | link | public), ``slug``
   and ``image`` (the cover);
2. its NIP-29 group ``ev-<d>`` on the groups relay: its chat, and the people in it; the hosts
   are the group's admins (39001) plus the creator;
3. ticket types, NIP-99 listings (kind 30402, ``a`` = the event, ``d`` = ``<d>:<id>``), without
   prices in v1. No ticket type = one implicit free, open, unlimited ticket ``<d>:free``.

Holding a ticket = a kind 9000 in the group with a ``ticket`` tag (by the groups key for an open
ticket taken here, by a host for an approved application); a later 9001 takes it back. An
application is a NIP-29 join request (9021, ``ticket`` tag, content = the answer; ``waitlist``
when the ticket is full), which the groups relay takes from people who are not relay members
(zooid patch 0004).

The server is the public reader: the community relay is members-only, so outsiders (and the
channel's guests) see an event through ``GET /api/events/<link>`` and the page
``https://<domain>/events/<link>``. It also enforces what clients only display: quantity,
members-only tickets, one ticket per person.

Rule B (co-host edits): among the versions with the event's ``d`` signed by a current host, the
newest wins; the coordinate stays the creator's (``<kind>:<creator>:<d>``).

Pages show fixed strings and the event's own content (escaped); never text from the URL.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import logging
import re
import sqlite3
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from .clients.odoo import OdooError
from .core import Services, m2o_id

log = logging.getLogger(__name__)
E = html.escape

D_RE = re.compile(r"^\d{4}-[a-z0-9]{6}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
LINK_RE = re.compile(r"^[a-z0-9-]{3,40}$")
RESERVED_SLUGS = {"new", "slug", "feed", "past", "mine"}
KINDS = (31922, 31923)
TICKET_KIND = 30402
STATUS_NS = "belfry/event-status"
GUEST_DAYS_AFTER = 30  # an event's guest keeps the chat this long after the event
API = "/api/events"
VISIBILITIES = ("members", "link", "public")
FREE = "free"  # the implicit ticket's id: <d>:free

# Fixed messages for the pages, picked by key.
TEXT = {
    "not_found": "There is no such event here.",
    "members": "This event is for the community's members.",
    "cancelled": "This event is cancelled.",
}


# ── the store: slugs, mails already sent, the watcher's place ───────────────────────────────
class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS slugs (
                slug TEXT PRIMARY KEY, d TEXT NOT NULL, current INTEGER NOT NULL, at INTEGER);
            CREATE TABLE IF NOT EXISTS sent (key TEXT PRIMARY KEY, at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS params (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )

    def slug(self, slug: str) -> tuple[str, bool] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT d, current FROM slugs WHERE slug = ?", (slug,)
            ).fetchone()
        return (row[0], bool(row[1])) if row else None

    def current_slug(self, d: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT slug FROM slugs WHERE d = ? AND current = 1", (d,)
            ).fetchone()
        return row[0] if row else None

    def claim(self, d: str, slug: str) -> bool:
        """SLUG becomes D's address; its old one keeps redirecting. False: taken by another."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT d FROM slugs WHERE slug = ?", (slug,)).fetchone()
                if row and row[0] != d:
                    self._db.execute("ROLLBACK")
                    return False
                self._db.execute("UPDATE slugs SET current = 0 WHERE d = ?", (d,))
                self._db.execute(
                    "INSERT OR REPLACE INTO slugs (slug, d, current, at) VALUES (?, ?, 1, ?)",
                    (slug, d, int(time.time())),
                )
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")
            return True

    def once(self, key: str) -> bool:
        """True the first time KEY is asked (a mail to send once)."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO sent (key, at) VALUES (?, ?)", (key, int(time.time()))
            )
        return cur.rowcount == 1

    def param(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._db.execute("SELECT value FROM params WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_param(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO params (key, value) VALUES (?, ?)", (key, value)
            )


_stores: dict[str, Store] = {}
_stores_lock = threading.Lock()


def store(svc: Services) -> Store:
    path = str(Path(svc.settings.register_path).with_name("events.sqlite"))
    with _stores_lock:
        if path not in _stores:
            _stores[path] = Store(path)
        return _stores[path]


# ── the relays ──────────────────────────────────────────────────────────────────────────────
def community(svc: Services, filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The community relay, read with the belfry's key (members-only relay)."""
    from . import community as c

    return c.query(svc, filters)


def groups(svc: Services, filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The groups relay, read with the groups key (its owner)."""
    from . import followers

    got = followers._relay(svc)
    if got is None:
        return []
    relay, sign = got
    return relay.query(filters, sign)


def publish(svc: Services, kind: int, tags: list[list[str]], content: str = "") -> bool:
    """An event by the groups key in the groups relay (a 9000, a label)."""
    from . import followers

    got = followers._relay(svc)
    if got is None:
        return False
    relay, sign = got
    try:
        relay.publish(
            sign({"kind": kind, "created_at": int(time.time()), "tags": tags, "content": content}),
            sign,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — the caller says so
        log.warning("events: kind %s not published: %s", kind, exc)
        return False


def forward(svc: Services, event: dict[str, Any]) -> None:
    """Hand in someone's own signed join request (zooid patch 0004: the owner may)."""
    from . import followers

    got = followers._relay(svc)
    if got is None:
        raise RuntimeError("no groups relay")
    relay, sign = got
    relay.publish(event, sign)


# ── reading an event ────────────────────────────────────────────────────────────────────────
def tag(e: dict[str, Any], name: str) -> str | None:
    for t in e.get("tags", []):
        if t[:1] == [name] and len(t) > 1:
            return str(t[1])
    return None


def has(e: dict[str, Any], name: str) -> bool:
    return any(t[:1] == [name] for t in e.get("tags", []))


def group_id(d: str) -> str:
    return "ev-" + d


@dataclass
class Ticket:
    d: str
    id: str
    title: str
    summary: str = ""
    quantity: int | None = None
    closed: bool = False
    prompt: str = ""
    members: bool = False
    position: int = 0
    implicit: bool = False

    def public(self, taken: int) -> dict[str, Any]:
        return {
            "d": self.d,
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "quantity": self.quantity,
            "left": None if self.quantity is None else max(0, self.quantity - taken),
            "closed": self.closed,
            "prompt": self.prompt if self.closed else "",
            "members": self.members,
            "position": self.position,
            "implicit": self.implicit,
        }


@dataclass
class Event:
    d: str
    kind: int
    creator: str
    version: dict[str, Any]
    hosts: set[str]
    title: str = ""
    about: str = ""
    start: dt.datetime | dt.date | None = None
    end: dt.datetime | dt.date | None = None
    tz: str = ""
    location: str = ""
    links: list[str] = field(default_factory=list)
    cover: str = ""
    visibility: str = "members"
    status: str = "scheduled"
    status_reason: str = ""

    @property
    def coord(self) -> str:
        return f"{self.kind}:{self.creator}:{self.d}"

    @property
    def group(self) -> str:
        return group_id(self.d)

    @property
    def all_day(self) -> bool:
        return self.kind == 31922

    def last_day(self) -> dt.date:
        x = self.end or self.start
        if isinstance(x, dt.datetime):
            return x.date()
        return x or dt.date.today()

    def ended(self, now: dt.datetime | None = None) -> bool:
        now = now or dt.datetime.now(dt.UTC)
        x = self.end or self.start
        if isinstance(x, dt.datetime):
            return x < now
        if isinstance(x, dt.date):
            return x < now.date()
        return False

    def guest_until(self) -> str:
        return (self.last_day() + dt.timedelta(days=GUEST_DAYS_AFTER)).isoformat()


def _when(value: str | None, all_day: bool) -> dt.datetime | dt.date | None:
    if not value:
        return None
    try:
        if all_day:
            return dt.date.fromisoformat(value[:10])
        return dt.datetime.fromtimestamp(int(value), dt.UTC)
    except (ValueError, OverflowError, OSError):
        return None


def hosts_of(svc: Services, group: str) -> tuple[set[str], str | None]:
    """The group's admins (39001), and its creator (the author of its 9007), when known."""
    found = groups(
        svc, [{"kinds": [39001], "#d": [group], "limit": 1}, {"kinds": [9007], "#h": [group]}]
    )
    admins = {
        t[1]
        for e in found
        if e.get("kind") == 39001
        for t in e.get("tags", [])
        if t[:1] == ["p"] and len(t) > 1
    }
    made = sorted((e for e in found if e.get("kind") == 9007), key=lambda e: e["created_at"])
    return admins, (made[0]["pubkey"] if made else None)


def load(svc: Services, d: str) -> Event | None:
    if not D_RE.match(d):
        return None
    versions = [
        v
        for v in community(svc, [{"kinds": list(KINDS), "#d": [d]}])
        if v.get("kind") in KINDS and tag(v, "d") == d
    ]
    if not versions:
        return None
    admins, creator = hosts_of(svc, group_id(d))
    if creator is None:  # no group seen: the oldest version's author made it
        creator = min(versions, key=lambda v: v["created_at"])["pubkey"]
    hosts = admins | {creator}
    mine = [v for v in versions if v["pubkey"] in hosts]
    if not mine:
        return None
    latest = max(mine, key=lambda v: (v["created_at"], v.get("id", "")))
    first_kind = next(
        (
            v["kind"]
            for v in sorted(versions, key=lambda v: v["created_at"])
            if v["pubkey"] == creator
        ),
        latest["kind"],
    )
    # deleted by its creator (kind 5 on the coordinate)
    coord = f"{first_kind}:{creator}:{d}"
    gone = community(svc, [{"kinds": [5], "authors": [creator], "#a": [coord]}])
    if any(e.get("created_at", 0) >= latest["created_at"] for e in gone):
        return None
    all_day = latest["kind"] == 31922
    visibility = tag(latest, "visibility") or "members"
    ev = Event(
        d=d,
        kind=first_kind,
        creator=creator,
        version=latest,
        hosts=hosts,
        title=(tag(latest, "title") or tag(latest, "name") or "Untitled")[:200],
        about=(latest.get("content") or "")[:20000],
        start=_when(tag(latest, "start"), all_day),
        end=_when(tag(latest, "end"), all_day),
        tz=tag(latest, "start_tzid") or "",
        location=(tag(latest, "location") or "")[:300],
        links=[
            str(t[1])
            for t in latest.get("tags", [])
            if t[:1] == ["r"] and len(t) > 1 and str(t[1]).startswith("https://")
        ],
        cover=tag(latest, "image") or "",
        visibility=visibility if visibility in VISIBILITIES else "members",
        status="cancelled" if tag(latest, "status") == "cancelled" else "scheduled",
    )
    # the status label in the group, by a host: the latest wins over the version's tag
    labels = [
        e
        for e in groups(svc, [{"kinds": [1985], "#h": [ev.group], "#L": [STATUS_NS]}])
        if e.get("pubkey") in hosts
    ]
    if labels:
        last = max(labels, key=lambda e: e["created_at"])
        if last["created_at"] >= latest["created_at"] or ev.status == "scheduled":
            label = next(
                (t[1] for t in last.get("tags", []) if t[:1] == ["l"] and len(t) > 1), "scheduled"
            )
            ev.status = "cancelled" if label == "cancelled" else "scheduled"
            ev.status_reason = (last.get("content") or "")[:500] if ev.status == "cancelled" else ""
    return ev


def resolve(svc: Services, link: str) -> tuple[Event | None, str | None]:
    """The event behind LINK (its id or an alias), and the current alias when LINK is an old one
    (the caller redirects)."""
    link = (link or "").lower()
    if D_RE.match(link):
        return load(svc, link), None
    if not LINK_RE.match(link):
        return None, None
    found = store(svc).slug(link)
    if found is None:
        return None, None
    d, current = found
    if not current:
        return load(svc, d), store(svc).current_slug(d) or d
    return load(svc, d), None


def tickets(svc: Services, ev: Event) -> list[Ticket]:
    """The event's ticket types (newest version per ``d`` by a host, not deleted), by position;
    none defined: the implicit free one."""
    found = community(svc, [{"kinds": [TICKET_KIND, 5], "#a": [ev.coord]}])
    latest: dict[str, dict[str, Any]] = {}
    for e in found:
        if e.get("kind") != TICKET_KIND or e.get("pubkey") not in ev.hosts:
            continue
        td = tag(e, "d") or ""
        if not td.startswith(ev.d + ":"):
            continue
        if td not in latest or e["created_at"] > latest[td]["created_at"]:
            latest[td] = e
    deletions = [e for e in found if e.get("kind") == 5 and e.get("pubkey") in ev.hosts]
    out = []
    for td, e in latest.items():
        gone = any(
            d.get("created_at", 0) >= e["created_at"]
            and any(
                t[:1] == ["a"] and len(t) > 1 and str(t[1]).endswith(":" + td)
                for t in d.get("tags", [])
            )
            for d in deletions
        )
        if gone:
            continue
        try:
            qty = int(tag(e, "quantity") or "")
        except ValueError:
            qty = None
        try:
            pos = int(tag(e, "position") or "0")
        except ValueError:
            pos = 0
        out.append(
            Ticket(
                d=td,
                id=td.split(":", 1)[1],
                title=(tag(e, "title") or "Ticket")[:120],
                summary=(tag(e, "summary") or "")[:500],
                quantity=qty if qty is not None and qty >= 0 else None,
                closed=has(e, "closed"),
                prompt=(tag(e, "prompt") or "")[:500],
                members=has(e, "members"),
                position=pos,
            )
        )
    if not out:
        return [Ticket(d=f"{ev.d}:{FREE}", id=FREE, title="RSVP", implicit=True)]
    return sorted(out, key=lambda t: (t.position, t.title))


@dataclass
class People:
    holders: dict[str, str]  # pubkey → ticket d
    applications: dict[str, dict[str, Any]]  # pubkey → their open 9021
    declined: set[str]
    members: set[str]  # in the group (39002)
    rsvps: dict[str, str]  # pubkey → accepted / tentative / declined (both relays)

    def taken(self, ticket_d: str) -> int:
        return sum(1 for t in self.holders.values() if t == ticket_d)

    def going(self) -> int:
        keys = set(self.holders) | {k for k, s in self.rsvps.items() if s == "accepted"}
        return sum(1 for k in keys if self.rsvps.get(k, "accepted") == "accepted")


def people(svc: Services, ev: Event) -> People:
    found = groups(
        svc,
        [
            {"kinds": [9000, 9001, 9021, 9022], "#h": [ev.group]},
            {"kinds": [39002], "#d": [ev.group], "limit": 1},
            {"kinds": [31925], "#h": [ev.group]},
        ],
    )
    put: dict[str, dict[str, Any]] = {}  # latest 9000 with a ticket
    out: dict[str, int] = defaultdict(int)  # latest 9001
    decided: dict[str, int] = defaultdict(int)
    applied: dict[str, dict[str, Any]] = {}
    left: dict[str, int] = defaultdict(int)
    members: set[str] = set()
    for e in sorted(found, key=lambda e: e["created_at"]):
        k = e.get("kind")
        p = tag(e, "p")
        if k == 9000 and p:
            decided[p] = e["created_at"]
            if tag(e, "ticket"):
                put[p] = e
        elif k == 9001 and p:
            decided[p] = e["created_at"]
            out[p] = e["created_at"]
        elif k == 9021:
            applied[e["pubkey"]] = e
        elif k == 9022:
            left[e["pubkey"]] = e["created_at"]
        elif k == 39002:
            members = {t[1] for t in e.get("tags", []) if t[:1] == ["p"] and len(t) > 1}
    holders = {p: str(tag(e, "ticket")) for p, e in put.items() if e["created_at"] > out.get(p, 0)}
    applications = {
        p: e
        for p, e in applied.items()
        if p not in holders
        and decided.get(p, 0) < e["created_at"]
        and left.get(p, 0) < e["created_at"]
    }
    declined = {p for p, at in out.items() if p not in holders and at >= decided.get(p, 0)}
    rsvps: dict[str, tuple[int, str]] = {}
    member_rsvps = community(svc, [{"kinds": [31925], "#a": [ev.coord]}])
    for e in [*member_rsvps, *(x for x in found if x.get("kind") == 31925)]:
        if tag(e, "a") != ev.coord:
            continue
        s = tag(e, "status") or ""
        if s in ("accepted", "tentative", "declined") and (
            e["pubkey"] not in rsvps or rsvps[e["pubkey"]][0] <= e["created_at"]
        ):
            rsvps[e["pubkey"]] = (e["created_at"], s)
    return People(
        holders=holders,
        applications=applications,
        declined=declined,
        members=members,
        rsvps={k: v[1] for k, v in rsvps.items()},
    )


# ── who is asking ───────────────────────────────────────────────────────────────────────────
def passes_of(svc: Services, pubkey: str) -> list[dict[str, Any]]:
    return svc.odoo.search_read(
        "belfry.pass",
        [["keycast_pubkey", "=", pubkey]],
        ["state", "tier", "partner_id", "guest_channel", "event_email"],
    )


def is_member(svc: Services, pubkey: str) -> bool:
    from .places import member_key

    return member_key(svc, pubkey) is not None


def may_read(svc: Services, ev: Event, pubkey: str | None, ppl: People | None = None) -> bool:
    if ev.visibility in ("public", "link"):
        return True
    if not pubkey:
        return False
    if pubkey in ev.hosts or is_member(svc, pubkey):
        return True
    ppl = ppl or people(svc, ev)
    return pubkey in ppl.members or pubkey in ppl.holders


def names(svc: Services, pubkeys: list[str]) -> dict[str, str]:
    from . import community as c

    try:
        return c.names_for(svc, pubkeys)
    except Exception:  # noqa: BLE001 — names are a nicety
        return {}


def domain(svc: Services) -> str:
    return svc.settings.nip05_domain


def site(svc: Services) -> str:
    return (svc.settings.site_url or f"https://{domain(svc)}").rstrip("/")


def link_of(svc: Services, ev: Event) -> str:
    return store(svc).current_slug(ev.d) or ev.d


def url_of(svc: Services, ev: Event) -> str:
    return f"{site(svc)}/events/{link_of(svc, ev)}"


def chat_url(svc: Services, ev: Event) -> str:
    return f"https://chat.{domain(svc)}/#/events/{quote(link_of(svc, ev), safe='')}"


def _iso(x: dt.datetime | dt.date | None) -> str | None:
    if x is None:
        return None
    return x.isoformat().replace("+00:00", "Z")


def public_json(svc: Services, ev: Event, viewer: str | None) -> dict[str, Any]:
    """What anyone allowed to read the event sees; the online link only for those in it."""
    ppl = people(svc, ev)
    tks = tickets(svc, ev)
    host_names = names(svc, sorted(ev.hosts))
    inside = bool(viewer) and (
        viewer in ev.hosts
        or viewer in ppl.holders
        or viewer in ppl.members
        or is_member(svc, viewer or "")
    )
    me: dict[str, Any] | None = None
    if viewer:
        me = {
            "host": viewer in ev.hosts,
            "ticket": ppl.holders.get(viewer),
            "applied": viewer in ppl.applications,
            "waitlist": has(ppl.applications[viewer], "waitlist")
            if viewer in ppl.applications
            else False,
            "declined": viewer in ppl.declined,
            "rsvp": ppl.rsvps.get(viewer),
            "member": is_member(svc, viewer),
        }
    return {
        "d": ev.d,
        "coord": ev.coord,
        "kind": ev.kind,
        "group": ev.group,
        "link": link_of(svc, ev),
        "url": url_of(svc, ev),
        "title": ev.title,
        "description": ev.about,
        "all_day": ev.all_day,
        "start": _iso(ev.start),
        "end": _iso(ev.end),
        "tz": ev.tz,
        "location": ev.location,
        "online": bool(ev.links),
        "links": ev.links if inside else [],
        "cover": f"{url_of(svc, ev)}/cover" if ev.cover else None,
        "visibility": ev.visibility,
        "status": ev.status,
        "status_reason": ev.status_reason,
        "hosts": [{"pubkey": h, "name": host_names.get(h, "")} for h in sorted(ev.hosts)],
        "tickets": [t.public(ppl.taken(t.d)) for t in tks],
        "going": ppl.going(),
        "community": _community_name(),
        "me": me,
    }


def _community_name() -> str:
    from . import theme

    return theme.current().name


# ── slugs ───────────────────────────────────────────────────────────────────────────────────
def slug_problem(slug: str) -> str | None:
    if not SLUG_RE.match(slug):
        return "3 to 40 of a-z, 0-9 and -, not starting or ending with -"
    if D_RE.match(slug):
        return "shaped like an event's own id"
    if slug in RESERVED_SLUGS:
        return "a word the belfry keeps"
    return None


def suggest(svc: Services, name: str, d: str = "") -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:34].strip("-") or "event"
    if len(base) < 3:
        base = (base + "-event")[:34]
    for n in range(0, 50):
        cand = base if n == 0 else f"{base}-{n + 1}"
        if slug_problem(cand) is None:
            got = store(svc).slug(cand)
            if got is None or got[0] == d:
                return cand
    return f"{base}-{int(time.time()) % 100000}"


# ── rate limits (per IP and per event, in memory) ───────────────────────────────────────────
class Limiter:
    def __init__(self, events: int, seconds: float):
        self.events, self.seconds = events, seconds
        self._seen: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._seen[key]
            while q and now - q[0] > self.seconds:
                q.popleft()
            if len(q) >= self.events:
                return False
            q.append(now)
            return True


PER_IP = Limiter(20, 60)
PER_EVENT = Limiter(120, 60)
_issue_lock = threading.Lock()  # one ticket issued at a time: the count stays right


def _limit(request: Request, d: str) -> None:
    from .holder_invites import ApiError

    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        request.client.host if request.client else "?"
    )
    if not PER_IP.allow("ip:" + ip) or not PER_EVENT.allow("ev:" + d):
        raise ApiError(429, "too_many", "Too many requests for now. Try again in a minute.")


# ── taking a ticket, applying ───────────────────────────────────────────────────────────────
def _pick(tks: list[Ticket], wanted: str | None) -> Ticket | None:
    if not wanted:
        return tks[0] if len(tks) == 1 else None
    return next((t for t in tks if t.d == wanted or t.id == wanted), None)


def _check_open(ev: Event) -> None:
    from .holder_invites import ApiError

    if ev.status == "cancelled":
        raise ApiError(409, "cancelled", "This event is cancelled.")
    if ev.ended():
        raise ApiError(410, "ended", "This event is over.")


def _ticket_tags(ev: Event, pubkey: str, t: Ticket, extra: list[list[str]]) -> list[list[str]]:
    return [
        ["h", ev.group],
        ["p", pubkey, "member"],
        ["ticket", t.d],
        ["paid", "free"],
        ["a", ev.coord],
        *extra,
    ]


def new_guest(
    svc: Services, ev: Event, name: str, email: str, client: str, state: str
) -> dict[str, Any]:
    """A person with no account: their contact, a guest pass of the event's group (STATE), an
    account in the signer and a session bound to CLIENT (the key that signed the request).
    Asking again from the same device gives the same account a new session."""
    from . import core
    from .clients.keycast import ClientKeyTaken
    from .holder_invites import ApiError

    bind = client.lower() if re.fullmatch(r"[0-9a-fA-F]{64}", client or "") else None
    try:
        made = svc.odoo.event_guest(ev.group, name, email, client, ev.guest_until(), state)
    except OdooError as exc:
        if "member_email" in str(exc.message or exc):
            raise ApiError(
                409, "member_email", "This address belongs to a member: sign in with it instead."
            ) from exc
        raise ApiError(409, "refused", str(exc.message or "Refused.")) from exc
    pid = made["partner_id"]
    row = svc.odoo.read_one("res.partner", pid, ["name"]) or {}
    partner = {"id": pid, "name": row.get("name") or name}
    pubkey, npub, ucan = core._ensure_keycast_user(svc, partner, f"guest-{pid}")
    try:
        bunker = svc.keycast.create_bunker(ucan, "Chat (guest)", bind) if ucan else {}
    except ClientKeyTaken as exc:
        if not made.get("again"):
            svc.odoo.write("belfry.pass", [made["pass_id"]], {"state": "revoked"})
        raise ApiError(
            409, "session_taken", "This session belongs to another account. Open the page again."
        ) from exc
    if not bunker.get("bunker_url"):
        raise RuntimeError("the signer gave no session")
    if not made.get("again"):
        svc.odoo.write("res.partner", [pid], {"belfry_npub": npub})
    vals: dict[str, Any] = {"keycast_pubkey": pubkey}
    if state == "active":
        vals["state"] = "active"
    svc.odoo.write("belfry.pass", [made["pass_id"]], vals)
    return {"pubkey": pubkey, "bunker_url": bunker["bunker_url"], "again": bool(made.get("again"))}


def join(svc: Services, ev: Event, caller: str, data: dict[str, Any]) -> dict[str, Any]:
    """An open ticket (free in v1): the caller's account gets it, or a new guest does."""
    from .holder_invites import ApiError

    _check_open(ev)
    tks = tickets(svc, ev)
    t = _pick(tks, str(data.get("ticket") or "") or None)
    if t is None:
        raise ApiError(404, "no_ticket", "Pick one of the event's tickets.")
    if t.closed:
        raise ApiError(409, "needs_application", "This ticket takes an application.")
    member = is_member(svc, caller)
    if (t.members or ev.visibility == "members") and not member and caller not in ev.hosts:
        raise ApiError(403, "members_only", "This ticket is for members.")
    name = " ".join(str(data.get("name") or "").split())[:100]
    email = str(data.get("email") or "").strip()[:254]
    if email and "@" not in email:
        raise ApiError(422, "invalid", "That is not an e-mail address.")
    with _issue_lock:
        ppl = people(svc, ev)
        if caller in ppl.holders:
            return {"already": True, "ticket": ppl.holders[caller], "pubkey": caller}
        # the same device asking again (a second tap): its guest holds the ticket already
        prior = [
            p
            for p in svc.odoo.search_read(
                "belfry.pass",
                [["guest_channel", "=", ev.group], ["guest_client", "=", caller]],
                ["keycast_pubkey", "state"],
            )
            if p.get("state") in ("pending", "active")
        ]
        again = bool(prior) and prior[0].get("keycast_pubkey") in ppl.holders
        if t.quantity is not None and ppl.taken(t.d) >= t.quantity and not again:
            raise ApiError(409, "full", "This ticket is sold out: join the waitlist.")
        has_account = any(p.get("state") == "active" for p in passes_of(svc, caller))
        if has_account:
            if not member:  # a guest of another channel: one more guest pass, this group's
                svc.odoo.event_guest_pass(caller, ev.group, ev.guest_until(), "active")
            if not publish(svc, 9000, _ticket_tags(ev, caller, t, [])):
                raise ApiError(503, "relay", "The event could not be reached. Try again.")
            return {"ticket": t.d, "pubkey": caller, "new": False}
        if not name:
            raise ApiError(422, "name_needed", "Tell us your name.")
        made = new_guest(svc, ev, name, email, caller, "active")
        if made["pubkey"] not in ppl.holders:
            extra = [["name", name], ["guest", ev.guest_until()]]
            if not email:
                extra.append(["unsaved", "1"])
            if not publish(svc, 9000, _ticket_tags(ev, made["pubkey"], t, extra)):
                raise ApiError(503, "relay", "The event could not be reached. Try again.")
        return {
            "ticket": t.d,
            "pubkey": made["pubkey"],
            "bunker_url": made["bunker_url"],
            "new": True,
            "guest": True,
        }


def check_request(ev: Event, caller: str, t: Ticket, req: Any, waitlist: bool) -> dict[str, Any]:
    """The applicant's own signed 9021 for this event's group and ticket, recent."""
    from .blossom import event_id, verify_signature
    from .holder_invites import ApiError

    if not isinstance(req, dict):
        raise ApiError(422, "request_needed", "Send your signed join request.")
    ok = (
        req.get("kind") == 9021
        and req.get("pubkey") == caller
        and tag(req, "h") == ev.group
        and tag(req, "ticket") == t.d
        and has(req, "waitlist") == waitlist
        and abs(int(req.get("created_at") or 0) - time.time()) < 600
        and isinstance(req.get("content"), str)
        and len(req["content"]) <= 2000
    )
    try:
        ok = ok and event_id(req) == req.get("id") and verify_signature(req)
    except (KeyError, TypeError, ValueError):
        ok = False
    if not ok:
        raise ApiError(422, "bad_request", "The join request does not hold.")
    return req


def apply(svc: Services, ev: Event, caller: str, data: dict[str, Any]) -> dict[str, Any]:
    """An application (a ticket that needs one) or a place on the waitlist (a full one).
    Someone with no account: first a guest identity (e-mail required) and its session; then,
    signed by that identity, the join request itself, which the belfry hands to the group."""
    from .holder_invites import ApiError

    _check_open(ev)
    tks = tickets(svc, ev)
    t = _pick(tks, str(data.get("ticket") or "") or None)
    if t is None:
        raise ApiError(404, "no_ticket", "Pick one of the event's tickets.")
    member = is_member(svc, caller)
    if (t.members or ev.visibility == "members") and not member and caller not in ev.hosts:
        raise ApiError(403, "members_only", "This ticket is for members.")
    ppl = people(svc, ev)
    if caller in ppl.holders:
        return {"already": True, "ticket": ppl.holders[caller]}
    full = t.quantity is not None and ppl.taken(t.d) >= t.quantity
    if not t.closed and not full:
        raise ApiError(409, "use_join", "This ticket is open: take it.")
    waitlist = full
    mine = passes_of(svc, caller)
    known = any(p.get("state") == "active" for p in mine) or any(
        p.get("state") == "pending" and p.get("guest_channel") == ev.group for p in mine
    )
    if not known:
        name = " ".join(str(data.get("name") or "").split())[:100]
        email = str(data.get("email") or "").strip()[:254]
        if not name:
            raise ApiError(422, "name_needed", "Tell us your name.")
        if "@" not in email or len(email) < 3:
            raise ApiError(422, "email_needed", "An application needs your e-mail address.")
        made = new_guest(svc, ev, name, email, caller, "pending")
        return {
            "pubkey": made["pubkey"],
            "bunker_url": made["bunker_url"],
            "new": True,
            "needs_request": True,
            "waitlist": waitlist,
        }
    if member:
        raise ApiError(409, "publish_yourself", "Members send their join request themselves.")
    req = check_request(ev, caller, t, data.get("request"), waitlist)
    if not any(p.get("guest_channel") == ev.group for p in mine):
        svc.odoo.event_guest_pass(caller, ev.group, ev.guest_until(), "pending")
    email = str(data.get("email") or "").strip()[:254]
    if email and "@" in email:
        for p in mine:
            if p.get("guest_channel") == ev.group and not p.get("event_email"):
                svc.odoo.write("belfry.pass", [p["id"]], {"event_email": email})
    try:
        forward(svc, req)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "duplicate" not in msg:
            log.warning("events %s: application not handed in: %s", ev.d, msg)
            raise ApiError(503, "relay", "The event could not be reached. Try again.") from exc
    _tell_hosts(svc, ev, caller, waitlist)
    return {"applied": True, "waitlist": waitlist, "ticket": t.d}


def _tell_hosts(svc: Services, ev: Event, who: str, waitlist: bool) -> None:
    from .mailer import Mailer

    name = names(svc, [who]).get(who) or "Someone"
    emails = [e for e in (_email_of(svc, h, "") for h in ev.hosts) if e]
    if not emails:
        return
    what = "joined the waitlist of" if waitlist else "applied to"
    try:
        Mailer(svc.settings).send(
            emails,
            f"{name} {what} {ev.title}",
            [f"{name} {what} {ev.title}.", "Open the event's guest list to answer."],
            button=(chat_url(svc, ev), "Open the guest list"),
        )
    except Exception as exc:  # noqa: BLE001 — a mail; the chat shows it too
        log.warning("events %s: hosts not mailed: %s", ev.d, exc)


def _email_of(svc: Services, pubkey: str, group: str) -> str:
    """Where to mail PUBKEY about an event: the event's own address on their guest pass of
    GROUP, else their contact's (proven) address."""
    rows = passes_of(svc, pubkey)
    for p in rows:
        if group and p.get("guest_channel") == group and p.get("event_email"):
            return str(p["event_email"])
    for p in rows:
        pid = m2o_id(p.get("partner_id"))
        if pid and p.get("state") in ("active", "pending"):
            partner = svc.odoo.read_one("res.partner", pid, ["email"]) or {}
            if partner.get("email"):
                return str(partner["email"])
    return ""


def decide(svc: Services, ev: Event, caller: str, data: dict[str, Any]) -> dict[str, Any]:
    """A host who can't moderate on the groups relay (a guest co-host) approves or declines
    through the belfry: the groups key publishes the 9000 / 9001, naming the host."""
    from .holder_invites import ApiError

    if caller not in ev.hosts:
        raise ApiError(403, "not_host", "Only the event's hosts decide.")
    who = str(data.get("pubkey") or "").lower()
    action = data.get("action")
    if not re.fullmatch(r"[0-9a-f]{64}", who) or action not in ("approve", "decline"):
        raise ApiError(422, "invalid", 'Send {"pubkey", "action": "approve" | "decline"}.')
    if action == "decline":
        ok = publish(svc, 9001, [["h", ev.group], ["p", who], ["by", caller]])
        return {"declined": ok}
    t = _pick(tickets(svc, ev), str(data.get("ticket") or "") or None)
    if t is None:
        raise ApiError(404, "no_ticket", "Pick one of the event's tickets.")
    with _issue_lock:
        ppl = people(svc, ev)
        if who in ppl.holders:
            return {"already": True}
        app = ppl.applications.get(who)
        extra = [["by", caller]] + ([["e", app["id"]]] if app else [])
        ok = publish(svc, 9000, _ticket_tags(ev, who, t, extra))
    return {"approved": ok}


# ── the watcher: mails after a decision, a cancellation, the day before ────────────────────
def watch(svc: Services) -> dict[str, int]:
    from .mailer import Mailer

    st = store(svc)
    now = int(time.time())
    since = int(st.param("watch.since", str(now - 3600)))
    found = groups(svc, [{"kinds": [9000, 9001, 1985], "since": max(0, since - 120)}])
    mailer = Mailer(svc.settings)
    done = {"going": 0, "declined": 0, "cancelled": 0, "reminded": 0}
    cache: dict[str, Event | None] = {}

    def event(d: str) -> Event | None:
        if d not in cache:
            cache[d] = load(svc, d)
        return cache[d]

    for e in sorted(found, key=lambda e: e["created_at"]):
        h = tag(e, "h") or ""
        if not h.startswith("ev-") or not D_RE.match(h[3:]):
            continue
        p = tag(e, "p") or ""
        if e["kind"] == 9000 and tag(e, "ticket") and p and st.once("going:" + e["id"]):
            ev = event(h[3:])
            if ev is None:
                continue
            _ticket_issued(svc, ev, p, mailer)
            done["going"] += 1
        elif e["kind"] == 9001 and p and st.once("declined:" + e["id"]):
            ev = event(h[3:])
            if ev is None:
                continue
            if _declined(svc, ev, p, mailer):
                done["declined"] += 1
        elif e["kind"] == 1985 and tag(e, "L") == STATUS_NS and st.once("status:" + e["id"]):
            ev = event(h[3:])
            if ev is None or e["pubkey"] not in ev.hosts:
                continue
            if any(t[:2] == ["l", "cancelled"] for t in e.get("tags", [])):
                done["cancelled"] += _cancelled(svc, ev, mailer, st)
    st.set_param("watch.since", str(now))
    done["reminded"] = remind(svc, mailer, st)
    return done


def _ticket_issued(svc: Services, ev: Event, pubkey: str, mailer: Any) -> None:
    """Someone holds a ticket: an applicant's pending pass opens; a guest with an e-mail is
    told; whoever has a proven address follows the event's announcements (asap)."""
    rows = passes_of(svc, pubkey)
    guest = not any(p.get("state") == "active" and p.get("tier") != "guest" for p in rows)
    for p in rows:
        if p.get("guest_channel") == ev.group and p.get("state") == "pending":
            svc.odoo.write("belfry.pass", [p["id"]], {"state": "active"})
    email = _email_of(svc, pubkey, ev.group)
    if guest and email:
        mailer.send(
            email,
            f"You're going: {ev.title}",
            [f"You have a place at {ev.title}, {when_text(ev)}.", _where_text(ev)],
            button=(chat_url(svc, ev), "Open the event"),
        )
    partner = next((m2o_id(p.get("partner_id")) for p in rows if p.get("partner_id")), None)
    if partner:
        try:
            svc.odoo.call(
                "belfry.follower", "belfry_follow_member_set", [partner, ev.group, "asap"]
            )
        except Exception:  # noqa: BLE001 — no proven address: no follower
            pass


def _declined(svc: Services, ev: Event, pubkey: str, mailer: Any) -> bool:
    rows = passes_of(svc, pubkey)
    pending = [
        p for p in rows if p.get("guest_channel") == ev.group and p.get("state") == "pending"
    ]
    if not pending:
        return False
    for p in pending:
        svc.odoo.write("belfry.pass", [p["id"]], {"state": "revoked"})
    email = _email_of(svc, pubkey, ev.group)
    if email:
        mailer.send(
            email,
            f"About {ev.title}",
            [
                f"The hosts of {ev.title} could not give you a place this time.",
                "Thank you for asking.",
            ],
        )
    return True


def _cancelled(svc: Services, ev: Event, mailer: Any, st: Store) -> int:
    ppl = people(svc, ev)
    n = 0
    for pk in sorted(set(ppl.holders) | set(ppl.applications)):
        email = _email_of(svc, pk, ev.group)
        if email and st.once(f"cancel-mail:{ev.d}:{pk}"):
            lines = [f"{ev.title} ({when_text(ev)}) is cancelled."]
            if ev.status_reason:
                lines.append(ev.status_reason)
            mailer.send(
                email, f"Cancelled: {ev.title}", lines, button=(chat_url(svc, ev), "Open the event")
            )
            n += 1
    return n


def remind(svc: Services, mailer: Any, st: Store) -> int:
    """The day before: ticket holders with an address get the when and where."""
    found = community(svc, [{"kinds": list(KINDS), "since": int(time.time()) - 120 * 86400}])
    ds = sorted({tag(e, "d") or "" for e in found if D_RE.match(tag(e, "d") or "")})
    now = dt.datetime.now(dt.UTC)
    n = 0
    for d in ds:
        ev = load(svc, d)
        if ev is None or ev.status == "cancelled" or ev.start is None:
            continue
        start = (
            ev.start
            if isinstance(ev.start, dt.datetime)
            else dt.datetime.combine(ev.start, dt.time(9), dt.UTC)
        )
        if not (now < start <= now + dt.timedelta(hours=30)):
            continue
        for pk in people(svc, ev).holders:
            email = _email_of(svc, pk, ev.group)
            if email and st.once(f"remind:{ev.d}:{pk}"):
                mailer.send(
                    email,
                    f"Tomorrow: {ev.title}",
                    [f"{ev.title} is {when_text(ev)}.", _where_text(ev)],
                    button=(chat_url(svc, ev), "Open the event"),
                )
                n += 1
    return n


def when_text(ev: Event) -> str:
    if ev.start is None:
        return ""
    if isinstance(ev.start, dt.datetime):
        tz = _zone(ev.tz)
        s = ev.start.astimezone(tz)
        out = s.strftime("%A %-d %B %Y, %H:%M")
        if isinstance(ev.end, dt.datetime):
            e = ev.end.astimezone(tz)
            out += "–" + (
                e.strftime("%H:%M") if e.date() == s.date() else e.strftime("%-d %B %H:%M")
            )
        return out + (f" ({ev.tz})" if ev.tz else " (UTC)")
    out = ev.start.strftime("%A %-d %B %Y")
    if isinstance(ev.end, dt.date) and ev.end != ev.start:
        out += " – " + ev.end.strftime("%A %-d %B %Y")
    return out


def _where_text(ev: Event) -> str:
    if ev.location and ev.links:
        return f"Where: {ev.location}, and online."
    if ev.location:
        return f"Where: {ev.location}."
    return "Online: the link is in the event." if ev.links else ""


def _zone(name: str):
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(name) if name else dt.UTC
    except Exception:  # noqa: BLE001
        return dt.UTC


# ── pages ───────────────────────────────────────────────────────────────────────────────────
STYLE = """<style>
.ev-cover{
  width:100%;
  aspect-ratio:2/1;
  object-fit:cover;
  border-radius:14px;
  display:block;
  margin:0 0 18px;
  background:var(--border);
}
.ev h1{
  margin:0 0 6px;
  font:600 1.9rem/1.15 var(--font-title);
  letter-spacing:-.01em;
  overflow-wrap:anywhere;
}
.ev .when{font-weight:600;margin:0 0 4px}
.ev .where{color:var(--fg-muted);margin:0 0 16px;overflow-wrap:anywhere}
.ev .hosts{color:var(--fg-muted);font-size:15px;margin:0 0 18px}
.ev .tag{
  display:inline-block;
  border:1px solid var(--border);
  border-radius:999px;
  padding:1px 10px;
  font-size:13px;
  margin:0 0 12px;
}
.ev .tag.cancel{border-color:#c0392b;color:#c0392b}
.ev .go{
  display:inline-block;
  background:var(--accent);
  color:var(--accent-fg,#fff);
  padding:11px 20px;
  border-radius:10px;
  text-decoration:none;
  font-weight:600;
  margin:6px 8px 6px 0;
}
.ev .ghost{
  display:inline-block;
  border:1px solid var(--border);
  color:var(--fg);
  padding:10px 16px;
  border-radius:10px;
  text-decoration:none;
  margin:6px 8px 6px 0;
}
.ev .about{margin:22px 0;overflow-wrap:anywhere}
.ev .tickets{margin:18px 0;padding:0;list-style:none}
.ev .tickets li{border:1px solid var(--border);border-radius:12px;padding:12px 14px;margin:0 0 8px}
.ev .tickets small{display:block;color:var(--fg-muted)}
.evlist{list-style:none;padding:0;margin:0}
.evlist li{margin:0 0 10px}
.evlist a{
  display:block;
  border:1px solid var(--border);
  border-radius:12px;
  padding:12px 14px;
  text-decoration:none;
  color:var(--fg);
}
.evlist small{display:block;color:var(--fg-muted)}
</style>"""


def _paragraphs(text: str) -> str:
    """The description, Markdown-ish: paragraphs, line breaks, **bold**, links. Escaped first."""
    out = []
    for block in re.split(r"\n\s*\n", text.strip()):
        b = E(block)
        b = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", b)
        b = re.sub(
            r"\[([^\]]{1,200})\]\((https://[^\s)]{1,500})\)",
            r'<a href="\2" rel="nofollow noopener">\1</a>',
            b,
        )
        b = re.sub(
            r'(?<![">])(https://[^\s<]{1,500})', r'<a href="\1" rel="nofollow noopener">\1</a>', b
        )
        out.append("<p>" + b.replace("\n", "<br>") + "</p>")
    return "".join(out)


def _page(title: str, body: str, head: str = "") -> str:
    from . import theme

    html_ = theme.frame(title, STYLE + body)
    return html_.replace("</head>", head + "</head>", 1) if head else html_


def _message(key: str, svc: Services, status: int) -> HTMLResponse:
    body = (
        f'<div class="ev"><p>{E(TEXT[key])}</p>'
        f'<p><a class="ghost" href="{E(site(svc))}/events">All public events</a></p></div>'
    )
    return HTMLResponse(_page("Event", body), status_code=status)


def event_page(svc: Services, ev: Event) -> str:
    url = url_of(svc, ev)
    ppl = people(svc, ev)
    tks = tickets(svc, ev)
    host_names = names(svc, sorted(ev.hosts))
    hosts = ", ".join(host_names.get(h) or "a member" for h in sorted(ev.hosts))
    desc = re.sub(r"\s+", " ", ev.about)[:200]
    head = (
        '<meta property="og:type" content="event">'
        f'<meta property="og:title" content="{E(ev.title)}">'
        f'<meta property="og:description" content="{E(desc)}">'
        f'<meta property="og:url" content="{E(url)}">'
        f'<link rel="canonical" href="{E(url)}"><meta name="description" content="{E(desc)}">'
        f'<link rel="alternate" type="text/calendar" href="{E(url)}.ics">'
    )
    if ev.cover:
        head += (
            f'<meta property="og:image" content="{E(url)}/cover">'
            '<meta name="twitter:card" content="summary_large_image">'
        )
    if ev.visibility != "public":
        head += '<meta name="robots" content="noindex">'
    cover = f'<img class="ev-cover" src="{E(url)}/cover" alt="">' if ev.cover else ""
    status = '<span class="tag cancel">Cancelled</span>' if ev.status == "cancelled" else ""
    where = ", ".join(x for x in (ev.location, "online" if ev.links else "") if x)
    ticket_rows = ""
    if not (len(tks) == 1 and tks[0].implicit):
        rows = []
        for t in tks:
            left = "" if t.quantity is None else f" · {max(0, t.quantity - ppl.taken(t.d))} left"
            kind = "Apply" if t.closed else "Free"
            mem = " · Members only" if t.members else ""
            rows.append(
                f"<li><b>{E(t.title)}</b><small>{kind}{mem}{left}</small>"
                + (f"<small>{E(t.summary)}</small>" if t.summary else "")
                + "</li>"
            )
        ticket_rows = f'<ul class="tickets">{"".join(rows)}</ul>'
    action = (
        ""
        if ev.status == "cancelled" or ev.ended()
        else f'<a class="go" href="{E(chat_url(svc, ev))}">RSVP</a>'
    )
    body = f"""<article class="ev">{cover}{status}
<h1>{E(ev.title)}</h1>
<p class="when">{E(when_text(ev))}</p>
{f'<p class="where">{E(where)}</p>' if where else ""}
<p class="hosts">Hosted by {E(hosts)} · {ppl.going()} going</p>
{action}<a class="ghost" href="{E(url)}.ics">Add to calendar</a>
{ticket_rows}
<div class="about">{_paragraphs(ev.about)}</div>
</article>"""
    return _page(ev.title, body, head)


def list_page(svc: Services) -> str:
    found = community(svc, [{"kinds": list(KINDS), "since": int(time.time()) - 400 * 86400}])
    ds = sorted({tag(e, "d") or "" for e in found if D_RE.match(tag(e, "d") or "")})
    evs = [ev for ev in (load(svc, d) for d in ds) if ev and ev.visibility == "public"]
    soon = sorted((e for e in evs if not e.ended()), key=lambda e: str(_iso(e.start)))
    rows = "".join(
        f'<li><a href="{E(url_of(svc, e))}"><b>{E(e.title)}</b><small>{E(when_text(e))}</small>'
        + (f"<small>{E(e.location)}</small>" if e.location else "")
        + "</a></li>"
        for e in soon
    )
    inner = f'<ul class="evlist">{rows}</ul>' if rows else "<p>No public events coming up.</p>"
    body = f'<div class="ev"><h1>Events</h1>{inner}</div>'
    return _page("Events", body)


def ics(svc: Services, ev: Event) -> str:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    def fold(line: str) -> str:
        out, b = [], line.encode()
        while len(b) > 74:
            cut = 74
            while cut and (b[cut] & 0xC0) == 0x80:
                cut -= 1
            out.append(b[:cut].decode())
            b = b" " + b[cut:]
        out.append(b.decode())
        return "\r\n".join(out)

    def stamp(x: dt.datetime | dt.date) -> str:
        if isinstance(x, dt.datetime):
            return "DTSTART:" + x.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        return "DTSTART;VALUE=DATE:" + x.strftime("%Y%m%d")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//Belfry//{esc(domain(svc))}//EN",
        "BEGIN:VEVENT",
        f"UID:{ev.d}@{domain(svc)}",
        "DTSTAMP:" + dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ"),
    ]
    if ev.start is not None:
        lines.append(stamp(ev.start))
        if ev.end is not None:
            end = ev.end
            if not isinstance(end, dt.datetime):
                end = end + dt.timedelta(days=1)  # DTEND of a date is exclusive
            lines.append(stamp(end).replace("DTSTART", "DTEND"))
    lines += [
        "SUMMARY:" + esc(ev.title),
        "DESCRIPTION:" + esc((ev.about[:2000] + "\n\n" + url_of(svc, ev)).strip()),
        "URL:" + url_of(svc, ev),
        "STATUS:" + ("CANCELLED" if ev.status == "cancelled" else "CONFIRMED"),
    ]
    if ev.location:
        lines.append("LOCATION:" + esc(ev.location))
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(fold(x) for x in lines) + "\r\n"


def cover_source(svc: Services, ev: Event) -> str | None:
    """Where the belfry fetches the cover: only a blob of the community's own Blossom."""
    u = urlparse(ev.cover)
    public = urlparse(svc.settings.blossom_public_url or f"https://blossom.{domain(svc)}")
    if u.scheme != "https" or u.netloc != public.netloc:
        return None
    blob = u.path.rsplit("/", 1)[-1]
    if not re.fullmatch(r"[0-9a-f]{64}(\.[a-z0-9]{1,5})?", blob):
        return None
    base = (getattr(svc.settings, "homepage_upload_url", "") or "http://blossom-gate").rstrip("/")
    return f"{base}/{blob}"


# ── routes ──────────────────────────────────────────────────────────────────────────────────
def _viewer(request: Request, path: str) -> str | None:
    """The NIP-98 signer of a GET, when there is one (optional)."""
    from .holder_invites import ApiError, authenticate

    auth = request.headers.get("authorization")
    if not auth:
        return None
    try:
        return authenticate(
            request.app.state.services, request.app.state.api_seen, auth, "GET", path, b""
        )
    except ApiError:
        return None


async def _signed(request: Request, method: str, path: str) -> tuple[Services, str, dict[str, Any]]:
    from .holder_invites import ApiError, authenticate

    s = request.app.state.services
    body = await request.body()
    pubkey = authenticate(
        s, request.app.state.api_seen, request.headers.get("authorization"), method, path, body
    )
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_json", "Send a JSON object.")
    return s, pubkey, data


def register(app: FastAPI) -> None:
    from .holder_invites import ApiError

    def own(s: Services) -> None:
        from . import invite

        if not invite.own(s):
            raise ApiError(409, "unsupported", "Event guests need the belfry's own register.")

    # -- public pages on the apex -------------------------------------------------------------
    @app.get("/events", response_class=HTMLResponse, include_in_schema=False)
    async def events_list(request: Request) -> Response:
        s = request.app.state.services
        try:
            page = await run_in_threadpool(list_page, s)
        except Exception as exc:  # noqa: BLE001
            log.warning("events list: %s", exc)
            page = _page(
                "Events", '<div class="ev"><h1>Events</h1><p>No public events coming up.</p></div>'
            )
        return HTMLResponse(page, headers={"Cache-Control": "public, max-age=60"})

    async def _found(request: Request, link: str, suffix: str = "") -> Event | Response:
        s = request.app.state.services
        try:
            ev, moved = await run_in_threadpool(resolve, s, link)
        except Exception as exc:  # noqa: BLE001
            log.warning("events %s: %s", link[:12], exc)
            return _message("not_found", s, 503)
        if ev is None:
            return _message("not_found", s, 404)
        if moved:
            return RedirectResponse(f"/events/{quote(moved, safe='')}{suffix}", status_code=301)
        if ev.visibility == "members":
            return _members_only(s, ev)
        return ev

    def _members_only(s: Services, ev: Event) -> HTMLResponse:
        body = (
            f'<div class="ev"><p>{E(TEXT["members"])}</p>'
            f'<p><a class="go" href="{E(chat_url(s, ev))}">Open it in the chat</a></p></div>'
        )
        page = _page("Event", body, '<meta name="robots" content="noindex">')
        return HTMLResponse(page, status_code=200, headers={"Cache-Control": "no-store"})

    @app.get("/events/{link}.ics", include_in_schema=False)
    async def event_ics(request: Request, link: str) -> Response:
        got = await _found(request, link, ".ics")
        if not isinstance(got, Event):
            return got
        text = await run_in_threadpool(ics, request.app.state.services, got)
        return Response(
            text,
            media_type="text/calendar; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{got.d}.ics"'},
        )

    @app.get("/events/{link}/cover", include_in_schema=False)
    async def event_cover(request: Request, link: str) -> Response:
        got = await _found(request, link, "/cover")
        if not isinstance(got, Event):
            return got if isinstance(got, RedirectResponse) else Response(status_code=404)
        src = cover_source(request.app.state.services, got)
        if not src:
            return Response(status_code=404)
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(src)
        except httpx.HTTPError:
            return Response(status_code=502)
        kind = r.headers.get("content-type", "")
        if r.status_code != 200 or not kind.startswith("image/") or len(r.content) > 15_000_000:
            return Response(status_code=404)
        return Response(
            r.content, media_type=kind, headers={"Cache-Control": "public, max-age=3600"}
        )

    @app.get("/events/{link}", response_class=HTMLResponse, include_in_schema=False)
    async def event_view(request: Request, link: str) -> Response:
        got = await _found(request, link)
        if not isinstance(got, Event):
            return got
        page = await run_in_threadpool(event_page, request.app.state.services, got)
        cache = "public, max-age=60" if got.visibility == "public" else "no-store"
        headers = {"Cache-Control": cache}
        if got.visibility == "link":
            headers["X-Robots-Tag"] = "noindex"
        return HTMLResponse(page, headers=headers)

    # -- the API ------------------------------------------------------------------------------
    @app.get(API + "/slug")
    async def slug_free(request: Request) -> JSONResponse:
        """``?name=`` → ``{"free", "suggestion", "problem"?}`` (members and hosts, NIP-98)."""
        name = (request.query_params.get("name") or "").strip().lower()[:60]
        d = (request.query_params.get("event") or "").strip().lower()
        path = API + "/slug" + ("?" + request.url.query if request.url.query else "")
        s = request.app.state.services
        who = _viewer(request, path)
        if not who:
            raise ApiError(401, "unauthorized", "Sign the request (NIP-98).")
        problem = slug_problem(name)
        taken = store(s).slug(name) if problem is None else None
        free = problem is None and (taken is None or taken[0] == d)
        out: dict[str, Any] = {"name": name, "free": free, "suggestion": suggest(s, name, d)}
        if problem:
            out["problem"] = problem
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    @app.get(API + "/{link}")
    async def event_json(request: Request, link: str) -> JSONResponse:
        s = request.app.state.services
        who = _viewer(request, f"{API}/{link}")
        ev, _moved = await run_in_threadpool(resolve, s, link)
        if ev is None:
            raise ApiError(404, "not_found", "No such event.")
        if not await run_in_threadpool(may_read, s, ev, who):
            raise ApiError(403, "members_only", "This event is for members.")
        out = await run_in_threadpool(public_json, s, ev, who)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    @app.put(API + "/{d}/slug")
    async def event_slug(request: Request, d: str) -> JSONResponse:
        """``{"slug"}`` by a host: the event's address becomes /events/<slug>."""
        s, caller, data = await _signed(request, "PUT", f"{API}/{d}/slug")
        ev = await run_in_threadpool(load, s, d)
        if ev is None:
            raise ApiError(404, "not_found", "No such event.")
        if caller not in ev.hosts:
            raise ApiError(403, "not_host", "Only the event's hosts change its URL.")
        slug = str(data.get("slug") or "").strip().lower()
        problem = slug_problem(slug)
        if problem:
            raise ApiError(422, "invalid", f"That URL can't be used: {problem}.")
        if not store(s).claim(ev.d, slug):
            raise ApiError(409, "taken", "Another event has that URL.")
        return JSONResponse({"slug": slug, "url": f"{site(s)}/events/{slug}"})

    @app.post(API + "/{d}/join")
    async def event_join(request: Request, d: str) -> JSONResponse:
        """An open ticket: ``{"ticket"?, "name"?, "email"?}``. An account's key → its ticket; a
        key nobody knows, with a name → a new guest (``bunker_url``: its session)."""
        s, caller, data = await _signed(request, "POST", f"{API}/{d}/join")
        own(s)
        _limit(request, d)
        ev = await run_in_threadpool(load, s, d)
        if ev is None:
            raise ApiError(404, "not_found", "No such event.")
        try:
            out = await run_in_threadpool(join, s, ev, caller, data)
        except ApiError:
            raise
        except Exception as exc:
            log.exception("events %s: join failed", d)
            raise ApiError(503, "failed", "That did not work just now. Try again.") from exc
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    @app.post(API + "/{d}/apply")
    async def event_apply(request: Request, d: str) -> JSONResponse:
        """An application or a waitlist place: ``{"ticket", "name"?, "email"?, "request"?}``.
        Without an account: the guest identity first (``needs_request``), then the request
        signed by it."""
        s, caller, data = await _signed(request, "POST", f"{API}/{d}/apply")
        own(s)
        _limit(request, d)
        ev = await run_in_threadpool(load, s, d)
        if ev is None:
            raise ApiError(404, "not_found", "No such event.")
        try:
            out = await run_in_threadpool(apply, s, ev, caller, data)
        except ApiError:
            raise
        except Exception as exc:
            log.exception("events %s: apply failed", d)
            raise ApiError(503, "failed", "That did not work just now. Try again.") from exc
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    @app.post(API + "/{d}/decide")
    async def event_decide(request: Request, d: str) -> JSONResponse:
        """``{"pubkey", "action": "approve" | "decline", "ticket"?}`` by a host."""
        s, caller, data = await _signed(request, "POST", f"{API}/{d}/decide")
        ev = await run_in_threadpool(load, s, d)
        if ev is None:
            raise ApiError(404, "not_found", "No such event.")
        out = await run_in_threadpool(decide, s, ev, caller, data)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})


__all__ = ["register", "watch", "load", "resolve", "tickets", "people", "Store"]
