"""A channel's public page (webthreads.py): /<slug> lists threads, anyone opens one after
confirming their address, members' replies are mailed to them, and the list can be private."""

from __future__ import annotations

import hashlib
import json
import re

import pytest
from fastapi.testclient import TestClient

from belfry_onboarding import (
    apps,
    community,
    core,
    followers,
    guests,
    mail,
    mailer,
    mentions,
    webthreads,
    whois,
)
from belfry_onboarding.app import create_app
from belfry_onboarding.register import Register

ADMIN, MEMBER, GK, EVE = "a" * 64, "b" * 64, "f" * 64, "e" * 64
WEB_IDENTITY = webthreads.web_identity


class FakeRelay:
    def __init__(self):
        self.events: list[dict] = []
        self.n = 0

    def sign(self, t):
        self.n += 1
        e = {"pubkey": GK, **t}
        e.setdefault("created_at", 1_800_000_000 + self.n)
        e["id"] = hashlib.sha256(json.dumps([self.n, t], sort_keys=True).encode()).hexdigest()
        return e

    def query(self, filters, sign):
        f = filters[0]
        out = []
        for e in self.events:
            if e["kind"] not in f.get("kinds", [e["kind"]]):
                continue
            ok = True
            for k, vals in f.items():
                if k.startswith("#") and not any(t[0] == k[1:] and t[1] in vals for t in e["tags"]):
                    ok = False
            if ok:
                out.append(e)
        return out

    def publish(self, e, sign):
        self.events.append(e)

    def sign_as(self, pubkey):
        return lambda t: {**self.sign(t), "pubkey": pubkey}


@pytest.fixture
def world(services, tmp_path, settings, monkeypatch):
    mailer.SENT.clear()
    mail._counts.clear()
    services.odoo = Register(tmp_path / "register.sqlite", settings)
    services.settings.site_url = "https://belfry.test"
    services.settings.nip05_domain = "belfry.test"
    relay = FakeRelay()
    monkeypatch.setattr(followers, "_relay", lambda svc: (relay, relay.sign))
    monkeypatch.setattr(webthreads, "_groups_pubkey", lambda svc: GK)
    monkeypatch.setattr(apps, "admin_pubkeys", lambda svc: {ADMIN})
    monkeypatch.setattr(whois, "kinds", lambda svc: {MEMBER: "member"})
    # these tests cover the card path (no account could be made); test_an_account_* the other
    monkeypatch.setattr(webthreads, "web_identity", lambda *a: None)
    webthreads._cache.update(at=0.0, sites={})
    relay.events += [
        {
            "id": "m1",
            "kind": 39000,
            "pubkey": GK,
            "created_at": 1,
            "tags": [["d", "props"], ["name", "Proposals"], ["about", "Ideas for the community"]],
            "content": "",
        },
        {
            "id": "m2",
            "kind": 39001,
            "pubkey": GK,
            "created_at": 1,
            "tags": [["d", "props"], ["p", MEMBER]],
            "content": "",
        },
        {
            "id": "k0",
            "kind": 0,
            "pubkey": MEMBER,
            "created_at": 1,
            "tags": [],
            "content": '{"name":"Mia"}',
        },
    ]
    yield services, relay
    webthreads._cache.update(at=0.0, sites={})


def setting(relay, by, on=True, slug="ideas", lst="public", at=10, vis=None):
    relay.events.append(
        {
            "id": f"s{at}",
            "kind": 30078,
            "pubkey": by,
            "created_at": at,
            "content": json.dumps(
                {"on": on, "slug": slug, **({"visibility": vis} if vis else {"list": lst})}
            ),
            "tags": [["d", "belfry:web:props"], ["h", "props"], ["t", "belfry-web"]],
        }
    )
    webthreads._cache.update(at=0.0, sites={})


def link_in(msg) -> str:
    text = msg.get_body(preferencelist=("plain",)).get_content()
    return re.search(r"https://belfry\.test(/\S+)", text).group(1)


