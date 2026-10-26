"""The village programme and the room bookings as public calendar events on the community relay.

Two sources, both public:

- the programme grid of opensourcevillage.org (``PROGRAMME_SCHEDULE_URL``, the site's
  ``/schedule.json``): one event per day and slot (morning, afternoon, evening), with the slot
  times from ``PROGRAMME_SLOTS``;
- the rooms of Commons Hub Brussels (``PROGRAMME_ROOMS_URL``, the ``rooms.json`` of
  commonshub.brussels) and each room's public Google calendar (its iCal feed): one event per
  booking, recurring bookings expanded, with the booking's title.

Each becomes a NIP-52 time-based calendar event (kind 31923) signed with Belfry's programme key
(secret ``programme_nsec``, hex), whose pubkey the relay whitelist carries. The ``d`` tag is
stable per slot or booking, so a change replaces the event; an event whose slot or booking is
gone is deleted (kind 5). The sync is one way: Google and the website stay the source, and a
run publishes only what changed. ``python -m belfry_onboarding sync-programme`` (cron).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events
from coincurve import PrivateKey

from .nip46check import sign_event
from .relay import RelayError, query_all

log = logging.getLogger(__name__)

CALENDAR_EVENT_KIND = 31923
DELETION_KIND = 5
D_PREFIX = "osv-"  # every d tag this job owns; others' events of the same key are left alone
SLOTS = ("morning", "afternoon", "evening")
GOOGLE_ICAL = "https://calendar.google.com/calendar/ical/{}/public/basic.ics"
# The community relay takes 60 events a minute per address: a first run (a hundred or more
# events) waits out the limit and goes on; later runs publish only what changed.
RATE_LIMIT_WAIT = 61.0
RATE_LIMIT_RETRIES = 10


class ProgrammeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Entry:
    """One calendar event as the sync wants it on the relay."""

    d: str
    title: str
    start: dt.datetime
    end: dt.datetime
    location: str = ""
    description: str = ""
    hashtags: tuple[str, ...] = ()
    links: tuple[str, ...] = ()

    def tags(self, tzid: str) -> list[list[str]]:
        tags = [
            ["d", self.d],
            ["title", self.title],
            ["start", str(int(self.start.timestamp()))],
            ["end", str(int(self.end.timestamp()))],
            ["start_tzid", tzid],
            ["end_tzid", tzid],
        ]
        if self.location:
            tags.append(["location", self.location])
        tags += [["t", tag] for tag in self.hashtags]
        tags += [["r", link] for link in self.links]
        return tags


@dataclass
class Plan:
    publish: list[Entry] = field(default_factory=list)
    unchanged: int = 0
    delete: list[str] = field(default_factory=list)  # d tags


# -- configuration --------------------------------------------------------------------------------


def programme_key(settings: Any) -> PrivateKey | None:
    raw = (settings.programme_nsec or "").strip()
    if not raw:
        return None
    try:
        return PrivateKey(bytes.fromhex(raw))
    except ValueError as exc:
        raise ProgrammeError("programme_nsec is not a 64-character hex key") from exc


def programme_pubkey(settings: Any) -> str:
    """The programme key's pubkey (hex), or "" without a key; the relay whitelist adds it."""
    try:
        key = programme_key(settings)
    except ProgrammeError:
        log.warning("programme_nsec is not a hex key; the programme key is not whitelisted")
        return ""
    return key.public_key_xonly.format().hex() if key else ""


def parse_slots(spec: str) -> dict[str, tuple[dt.time, dt.time]]:
    """``morning=09:30-12:30,afternoon=14:00-18:00,...`` → {slot: (start, end)}."""
    slots: dict[str, tuple[dt.time, dt.time]] = {}
    for part in spec.split(","):
        if not part.strip():
            continue
        name, _, times = part.partition("=")
        start, _, end = times.partition("-")
        try:
            slots[name.strip()] = (
                dt.time.fromisoformat(start.strip()),
                dt.time.fromisoformat(end.strip()),
            )
        except ValueError as exc:
            raise ProgrammeError(f"PROGRAMME_SLOTS: cannot read {part!r}") from exc
    return slots


# -- sources --------------------------------------------------------------------------------------


