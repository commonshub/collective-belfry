"""Belfry events (events.py): the public page and its JSON, custom URLs, open tickets for
members and for people with no account, applications and the waitlist, co-host links, and the
watcher's mails."""

from __future__ import annotations

import datetime as dt
import json
import sys
import time

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import events, guests, mail, mailer, steward
from belfry_onboarding.app import create_app
from belfry_onboarding.groups_members import signed
from belfry_onboarding.register import Register

from .fakes import FakeRelay
from .test_holder_invites import BASE, nip98

D = "2026-k7p2xq"
GROUP = "ev-" + D
API = "https://belfry.belfry.test/api/v1/events"
HOST = {"Host": "belfry.belfry.test", "Content-Type": "application/json"}


class World:
    """The two relays as the belfry sees them, and the groups key."""

    def __init__(self) -> None:
        self.comm = FakeRelay()
        self.grp = FakeRelay()
        self.gkey = PrivateKey()

    def put(self, key: PrivateKey, kind: int, tags: list[list[str]], content: str = "", at=None):
        e = signed(
            key,
            {"kind": kind, "created_at": at or int(time.time()), "tags": tags, "content": content},
        )
        target = (
            self.comm
            if kind in (31922, 31923, 30402, 5, 0)
            or (kind == 31925 and not any(t[0] == "h" for t in tags))
            else self.grp
        )
        target.events.append(e)
        return e

    def admins(self, group: str, keys: list[str]) -> None:
        self.grp.events = [
            e for e in self.grp.events if not (e["kind"] == 39001 and ["d", group] in e["tags"])
        ]
        self.put(self.gkey, 39001, [["d", group], *[["p", k, "admin"] for k in keys]])


@pytest.fixture
def world(monkeypatch) -> World:
    w = World()
    monkeypatch.setattr(events, "community", lambda svc, f: w.comm.query(f, None))
    monkeypatch.setattr(events, "groups", lambda svc, f: w.grp.query(f, None))

    def publish(svc, kind, tags, content=""):
        w.put(w.gkey, kind, tags, content)
        return True

    monkeypatch.setattr(events, "publish", publish)
    monkeypatch.setattr(events, "forward", lambda svc, e: w.grp.events.append(e))
    monkeypatch.setattr(guests, "_channel_event", lambda svc, kind, tags: publish(svc, kind, tags))
    monkeypatch.setattr(events, "names", lambda svc, pks: {})
    return w


@pytest.fixture
def rsvc(services, tmp_path, settings, monkeypatch):
    mailer.SENT.clear()
    events._stores.clear()
    events.PER_IP.__init__(1000, 60)
    events.PER_EVENT.__init__(1000, 60)
    settings.register_path = str(tmp_path / "register.sqlite")
    settings.public_url = BASE
    settings.console_url = "https://belfry.belfry.test"
    settings.site_url = "https://belfry.test"
    settings.nip05_domain = "belfry.test"
    monkeypatch.setattr(guests, "_closed", lambda svc, channel: False)
    services.odoo = Register(tmp_path / "register.sqlite", settings)
    return services


def member(rsvc, email: str, name: str) -> PrivateKey:
    made = steward.steward_pass(rsvc, email, name)
    key = PrivateKey()
    rsvc.odoo.write(
        "belfry.pass", [made["pass_id"]], {"keycast_pubkey": key.public_key_xonly.format().hex()}
    )
    return key


def pk(key: PrivateKey) -> str:
    return key.public_key_xonly.format().hex()


def make_event(w: World, host: PrivateKey, visibility="public", start_in=7 * 86400, **extra):
    start = int(time.time()) + start_in
    tags = [
        ["d", D],
        ["title", "Garden day"],
        ["start", str(start)],
        ["end", str(start + 3 * 3600)],
        ["start_tzid", "Europe/Brussels"],
        ["location", "The garden"],
        ["r", "https://meet.jit.si/garden-day"],
        ["h", GROUP, "wss://groups-relay.belfry.test"],
        ["visibility", visibility],
        ["image", "https://blossom.belfry.test/" + "a" * 64 + ".jpg"],
    ]
    for k, v in extra.items():
        tags.append([k, v])
    w.put(host, 9007, [["h", GROUP]])
    w.admins(GROUP, [pk(host)])
    return w.put(host, 31923, tags, "We plant **beans**.\n\nBring gloves: https://example.org")


