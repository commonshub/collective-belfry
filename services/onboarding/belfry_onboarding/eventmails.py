"""The e-mails people going to an event get (docs/checkin.md "What people going are told"):

- the RSVP mail ("You're going to <title>"): when, where, Join online (the link comes later),
  the Event website, the event's page and its calendar file;
- the change mail: when a host saves a new version with ``["notify", "1"]``, one mail per person
  going listing only what changed (time, place, Join online, title, Event website), computed
  from the versions themselves (the app's ``["changed", field]`` tags are not trusted);
- the Join online mail, one hour before an event with a call link: the link itself, to people
  going only.

Each is sent once (records in events.sqlite, like the reminders). Every text that comes from a
person is escaped by the mailer."""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any

from . import events as ev_mod
from .core import Services
from .events import (
    KINDS,
    Event,
    Store,
    _when,
    _zone,
    tag,
    website_of,
    website_text,
    when_text,
)

log = logging.getLogger(__name__)

CHANGE_QUIET = 3600  # a version older than this when first seen: nobody is told of it
LINK_BEFORE = 3600  # the Join online link goes out this long before the start


# ── what a version says ─────────────────────────────────────────────────────────────────────
def facts(v: dict[str, Any]) -> dict[str, Any]:
    """The parts of a version a change mail compares (the same reading as ``events.load``)."""
    all_day = v.get("kind") == 31922
    return {
        "all_day": all_day,
        "start": _when(tag(v, "start"), all_day),
        "end": _when(tag(v, "end"), all_day),
        "tz": tag(v, "start_tzid") or "",
        "location": (tag(v, "location") or "")[:300],
        "links": [
            str(t[1])
            for t in v.get("tags", [])
            if t[:1] == ["r"] and len(t) > 1 and str(t[1]).startswith("https://")
        ],
        "title": (tag(v, "title") or tag(v, "name") or "Untitled")[:200],
        "website": website_of(v),
    }


def _day(x: dt.datetime | dt.date, tz: Any) -> dt.date:
    return x.astimezone(tz).date() if isinstance(x, dt.datetime) else x


def _moment(x: dt.datetime | dt.date | None, tz: Any, with_day: bool) -> str:
    if x is None:
        return "not set"
    if isinstance(x, dt.datetime):
        local = x.astimezone(tz)
        return local.strftime("%A %-d %B %Y, %H:%M" if with_day else "%H:%M")
    return x.strftime("%A %-d %B %Y")


def _time_change(old: Any, new: Any, tz: Any) -> str:
    same_day = (
        isinstance(old, dt.datetime)
        and isinstance(new, dt.datetime)
        and _day(old, tz) == _day(new, tz)
    )
    return f"{_moment(new, tz, not same_day)} (was {_moment(old, tz, not same_day)})"


def changed(old: dict[str, Any], new: dict[str, Any]) -> list[tuple[str, str, Any]]:
    """What NEW changes from OLD, in the board's order and words: (field, label, value); a value
    is a text, or mailer parts (a link). Never a description, a cover, tickets or co-hosts."""
    tz = _zone(new["tz"] or old["tz"])
    out: list[tuple[str, str, Any]] = []
    if old["start"] != new["start"]:
        out.append(("start", "Starts", _time_change(old["start"], new["start"], tz)))
    if old["end"] != new["end"]:
        out.append(("end", "Ends", _time_change(old["end"], new["end"], tz)))
    if old["location"] != new["location"]:
        if new["location"] and old["location"]:
            out.append(("location", "Where", f"{new['location']} (was {old['location']})"))
        elif new["location"]:
            out.append(("location", "Where", new["location"]))
        else:
            out.append(("location", "Where", "no place any more (was " + old["location"] + ")"))
    if old["links"] != new["links"]:
        if new["links"]:
            link = new["links"][0]
            out.append(("online", "Join online", ["A new link: ", (website_text(link), link)]))
        else:
            out.append(("online", "Join online", "No online link any more"))
    if old["title"] != new["title"]:
        out.append(("title", "Title", f"{new['title']} (was {old['title']})"))
    if old["website"] != new["website"]:
        if new["website"]:
            out.append(
                ("website", "Event website", [(website_text(new["website"]), new["website"])])
            )
        else:
            out.append(("website", "Event website", "none any more"))
    return out


def subject_of(title: str, changes: list[tuple[str, str, Any]], new: dict[str, Any]) -> str:
    start = next((c for c in changes if c[0] == "start"), None)
    if start and isinstance(new["start"], dt.datetime):
        tz = _zone(new["tz"])
        return f"{title} now starts at {new['start'].astimezone(tz).strftime('%H:%M')}"
    if start and isinstance(new["start"], dt.date):
        return f"{title} is now on {new['start'].strftime('%A %-d %B %Y')}"
    return f"{title} has changed"


# ── who, and the parts every mail shares ────────────────────────────────────────────────────
def page_button(svc: Services, ev: Event) -> tuple[str, str]:
    """The event's page on the community's website when it has one (public or by link), else
    the event in the chat (a members-only page shows nothing to someone not signed in)."""
    if ev.visibility in ("public", "link"):
        return ev_mod.url_of(svc, ev), "Open the event page"
    return ev_mod.chat_url(svc, ev), "Open the event"


def calendar_file(svc: Services, ev: Event) -> list[tuple[str, bytes, str]]:
    return [("event.ics", ev_mod.ics(svc, ev).encode(), "text/calendar")]


def _hello(name: str) -> list[Any]:
    return [f"Hi {name},"] if name else []


