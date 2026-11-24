"""Event details from an event's website (eventdetails.py, docs/event-details.md): JSON-LD
first, microdata, Open Graph, a linked calendar file; times and all-day; online locations never
leaking an address; the picture through the belfry; the guarded fetcher; the cache; who may
ask. The network is a fake: linkpreview's ``resolve`` and ``_get``."""

from __future__ import annotations

import itertools
import json
import threading
import time
import urllib.parse

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import eventdetails as ed
from belfry_onboarding import linkpreview as lp
from belfry_onboarding.app import create_app
from belfry_onboarding.events import Limiter

from .test_holder_invites import nip98
from .test_linkpreview import HTML, PNG, Net

ZOOM = "https://zoom.us/j/123456789?pwd=secret"


def page(*, head: str = "", body: str = "") -> bytes:
    return f"<!doctype html><html><head>{head}</head><body>{body}</body></html>".encode()


def ld(obj) -> str:
    return f'<script type="application/ld+json">{json.dumps(obj)}</script>'


EVENT = {
    "@context": "https://schema.org",
    "@type": "Event",
    "name": "Open Source Village 2027",
    "startDate": "2027-01-25T10:00:00+01:00",
    "endDate": "2027-02-05T18:00:00+01:00",
    "location": {
        "@type": "Place",
        "name": "Commons Hub",
        "address": {
            "@type": "PostalAddress",
            "streetAddress": "Rue de la Loi 1",
            "postalCode": "1000",
            "addressLocality": "Brussels",
            "addressCountry": {"@type": "Country", "name": "BE"},
        },
    },
    "image": ["/cover.png"],
    "description": "<p>Two weeks of <b>building</b> together.</p><p>Bring a laptop.</p>",
    "organizer": {"@type": "Organization", "name": "Commons Hub"},
}


@pytest.fixture
def net(services, monkeypatch):
    services.settings.nip05_domain = "belfry.test"
    services.settings.console_url = "https://belfry.belfry.test"
    services.settings.pass_page_secret = "s" * 32
    n = Net()
    n.names["belfry.test"] = ["203.0.113.9"]
    monkeypatch.setattr(lp, "resolve", n.resolve)
    monkeypatch.setattr(lp, "_get", n.get)
    lp.forget()
    ed.forget()
    ed.CALLS._seen.clear()
    yield n
    lp.forget()
    ed.forget()


def _from(services, net, body, url="https://example.org/e"):
    net.pages[url] = (200, HTML, body)
    return ed.details(services, url)


# ── JSON-LD ─────────────────────────────────────────────────────────────────────────────────
def test_a_json_ld_event(services, net):
    got = _from(services, net, page(head="<title>x</title>" + ld(EVENT)))
    assert got["source"] == "jsonld" and got["title"] == "Open Source Village 2027"
    assert got["starts_at"] == "2027-01-25T10:00:00+01:00"
    assert got["ends_at"] == "2027-02-05T18:00:00+01:00"
    assert got["all_day"] is False and got["timezone"] is None and got["online"] is False
    assert got["place"] == {"name": "Commons Hub", "address": "Rue de la Loi 1, 1000 Brussels, BE"}
    assert got["description"] == "Two weeks of building together.\nBring a laptop."
    assert got["organizer"] == "Commons Hub"
    assert got["url"] == "https://example.org/e"
    assert got["image"].startswith("https://belfry.belfry.test/api/v1/link-preview/image/")


def test_inside_a_graph_an_array_and_as_a_subtype(services, net):
    graph = {"@context": "https://schema.org", "@graph": [
        {"@type": "WebPage", "name": "Not this"},
        dict(EVENT, **{"@type": ["MusicEvent"], "name": "Concert"}),
    ]}  # fmt: skip
    assert _from(services, net, page(head=ld(graph)), "https://example.org/g")["title"] == "Concert"
    arr = [{"@type": "Organization", "name": "Org"}, dict(EVENT, **{"@type": "schema:Festival"})]
    got = _from(services, net, page(body=ld(arr)), "https://example.org/a")
    assert got["title"] == "Open Source Village 2027" and got["source"] == "jsonld"


def test_a_broken_block_does_not_hide_a_good_one(services, net):
    head = '<script type="application/ld+json">{oops</script>' + ld(EVENT)
    assert _from(services, net, page(head=head))["source"] == "jsonld"