def ticket(w: World, host: PrivateKey, tid: str, title: str, **opts):
    tags = [
        ["d", f"{D}:{tid}"],
        ["a", f"31923:{pk(host)}:{D}"],
        ["h", GROUP],
        ["title", title],
        ["t", "ticket"],
    ]
    if opts.get("quantity") is not None:
        tags.append(["quantity", str(opts["quantity"])])
    if opts.get("closed"):
        tags += [["closed"], ["prompt", opts.get("prompt", "Why?")]]
    if opts.get("members"):
        tags.append(["members"])
    if opts.get("price"):
        tags.append(["price", *opts["price"]])
    return w.put(host, 30402, tags)


def call(c, key, method, path, data=None):
    body = json.dumps(data).encode() if data is not None else b""
    time.sleep(1.05)  # the same signed request twice in a second is a replay
    return c.request(
        method,
        "/api/v1/events" + path,
        content=body or None,
        headers={**HOST, "Authorization": nip98(key, API + path, method, body)},
    )


def test_the_public_page_json_ics_and_cover(rsvc, world, router):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    make_event(world, ada)
    router.get("http://blossom-gate/" + "a" * 64 + ".jpg").respond(
        200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"}
    )
    with TestClient(create_app(rsvc)) as c:
        page = c.get(f"/events/{D}")
        assert page.status_code == 200
        assert "Garden day" in page.text and "<b>beans</b>" in page.text
        assert 'property="og:title" content="Garden day"' in page.text
        assert f"https://belfry.test/events/{D}/cover" in page.text
        assert "noindex" not in page.text  # public: indexable
        assert f"https://chat.belfry.test/#/events/{D}" in page.text  # RSVP happens in the chat
        assert "meet.jit.si" not in page.text  # the online link is for those who are in
        j = c.get(f"/api/v1/events/{D}", headers=HOST).json()
        assert j["coord"] == f"31923:{pk(ada)}:{D}" and j["visibility"] == "public"
        assert j["online"] is True and j["links"] == [] and j["going"] == 0
        assert j["tickets"][0]["implicit"] is True and j["tickets"][0]["d"] == f"{D}:free"
        ics = c.get(f"/events/{D}.ics")
        assert ics.headers["content-type"].startswith("text/calendar")
        assert "SUMMARY:Garden day" in ics.text and "STATUS:CONFIRMED" in ics.text
        cover = c.get(f"/events/{D}/cover")
        assert cover.status_code == 200 and cover.headers["content-type"] == "image/jpeg"
        lst = c.get("/events")
        assert "Garden day" in lst.text
        # nothing from the URL is shown back
        missing = c.get("/events/<script>x")
        assert missing.status_code == 404 and "<script>" not in missing.text
        assert c.get("/events/2026-zzzzzz").status_code == 404


def test_visibility_members_and_link(rsvc, world):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    make_event(world, ada, visibility="members")
    with TestClient(create_app(rsvc)) as c:
        page = c.get(f"/events/{D}")
        assert "Garden day" not in page.text and "for the community&#x27;s members" in page.text
        assert c.get(f"/api/v1/events/{D}", headers=HOST).status_code == 403
        # a member reads it, with the online link
        r = call(c, ada, "GET", f"/{D}")
        assert r.status_code == 200 and r.json()["links"] == ["https://meet.jit.si/garden-day"]
        assert "Garden day" not in c.get("/events").text
    world.comm.events.clear()
    world.grp.events.clear()
    make_event(world, ada, visibility="link")
    with TestClient(create_app(rsvc)) as c:
        page = c.get(f"/events/{D}")
        assert "Garden day" in page.text and 'name="robots" content="noindex"' in page.text
        assert page.headers["x-robots-tag"] == "noindex"
        assert "Garden day" not in c.get("/events").text  # only public ones are listed