def test_a_public_page_where_anyone_opens_a_thread(world):
    svc, relay = world
    with TestClient(create_app(svc), base_url="https://belfry.test") as c:
        assert c.get("/ideas").status_code != 200 or "Open a thread" not in c.get("/ideas").text
        setting(relay, "c" * 64)  # not an admin of the channel: ignored
        assert "Open a thread" not in c.get("/ideas").text
        setting(relay, MEMBER, at=11)  # the channel's admin
        page = c.get("/ideas")
        assert (
            page.status_code == 200
            and "Proposals" in page.text
            and "Ideas for the community" in page.text
        )
        # the issues template (#issues 2026-11-13): Open / Closed, sign in or post as a guest
        assert "Nothing open: be the first" in page.text and "Open an issue" in page.text
        assert "Sign in</a>" in page.text and "chat.belfry.test/#/c/props" in page.text
        r = c.post(
            "/ideas",
            data={
                "title": "A tool library",
                "text": "Let's share tools.",
                "name": "Eve",
                "email": "eve@example.org",
            },
        )
        assert r.status_code == 200 and "Check your e-mail" in r.text
        assert not [
            e for e in relay.events if e["kind"] == 9
        ]  # nothing posted before the confirmation
        confirm_link = link_in(mailer.SENT[-1])
        r = c.get(confirm_link, follow_redirects=False)
        assert r.status_code == 303
        card = next(e for e in relay.events if e["kind"] == 9)
        assert ["mail-from", "eve@example.org"] in card["tags"] and [
            "client",
            "belfry-web",
        ] in card["tags"]
        assert ["subject", "A tool library"] in card["tags"]
        mine = r.headers["location"]
        assert mine.startswith(f"/ideas/{card['id']}?k=")
        # the list: the thread, its author from outside
        page = c.get("/ideas").text
        assert "A tool library" in page and "from outside" in page
        assert "Open <b>1</b>" in page and "wt-state open" in page
        # a member closes it in the chat: under Closed now
        relay.events.append(
            {
                "id": "l" * 64,
                "kind": 1985,
                "pubkey": MEMBER,
                "created_at": card["created_at"] + 1,
                "content": "",
                "tags": [
                    ["h", "props"],
                    ["L", "belfry/inbox"],
                    ["l", "closed", "belfry/inbox"],
                    ["e", card["id"]],
                ],
            }
        )
        assert "A tool library" not in c.get("/ideas").text
        closed = c.get("/ideas?state=closed").text
        assert "A tool library" in closed and "Closed <b>1</b>" in closed
        relay.events.pop()
        # a member answers in the chat; Eve gets a mail; the page shows Mia as a member
        relay.events.append(
            {
                "id": "r" * 64,
                "kind": 1111,
                "pubkey": MEMBER,
                "created_at": card["created_at"] + 5,
                "content": "Great idea!",
                "tags": [
                    ["h", "props"],
                    ["E", card["id"], "", GK],
                    ["K", "9"],
                    ["e", card["id"]],
                    ["k", "9"],
                ],
            }
        )
        mailer.SENT.clear()
        assert webthreads.notify(svc, now=card["created_at"] + 60) == 1
        assert (
            "Mia: Great idea!" in mailer.SENT[-1].get_body(preferencelist=("plain",)).get_content()
        )
        assert webthreads.notify(svc, now=card["created_at"] + 120) == 0  # once
        t = c.get(mine.split("&")[0]).text
        assert "Great idea!" in t and "Mia" in t and 'class="wt-role member"' in t
        # Eve comments through her link; someone else can't
        assert c.post(f"/ideas/{card['id']}?k=nope", data={"text": "hi"}).status_code == 403
        c.post(mine.split("&")[0], data={"text": "Thanks!"}, follow_redirects=False)
        reply = [e for e in relay.events if e["kind"] == 1111 and e["pubkey"] == GK]
        assert (
            reply
            and reply[0]["content"] == "Thanks!"
            and ["E", card["id"], "", GK] in reply[0]["tags"]
        )
        # the confirmation link again: back to the same thread, no second post
        again = c.get(confirm_link, follow_redirects=False)
        assert again.headers["location"].startswith(f"/ideas/{card['id']}?k=")
        assert len([e for e in relay.events if e["kind"] == 9]) == 1


def test_externals_see_only_their_own_thread_when_the_list_is_private(world):
    svc, relay = world
    setting(relay, ADMIN, lst="own")  # a belfry admin may set it too
    with TestClient(create_app(svc), base_url="https://belfry.test") as c:
        page = c.get("/ideas").text
        # the requests template: the form first, nobody's list
        assert "Only they read what you send" in page and "Issues</h2>" not in page
        assert page.index("Send a request") < page.index("</form>")
        c.post(
            "/ideas",
            data={"title": "Secret", "text": "Hush", "name": "Eve", "email": "eve@example.org"},
        )
        r = c.get(link_in(mailer.SENT[-1]), follow_redirects=False)
        card = next(e for e in relay.events if e["kind"] == 9)
        assert c.get(f"/ideas/{card['id']}").status_code == 403
        assert c.get(r.headers["location"]).status_code == 200