def test_a_date_alone_is_all_day_and_offsets_are_kept(services, net):
    ev = dict(EVENT, startDate="2027-01-25", endDate="2027-01-26")
    got = _from(services, net, page(head=ld(ev)))
    assert got["all_day"] is True
    assert (got["starts_at"], got["ends_at"]) == ("2027-01-25", "2027-01-26")
    ev = dict(EVENT, startDate="2027-01-25T10:00:00Z", endDate="2027-01-25T12:00-0500")
    got = _from(services, net, page(head=ld(ev)), "https://example.org/z")
    assert got["starts_at"] == "2027-01-25T10:00:00+00:00"
    assert got["ends_at"] == "2027-01-25T12:00:00-05:00"
    ev = dict(EVENT, startDate="2027-01-25T10:00", endDate=None)
    got = _from(services, net, page(head=ld(ev)), "https://example.org/l")
    assert got["starts_at"] == "2027-01-25T10:00:00" and got["ends_at"] is None  # local time
    ev = dict(EVENT, eventSchedule={"@type": "Schedule", "scheduleTimezone": "Europe/Brussels"})
    got = _from(services, net, page(head=ld(ev)), "https://example.org/tz")
    assert got["timezone"] == "Europe/Brussels"
    ev = dict(EVENT, eventSchedule={"scheduleTimezone": "../../etc/passwd"})
    assert _from(services, net, page(head=ld(ev)), "https://example.org/t2")["timezone"] is None


def test_an_online_location_never_gives_its_address(services, net):
    ev = dict(
        EVENT,
        eventAttendanceMode="https://schema.org/MixedEventAttendanceMode",
        location=[{"@type": "VirtualLocation", "url": ZOOM}, EVENT["location"]],
        description=f"Join at {ZOOM} or come by.",
    )
    got = _from(services, net, page(head=ld(ev)))
    assert got["online"] is True and got["place"]["name"] == "Commons Hub"
    assert "zoom" not in json.dumps(got)
    ev = dict(EVENT, location=ZOOM)
    got = _from(services, net, page(head=ld(ev)), "https://example.org/o")
    assert got["online"] is True and got["place"] is None and "zoom" not in json.dumps(got)
    ev = dict(EVENT, eventAttendanceMode="OnlineEventAttendanceMode", location=None)
    got = _from(services, net, page(head=ld(ev)), "https://example.org/o2")
    assert got["online"] is True and got["place"] is None


def test_a_place_given_as_text(services, net):
    ev = dict(EVENT, location="Commons Hub, Brussels")
    got = _from(services, net, page(head=ld(ev)))
    assert got["place"] == {"name": None, "address": "Commons Hub, Brussels"}


# ── microdata, Open Graph, calendar files ───────────────────────────────────────────────────
def test_microdata(services, net):
    body = """<div itemscope itemtype="https://schema.org/Event">
      <h1 itemprop="name">Repair <b>café</b></h1>
      <time itemprop="startDate" datetime="2027-03-01T14:00:00+01:00">1 March</time>
      <div itemprop="location" itemscope itemtype="https://schema.org/Place">
        <span itemprop="name">Library</span><span itemprop="address">Main St 3</span>
      </div>
      <img itemprop="image" src="/m.png"><p itemprop="description">Fix things.</p>
    </div>"""
    got = _from(services, net, page(body=body))
    assert got["source"] == "microdata" and got["title"] == "Repair café"
    assert got["starts_at"] == "2027-03-01T14:00:00+01:00"
    assert got["place"] == {"name": "Library", "address": "Main St 3"}
    assert got["description"] == "Fix things." and got["image"]


def test_open_graph_only(services, net):
    head = """<title>Fallback</title><meta property="og:title" content="Meetup &amp; drinks">
    <meta property="og:description" content="Come along"><meta property="og:image"
    content="https://img.example.org/c.png"><meta property="event:start_time"
    content="2027-04-01T19:00:00+02:00">"""
    got = _from(services, net, page(head=head))
    assert got["source"] == "opengraph" and got["title"] == "Meetup & drinks"
    assert got["starts_at"] == "2027-04-01T19:00:00+02:00" and got["description"] == "Come along"
    assert got["image"] and "img.example.org" not in got["image"]
    got = _from(services, net, page(head="<title>Just a page</title>"), "https://example.org/p")
    assert got["title"] == "Just a page" and got["starts_at"] is None