def test_a_custom_url_and_its_old_one(rsvc, world, monkeypatch):
    from belfry_onboarding import apps

    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    make_event(world, ada)
    with TestClient(create_app(rsvc)) as c:
        # a host who is no belfry admin: an address with 4 random characters at the end only
        # (#issues 2026-11-15)
        monkeypatch.setattr(apps, "admin_pubkeys", lambda svc: set())
        r = call(c, ada, "PUT", f"/{D}/slug", {"slug": "garden-day"})
        assert r.status_code == 403 and r.json()["error"] == "admins_only"
        assert call(c, ada, "PUT", f"/{D}/slug", {"slug": "garden-day-k3x9"}).status_code == 200
        monkeypatch.setattr(apps, "admin_pubkeys", lambda svc: {pk(ada)})
        r = call(c, ada, "GET", "/slug?name=garden-day")
        assert r.json() == {"name": "garden-day", "free": True, "suggestion": "garden-day"}
        assert call(c, ada, "GET", f"/slug?name={D}").json()["free"] is False  # an id's shape
        assert call(c, ada, "GET", "/slug?name=new").json()["free"] is False
        assert c.get("/api/v1/events/slug?name=x", headers=HOST).status_code == 401
        assert call(c, bea, "PUT", f"/{D}/slug", {"slug": "garden-day"}).status_code == 403
        assert call(c, ada, "PUT", f"/{D}/slug", {"slug": "-bad"}).status_code == 422
        r = call(c, ada, "PUT", f"/{D}/slug", {"slug": "garden-day"})
        assert r.status_code == 200 and r.json()["url"] == "https://belfry.test/events/garden-day"
        assert call(c, ada, "GET", "/slug?name=garden-day").json()["free"] is False
        assert call(c, ada, "GET", f"/slug?name=garden-day&event={D}").json()["free"] is True
        assert "Garden day" in c.get("/events/garden-day").text
        assert call(c, ada, "PUT", f"/{D}/slug", {"slug": "beans-2027"}).status_code == 200
        old = c.get("/events/garden-day", follow_redirects=False)
        assert old.status_code == 301 and old.headers["location"] == "/events/beans-2027"
        assert c.get(f"/api/v1/events/{D}", headers=HOST).json()["link"] == "beans-2027"


def test_open_ticket_for_a_member_and_for_someone_new(rsvc, world, keycast):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    make_event(world, ada)
    ticket(world, ada, "std", "Standard", quantity=2)
    with TestClient(create_app(rsvc)) as c:
        r = call(c, bea, "POST", f"/{D}/join", {"ticket": "std"})
        assert r.status_code == 200, r.text
        put = world.grp.events[-1]
        assert put["kind"] == 9000 and ["ticket", f"{D}:std"] in put["tags"]
        assert ["p", pk(bea), "member"] in put["tags"] and ["paid", "free"] in put["tags"]
        n = len(world.grp.events)
        assert call(c, bea, "POST", f"/{D}/join", {"ticket": "std"}).json()["already"] is True
        assert len(world.grp.events) == n  # one ticket per person
        # someone with no account: a name, and a guest with a session made for that key
        session = PrivateKey()
        assert call(c, session, "POST", f"/{D}/join", {"ticket": "std"}).json()["error"] == (
            "name_needed"
        )
        got = call(c, session, "POST", f"/{D}/join", {"ticket": "std", "name": "Gus"})
        assert got.status_code == 200 and got.json()["bunker_url"].startswith("bunker://")
        assert keycast.bunkers[-1]["client_pubkey"] == pk(session)
        gus = got.json()["pubkey"]
        put = world.grp.events[-1]
        assert ["p", gus, "member"] in put["tags"] and ["name", "Gus"] in put["tags"]
        assert ["unsaved", "1"] in put["tags"]
        p = rsvc.odoo.search_read(
            "belfry.pass",
            [["keycast_pubkey", "=", gus]],
            ["state", "tier", "date_end", "guest_channel"],
        )[0]
        assert p["state"] == "active" and p["tier"] == "guest" and p["guest_channel"] == GROUP
        ev = events.load(rsvc, D)
        assert p["date_end"] == (ev.last_day() + dt.timedelta(days=30)).isoformat()
        # the same device asks again: the same account, no second ticket
        again = call(c, session, "POST", f"/{D}/join", {"ticket": "std", "name": "Gus"})
        assert again.json().get("pubkey") == gus, again.text
        assert (
            sum(
                1
                for e in world.grp.events
                if e["kind"] == 9000 and ["p", gus, "member"] in e["tags"]
            )
            == 1
        )
        # full: the next one is told to join the waitlist
        r = call(c, PrivateKey(), "POST", f"/{D}/join", {"ticket": "std", "name": "Ivy"})
        assert r.status_code == 409 and r.json()["error"] == "full"
        j = c.get(f"/api/v1/events/{D}", headers=HOST).json()
        assert j["tickets"][0]["left"] == 0 and j["going"] == 2


def test_members_only_tickets_and_a_cancelled_event(rsvc, world, keycast):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    make_event(world, ada)
    ticket(world, ada, "crew", "Crew", members=True)
    with TestClient(create_app(rsvc)) as c:
        r = call(c, PrivateKey(), "POST", f"/{D}/join", {"ticket": "crew", "name": "Gus"})
        assert r.status_code == 403 and r.json()["error"] == "members_only"
        world.put(
            ada,
            1985,
            [["h", GROUP], ["L", events.STATUS_NS], ["l", "cancelled", events.STATUS_NS]],
            "Rain",
        )
        r = call(c, ada, "POST", f"/{D}/join", {"ticket": "crew"})
        assert r.status_code == 409 and r.json()["error"] == "cancelled"
        assert "STATUS:CANCELLED" in c.get(f"/events/{D}.ics").text
        assert "Cancelled" in c.get(f"/events/{D}").text


