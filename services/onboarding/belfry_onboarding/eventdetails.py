"""Event details from an event's website: ``GET /api/event-details?url=…``
(belfry.<domain>/api/v1/event-details), NIP-98. Requirement EV-61; contract for the apps:
docs/event-details.md.

The event form asks for the Event website first; the app sends it here and fills in the cover,
title, start / end (time zone, all day), place and description from the answer, and the host
checks and changes them. The Join online link is never taken from a site: a virtual location
only sets ``online``, its address is never returned (and is taken out of the description).

- Who may ask: a key that may create events (an active pass that is not a guest's: members and
  members' agents; guests, reading guests and website sessions are refused). 60 a minute per
  caller.
- The page is fetched with linkpreview.py's guarded fetcher (https on 443 only, every name
  looked up and the connection pinned to the address checked, never a private address nor the
  belfry's own, at most 3 redirects each checked again, 5 s), ``text/html`` only, the first
  1 MB (JSON-LD often sits at the end of a big page; link previews read 512 kB).
- Read, best first: schema.org JSON-LD (``@type`` Event or a subtype, also inside ``@graph``
  and arrays), then microdata (``itemtype`` …/Event), then Open Graph / twitter and
  ``event:start_time``-style meta, then the page's calendar file (``<link rel=alternate
  type=text/calendar>`` or an ``<a href=….ics>`` on the same host), fetched with the same guard
  (256 kB) when the page itself gave no start.
- The picture goes through linkpreview's picture proxy (its HMAC address), never the site's.
- Kept by address (asked and final) 6 h; nothing usable 30 min. At most 4 fetches at once.

The network is only touched through linkpreview's ``resolve`` and ``_get``: tests replace them.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import logging
import re
import threading
import time
import unicodedata
import urllib.parse
import zoneinfo
from collections import OrderedDict
from html.parser import HTMLParser
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import linkpreview as lp
from .core import Services
from .events import Limiter
from .holder_invites import ApiError, authenticate

log = logging.getLogger(__name__)

PATH = "/api/event-details"
HTML_MAX = 1024 * 1024
ICS_MAX = 256 * 1024
TITLE_MAX, DESCRIPTION_MAX, PLACE_MAX, ORGANIZER_MAX = 200, 4000, 300, 200
FOUND_SECONDS = 6 * 3600
NONE_SECONDS = 30 * 60
ENTRIES = 1000
CALLS = Limiter(60, 60)  # per caller
UPSTREAM = threading.BoundedSemaphore(4)
SOURCES = ("jsonld", "microdata", "opengraph", "ics")


class Busy(Exception):
    pass


# ── text ────────────────────────────────────────────────────────────────────────────────────
BLOCK_END = re.compile(r"(?i)<\s*(br\s*/?|/p|/div|/li|/h[1-6]|/tr|/blockquote|/section)\s*>")
LI_START = re.compile(r"(?i)<\s*li\b[^>]*>")
TAG = re.compile(r"<[^>]*>")
URL_IN_TEXT = re.compile(r"https?://\S+")


def plain(text: Any, limit: int, drop: tuple[str, ...] = ()) -> str | None:
    """Text as a person reads it with its paragraphs: entities decoded, tags out (line breaks
    where blocks ended), spaces collapsed in each line, at most two line breaks in a row, the
    addresses in DROP removed, at most LIMIT characters (cut at a word, "…")."""
    if not isinstance(text, str):
        return None
    text = html.unescape(text)
    text = LI_START.sub("\n• ", BLOCK_END.sub("\n", text))
    text = html.unescape(TAG.sub(" ", text))
    for d in drop:
        if d:
            text = text.replace(d, "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(c if c == "\n" or unicodedata.category(c)[0] != "C" else " " for c in text)
    lines = [" ".join(line.split()) for line in text.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if not text:
        return None
    if len(text) > limit:
        cut = text[: limit - 1]
        if " " in cut[limit // 2 :]:
            cut = cut.rsplit(" ", 1)[0]
        text = cut.rstrip(" .,;:-\n") + "…"
    return text


def _one_line(text: Any, limit: int) -> str | None:
    got = lp.clean(text, limit) if isinstance(text, str) else None
    if got and URL_IN_TEXT.match(got):
        return None  # a place or organiser that is only an address: never shown
    return got


# ── times ───────────────────────────────────────────────────────────────────────────────────
DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ZONE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+){0,2}$")


def zone(name: Any) -> str | None:
    """NAME when it is an IANA time zone this machine knows, else None."""
    if not isinstance(name, str) or not ZONE_NAME.fullmatch(name.strip()) or len(name) > 64:
        return None
    try:
        zoneinfo.ZoneInfo(name.strip())
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        return None
    return name.strip()


def when(raw: Any) -> tuple[str, bool] | None:
    """(ISO text, all day) of a schema.org / meta date: a date alone = all day; a date and time
    with an offset (or Z) = ISO with the offset; without an offset it is returned as given, the
    site's local time (see docs/event-details.md)."""
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if DATE_ONLY.fullmatch(raw):
        try:
            dt.date.fromisoformat(raw)
        except ValueError:
            return None
        return raw, True
    text = raw.replace(" ", "T", 1) if re.match(r"^\d{4}-\d{2}-\d{2} \d", raw) else raw
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)  # +0100 → +01:00
    try:
        t = dt.datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    return t.replace(microsecond=0).isoformat(), False