ICS = (
    b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nSUMMARY:Board games\\, night\r\n"
    b"DTSTART;TZID=Europe/Brussels:20270510T190000\r\nDTEND;TZID=Europe/Brussels:2027051"
    b"0T230000\r\nLOCATION:De Kroeg\\, Grote Markt 1\\, Gent\r\nDESCRIPTION:Bring a game.\\nOr "
    b'two.\r\nORGANIZER;CN="Ann":mailto:ann@example.org\r\nBEGIN:VALARM\r\nDESCRIPTION:x\r\n'
    b"END:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)


def test_the_calendar_file_when_the_page_has_no_time(services, net):
    head = '<meta property="og:title" content="Games"><meta property="og:image" content="/i.png">'
    head += '<link rel="alternate" type="text/calendar" href="/e.ics">'
    net.pages["https://example.org/e.ics"] = (200, {"content-type": "text/calendar"}, ICS)
    got = _from(services, net, page(head=head))
    assert got["source"] == "ics" and got["title"] == "Games"  # the page's title first
    assert got["starts_at"] == "2027-05-10T19:00:00+02:00"
    assert got["ends_at"] == "2027-05-10T23:00:00+02:00"
    assert got["timezone"] == "Europe/Brussels" and got["all_day"] is False
    assert got["place"] == {"name": "De Kroeg", "address": "Grote Markt 1, Gent"}
    assert got["organizer"] == "Ann" and got["image"]
    assert got["description"] == "Bring a game.\nOr two."


def test_calendar_files_only_on_the_same_host_and_not_when_the_page_has_a_time(services, net):
    head = '<title>T</title><a href="https://other.example.net/e.ics">ics</a>'
    got = _from(services, net, page(body=head))
    assert got["starts_at"] is None
    assert not any("other.example.net" in u for u, _ in net.asked)
    net.asked.clear()
    _from(
        services, net, page(head=ld(EVENT), body='<a href="/x.ics">x</a>'), "https://example.org/b"
    )
    assert [u for u, _ in net.asked] == ["https://example.org/b"]


def test_ics_all_day_and_utc_and_online():
    all_day = ed.from_ics(
        b"BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Fair\nDTSTART;VALUE=DATE:20270601\n"
        b"DTEND;VALUE=DATE:20270603\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    assert all_day["start"] == ("2027-06-01", True) and all_day["end"] == ("2027-06-02", True)
    utc = ed.from_ics(
        b"BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Call\nDTSTART:20270601T080000Z\n"
        b"LOCATION:https://meet.example.org/abc\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    assert utc["start"] == ("2027-06-01T08:00:00+00:00", False)
    assert utc["online"] is True and utc["place"] is None
    assert ed.from_ics(b"not a calendar") is None


# ── the answer ──────────────────────────────────────────────────────────────────────────────
def test_nothing_usable_is_none_and_kept_half_an_hour(services, net, monkeypatch):
    url = "https://example.org/blank"
    assert _from(services, net, b"<p>no title</p>", url) is None
    asked = len(net.asked)
    assert ed.details(services, url) is None and len(net.asked) == asked
    now = time.monotonic()
    monkeypatch.setattr(ed.time, "monotonic", lambda: now + ed.NONE_SECONDS + 1)
    ed.details(services, url)
    assert len(net.asked) == asked + 1


def test_kept_six_hours_by_address_and_by_final_address(services, net, monkeypatch):
    net.pages["https://example.org/short"] = (301, {"location": "/e"}, b"")
    net.pages["https://example.org/e"] = (200, HTML, page(head=ld(EVENT)))
    got = ed.details(services, "https://example.org/short")
    assert got["url"] == "https://example.org/e"
    asked = len(net.asked)
    assert ed.details(services, "https://example.org/e") == got  # the final address: kept
    assert ed.details(services, "https://example.org/short") == got
    assert len(net.asked) == asked
    now = time.monotonic()
    monkeypatch.setattr(ed.time, "monotonic", lambda: now + ed.FOUND_SECONDS - 60)
    ed.details(services, "https://example.org/e")
    assert len(net.asked) == asked
    monkeypatch.setattr(ed.time, "monotonic", lambda: now + ed.FOUND_SECONDS + 1)
    ed.details(services, "https://example.org/e")
    assert len(net.asked) == asked + 1


def test_the_long_description_is_cut_and_paragraphs_kept(services, net):
    ev = dict(EVENT, description="Para one.\n\n\n\nPara   two. " + "word " * 2000)
    got = _from(services, net, page(head=ld(ev)))
    d = got["description"]
    assert d.startswith("Para one.\n\nPara two. ") and len(d) <= 4000 and d.endswith("…")


def test_a_megabyte_is_read_and_no_more(services, net):
    pad = "<!--" + "x" * (700 * 1024) + "-->"
    got = _from(services, net, page(body=pad + ld(EVENT)))
    assert got["source"] == "jsonld"  # past link previews' 512 kB
    pad = "<!--" + "x" * (1100 * 1024) + "-->"
    got = _from(
        services,
        net,
        page(head="<title>Big</title>", body=pad + ld(EVENT)),
        "https://example.org/big",
    )
    assert got["source"] == "opengraph" and got["starts_at"] is None


def test_refused_addresses_and_pictures(services, net):
    for url in ("http://example.org/", "https://10.0.0.1/", "https://belfry.test/e",
                "https://x.belfry.test/", "https://example.org:8443/"):  # fmt: skip
        with pytest.raises(lp.Refused):
            ed.details(services, url)
    assert net.asked == []
    net.names["inside.example.org"] = ["192.168.1.2"]
    assert _from(services, net, b"", "https://inside.example.org/") is None
    assert net.asked == []  # its name leads to a private address: never connected
    ev = dict(EVENT, image="https://belfry.test/own.png")
    assert _from(services, net, page(head=ld(ev)), "https://example.org/own")["image"] is None
    ev = dict(EVENT, image="http://example.org/plain.png")
    assert _from(services, net, page(head=ld(ev)), "https://example.org/pl")["image"] is None


def test_the_picture_is_served_by_the_belfry(services, net):
    got = _from(services, net, page(head=ld(EVENT)))
    net.pages["https://example.org/cover.png"] = (200, {"content-type": "image/png"}, PNG)
    digest = got["image"].rsplit("/", 1)[1]
    assert lp.image(services, digest) == ("image/png", PNG)


def test_busy(services, net, monkeypatch):
    monkeypatch.setattr(ed, "UPSTREAM", threading.BoundedSemaphore(1))
    monkeypatch.setattr(lp, "TIMEOUT", 0.05)
    ed.UPSTREAM.acquire()
    try:
        with pytest.raises(ed.Busy):
            ed.details(services, "https://example.org/e")
    finally:
        ed.UPSTREAM.release()


def test_a_slow_site_gives_nothing(services, net):
    net.pages["https://example.org/slow"] = (200, HTML, TimeoutError("slow"))
    assert ed.details(services, "https://example.org/slow") is None


# ── the endpoint ────────────────────────────────────────────────────────────────────────────
TICK = itertools.count()


def _ask(c, key, url):
    path = "/api/v1/event-details?" + urllib.parse.urlencode({"url": url})
    auth = nip98(key, "https://belfry.belfry.test" + path, created_at=int(time.time()) - next(TICK))
    return c.get(
        path,
        headers={
            "Host": "belfry.belfry.test",
            "Authorization": auth,
            "Origin": "https://chat.belfry.test",
        },
    )


def _person(odoo, tier):
    key = PrivateKey()
    partner = odoo.add("res.partner", name="Ada", email=f"{tier}@example.org")
    pk = key.public_key_xonly.format().hex()
    odoo.add("belfry.pass", partner_id=partner, tier=tier, state="active", keycast_pubkey=pk)
    return key


def test_the_api(services, odoo, net, monkeypatch):
    net.pages["https://example.org/e"] = (200, HTML, page(head=ld(EVENT)))
    net.pages["https://example.org/empty"] = (200, HTML, b"<p>x</p>")
    member = _person(odoo, "member")
    guest = _person(odoo, "guest")
    with TestClient(create_app(services)) as c:
        r = _ask(c, member, "https://example.org/e")
        assert r.status_code == 200, r.text
        assert r.json()["title"] == "Open Source Village 2027"
        assert r.headers["access-control-allow-origin"] == "https://chat.belfry.test"
        r = _ask(c, member, "https://example.org/empty")
        assert r.status_code == 404 and r.json()["error"] == "none"
        r = _ask(c, member, "http://example.org/")
        assert r.status_code == 422 and r.json()["error"] == "invalid"
        r = _ask(c, guest, "https://example.org/e")
        assert r.status_code == 403 and r.json()["error"] == "not_member"
        r = _ask(c, PrivateKey(), "https://example.org/e")
        assert r.status_code == 403
        r = c.get("/api/v1/event-details?url=https://example.org/e", headers={
            "Host": "belfry.belfry.test"
        })  # fmt: skip
        assert r.status_code == 401
        monkeypatch.setattr(ed, "CALLS", Limiter(1, 60))
        assert _ask(c, member, "https://example.org/e").status_code == 200
        r = _ask(c, member, "https://example.org/e")
        assert r.status_code == 429 and r.json()["error"] == "too_many"