def test_an_application_from_someone_new_and_the_hosts_answer(rsvc, world, keycast):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    make_event(world, ada)
    ticket(world, ada, "sp", "Speaker", closed=True, prompt="What will you show?")
    with TestClient(create_app(rsvc)) as c:
        session = PrivateKey()
        r = call(c, session, "POST", f"/{D}/join", {"ticket": "sp", "name": "Gus"})
        assert r.json()["error"] == "needs_application"
        r = call(c, session, "POST", f"/{D}/apply", {"ticket": "sp", "name": "Gus"})
        assert r.status_code == 422 and r.json()["error"] == "email_needed"
        r = call(
            c,
            session,
            "POST",
            f"/{D}/apply",
            {"ticket": "sp", "name": "Gus", "email": "gus@example.org"},
        )
        assert r.status_code == 200 and r.json()["needs_request"] is True
        ident = r.json()["pubkey"]
        row = rsvc.odoo.search_read(
            "belfry.pass", [["keycast_pubkey", "=", ident]], ["id", "state", "event_email"]
        )[0]
        assert row["state"] == "pending" and row["event_email"] == "gus@example.org"
        # the identity (the signer holds it; a key of ours stands in) signs its join request
        gus = PrivateKey()
        rsvc.odoo.write("belfry.pass", [row["id"]], {"keycast_pubkey": pk(gus)})
        bad = signed(
            gus,
            {
                "kind": 9021,
                "created_at": int(time.time()),
                "content": "Compost",
                "tags": [["h", "general"], ["ticket", f"{D}:sp"]],
            },
        )
        assert (
            call(c, gus, "POST", f"/{D}/apply", {"ticket": "sp", "request": bad}).json()["error"]
            == "bad_request"
        )
        req = signed(
            gus,
            {
                "kind": 9021,
                "created_at": int(time.time()),
                "content": "Compost",
                "tags": [["h", GROUP], ["ticket", f"{D}:sp"]],
            },
        )
        mailer.SENT.clear()
        r = call(c, gus, "POST", f"/{D}/apply", {"ticket": "sp", "request": req})
        assert r.status_code == 200 and r.json()["applied"] is True
        assert world.grp.events[-1]["id"] == req["id"]  # handed in as signed
        assert "applied to Garden day" in mailer.SENT[-1]["Subject"]  # the hosts are told
        j = call(c, gus, "GET", f"/{D}").json()
        assert j["me"]["applied"] is True and j["tickets"][0]["prompt"] == "What will you show?"
        # the host approves (a 9000 with the ticket): the watcher opens the pass and mails
        world.put(
            ada,
            9000,
            [["h", GROUP], ["p", pk(gus), "member"], ["ticket", f"{D}:sp"], ["e", req["id"]]],
        )
        mailer.SENT.clear()
        done = events.watch(rsvc)
        assert done["going"] == 1
        assert rsvc.odoo.read_one("belfry.pass", row["id"], ["state"])["state"] == "active"
        assert mailer.SENT[-1]["To"] == "gus@example.org"
        assert "You're going: Garden day" in mailer.SENT[-1]["Subject"]
        assert events.watch(rsvc)["going"] == 0  # once
        assert call(c, gus, "GET", f"/{D}").json()["me"]["ticket"] == f"{D}:sp"


def test_a_declined_application_and_a_members_own_request(rsvc, world, keycast):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    make_event(world, ada)
    ticket(world, ada, "sp", "Speaker", closed=True)
    with TestClient(create_app(rsvc)) as c:
        # a member publishes their own 9021 (they are on the relay)
        r = call(c, bea, "POST", f"/{D}/apply", {"ticket": "sp"})
        assert r.status_code == 409 and r.json()["error"] == "publish_yourself"
        r = call(
            c,
            PrivateKey(),
            "POST",
            f"/{D}/apply",
            {"ticket": "sp", "name": "Ivy", "email": "ivy@example.org"},
        )
        ivy = r.json()["pubkey"]
        world.put(ada, 9001, [["h", GROUP], ["p", ivy]])
        mailer.SENT.clear()
        assert events.watch(rsvc)["declined"] == 1
        assert mailer.SENT[-1]["To"] == "ivy@example.org"
        p = rsvc.odoo.search_read("belfry.pass", [["keycast_pubkey", "=", ivy]], ["state"])[0]
        assert p["state"] == "revoked"