def test_bots_wrong_addresses_and_reserved_slugs(world):
    svc, relay = world
    setting(relay, MEMBER, slug="api", at=11)
    assert webthreads.sites(svc, fresh=True) == {}
    setting(relay, MEMBER, at=12)
    with TestClient(create_app(svc), base_url="https://belfry.test") as c:
        r = c.post(
            "/ideas", data={"title": "x", "text": "y", "name": "z", "email": "not-an-address"}
        )
        assert r.status_code == 400 and "does not look right" in r.text
        mailer.SENT.clear()
        c.post(
            "/ideas",
            data={"title": "x", "text": "y", "name": "z", "email": "a@b.org", "website": "spam"},
        )
        assert not mailer.SENT
        # an off switch takes the page away
        setting(relay, MEMBER, on=False, at=13)
        assert "Open a thread" not in c.get("/ideas").text


def reply(relay, root, at, text, by=MEMBER):
    relay.events.append(
        {
            "id": hashlib.sha256(f"{root}{at}{text}".encode()).hexdigest(),
            "kind": 1111,
            "pubkey": by,
            "created_at": at,
            "content": text,
            "tags": [["h", "props"], ["E", root, "", GK], ["K", "9"], ["e", root], ["k", "9"]],
        }
    )


def test_replies_are_gathered_hourly_one_mail_per_address(world):
    """Xavier, 2026-11-11: mail is not real time; gather the replies, one mail at most an hour."""
    svc, relay = world
    setting(relay, MEMBER, vis="members")  # the channel's visibility, the newer form
    assert webthreads.sites(svc, fresh=True)["ideas"]["list"] == "own"
    with TestClient(create_app(svc), base_url="https://belfry.test") as c:
        cards = []
        for title in ("First", "Second"):
            c.post(
                "/ideas",
                data={"title": title, "text": "x", "name": "Eve", "email": "eve@example.org"},
            )
            c.get(link_in(mailer.SENT[-1]), follow_redirects=False)
            cards.append([e for e in relay.events if e["kind"] == 9][-1])
        t0 = max(e["created_at"] for e in cards)
        reply(relay, cards[0]["id"], t0 + 5, "One")
        reply(relay, cards[0]["id"], t0 + 9, "Two")
        reply(relay, cards[1]["id"], t0 + 12, "Three")
        mailer.SENT.clear()
        assert webthreads.notify(svc, now=t0 + 60) == 1  # one mail, both threads
        text = mailer.SENT[-1].get_body(preferencelist=("plain",)).get_content()
        assert "Mia: One" in text and "Mia: Two" in text and "Mia: Three" in text
        assert "2 threads" in text
        reply(relay, cards[0]["id"], t0 + 120, "Four")
        assert webthreads.notify(svc, now=t0 + 600) == 0  # not within the hour
        assert webthreads.notify(svc, now=t0 + 60 + 3600) == 1
        assert "Mia: Four" in mailer.SENT[-1].get_body(preferencelist=("plain",)).get_content()
        # the page lives on the public website only: the service's hosts send there
        r = c.get("https://pass.belfry.test/ideas?x=1", follow_redirects=False)
        assert r.status_code == 301 and r.headers["location"] == "https://belfry.test/ideas?x=1"


