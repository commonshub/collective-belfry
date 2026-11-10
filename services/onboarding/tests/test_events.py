"""Belfry events (events.py): the public page and its JSON, custom URLs, open tickets for
members and for people with no account, applications and the waitlist, co-host links, and the
watcher's mails."""

from __future__ import annotations

import datetime as dt
import json
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


def test_a_custom_url_and_its_old_one(rsvc, world):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    make_event(world, ada)
    with TestClient(create_app(rsvc)) as c:
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