def test_the_waitlist_when_full(rsvc, world, keycast):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    make_event(world, ada)
    ticket(world, ada, "std", "Standard", quantity=1)
    with TestClient(create_app(rsvc)) as c:
        r = call(
            c,
            PrivateKey(),
            "POST",
            f"/{D}/apply",
            {"ticket": "std", "name": "Ivy", "email": "ivy@example.org"},
        )
        assert r.json()["error"] == "use_join"  # places left: take one
        assert call(c, bea, "POST", f"/{D}/join", {"ticket": "std"}).status_code == 200
        r = call(
            c,
            PrivateKey(),
            "POST",
            f"/{D}/apply",
            {"ticket": "std", "name": "Ivy", "email": "ivy@example.org"},
        )
        assert r.status_code == 200 and r.json()["waitlist"] is True


def test_a_guest_host_decides_through_the_belfry(rsvc, world, keycast):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    make_event(world, ada)
    ticket(world, ada, "sp", "Speaker", closed=True)
    who = pk(PrivateKey())
    with TestClient(create_app(rsvc)) as c:
        assert (
            call(c, bea, "POST", f"/{D}/decide", {"pubkey": who, "action": "approve"}).status_code
            == 403
        )
        r = call(
            c, ada, "POST", f"/{D}/decide", {"pubkey": who, "action": "approve", "ticket": "sp"}
        )
        assert r.json()["approved"] is True
        last = world.grp.events[-1]
        assert last["kind"] == 9000 and ["by", pk(ada)] in last["tags"]
        assert (
            call(
                c, ada, "POST", f"/{D}/decide", {"pubkey": who, "action": "approve", "ticket": "sp"}
            ).json()["already"]
            is True
        )


def test_co_host_edits_follow_rule_b(rsvc, world):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    eve = PrivateKey()
    v1 = make_event(world, ada)
    later = int(time.time()) + 5
    tags = [t for t in v1["tags"] if t[0] != "title"] + [["title", "Garden day (moved)"]]
    world.put(eve, 31923, tags, "x", at=later + 5)  # not a host: ignored
    assert events.load(rsvc, D).title == "Garden day"
    world.put(bea, 31923, tags, "x", at=later)  # a host's version, before being a host: ignored
    assert events.load(rsvc, D).title == "Garden day"
    world.admins(GROUP, [pk(ada), pk(bea)])
    ev = events.load(rsvc, D)
    assert ev.title == "Garden day (moved)" and ev.coord == f"31923:{pk(ada)}:{D}"


def test_a_co_host_link(rsvc, world, keycast, monkeypatch):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    make_event(world, ada)
    monkeypatch.setattr(mail, "channel_exists", lambda svc, ch: True)
    monkeypatch.setattr(mail, "group_admins", lambda svc, ch: {pk(ada)} if ch == GROUP else set())
    monkeypatch.setattr(guests, "_in_channel", lambda *a: False)

    def post(c, key, path, data):
        body = json.dumps(data).encode()
        url = "https://belfry.belfry.test/api/v1" + path
        time.sleep(1.05)
        return c.post(
            "/api/v1" + path,
            content=body,
            headers={**HOST, "Authorization": nip98(key, url, "POST", body)},
        )

    with TestClient(create_app(rsvc)) as c:
        assert (
            post(c, bea, "/channel-invites", {"channel": GROUP, "role": "host"}).status_code == 403
        )
        assert (
            post(c, ada, "/channel-invites", {"channel": "general", "role": "host"}).status_code
            == 422
        )
        r = post(c, ada, "/channel-invites", {"channel": GROUP, "role": "host"})
        assert r.status_code == 200 and r.json()["role"] == "host"
        token = r.json()["url"].rsplit("/", 1)[1]
        got = post(c, bea, f"/channel-invites/{token}/join", {})
        assert got.status_code == 200 and got.json()["host"] is True
        last = world.grp.events[-1]
        assert last["kind"] == 9000 and ["p", pk(bea), "admin"] in last["tags"]
        # someone new: in as a guest, told to save an e-mail before becoming a co-host
        r = post(c, ada, "/channel-invites", {"channel": GROUP, "role": "host"})
        token = r.json()["url"].rsplit("/", 1)[1]
        session = PrivateKey()
        got = post(c, session, f"/channel-invites/{token}/join", {"name": "Gus"})
        assert got.json()["email_needed"] is True
        assert ["p", got.json()["pubkey"], "admin"] not in world.grp.events[-1]["tags"]
        gus = PrivateKey()
        row = rsvc.odoo.search_read(
            "belfry.pass", [["keycast_pubkey", "=", got.json()["pubkey"]]], ["id", "partner_id"]
        )[0]
        rsvc.odoo.write("belfry.pass", [row["id"]], {"keycast_pubkey": pk(gus)})
        r = post(c, gus, f"/channel-invites/{token}/join", {})
        assert r.status_code == 409 and r.json()["error"] == "email_needed"
        rsvc.odoo.write("res.partner", [row["partner_id"][0]], {"email": "gus@example.org"})
        r = post(c, gus, f"/channel-invites/{token}/join", {})
        assert r.status_code == 200, r.text
        assert ["p", pk(gus), "admin"] in world.grp.events[-1]["tags"]