# ── JSON-LD ─────────────────────────────────────────────────────────────────────────────────
EVENT_TYPES = {
    "event", "businessevent", "childrensevent", "comedyevent", "courseinstance", "danceevent",
    "deliveryevent", "educationevent", "eventseries", "exhibitionevent", "festival",
    "foodevent", "hackathon", "literaryevent", "musicevent", "publicationevent",
    "broadcastevent", "onDemandEvent".lower(), "saleevent", "screeningevent", "socialevent",
    "sportsevent", "theaterevent", "visualartsevent",
}  # fmt: skip


def _type_names(node: dict) -> list[str]:
    t = node.get("@type")
    names = t if isinstance(t, list) else [t]
    out = []
    for n in names:
        if isinstance(n, str):
            out.append(re.split(r"[/:#]", n.strip())[-1].lower())
    return out


def is_event(node: Any) -> bool:
    return isinstance(node, dict) and any(n in EVENT_TYPES for n in _type_names(node))


def _walk(node: Any, depth: int = 0):
    """Every dict in a JSON-LD document, outer ones first (``@graph``, arrays, nesting)."""
    if depth > 8:
        return
    if isinstance(node, list):
        for item in node[:200]:
            yield from _walk(item, depth + 1)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "mainEntity", "itemListElement", "item", "subEvent"):
            if key in node:
                yield from _walk(node[key], depth + 1)


def _loads(text: str) -> Any:
    text = text.strip()
    text = re.sub(r"^\s*(<!--|//\s*<!\[CDATA\[|<!\[CDATA\[)", "", text)
    text = re.sub(r"(-->|//\s*\]\]>|\]\]>)\s*$", "", text).strip()
    try:
        return json.loads(text)
    except ValueError:
        # some sites put several objects or a trailing ";": take the first one
        try:
            return json.JSONDecoder().raw_decode(text)[0]
        except ValueError:
            return None


def _text(v: Any) -> str | None:
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return next((x for x in (_text(i) for i in v) if x), None)
    if isinstance(v, dict):
        return _text(v.get("@value") or v.get("name"))
    return None


def _image(v: Any) -> str | None:
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, list):
        return next((x for x in (_image(i) for i in v) if x), None)
    if isinstance(v, dict):
        return _image(v.get("url") or v.get("contentUrl") or v.get("@id"))
    return None


def _address(v: Any) -> str | None:
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return next((x for x in (_address(i) for i in v) if x), None)
    if not isinstance(v, dict):
        return None
    if v.get("name") and not any(
        v.get(k) for k in ("streetAddress", "addressLocality", "postalCode")
    ):
        return _text(v.get("name"))
    country = v.get("addressCountry")
    if isinstance(country, dict):
        country = country.get("name")
    city = " ".join(x for x in (_text(v.get("postalCode")), _text(v.get("addressLocality"))) if x)
    parts = [_text(v.get("streetAddress")), city, _text(v.get("addressRegion")), _text(country)]
    seen: list[str] = []
    for p in parts:
        p = (p or "").strip()
        if p and p not in seen:
            seen.append(p)
    return ", ".join(seen) or None


