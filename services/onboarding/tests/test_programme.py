"""sync-programme: the website's programme grid and the rooms' Google calendars as kind-31923
calendar events on the community relay (programme.py)."""

import datetime as dt
from zoneinfo import ZoneInfo

import httpx
import pytest
from coincurve import PrivateKey

from belfry_onboarding import programme

KEY = "11" * 32
SCHEDULE_URL = "https://osv.test/schedule.json"
ROOMS_URL = "https://chb.test/rooms.json"
TZ = ZoneInfo("Europe/Brussels")

SCHEDULE = {
    "dates": {"start": "2027-01-25", "end": "2027-02-05"},
    "venue": {"name": "Commons Hub Brussels", "address": "Rue de la Madeleine 51, 1000 Brussels"},
    "days": [
        {
            "date": "2027-01-25",
            "label": "Mon 25/01",
            "morning": "Funding open source",
            "afternoon": "",
            "evening": "Nostr meetup",
        },
        {"date": None, "label": "Later", "morning": "skipped", "afternoon": "", "evening": ""},
    ],
}

ROOMS = {
    "rooms": [
        {
            "name": "Mush Room",
            "slug": "mushroom",
            "googleCalendarId": "c_mush@group.calendar.google.com",
        },
        {"name": "Playroom", "slug": "playroom", "googleCalendarId": None},
    ]
}

ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:test
BEGIN:VEVENT
UID:once@google.com
DTSTART:20270126T090000Z
DTEND:20270126T100000Z
SUMMARY:Python workshop
DESCRIPTION:Bring a laptop
END:VEVENT
BEGIN:VEVENT
UID:weekly@google.com
DTSTART;TZID=Europe/Brussels:20270104T180000
DTEND;TZID=Europe/Brussels:20270104T190000
RRULE:FREQ=WEEKLY;COUNT=10
SUMMARY:Weekly standup
END:VEVENT
BEGIN:VEVENT
UID:cancelled@google.com
DTSTART:20270127T090000Z
DTEND:20270127T100000Z
STATUS:CANCELLED
SUMMARY:Called off
END:VEVENT
BEGIN:VEVENT
UID:outside@google.com
DTSTART:20270301T090000Z
DTEND:20270301T100000Z
SUMMARY:After the village
END:VEVENT
BEGIN:VEVENT
UID:allday@google.com
DTSTART;VALUE=DATE:20270128
DTEND;VALUE=DATE:20270129
END:VEVENT
END:VCALENDAR
"""


def http(schedule=SCHEDULE, rooms=ROOMS, ics=ICS, down=()):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if any(part in url for part in down):
            return httpx.Response(503)
        if url == SCHEDULE_URL:
            return httpx.Response(200, json=schedule)
        if url == ROOMS_URL:
            return httpx.Response(200, json=rooms)
        if url.startswith("https://calendar.google.com/calendar/ical/c_mush%40group"):
            return httpx.Response(200, content=ics)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def svc(services):
    services.settings.programme_nsec = KEY
    services.settings.programme_schedule_url = SCHEDULE_URL
    services.settings.programme_rooms_url = ROOMS_URL
    return services


def tag(event, name):
    return next((t[1] for t in event["tags"] if t[0] == name), None)


def calendar_events(relay):
    return {tag(e, "d"): e for e in relay.events if e["kind"] == 31923}


def test_slots_and_bookings_become_calendar_events(svc, relay):
    result = programme.sync(svc, http())
    pubkey = PrivateKey(bytes.fromhex(KEY)).public_key_xonly.format().hex()
    assert result["pubkey"] == pubkey and result["failed_sources"] == []
    events = calendar_events(relay)
    assert all(e["pubkey"] == pubkey for e in relay.events)

    morning = events["osv-programme-2027-01-25-morning"]
    assert tag(morning, "title") == "Funding open source"
    start = dt.datetime.fromtimestamp(int(tag(morning, "start")), TZ)
    assert (start.hour, start.minute) == (9, 30) and tag(morning, "start_tzid") == "Europe/Brussels"
    assert "Commons Hub Brussels" in tag(morning, "location")
    assert "osv-programme-2027-01-25-evening" in events
    assert "osv-programme-2027-01-25-afternoon" not in events  # empty slot
    assert not any("skipped" == tag(e, "title") for e in events.values())  # no date

    rooms = [e for d, e in events.items() if d.startswith("osv-room-mushroom-")]
    titles = sorted(tag(e, "title") for e in rooms)
    # the weekly booking only on the village's Mondays (Jan 25 and Feb 1), the cancelled and the
    # March one not at all, the untitled all-day booking as "Booked"
    assert titles == ["Booked", "Python workshop", "Weekly standup", "Weekly standup"]
    workshop = next(e for e in rooms if tag(e, "title") == "Python workshop")
    assert workshop["content"] == ""  # the booking note (who, price, how to cancel) stays out
    assert tag(workshop, "location").startswith("Mush Room, ")
    assert ["t", "room-mushroom"] in workshop["tags"]
    assert result["published"] == len(events) == 6


def test_a_second_run_publishes_nothing(svc, relay):
    programme.sync(svc, http())
    before = len(relay.events)
    result = programme.sync(svc, http())
    assert result["published"] == 0 and result["deleted"] == 0 and result["unchanged"] == 6
    assert len(relay.events) == before


def test_changes_replace_and_removals_delete(svc, relay):
    programme.sync(svc, http())
    changed = {**SCHEDULE, "days": [{**SCHEDULE["days"][0], "evening": "Cashu meetup"}]}
    result = programme.sync(svc, http(schedule=changed, ics=ICS.replace(b"Python", b"Rust")))
    assert result["published"] == 2 and result["deleted"] == 0
    deletion_free = programme.sync(svc, http(schedule={**SCHEDULE, "days": []}))
    assert deletion_free["deleted"] == 2
    deletion = [e for e in relay.events if e["kind"] == 5][-1]
    assert ["k", "31923"] in deletion["tags"]
    assert any(
        t[0] == "a" and t[1].endswith(":osv-programme-2027-01-25-morning") for t in deletion["tags"]
    )
    # deleted events are not deleted again, even though the relay still returns them
    assert programme.sync(svc, http(schedule={**SCHEDULE, "days": []}))["deleted"] == 0


def test_an_unreadable_source_keeps_its_events(svc, relay):
    programme.sync(svc, http())
    result = programme.sync(svc, http(down=("calendar.google.com",)))
    assert result["failed_sources"] == ["room-mushroom"] and result["deleted"] == 0
    result = programme.sync(svc, http(down=(SCHEDULE_URL,)))
    assert result["failed_sources"] == ["programme", "room"] and result["deleted"] == 0


def test_without_a_key_nothing_happens(services, relay):
    assert programme.sync(services, http())["result"] == "skipped"
    assert relay.events == []


def test_the_programme_key_is_whitelisted(settings):
    settings.programme_nsec = KEY
    assert (
        programme.programme_pubkey(settings)
        == PrivateKey(bytes.fromhex(KEY)).public_key_xonly.format().hex()
    )
    settings.programme_nsec = "not hex"
    assert programme.programme_pubkey(settings) == ""


def test_slots_setting():
    slots = programme.parse_slots("morning=09:00-12:00, evening=19:00-22:30")
    assert slots == {
        "morning": (dt.time(9), dt.time(12)),
        "evening": (dt.time(19), dt.time(22, 30)),
    }
    with pytest.raises(programme.ProgrammeError):
        programme.parse_slots("morning=9-noon")


def test_the_relay_rate_limit_is_waited_out(svc, relay, monkeypatch):
    from belfry_onboarding.relay import RelayError

    waits = []
    monkeypatch.setattr(programme.time, "sleep", waits.append)
    publish, calls = relay.publish, {"n": 0}

    def limited(event, sign):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RelayError("event rejected: rate-limited: allow only sixty events per minute")
        publish(event, sign)

    monkeypatch.setattr(relay, "publish", limited)
    assert programme.sync(svc, http())["published"] == 6
    assert waits == [programme.RATE_LIMIT_WAIT] and len(calendar_events(relay)) == 6


def test_more_events_than_the_relay_answers_at_once(svc, relay):
    # the relay caps every answer (rnostr max_limit); the sync pages through by `until`
    relay.max_limit = 4
    assert programme.sync(svc, http())["published"] == 6
    again = programme.sync(svc, http())
    assert again["published"] == 0 and again["unchanged"] == 6
    gone = programme.sync(svc, http(schedule={**SCHEDULE, "days": []}))
    assert gone["deleted"] == 2
    assert programme.sync(svc, http(schedule={**SCHEDULE, "days": []}))["deleted"] == 0