def test_cancellation_and_reminder_mails(rsvc, world, keycast):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    make_event(world, ada, start_in=20 * 3600)
    with TestClient(create_app(rsvc)) as c:
        got = call(
            c, PrivateKey(), "POST", f"/{D}/join", {"name": "Gus", "email": "gus@example.org"}
        )
        assert got.status_code == 200, got.text
        mailer.SENT.clear()
        done = events.watch(rsvc)
        subjects = [m["Subject"] for m in mailer.SENT]
        assert done["going"] == 1 and "You're going: Garden day" in subjects
        assert done["reminded"] == 1 and "Tomorrow: Garden day" in subjects
        assert events.watch(rsvc)["reminded"] == 0
        world.put(
            ada,
            1985,
            [["h", GROUP], ["L", events.STATUS_NS], ["l", "cancelled", events.STATUS_NS]],
            "Rain",
        )
        mailer.SENT.clear()
        assert events.watch(rsvc)["cancelled"] == 1
        assert (
            mailer.SENT[-1]["Subject"] == "Cancelled: Garden day"
            and "Rain" in mailer.SENT[-1].get_body(preferencelist=("plain",)).get_content()
        )


def test_slug_rules():
    assert events.slug_problem("garden-day") is None
    assert events.slug_problem("ab") and events.slug_problem("2026-abcdef")
    assert events.slug_problem("mine") and events.slug_problem("Garden")
    assert events.slug_problem("a" * 41)


def test_the_chat_may_put_a_custom_url():
    """PUT /api/events/<d>/slug comes from chat.<domain>: the CORS answer allows PUT."""
    from belfry_onboarding import discovery

    assert (
        "PUT" in discovery.cors_headers("https://chat.belfry.test")["Access-Control-Allow-Methods"]
    )


def test_the_public_list_for_guests(rsvc, world):
    """``GET /api/events?visibility=public`` (no auth): what the apex page lists, as JSON."""
    ada = member(rsvc, "ada@belfry.test", "Ada")
    make_event(world, ada)
    soon = int(time.time()) + 3600
    other = "2026-abc123"
    world.put(
        ada,
        31923,
        [
            ["d", other],
            ["title", "Members' supper"],
            ["start", str(soon)],
            ["visibility", "members"],
        ],
    )
    early = "2026-early1"
    world.put(
        ada,
        31923,
        [["d", early], ["title", "Coffee"], ["start", str(soon)], ["visibility", "public"]],
    )
    with TestClient(create_app(rsvc)) as c:
        r = c.get("/api/v1/events?visibility=public", headers=HOST)
        assert r.status_code == 200, r.text
        got = r.json()["events"]
        assert [e["d"] for e in got] == [early, D]  # soonest first; members-only left out
        one = c.get(f"/api/v1/events/{D}", headers=HOST).json()
        assert got[1] == one and got[1]["me"] is None  # the same JSON, as an anonymous viewer
        later = int(time.time()) + 2 * 86400
        r = c.get(f"/api/v1/events?visibility=public&from={later}", headers=HOST)
        assert [e["d"] for e in r.json()["events"]] == [D]
        assert c.get("/api/v1/events?visibility=members", headers=HOST).status_code == 422
        assert c.get("/api/v1/events?from=soon", headers=HOST).json()["error"] == "invalid"
        # CORS for the chat, like the other events routes
        r = c.get("/api/v1/events", headers={**HOST, "Origin": "https://chat.belfry.test"})
        assert r.headers["access-control-allow-origin"] == "https://chat.belfry.test"
        world.put(
            ada,
            1985,
            [["h", GROUP], ["L", events.STATUS_NS], ["l", "cancelled", events.STATUS_NS]],
            "Rain",
        )
        assert [e["d"] for e in c.get("/api/v1/events", headers=HOST).json()["events"]] == [early]
        r = c.get("/api/v1/events?cancelled=1", headers=HOST)
        assert [e["d"] for e in r.json()["events"]] == [early, D]