def test_an_account_for_the_author_from_the_web(world, monkeypatch):
    """Xavier, 2026-11-11: behind the confirmed address, an identity on the belfry and its
    signer, so the same pipes carry it: the thread and comments are theirs, signed as them."""
    svc, relay = world
    monkeypatch.setattr(webthreads, "web_identity", WEB_IDENTITY)
    made, admitted = [], []

    def ensure(svc, partner, number, own_pubkey=None):
        made.append((partner["id"], number))
        return EVE, "npub1eve", ""

    monkeypatch.setattr(core, "_ensure_keycast_user", ensure)
    monkeypatch.setattr(community, "member_signer", lambda svc, pid: (EVE, relay.sign_as(EVE)))
    monkeypatch.setattr(guests, "admitted", lambda svc, pass_id: admitted.append(pass_id) or True)
    setting(relay, MEMBER)
    with TestClient(create_app(svc), base_url="https://belfry.test") as c:
        for title in ("A tool library", "A repair café"):
            c.post(
                "/ideas",
                data={"title": title, "text": "Let's.", "name": "Eve", "email": "Eve@Example.org"},
            )
            r = c.get(link_in(mailer.SENT[-1]), follow_redirects=False)
        cards = [e for e in relay.events if e["kind"] == 9]
        assert [e["pubkey"] for e in cards] == [EVE, EVE]  # posted as her, both times
        assert all(not any(t[0] == "mail-from" for t in e["tags"]) for e in cards)
        assert ["subject", "A tool library"] in cards[0]["tags"]
        assert len(made) == 1  # one account; the second thread reuses it
        pid = made[0][0]
        assert svc.odoo.read_one("res.partner", pid, ["email"])["email"] == "eve@example.org"
        p = svc.odoo.search_read(
            "belfry.pass",
            [["partner_id", "=", pid]],
            ["tier", "guest_channel", "state", "keycast_pubkey"],
        )
        assert len(p) == 1 and (p[0]["tier"], p[0]["guest_channel"]) == ("guest", "props")
        assert (p[0]["state"], p[0]["keycast_pubkey"]) == ("active", EVE)
        assert admitted == [p[0]["id"]]  # into the channel, once
        assert mentions.prefs(svc, pid)["f"] == "off"  # the thread mail instead
        # her link: she reads and comments, as herself
        mine = r.headers["location"].split("&")[0]
        assert c.get(mine).status_code == 200
        c.post(mine, data={"text": "Thanks!"}, follow_redirects=False)
        comment = [e for e in relay.events if e["kind"] == 1111][-1]
        assert comment["pubkey"] == EVE and ["E", cards[1]["id"], "", EVE] in comment["tags"]
        # a member answers: the hourly mail goes to her proven address
        reply(relay, cards[1]["id"], cards[1]["created_at"] + 5, "Count me in")
        mailer.SENT.clear()
        assert webthreads.notify(svc, now=cards[1]["created_at"] + 60) == 1
        assert mailer.SENT[-1]["To"] == "eve@example.org"


def test_default_channels(world, monkeypatch):
    """Xavier, 2026-11-11: the channel says who sees it. 🌐 #events is public (the events page at
    <domain>/events), #proposals is for members; each takes its own type of post. The relay's own
    key sets them up. Since 2026-11-14 no #bookings and no #requests on new belfries."""
    svc, relay = world
    monkeypatch.setattr(followers, "_relay", lambda svc: (relay, lambda t: relay.sign(t)))
    relay.events = [e for e in relay.events if e["kind"] != 39000]
    made = followers.ensure_channels(svc)["made"]
    assert made == ["general", "events", "proposals"]
    assert not [
        e
        for e in relay.events
        if e["kind"] in (9002, 9007, 30078)
        and any(t[:2] in (["h", "bookings"], ["h", "requests"]) for t in e["tags"])
    ]
    for cid in made:  # the relay publishes 39000 for them, signed by its key
        relay.events.append(
            {
                "id": "meta-" + cid,
                "kind": 39000,
                "pubkey": GK,
                "created_at": 2,
                "tags": [["d", cid], ["name", cid]],
                "content": "",
            }
        )
    assert webthreads.sites(svc, fresh=True) == {}  # /events is the events page, proposals members'
    assert followers.ensure_channels(svc)["made"] == []  # once


def test_requests_channel_of_an_earlier_default_keeps_working(world, monkeypatch):
    """A belfry set up before 2026-11-14 has #requests and #bookings: they stay as they are, and
    the public /requests page still takes requests."""
    svc, relay = world
    monkeypatch.setattr(followers, "_relay", lambda svc: (relay, lambda t: relay.sign(t)))
    old = {
        "requests": {"on": True, "slug": "requests", "visibility": "public", "posts": "request"},
        "bookings": {"on": False, "slug": "bookings", "visibility": "members", "posts": "event"},
    }
    for cid in ("general", "events", "bookings", "requests", "proposals"):
        relay.events.append(
            {
                "id": "meta-" + cid,
                "kind": 39000,
                "pubkey": GK,
                "created_at": 2,
                "tags": [["d", cid], ["name", cid]],
                "content": "",
            }
        )
        if cid in old:
            relay.events.append(
                {
                    "id": "web-" + cid,
                    "kind": 30078,
                    "pubkey": GK,
                    "created_at": 3,
                    "content": json.dumps(old[cid]),
                    "tags": [["d", f"belfry:web:{cid}"], ["h", cid], ["t", "belfry-web"]],
                }
            )
    before = len(relay.events)
    assert followers.ensure_channels(svc)["made"] == []
    assert len(relay.events) == before  # nothing changed, nothing removed
    found = webthreads.sites(svc, fresh=True)
    assert sorted(found) == ["requests"]
    assert found["requests"]["posts"] == "request" and found["requests"]["list"] == "public"
    with TestClient(create_app(svc), base_url="https://belfry.test") as c:
        page = c.get("/requests").text
        assert "Make a request" in page and "Requests</h2>" in page
        r = c.post(
            "/requests",
            data={
                "title": "A bench",
                "text": "By the door",
                "name": "Eve",
                "email": "eve@example.org",
            },
        )
        assert r.status_code == 200 and "Check your e-mail" in r.text