def _urls_of(v: Any) -> list[str]:
    """Every address in a virtual location (to keep out of the answer)."""
    out: list[str] = []
    if isinstance(v, str):
        out.append(v)
    elif isinstance(v, list):
        for i in v:
            out += _urls_of(i)
    elif isinstance(v, dict):
        for k in ("url", "@id", "sameAs"):
            out += _urls_of(v.get(k))
    return [u for u in out if isinstance(u, str) and u.startswith(("http://", "https://"))]


def _location(node: dict) -> tuple[dict | None, bool, list[str]]:
    """(place {name, address} or None, online, the online addresses to keep out)."""
    mode = _text(node.get("eventAttendanceMode")) or ""
    mode = re.split(r"[/:#]", mode)[-1].lower()
    online = mode in ("onlineeventattendancemode", "mixedeventattendancemode")
    hidden: list[str] = []
    place = None
    locs = node.get("location")
    for loc in locs if isinstance(locs, list) else [locs]:
        if isinstance(loc, str):
            if loc.strip().startswith(("http://", "https://")):
                online = True
                hidden.append(loc.strip())
            elif place is None:
                place = {"name": None, "address": loc}
            continue
        if not isinstance(loc, dict):
            continue
        if "virtuallocation" in _type_names(loc):
            online = True
            hidden += _urls_of(loc)
            continue
        if place is None:
            place = {"name": _text(loc.get("name")), "address": _address(loc.get("address"))}
    if place is not None:
        place = {
            "name": _one_line(place["name"], PLACE_MAX),
            "address": _one_line(place["address"], PLACE_MAX),
        }
        if place["name"] and place["name"] == place["address"]:
            place["name"] = None
        if not place["name"] and not place["address"]:
            place = None
    return place, online, hidden


def from_jsonld(blocks: list[str]) -> dict[str, Any] | None:
    """The first schema.org Event of the page's JSON-LD blocks (one with a start first)."""
    events: list[dict] = []
    for block in blocks[:20]:
        doc = _loads(block)
        events += [n for n in _walk(doc) if is_event(n)]
    events = [e for e in events if _text(e.get("name"))]
    if not events:
        return None
    ev = next((e for e in events if when(_text(e.get("startDate")))), events[0])
    place, online, hidden = _location(ev)
    sched = ev.get("eventSchedule")
    tz = None
    for s in sched if isinstance(sched, list) else [sched]:
        if isinstance(s, dict):
            tz = tz or zone(s.get("scheduleTimezone"))
    organizer = ev.get("organizer")
    return {
        "title": _text(ev.get("name")),
        "start": when(_text(ev.get("startDate"))),
        "end": when(_text(ev.get("endDate"))),
        "timezone": tz,
        "place": place,
        "online": online,
        "hidden": hidden,
        "image_url": _image(ev.get("image")),
        "description": _text(ev.get("description")),
        "organizer": _text(organizer),
    }


# ── the page: JSON-LD blocks, microdata, meta, calendar links ───────────────────────────────
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
        "track", "wbr"}  # fmt: skip
MICRO_PROPS = {"name", "startdate", "enddate", "description", "image", "location", "address",
               "organizer"}  # fmt: skip