def test_a_priced_ticket_is_never_self_served(rsvc, world, keycast):
    from belfry_onboarding import events

    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    make_event(world, ada)
    ticket(world, ada, "std", "Standard", price=["15.50", "eur"])
    ticket(world, ada, "time", "Helper", price=["2", "hours"])
    ticket(world, ada, "odd", "Odd", price=["lots", "<b>"])
    ticket(world, ada, "zero", "Zero", price=["0", "EUR"])
    with TestClient(create_app(rsvc)) as c:
        by = {t["id"]: t for t in call(c, bea, "GET", f"/{D}").json()["tickets"]}
        assert (by["std"]["price"]["amount"], by["std"]["price"]["currency"]) == ("15.50", "EUR")
        assert by["std"]["needs_payment"] is True and by["std"]["closed"] is False
        assert (by["time"]["price"]["amount"], by["time"]["price"]["currency"]) == ("2", "HOUR")
        assert by["odd"]["price"] is None and by["odd"]["needs_payment"] is True
        assert by["zero"]["price"] is None and by["zero"]["needs_payment"] is False
        page = c.get(f"/events/{D}").text
        assert "€15.50" in page and "2 hours" in page and "lots" not in page
        n = len(world.grp.events)
        for tid in ("std", "time", "odd"):
            r = call(c, bea, "POST", f"/{D}/join", {"ticket": tid})
            assert r.status_code == 409 and r.json()["error"] == "payment"
        r = call(c, PrivateKey(), "POST", f"/{D}/join", {"ticket": "std", "name": "Gus"})
        assert r.json()["error"] == "payment"
        assert len(world.grp.events) == n  # nobody got a ticket
        # asking goes through the application path: a member sends the request themselves
        r = call(c, bea, "POST", f"/{D}/apply", {"ticket": "std"})
        assert r.json()["error"] == "publish_yourself"
        # the host settles it: the ticket says paid
        r = call(
            c,
            ada,
            "POST",
            f"/{D}/decide",
            {"pubkey": pk(bea), "action": "approve", "ticket": "std"},
        )
        assert r.json()["approved"] is True
        put = world.grp.events[-1]
        assert put["kind"] == 9000 and ["paid", "paid"] in put["tags"]
        assert call(c, bea, "POST", f"/{D}/join", {"ticket": "zero"}).json().get("already")
    got = events.price_of({"tags": [["price", "007.10", "tokens"]]})
    assert (got["priced"], got["amount"], got["currency"]) == (True, "7.10", "tokens")
    got = events.price_of({"tags": [["price", "5"]]})
    assert (got["priced"], got["amount"]) == (True, "")


def test_vat_and_prices_for_a_while(rsvc, world, keycast):
    from belfry_onboarding import events

    jan1 = 1798761600  # 2027-01-01 00:00 UTC
    tags = [
        ["price_at", "50", "EUR", "", str(jan1)],
        ["price", "75", "EUR"],
        ["vat", "21"],
    ]
    early = events.price_of({"tags": tags}, now=jan1 - 10)
    assert (early["amount"], early["vat"], early["vat_included"]) == ("50", "21", False)
    assert early["until"] == jan1 and early["on_sale"] is True
    late = events.price_of({"tags": tags}, now=jan1)
    assert late["amount"] == "75" and late["until"] is None
    incl = events.price_of({"tags": [["price", "12.5", "EUR"], ["vat", "6.00", "included"]]})
    assert (incl["amount"], incl["vat"], incl["vat_included"]) == ("12.50", "6", True)
    # the plain price holds until a later dated one starts
    nxt = events.price_of(
        {"tags": [["price", "50", "EUR"], ["price_at", "75", "EUR", str(jan1), ""]]}, now=jan1 - 10
    )
    assert nxt["amount"] == "50" and nxt["until"] == jan1
    # no VAT on hours; dated prices only and none now = not on sale, never free
    assert events.price_of({"tags": [["price", "2", "HOUR"], ["vat", "21"]]})["vat"] == ""
    off = events.price_of({"tags": [["price_at", "50", "EUR", "", str(jan1)]]}, now=jan1 + 1)
    assert off["priced"] is True and off["on_sale"] is False and off["amount"] == ""
    bad = events.price_of({"tags": [["price_at", "50", "EUR", "soon", ""], ["price", "75", "EUR"]]})
    assert bad["priced"] is True and bad["amount"] == ""

    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    make_event(world, ada)
    far = str(int(time.time()) + 400 * 86400)
    world.put(
        ada,
        30402,
        [
            ["d", f"{D}:std"],
            ["a", f"31923:{pk(ada)}:{D}"],
            ["h", GROUP],
            ["title", "Standard"],
            ["t", "ticket"],
            ["price_at", "50", "EUR", "", far],
            ["price", "75", "EUR"],
            ["vat", "21"],
        ],
    )
    world.put(
        ada,
        30402,
        [
            ["d", f"{D}:gone"],
            ["a", f"31923:{pk(ada)}:{D}"],
            ["h", GROUP],
            ["title", "Early"],
            ["t", "ticket"],
            ["price_at", "10", "EUR", "", "1700000000"],
        ],
    )
    with TestClient(create_app(rsvc)) as c:
        by = {t["id"]: t for t in call(c, bea, "GET", f"/{D}").json()["tickets"]}
        assert by["std"]["price"] == {
            "amount": "50",
            "currency": "EUR",
            "vat": "21",
            "vat_included": False,
            "until": int(far),
        }
        assert len(by["std"]["prices"]) == 2 and by["std"]["on_sale"] is True
        assert by["gone"]["on_sale"] is False and by["gone"]["needs_payment"] is True
        page = c.get(f"/events/{D}").text
        assert "€50 + 21% VAT · until" in page and "Not on sale now" in page
        for path in ("join", "apply"):
            r = call(c, bea, "POST", f"/{D}/{path}", {"ticket": "gone"})
            assert r.status_code == 409 and r.json()["error"] == "not_on_sale"


