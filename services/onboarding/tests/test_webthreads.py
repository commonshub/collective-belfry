"""A channel's public page (webthreads.py): /<slug> lists threads, anyone opens one after
confirming their address, members' replies are mailed to them, and the list can be private."""

from __future__ import annotations

import hashlib
import json
import re

import pytest
from fastapi.testclient import TestClient

from belfry_onboarding import apps, followers, mail, mailer, webthreads, whois
from belfry_onboarding.app import create_app
from belfry_onboarding.register import Register

ADMIN, MEMBER, GK = "a" * 64, "b" * 64, "f" * 64


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
        assert "No threads yet" in page.text
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
        assert "between you and the members" in page and "Threads</h2>" not in page
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