def going_mail(svc: Services, ev: Event, email: str, mailer: Any, name: str = "") -> bool:
    """The RSVP mail (board evmail-rsvp): the three links, each where it belongs."""
    lines: list[Any] = [*_hello(name), f"You're going to {ev.title}."]
    when = when_text(ev)
    if when:
        lines.append(f"When: {when}")
    if ev.location:
        lines.append(f"Where: {ev.location}")
    if ev.links:
        if isinstance(ev.start, dt.datetime):
            lines.append("Join online: we'll e-mail you the link 1 hour before.")
        else:
            lines.append("Join online: the link is in the event.")
    if ev.website:
        lines.append(["Event website: ", (website_text(ev.website), ev.website)])
    url, label = page_button(svc, ev)
    after = [f"{url} · Add to calendar (.ics attached)"]
    return mailer.send(
        email,
        f"You're going to {ev.title}",
        lines,
        button=(url, label),
        after=after,
        attachments=calendar_file(svc, ev),
    )


def _going(svc: Services, ev: Event, skip: set[str]) -> list[tuple[str, str]]:
    """(pubkey, address) of everyone going who can be mailed, less SKIP."""
    out = []
    for pk in ev_mod.people(svc, ev).going_keys():
        if pk in skip:
            continue
        email = ev_mod._email_of(svc, pk, ev.group)
        if email:
            out.append((pk, email))
    return out


# ── the change mail ─────────────────────────────────────────────────────────────────────────
def _host_versions(svc: Services, ev: Event) -> list[dict[str, Any]]:
    found = ev_mod.community(svc, [{"kinds": list(KINDS), "#d": [ev.d]}])
    mine = [
        v
        for v in found
        if v.get("kind") in KINDS and tag(v, "d") == ev.d and v.get("pubkey") in ev.hosts
    ]
    return sorted(mine, key=lambda v: (v["created_at"], v.get("id", "")))


def _notify(v: dict[str, Any]) -> bool:
    return any(t[:2] == ["notify", "1"] for t in v.get("tags", []))


def changes(svc: Services, mailer: Any, st: Store, recent: list[Event]) -> int:
    """A new version of an upcoming event that asks for it (``["notify", "1"]``): everyone going
    gets one mail with what changed since the version the watcher last saw as the event (or,
    the first time, the version before it). Once per (version, person); nothing for a version
    that was more than an hour old when first seen (no flood after a restart)."""
    from .community import UNKNOWN

    now = int(time.time())
    sent = 0
    for ev in recent:
        cur = ev.version
        key = f"change.base.{ev.d}"
        base_id = st.param(key)
        if base_id == cur.get("id"):
            continue
        if ev.status == "cancelled" or ev.ended():
            st.set_param(key, str(cur.get("id", "")))
            continue
        if not _notify(cur) or int(cur.get("created_at", 0)) < now - CHANGE_QUIET:
            st.set_param(key, str(cur.get("id", "")))
            continue
        versions = _host_versions(svc, ev)
        prev = next((v for v in versions if v.get("id") == base_id), None)
        if prev is None:
            older = [
                v
                for v in versions
                if (v["created_at"], v.get("id", "")) < (cur["created_at"], cur.get("id", ""))
            ]
            prev = older[-1] if older else None
        if prev is None:  # the first version: nothing changed, invitations tell people
            st.set_param(key, str(cur.get("id", "")))
            continue
        new = facts(cur)
        diff = changed(facts(prev), new)
        if diff:
            by = str(cur.get("pubkey") or ev.creator)
            who = _going(svc, ev, {by})
            found = ev_mod.names(svc, [by, *(pk for pk, _ in who)])
            host = found.get(by) or UNKNOWN
            url, label = page_button(svc, ev)
            subject = subject_of(ev.title, diff, new)
            for pk, email in who:
                if not st.once(f"change:{cur.get('id')}:{pk}"):
                    continue
                lines: list[Any] = [*_hello(ev_mod._known(found, pk)), f"{ev.title} changed:"]
                for _, label_, value in diff:
                    lines.append(
                        [f"{label_}: ", *value] if isinstance(value, list) else f"{label_}: {value}"
                    )
                mailer.send(
                    email,
                    subject,
                    lines,
                    button=(url, label),
                    after=[f"Sent to everyone going, by {host}. The calendar file is updated."],
                    attachments=calendar_file(svc, ev),
                )
                sent += 1
        st.set_param(key, str(cur.get("id", "")))
    return sent


# ── the Join online link, an hour before ────────────────────────────────────────────────────
def join_links(svc: Services, mailer: Any, st: Store, recent: list[Event]) -> int:
    """One hour before an event with a Join online link (a timed one), everyone going gets the
    link by mail; once per (event, person). Never to anyone not going."""
    now = dt.datetime.now(dt.UTC)
    sent = 0
    for ev in recent:
        if ev.status == "cancelled" or not ev.links or not isinstance(ev.start, dt.datetime):
            continue
        if not (now < ev.start <= now + dt.timedelta(seconds=LINK_BEFORE)):
            continue
        link = ev.links[0]
        who = _going(svc, ev, set())
        if not who:
            continue
        found = ev_mod.names(svc, [pk for pk, _ in who])
        local = ev.start.astimezone(_zone(ev.tz)).strftime("%H:%M")
        zone = f" ({ev.tz})" if ev.tz else " (UTC)"
        url, label = page_button(svc, ev)
        for pk, email in who:
            if not st.once(f"joinlink:{ev.d}:{pk}"):
                continue
            mailer.send(
                email,
                f"In 1 hour: {ev.title}",
                [
                    *_hello(ev_mod._known(found, pk)),
                    f"{ev.title} starts at {local}{zone}.",
                    ["Join online: ", (website_text(link), link)],
                ],
                button=(link, "Join online"),
                after=[[(label, url)]],
            )
            sent += 1
    return sent