class _Page(HTMLParser):
    """JSON-LD blocks, meta tags, calendar links, and the first microdata Event's properties."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.jsonld: list[str] = []
        self.meta: dict[str, str] = {}
        self.title = ""
        self.ics: list[str] = []
        self.micro: dict[str, str] = {}
        self._script: list[str] | None = None
        self._in_title = False
        self._depth = 0
        self._scopes: list[tuple[int, str]] = []  # (depth, "event" | "place" | "other")
        self._event_done = False
        self._capture: tuple[int, str, list[str]] | None = None  # (depth, prop, text)

    def _scope(self) -> str | None:
        return self._scopes[-1][1] if self._scopes else None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script":
            if a.get("type", "").strip().lower() == "application/ld+json":
                self._script = []
            return
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").strip().lower()
            if key and "content" in a and key not in self.meta:
                self.meta[key] = a["content"]
        elif tag == "title" and not self.title:
            self._in_title = True
        elif tag == "link":
            rel = a.get("rel", "").lower().split()
            if "alternate" in rel and a.get("type", "").lower().startswith("text/calendar"):
                self.ics.append(a.get("href", ""))
        elif tag == "a":
            href = a.get("href", "")
            if urllib.parse.urlsplit(href).path.lower().endswith(".ics"):
                self.ics.append(href)
        self._micro_start(tag, a)

    def _micro_start(self, tag: str, a: dict[str, str]) -> None:
        if tag not in VOID:
            self._depth += 1
        prop = a.get("itemprop", "").strip().split()
        prop = prop[0].lower() if prop else ""
        scope = self._scope()
        if scope == "event" and prop in MICRO_PROPS and prop not in self.micro:
            value = a.get("content") or a.get("datetime")
            if not value and prop == "image":
                value = a.get("src") or a.get("href")
            if value:
                self.micro[prop] = value
            elif tag not in VOID and prop not in ("location", "organizer"):
                self._capture = (self._depth, prop, [])
        elif scope in ("place", "address") and prop in (
            "name",
            "address",
            "streetaddress",
            "postalcode",
            "addresslocality",
        ):
            key = f"place.{prop}"
            value = a.get("content")
            if value and key not in self.micro:
                self.micro[key] = value
            elif not value and tag not in VOID and key not in self.micro:
                self._capture = (self._depth, key, [])
        if "itemscope" in a and tag not in VOID:
            kind = re.split(r"[/:#]", a.get("itemtype", "").strip())[-1].lower()
            if kind in EVENT_TYPES and not self._event_done and scope is None:
                self._scopes.append((self._depth, "event"))
            elif scope == "event" and prop == "location":
                if kind == "virtuallocation":
                    self.micro["online"] = "1"
                    self._scopes.append((self._depth, "other"))
                else:
                    self._scopes.append((self._depth, "place"))
            elif scope == "place" and prop == "address":
                self._scopes.append((self._depth, "address"))
            else:
                self._scopes.append((self._depth, "other"))

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            if self._script is not None and len(self.jsonld) < 20:
                self.jsonld.append("".join(self._script))
            self._script = None
            return
        if tag == "title":
            self._in_title = False
        if tag in VOID:
            return
        if self._capture and self._capture[0] == self._depth:
            _, prop, parts = self._capture
            self.micro.setdefault(prop, "".join(parts))
            self._capture = None
        if self._scopes and self._scopes[-1][0] == self._depth:
            if self._scopes.pop()[1] == "event":
                self._event_done = True
        self._depth = max(0, self._depth - 1)

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            if sum(map(len, self._script)) < HTML_MAX:
                self._script.append(data)
            return
        if self._in_title and len(self.title) < 2000:
            self.title += data
        if self._capture and sum(map(len, self._capture[2])) < 20000:
            self._capture[2].append(data)


def from_micro(m: dict[str, str]) -> dict[str, Any] | None:
    if not m.get("name"):
        return None
    street = " ".join(x for x in (m.get("place.postalcode"), m.get("place.addresslocality")) if x)
    address = m.get("place.address") or ", ".join(
        x.strip() for x in (m.get("place.streetaddress"), street) if x and x.strip()
    )
    place = None
    if m.get("place.name") or address:
        place = {
            "name": _one_line(m.get("place.name"), PLACE_MAX),
            "address": _one_line(address, PLACE_MAX),
        }
        if not place["name"] and not place["address"]:
            place = None
    return {
        "title": m.get("name"),
        "start": when(m.get("startdate")),
        "end": when(m.get("enddate")),
        "timezone": None,
        "place": place,
        "online": bool(m.get("online")),
        "hidden": [],
        "image_url": m.get("image"),
        "description": m.get("description"),
        "organizer": m.get("organizer"),
    }


START_META = ("event:start_time", "og:event:start_time", "event:start_date", "startdate",
              "og:start_time", "event:start")  # fmt: skip
END_META = ("event:end_time", "og:event:end_time", "event:end_date", "enddate",
            "og:end_time", "event:end")  # fmt: skip


def from_meta(meta: dict[str, str], title: str) -> dict[str, Any] | None:
    def first(*keys: str) -> str | None:
        return next((meta[k] for k in keys if meta.get(k, "").strip()), None)

    name = first("og:title", "twitter:title") or title
    if not (name or "").strip():
        return None
    return {
        "title": name,
        "start": when(first(*START_META)),
        "end": when(first(*END_META)),
        "timezone": None,
        "place": None,
        "online": False,
        "hidden": [],
        "image_url": first(
            "og:image", "og:image:secure_url", "og:image:url", "twitter:image", "twitter:image:src"
        ),
        "description": first("og:description", "twitter:description", "description"),
        "organizer": None,
    }


# ── calendar files ──────────────────────────────────────────────────────────────────────────
def _ics_unescape(v: str) -> str:
    return re.sub(r"\\([\\;,nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), v)


def _ics_time(value: str, params: dict[str, str]) -> tuple[str, bool, str | None] | None:
    """(ISO text, all day, IANA zone or None) of a DTSTART / DTEND."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        try:
            return dt.datetime.strptime(value[:8], "%Y%m%d").date().isoformat(), True, None
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{8}T\d{6})(Z?)", value)
    if not m:
        return None
    try:
        t = dt.datetime.strptime(m.group(1), "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    if m.group(2):
        return t.replace(tzinfo=dt.UTC).isoformat(), False, None
    tz = zone(params.get("TZID", "").strip('"'))
    if tz:
        return t.replace(tzinfo=zoneinfo.ZoneInfo(tz)).isoformat(), False, tz
    return t.isoformat(), False, None  # floating: the site's local time


def from_ics(body: bytes) -> dict[str, Any] | None:
    """The first VEVENT of a calendar file (a small reader: unfolded lines, the properties the
    form uses; a TZID that is an IANA name gives the offset, others stay local time)."""
    text = body.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    if "BEGIN:VCALENDAR" not in text[:2048].upper():
        return None
    text = re.sub(r"\n[ \t]", "", text)  # unfold
    props: dict[str, tuple[dict[str, str], str]] = {}
    cal_tz = None
    inside = False
    nested = 0
    for line in text.split("\n"):
        if ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, *rest = head.split(";")
        name = name.strip().upper()
        params = {}
        for p in rest:
            if "=" in p:
                k, v = p.split("=", 1)
                params[k.strip().upper()] = v.strip()
        if name == "X-WR-TIMEZONE":
            cal_tz = zone(value.strip())
        if name == "BEGIN" and value.strip().upper() == "VEVENT" and not props:
            inside = True
            continue
        if not inside:
            continue
        if name == "BEGIN":
            nested += 1  # a VALARM inside the event
        elif name == "END" and nested:
            nested -= 1
        elif name == "END" and value.strip().upper() == "VEVENT":
            break
        elif not nested and name not in props:
            props[name] = (params, value)
    if not props.get("SUMMARY"):
        return None
    start = _ics_time(props["DTSTART"][1], props["DTSTART"][0]) if "DTSTART" in props else None
    end = _ics_time(props["DTEND"][1], props["DTEND"][0]) if "DTEND" in props else None
    if start and not start[1] and cal_tz and "T" in start[0] and "+" not in start[0][10:]:
        start = _ics_time(props["DTSTART"][1], {"TZID": cal_tz})
        if end and not end[1]:
            end = _ics_time(props["DTEND"][1], {"TZID": cal_tz})
    if start and end and start[1] and end[1]:
        # DTEND of an all-day event is the day after; the answer gives the last day
        last = dt.date.fromisoformat(end[0]) - dt.timedelta(days=1)
        end = (max(last, dt.date.fromisoformat(start[0])).isoformat(), True, None)
    location = _ics_unescape(props.get("LOCATION", ({}, ""))[1]).strip()
    place, online, hidden = None, False, []
    if location.startswith(("http://", "https://")):
        online, hidden = True, [location]
    elif location:
        name, _, rest = location.partition(",")
        place = {
            "name": _one_line(name, PLACE_MAX) if rest.strip() else None,
            "address": _one_line(rest if rest.strip() else location, PLACE_MAX),
        }
    organizer = props.get("ORGANIZER")
    return {
        "title": _ics_unescape(props["SUMMARY"][1]),
        "start": start[:2] if start else None,
        "end": end[:2] if end else None,
        "timezone": (start[2] if start else None) or cal_tz,
        "place": place,
        "online": online,
        "hidden": hidden,
        "image_url": None,
        "description": _ics_unescape(props.get("DESCRIPTION", ({}, ""))[1]),
        "organizer": organizer[0].get("CN", "").strip('"') or None if organizer else None,
    }


# ── putting it together ─────────────────────────────────────────────────────────────────────
def merge(found: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """The sources in order, best first: each field from the first that has it. ``source`` = the
    one the start came from, else the title's."""
    out: dict[str, Any] = {"hidden": [], "online": False}
    for source, f in found:
        out["hidden"] += f.get("hidden") or []
        out["online"] = out["online"] or bool(f.get("online"))
        for key in ("title", "image_url", "description", "organizer", "place", "timezone"):
            if not out.get(key) and f.get(key):
                out[key] = f[key]
        if not out.get("start") and f.get("start"):
            out["start"], out["source"] = f["start"], source
            if f.get("end"):
                out["end"] = f["end"]
            if f.get("timezone"):
                out["timezone"] = f["timezone"]
        if not out.get("title_source") and f.get("title"):
            out["title_source"] = source
    out.setdefault("source", out.get("title_source"))
    return out


def parse_page(page: bytes, base: str, charset: str = "utf-8") -> tuple[list, list[str]]:
    """[(source, fields)] of a page, best first, and the calendar files it links to (absolute,
    https, same host)."""
    try:
        text = page.decode(charset, errors="replace")
    except LookupError:
        text = page.decode("utf-8", errors="replace")
    p = _Page()
    try:
        p.feed(text)
        p.close()
    except Exception:  # noqa: BLE001 — what was read before the parser gave up still counts
        pass
    found: list[tuple[str, dict[str, Any]]] = []
    for source, got in (
        ("jsonld", from_jsonld(p.jsonld)),
        ("microdata", from_micro(p.micro)),
        ("opengraph", from_meta(p.meta, p.title)),
    ):
        if got:
            found.append((source, got))
    host = urllib.parse.urlsplit(base).hostname
    links: list[str] = []
    for href in p.ics:
        u = urllib.parse.urljoin(base, html.unescape(href.strip()))
        if u.startswith("https://") and urllib.parse.urlsplit(u).hostname == host:
            if u not in links:
                links.append(u)
    return found, links[:2]


def _picture(svc: Services, raw: Any, base: str) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    url = urllib.parse.urljoin(base, html.unescape(raw.strip()))
    if not url.startswith("https://") or len(url) > lp.URL_MAX:
        return None
    try:
        lp.check_url(svc, url)  # never the belfry's own nor a private name
    except lp.Refused:
        return None
    digest = lp.image_hash(svc, url)
    lp._note_image(svc, digest, url)
    return f"{lp.api_base(svc)}/link-preview/image/{digest}"


def answer(svc: Services, merged: dict[str, Any], final: str) -> dict[str, Any] | None:
    title = plain(merged.get("title"), TITLE_MAX)
    title = " ".join(title.split()) if title else None
    if not title:
        return None
    hidden = tuple(sorted(set(merged.get("hidden") or []), key=len, reverse=True))
    start, end = merged.get("start"), merged.get("end")
    all_day = bool(start and start[1])
    if start and end and end[1] != start[1]:
        end = None  # one all day and the other not: the end can't be read alongside
    organizer = _one_line(plain(merged.get("organizer"), ORGANIZER_MAX), ORGANIZER_MAX)
    return {
        "url": final,
        "title": title,
        "starts_at": start[0] if start else None,
        "ends_at": end[0] if end else None,
        "all_day": all_day,
        "timezone": merged.get("timezone"),
        "place": merged.get("place"),
        "online": bool(merged.get("online")),
        "image": _picture(svc, merged.get("image_url"), final),
        "description": plain(merged.get("description"), DESCRIPTION_MAX, hidden),
        "organizer": organizer,
        "source": merged.get("source") or "opengraph",
    }


# ── the cache ───────────────────────────────────────────────────────────────────────────────
_lock = threading.Lock()
_kept: OrderedDict[str, tuple[float, dict[str, Any] | None]] = OrderedDict()


def forget() -> None:
    with _lock:
        _kept.clear()


def _key(url: str) -> str:
    return "ev:" + hashlib.sha256(url.encode()).hexdigest()


def _held(key: str) -> tuple[bool, dict[str, Any] | None]:
    with _lock:
        got = _kept.get(key)
        if got is None:
            return False, None
        at, value = got
        if time.monotonic() - at < (FOUND_SECONDS if value else NONE_SECONDS):
            _kept.move_to_end(key)
            return True, value
        _kept.pop(key, None)
        return False, None


def _keep(key: str, value: dict[str, Any] | None) -> None:
    with _lock:
        _kept[key] = (time.monotonic(), value)
        _kept.move_to_end(key)
        while len(_kept) > ENTRIES:
            _kept.popitem(last=False)


def details(svc: Services, raw: Any) -> dict[str, Any] | None:
    """The details of the event page RAW (checked first: lp.Refused), or None when it has
    nothing usable. Raises Busy when every slot stays taken."""
    url, _host = lp.check_url(svc, raw)
    key = _key(url)
    hit, value = _held(key)
    if hit:
        return value
    f = lp._flight(key)
    try:
        with f[0]:
            hit, value = _held(key)
            if hit:
                return value
            final, value = _fetch(svc, url)
            _keep(key, value)
            if final and final != url:
                _keep(_key(final), value)
            return value
    finally:
        lp._land(key, f)


def _fetch(svc: Services, url: str) -> tuple[str | None, dict[str, Any] | None]:
    if not UPSTREAM.acquire(timeout=lp.TIMEOUT):
        raise Busy
    try:
        try:
            final, ctype, body, _cut = lp.fetch(
                svc, url, "text/html,application/xhtml+xml;q=0.9", HTML_MAX
            )
        except lp.Refused as exc:
            log.info("event details: nothing for a page: %s", exc)
            return None, None
        except Exception as exc:  # noqa: BLE001 — a timeout, a bad certificate: nothing
            log.info("event details: a page did not answer: %s", type(exc).__name__)
            return None, None
        if ctype not in lp.HTML_TYPES:
            return final, None
        held, value = _held(_key(final))  # another address that led to the same page
        if held:
            return final, value
        found, links = parse_page(body, final, lp._charset(ctype, body))
        if not any(f.get("start") for _, f in found):
            for link in links:
                got = _calendar(svc, link)
                if got:
                    found.append(("ics", got))
                    break
        return final, answer(svc, merge(found), final)
    finally:
        UPSTREAM.release()


def _calendar(svc: Services, url: str) -> dict[str, Any] | None:
    try:
        _final, _ctype, body, cut = lp.fetch(
            svc, url, "text/calendar,application/ics;q=0.9,*/*;q=0.1", ICS_MAX
        )
    except Exception as exc:  # noqa: BLE001 — Refused, a timeout: no calendar
        log.info("event details: no calendar file: %s", type(exc).__name__)
        return None
    if cut:
        body = body[:ICS_MAX]  # the first event is near the top: still read it
    return from_ics(body)


# ── who asks ────────────────────────────────────────────────────────────────────────────────
def may_create_events(svc: Services, key: str) -> bool:
    """An active pass that is not a guest's (events.is_member: members and their agents, the
    keys the relays let create a group); guests, reading guests and website sessions: no."""
    from .events import is_member

    return is_member(svc, key)


def register(app: FastAPI) -> None:
    @app.get(PATH)
    async def event_details(request: Request) -> JSONResponse:
        s: Services = request.app.state.services
        auth = request.headers.get("authorization")
        if not auth:
            raise ApiError(401, "unauthorized", "Sign the request (NIP-98).")
        path = request.url.path + ("?" + request.url.query if request.url.query else "")
        key = authenticate(s, request.app.state.api_seen, auth, "GET", path, b"")
        if not await run_in_threadpool(may_create_events, s, key):
            raise ApiError(403, "not_member", "Only members can fetch event details.")
        if not CALLS.allow("k:" + key):
            raise ApiError(429, "too_many", "Too many for now. Try again in a minute.")
        raw = request.query_params.get("url")
        try:
            got = await run_in_threadpool(details, s, raw)
        except lp.Refused as exc:
            raise ApiError(422, "invalid", "url: an https address on the public internet.") from exc
        except Busy as exc:
            raise ApiError(503, "busy", "Fetching is busy just now. Try again.") from exc
        if got is None:
            return JSONResponse(
                {"error": "none", "message": "No event details on this page."},
                status_code=404,
                headers={"Cache-Control": "private, max-age=600"},
            )
        return JSONResponse(got, headers={"Cache-Control": "private, max-age=600"})


__all__ = ["register", "details", "parse_page", "from_ics", "forget"]