def schedule_entries(
    doc: dict[str, Any], slots: dict[str, tuple[dt.time, dt.time]], tz: ZoneInfo, site_url: str
) -> list[Entry]:
    """The website's programme grid: one entry per day and non-empty slot."""
    venue = doc.get("venue") or {}
    location = ", ".join(v for v in (venue.get("name"), venue.get("address")) if v)
    entries = []
    for day in doc.get("days") or []:
        if not day.get("date"):
            continue
        date = dt.date.fromisoformat(day["date"])
        label = day.get("label") or date.isoformat()
        for slot in SLOTS:
            text = (day.get(slot) or "").strip()
            if not text or slot not in slots:
                continue
            start, end = slots[slot]
            entries.append(
                Entry(
                    d=f"{D_PREFIX}programme-{date.isoformat()}-{slot}",
                    title=text,
                    start=dt.datetime.combine(date, start, tz),
                    end=dt.datetime.combine(date, end, tz),
                    location=location,
                    description=f"Open Source Village programme, {label} {slot}.",
                    hashtags=("osv", "programme"),
                    links=(site_url,) if site_url else (),
                )
            )
    return entries


def _as_datetime(value: dt.date | dt.datetime, tz: ZoneInfo) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz)
    return dt.datetime.combine(value, dt.time(0, 0), tz)  # all-day bookings


def room_entries(
    room: dict[str, Any], ics: bytes, start: dt.date, end: dt.date, tz: ZoneInfo, venue: str
) -> list[Entry]:
    """A room's bookings between ``start`` and ``end`` (inclusive), recurring ones expanded. Only
    the title: a booking's description is the Commons Hub's booking note (who booked it, their
    Discord handle, the price in tokens, how to cancel, sometimes an invoice address), not what
    the session is about."""
    calendar = icalendar.Calendar.from_ical(ics)
    window_start = dt.datetime.combine(start, dt.time(0, 0), tz)
    window_end = dt.datetime.combine(end + dt.timedelta(days=1), dt.time(0, 0), tz)
    entries = []
    for event in recurring_ical_events.of(calendar).between(window_start, window_end):
        if str(event.get("STATUS", "")).upper() == "CANCELLED":
            continue
        begin = _as_datetime(event.decoded("DTSTART"), tz)
        finish = _as_datetime(event.decoded("DTEND"), tz) if "DTEND" in event else begin
        uid = str(event.get("UID", ""))
        # one d per booking and occurrence (a recurring booking repeats its UID)
        key = hashlib.sha256(f"{uid}|{int(begin.timestamp())}".encode()).hexdigest()[:20]
        title = str(event.get("SUMMARY", "")).strip() or "Booked"
        entries.append(
            Entry(
                d=f"{D_PREFIX}room-{room['slug']}-{key}",
                title=title,
                start=begin,
                end=finish,
                location=", ".join(v for v in (room["name"], venue) if v),
                hashtags=("osv", "room", f"room-{room['slug']}"),
            )
        )
    return entries


def collect(settings: Any, client: httpx.Client) -> tuple[list[Entry], list[str]]:
    """Every entry the relay should carry, and the sources that could not be read (their
    events are then kept as they are rather than deleted)."""
    tz = ZoneInfo(settings.programme_timezone)
    entries: list[Entry] = []
    failed: list[str] = []
    window = (settings.event_start, settings.event_end)
    venue = settings.programme_venue

    if settings.programme_schedule_url:
        try:
            response = client.get(settings.programme_schedule_url)
            response.raise_for_status()
            doc = response.json()
            dates = doc.get("dates") or {}
            if dates.get("start") and dates.get("end"):
                window = (
                    dt.date.fromisoformat(dates["start"]),
                    dt.date.fromisoformat(dates["end"]),
                )
            site = settings.programme_site_url
            entries += schedule_entries(doc, parse_slots(settings.programme_slots), tz, site)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("programme: schedule %s not read: %s", settings.programme_schedule_url, exc)
            # without the website's dates the rooms' window is EVENT_START..EVENT_END, which may
            # be shorter: delete no booking on such a run
            failed += ["programme", "room"]

    if settings.programme_rooms_url:
        try:
            response = client.get(settings.programme_rooms_url)
            response.raise_for_status()
            doc = response.json()
            rooms = doc["rooms"] if isinstance(doc, dict) else doc
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.warning("programme: rooms %s not read: %s", settings.programme_rooms_url, exc)
            rooms = []
            if "room" not in failed:
                failed.append("room")
        for room in rooms:
            calendar_id = room.get("googleCalendarId") or room.get("calendarId")
            if not calendar_id or not room.get("slug"):
                continue
            try:
                response = client.get(GOOGLE_ICAL.format(urllib.parse.quote(calendar_id, safe="")))
                response.raise_for_status()
                entries += room_entries(room, response.content, *window, tz, venue)
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("programme: calendar of %s not read: %s", room["slug"], exc)
                failed.append(f"room-{room['slug']}")
    return entries, failed