def test_an_earlier_public_proposals_channel_becomes_members_only(world, monkeypatch):
    svc, relay = world
    monkeypatch.setattr(followers, "_relay", lambda svc: (relay, lambda t: relay.sign(t)))
    for cid in ("general", "events", "bookings", "requests", "proposals"):
        relay.events.append(
            {
                "id": "meta-" + cid,
                "kind": 39000,
                "pubkey": GK,
                "created_at": 2,
                "tags": [["d", cid], ["name", cid]],
                "content": "",
            }
        )
    relay.events.append(
        {
            "id": "old",
            "kind": 30078,
            "pubkey": GK,
            "created_at": 3,
            "content": json.dumps(
                {"on": True, "slug": "proposals", "visibility": "public", "posts": "proposal"}
            ),
            "tags": [["d", "belfry:web:proposals"], ["h", "proposals"], ["t", "belfry-web"]],
        }
    )
    assert "proposals" in webthreads.sites(svc, fresh=True)
    followers.ensure_channels(svc)
    assert "proposals" not in webthreads.sites(svc, fresh=True)  # members only now
    # a community that opened a public /proposals page itself keeps it
    setting(relay, MEMBER, slug="proposals", at=10**10)
    relay.events[-1]["tags"] = [
        ["d", "belfry:web:proposals"],
        ["h", "proposals"],
        ["t", "belfry-web"],
    ]
    relay.events.append(
        {
            "id": "m3",
            "kind": 39001,
            "pubkey": GK,
            "created_at": 4,
            "tags": [["d", "proposals"], ["p", MEMBER]],
            "content": "",
        }
    )
    followers.ensure_channels(svc)
    assert "proposals" in webthreads.sites(svc, fresh=True)


def test_old_default_descriptions_are_refreshed(world, monkeypatch):
    svc, relay = world
    monkeypatch.setattr(followers, "_relay", lambda svc: (relay, lambda t: relay.sign(t)))
    dom = followers.domain(svc)
    for cid, about in (
        ("proposals", f"Proposals for {dom}: one per thread, discussed and voted on."),
        ("events", "Mine"),
    ):
        relay.events.append(
            {
                "id": "meta-" + cid,
                "kind": 39000,
                "pubkey": GK,
                "created_at": 2,
                "tags": [["d", cid], ["name", cid], ["about", about]],
                "content": "",
            }
        )
    followers.ensure_channels(svc)
    edits = [e for e in relay.events if e["kind"] == 9002 and ["h", "proposals"] in e["tags"]]
    assert (
        edits
        and ["about", "Proposals members discuss and vote on. Members only."] in edits[-1]["tags"]
    )
    assert not [
        e for e in relay.events if e["kind"] == 9002 and ["h", "events"] in e["tags"]
    ]  # changed by people: kept


def test_moving_a_channel_is_never_retroactive(world):
    """#issues 2026-11-13: what was posted while members only stays private once it goes public,
    and what was posted while public stays public once it goes members only."""
    svc, relay = world

    def post(id_, at, text):
        relay.events.append(
            {
                "id": id_,
                "kind": 9,
                "pubkey": MEMBER,
                "created_at": at,
                "content": text,
                "tags": [["h", "props"], ["subject", text]],
            }
        )

    post("1" * 64, 100, "Before")
    post("2" * 64, 300, "During")
    post("3" * 64, 600, "After")
    relay.events.append(
        {
            "id": "s1",
            "kind": 30078,
            "pubkey": ADMIN,
            "created_at": 700,
            "content": json.dumps(
                {"on": True, "slug": "ideas", "visibility": "members", "periods": [[200, 500]]}
            ),
            "tags": [["d", "belfry:web:props"], ["h", "props"], ["t", "belfry-web"]],
        }
    )
    webthreads._cache.update(at=0.0, sites={})
    with TestClient(create_app(svc), base_url="https://belfry.test") as c:
        page = c.get("/ideas").text
        assert "During" in page and "Before" not in page and "After" not in page
        assert "posted while the channel was public" in page
        assert c.get("/ideas/" + "2" * 64).status_code == 200
        assert c.get("/ideas/" + "1" * 64).status_code == 403
        assert c.get("/ideas/" + "3" * 64).status_code == 403