def test_the_address_on_the_event_is_honoured_and_short_ids_work(rsvc, world, monkeypatch):
    from belfry_onboarding import apps, events

    assert events.D_RE.match("j5l6m3") and events.D_RE.match("2026-j5l6m3")
    assert events.slug_problem("abc123")  # an id's shape
    ada = member(rsvc, "ada@belfry.test", "Ada")
    monkeypatch.setattr(apps, "admin_pubkeys", lambda svc: {pk(ada)})
    # the chat's claim never arrived: only the tag on the event says the address
    make_event(world, ada, slug="garden2027")
    with TestClient(create_app(rsvc)) as c:
        assert "Garden day" in c.get("/events/garden2027").text
        moved = c.get(f"/events/{D}", follow_redirects=False)
        assert moved.status_code == 301 and moved.headers["location"] == "/events/garden2027"
        assert call(c, ada, "GET", f"/{D}").json()["link"] == "garden2027"
        assert c.get("/events/nothere2027").status_code == 404
    # someone who is no admin: a plain address on the event is not taken
    world.comm.events.clear()
    world.grp.events.clear()
    events._slug_miss.clear()
    monkeypatch.setattr(apps, "admin_pubkeys", lambda svc: set())
    bea = member(rsvc, "bea@belfry.test", "Bea")
    monkeypatch.setattr(sys.modules[__name__], "D", "k7p2xr")
    monkeypatch.setattr(sys.modules[__name__], "GROUP", "ev-k7p2xr")
    make_event(world, bea, slug="plainname")
    with TestClient(create_app(rsvc)) as c:
        assert c.get("/events/plainname").status_code == 404
        assert "Garden day" in c.get("/events/k7p2xr").text  # a short id, no address


def test_hosted_by_shows_names_and_never_a_key(rsvc, world, monkeypatch):
    from belfry_onboarding import checkin, community, events, theme

    ada = member(rsvc, "ada@belfry.test", "Ada")
    make_event(world, ada)
    monkeypatch.setattr(checkin, "groups_pubkey", lambda svc: pk(world.gkey))
    world.admins(GROUP, [pk(ada), pk(world.gkey)])  # the relay lists the belfry's own key too
    monkeypatch.setattr(community, "names_for", lambda svc, keys: {k: "Unknown" for k in keys})
    ev = events.load(rsvc, D)
    assert events.shown_hosts(rsvc, ev) == [pk(ada)]  # not the groups key
    html_ = community.people_html(rsvc, [pk(ada)])
    assert html_.startswith('<span title="npub1') and html_.endswith(">unknown</span>")
    named = {pk(ada): "Ada", "b" * 64: "Unknown", "c" * 64: "Unknown"}
    monkeypatch.setattr(community, "names_for", lambda svc, keys: {k: named[k] for k in keys})
    html_ = community.people_html(rsvc, [pk(ada), "b" * 64, "c" * 64])
    assert html_.startswith("Ada and <span title=") and ">2 others</span>" in html_
    with TestClient(create_app(rsvc)) as c:
        page = c.get(f"/events/{D}").text
        assert "Hosted by Ada" in page and "npub1" not in page.split("Hosted by")[1][:200]
    out = theme.footer_html(
        "Has its own [digital belfry](https://belfry.sh). <b> [x](//evil.example)"
    )
    assert '<a href="https://belfry.sh">digital belfry</a>' in out and "&lt;b&gt;" in out
    assert 'href="//evil' not in out