# -- the relay ------------------------------------------------------------------------------------


def _d(event: dict[str, Any]) -> str:
    return next((t[1] for t in event.get("tags", []) if len(t) > 1 and t[0] == "d"), "")


def plan(wanted: list[Entry], existing: list[dict[str, Any]], tzid: str, failed: list[str]) -> Plan:
    """What to publish (new or changed) and what to delete (gone from a source that was read)."""
    # NIP-09: a deletion covers the versions of an addressable event up to its own created_at.
    # Relays need not drop deleted events, so they are filtered here as clients would.
    deleted: dict[str, int] = {}
    for event in existing:
        if event["kind"] == DELETION_KIND:
            for tag in event.get("tags", []):
                if len(tag) > 1 and tag[0] == "a" and tag[1].count(":") == 2:
                    d = tag[1].split(":", 2)[2]
                    deleted[d] = max(deleted.get(d, 0), event["created_at"])
    current: dict[str, dict[str, Any]] = {}
    for event in existing:
        d = _d(event)
        if event["kind"] != CALENDAR_EVENT_KIND or not d.startswith(D_PREFIX):
            continue
        if event["created_at"] <= deleted.get(d, -1):
            continue
        if event["created_at"] >= current.get(d, {}).get("created_at", 0):
            current[d] = event
    result = Plan()
    wanted_ds = set()
    for entry in wanted:
        wanted_ds.add(entry.d)
        old = current.get(entry.d)
        if old and old["tags"] == entry.tags(tzid) and old["content"] == entry.description:
            result.unchanged += 1
        else:
            result.publish.append(entry)
    for d in current:
        if d in wanted_ds or any(d.startswith(f"{D_PREFIX}{source}") for source in failed):
            continue
        result.delete.append(d)
    return result


def sync(services: Any, client: httpx.Client | None = None) -> dict[str, Any]:
    settings = services.settings
    key = programme_key(settings)
    if key is None or services.relay is None:
        return {"result": "skipped", "reason": "programme_nsec or RELAY_URL not set"}
    secret = key.secret
    pubkey = key.public_key_xonly.format().hex()

    def sign(event: dict[str, Any]) -> dict[str, Any]:
        return sign_event(secret, event)

    def publish(event: dict[str, Any]) -> None:
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                services.relay.publish(event, sign)
                return
            except RelayError as exc:
                if "rate-limited" not in str(exc) or attempt == RATE_LIMIT_RETRIES:
                    raise
                log.info("programme: relay rate limit, waiting %.0f s", RATE_LIMIT_WAIT)
                time.sleep(RATE_LIMIT_WAIT)

    own_client = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=True)
    try:
        wanted, failed = collect(settings, client)
    finally:
        if own_client:
            client.close()
    # page by page: the relay answers at most PAGE_LIMIT events per request
    existing = query_all(
        services.relay, {"kinds": [CALENDAR_EVENT_KIND], "authors": [pubkey]}, sign
    ) + query_all(
        services.relay,
        {"kinds": [DELETION_KIND], "authors": [pubkey], "#k": [str(CALENDAR_EVENT_KIND)]},
        sign,
    )
    tzid = settings.programme_timezone
    todo = plan(wanted, existing, tzid, failed)
    now = int(time.time())
    for i, entry in enumerate(todo.publish):
        publish(
            sign(
                {
                    "kind": CALENDAR_EVENT_KIND,
                    # one second apart, so the next run can page through them by `until`
                    "created_at": now - len(todo.publish) + 1 + i,
                    "tags": entry.tags(tzid),
                    "content": entry.description,
                }
            )
        )
    for chunk in range(0, len(todo.delete), 100):
        ds = todo.delete[chunk : chunk + 100]
        publish(
            sign(
                {
                    "kind": DELETION_KIND,
                    "created_at": now,
                    "tags": [["a", f"{CALENDAR_EVENT_KIND}:{pubkey}:{d}"] for d in ds]
                    + [["k", str(CALENDAR_EVENT_KIND)]],
                    "content": "no longer in the programme or the room calendar",
                }
            )
        )
    return {
        "result": "ok",
        "pubkey": pubkey,
        "published": len(todo.publish),
        "unchanged": todo.unchanged,
        "deleted": len(todo.delete),
        "failed_sources": failed,
    }
