"""Printed cards (cards.py, cardsconsole.py; docs/cards.md): the id, what a card says it is, the
first scan by a signed-in account and by a visitor with a code, one group whoever races, a
topic's hashtag, a guest as the steward of their own group, retiring, archived groups, the
console's batches, print view and Reset."""

# ruff: noqa: F811
from __future__ import annotations

import datetime as dt
import re
import sqlite3
import threading
import time

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import (
    applyverified,
    cards,
    cardsconsole,
    cardspdf,
    checkin,
    events,
    followers,
    guests,
    mail,
    mailer,
    roles,
    usernames,
)
from belfry_onboarding.app import create_app
from belfry_onboarding.holder_invites import ApiError
from belfry_onboarding.places import member_key
from belfry_onboarding.register import Register

from .test_events import World
from .test_guests import _code, published, rsvc  # noqa: F401 — fixtures
from .test_roles import call, person, pk, world  # noqa: F401 — fixtures
from .test_sales_console import CSRF, H, post, signed_in

HOST = {"Host": "belfry.belfry.test"}


class Land:
    def __init__(self, rsvc, keycast, ada, ada_id, relay: World):
        self.rsvc, self.keycast, self.ada, self.ada_id, self.w = rsvc, keycast, ada, ada_id, relay
        self.channels: set[str] = set()

    def card(self, kind: str = "group") -> str:
        key = cardsconsole.make(self.rsvc, kind, "Test batch", 1, self.ada_id)
        return next(b for b in cardsconsole.batches(self.rsvc) if b["key"] == key)["cards"][0]["id"]

    def of(self, kind: int, group: str | None = None) -> list[dict]:
        return [
            e
            for e in self.w.grp.events
            if e["kind"] == kind and (group is None or ["h", group] in e["tags"])
        ]

    def mirror(self, group: str) -> None:
        """What the relay does with a 9002: its 39000 (the fake relay keeps what it is given)."""
        last = self.of(9002, group)[-1]
        tags = [["d", group], *[t for t in last["tags"] if t[0] != "h"]]
        self.w.put(self.w.gkey, 39000, tags)


@pytest.fixture
def land(world, monkeypatch) -> Land:
    rsvc, _published, keycast, ada, ada_id, _stewards = world
    rsvc.settings.pass_page_secret = "p" * 32
    rsvc.settings.pass_page_oidc_client_secret = "pass-secret"
    w = World()
    out = Land(rsvc, keycast, ada, ada_id, w)
    monkeypatch.setattr(events, "community", lambda svc, f: w.comm.query(f, None))
    monkeypatch.setattr(events, "groups", lambda svc, f: w.grp.query(f, None))

    def publish(svc, kind, tags, content=""):
        w.put(w.gkey, kind, tags, content)
        return True

    monkeypatch.setattr(events, "publish", publish)
    monkeypatch.setattr(guests, "_channel_event", lambda svc, kind, tags: publish(svc, kind, tags))
    monkeypatch.setattr(checkin, "groups_pubkey", lambda svc: pk(w.gkey))
    # the world's relay stand-in calls every word a channel: here only the ones a test names
    monkeypatch.setattr(mail, "channel_exists", lambda svc, name: name in out.channels)
    monkeypatch.setattr(mail, "channel_by_slug", lambda svc, name: None)
    for limiter in (cards.LOOKUPS, cards.HASHTAGS, applyverified.PER_IP):
        limiter._seen.clear()
    usernames._taken.clear()
    cards._locks.clear()
    return out


def get(c, path):
    return c.get("/api/v1" + path, headers=HOST)


def saved_guest(rsvc, email="gus@example.org", name="Gus Guest", channel="garden") -> PrivateKey:
    """A guest of one channel with a name and a proven address; our key stands in for theirs."""
    key = PrivateKey()
    reg = rsvc.odoo
    pid = reg.create("res.partner", {"name": name, "email": email})
    today = dt.datetime.now(dt.UTC).date()
    reg.create(
        "belfry.pass",
        {
            "partner_id": pid,
            "tier": "guest",
            "guest_channel": channel,
            "guest_days": 30,
            "keycast_pubkey": pk(key),
            "state": "active",
            "date_start": today.isoformat(),
            "date_end": (today + dt.timedelta(days=30)).isoformat(),
        },
    )
    return key


FORM = {"title": "Sourdough starters", "about": "Swap and feed"}


def visitor_claim(c, card, email, name="Vera Visitor", key=None, **form):
    key = key or PrivateKey()
    r = call(c, key, f"/cards/{card}/claim-start", {"email": email})
    assert r.status_code == 202, r.text
    body = {"email": email, "code": _code(), "name": name, **(form or FORM)}
    return key, call(c, key, f"/cards/{card}/claim-verify", body)


# ── ids ─────────────────────────────────────────────────────────────────────────────────────
def test_ids_are_six_characters_without_lookalikes():
    assert len(cards.ALPHABET) == 31 and not set(cards.ALPHABET) & set("OIL01")
    assert set(cards.ALPHABET) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ23456789")
    for _ in range(300):
        cid = cards.new_id()
        assert len(cid) == 6 and cards.ID.fullmatch(cid) and set(cid) <= set(cards.ALPHABET)
    for bad in ("ABCDE", "ABCDEFG", "ABCDEO", "ABCDE1", "ABCDE0", "ABCDEI", "ABCDEL", "abcdef"):
        assert not cards.ID.fullmatch(bad), bad


def test_the_table_is_added_to_a_register_made_before_it(tmp_path, settings):
    path = tmp_path / "old.sqlite"
    reg = Register(path, settings)
    reg._db.execute("DROP TABLE group_cards")
    reg._db.close()
    reg = Register(path, settings)
    assert reg.card_make(["ABCDEF"], "group", "B", 1, 1) == ["ABCDEF"]
    assert reg.card_make(["ABCDEF"], "group", "B", 1, 1) == []  # an id is made once
    db = sqlite3.connect(path)
    assert db.execute("SELECT state FROM group_cards").fetchall() == [("unclaimed",)]


def test_cards_need_the_own_register(client):
    r = client.get("/api/cards/ABCDEF")
    assert r.status_code == 409 and r.json()["error"] == "unsupported"
    r = client.get("/api/cards/hashtag/bread")
    assert r.status_code == 409 and r.json()["error"] == "unsupported"


# ── what a card is ──────────────────────────────────────────────────────────────────────────
def test_lookup_unknown_malformed_unclaimed_and_the_limit(land):
    card = land.card()
    topic = land.card("topic")
    with TestClient(create_app(land.rsvc)) as c:
        assert get(c, f"/cards/{card}").json() == {"kind": "group", "state": "unclaimed"}
        assert get(c, f"/cards/{card.lower()}").json() == {"kind": "group", "state": "unclaimed"}
        assert get(c, f"/cards/{topic}").json() == {"kind": "topic", "state": "unclaimed"}
        missing = next(i for i in ("ABCDEF", "GHJKMN") if i not in (card, topic))
        for bad in (missing, "ABC", "ABCDEFGH", "ABCDE0", "%3Cb%3E"):
            r = get(c, f"/cards/{bad}")
            assert r.status_code == 404 and r.json()["error"] == "not_found", bad
            assert "<b>" not in r.text
        r = c.options(
            f"/api/v1/cards/{card}",
            headers={
                **HOST,
                "Origin": "https://chat.belfry.test",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert r.status_code == 204
        # the community's website (and anything published there) is not let in
        r = c.options(
            f"/api/v1/cards/{card}/claim-verify",
            headers={
                **HOST,
                "Origin": "https://belfry.test",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert r.status_code == 403
        cards.LOOKUPS._seen.clear()
        codes = [get(c, f"/cards/{card}").status_code for _ in range(31)]
        assert codes[:30] == [200] * 30 and codes[30] == 429


# ── a signed-in account starts a group ──────────────────────────────────────────────────────
def test_a_member_starts_a_group_from_a_card(land):
    rsvc, w = land.rsvc, land.w
    bob, bob_id = person(rsvc, "bob@belfry.test", "Bob Baker")
    card = land.card()
    with TestClient(create_app(rsvc)) as c:
        for body, field in (
            ({"title": "", "about": ""}, "title"),
            ({"title": "x" * 81}, "title"),
            ({"title": "Ok", "about": "y" * 501}, "about"),
        ):
            r = call(c, bob, f"/cards/{card}/claim", body)
            assert r.status_code == 422 and r.json()["error"] == "invalid", r.text
            assert r.json()["field"] == field
        assert not land.of(9007)
        r = call(c, bob, f"/cards/{card.lower()}/claim", FORM)
        assert r.status_code == 200, r.text
        group = r.json()["group"]
        assert r.json() == {"group": group}
        assert re.fullmatch(r"pod-sourdough-starters-[a-z0-9]{4}", group)
        # by the groups key, with the tags the chat's New group writes
        made, meta = land.of(9007, group), land.of(9002, group)
        assert len(made) == 1 and made[0]["pubkey"] == pk(w.gkey)
        # ...and `by` Bob on each: the events the belfry writes for a person say whose they are
        by = ["by", pk(bob)]
        assert made[0]["tags"] == [["h", group], ["open"], ["public"], by]
        assert meta[0]["tags"] == [
            ["h", group],
            ["name", "Sourdough starters"],
            ["about", "Swap and feed"],
            ["open"],
            ["public"],
            by,
        ]
        # announced in general exactly as the chat does
        said = [e for e in land.of(9, "general") if ["pod", group] in e["tags"]]
        assert len(said) == 1
        assert said[0]["content"] == "⬡ New group: Sourdough starters · Swap and feed"
        assert said[0]["tags"] == [["h", "general"], ["pod", group], by]
        # Bob is in it, with the admin role; the first line names him, never the card
        put = land.of(9000, group)
        assert len(put) == 1 and ["p", pk(bob), "admin"] in put[0]["tags"]
        assert ["role", "admin"] in put[0]["tags"] and ["name", "Bob Baker"] in put[0]["tags"]
        assert not any(t[0] == "guest" for t in put[0]["tags"])
        st = checkin.group_state(rsvc, group)
        assert pk(bob) in st.admins and pk(bob) in st.members
        line = land.of(9, group)
        assert [e["content"] for e in line] == ["Bob Baker started this group from a printed card"]
        assert line[0]["tags"] == [["h", group], ["p", pk(bob)], ["card", "1"], by]
        assert by in put[0]["tags"]
        assert not any(card in str(e) for e in w.grp.events)
        # the card is the group's guest link now: no end date, no number of people
        land.mirror(group)
        got = get(c, f"/cards/{card}").json()
        assert got == {
            "kind": "group",
            "state": "claimed",
            "group": group,
            "title": "Sourdough starters",
            "invite": got["invite"],
        }
        inv = rsvc.odoo.invite_by_token(got["invite"])
        assert inv["state"] == "open" and not inv["expires_at"] and inv["guest_unlimited"]
        assert inv["guest_channel"] == group and inv["guest_inviter"] == pk(bob)
        shown = c.get(f"/api/v1/channel-invites/{got['invite']}", headers=HOST).json()
        assert shown["channel"] == group and shown["state"] == "open" and shown["as"] == "guest"
        assert shown["inviter"] == "Bob Baker"
        # a second claim: theirs, and nothing new on the relay
        n = len(w.grp.events)
        cy, _ = person(rsvc, "cy@belfry.test", "Cy")
        r = call(c, cy, f"/cards/{card}/claim", {"title": "Mine"})
        assert r.status_code == 409 and r.json()["error"] == "claimed"
        assert r.json()["group"] == group and r.json()["invite"] == got["invite"]
        assert len(w.grp.events) == n
        # a renamed group: the card says its name of today
        w.put(w.gkey, 39000, [["d", group], ["name", "Bread club"]], at=2_000_000_000)
        assert get(c, f"/cards/{card}").json()["title"] == "Bread club"


def test_the_link_of_a_card_never_fills_up(land):
    rsvc = land.rsvc
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    card = land.card()
    with TestClient(create_app(rsvc)) as c:
        group = call(c, bob, f"/cards/{card}/claim", FORM).json()["group"]
        token = get(c, f"/cards/{card}").json()["invite"]
        for i in range(52):  # more than a guest link's fifty
            r = call(c, PrivateKey(), f"/channel-invites/{token}/join", {"name": f"Guest {i}"})
            assert r.status_code == 200, r.text
        inv = rsvc.odoo.invite_by_token(token)
        assert inv["state"] == "open" and inv["guest_uses"] == 52
        # a member who joins by the card counts too, and the link stays open
        cy, _ = person(rsvc, "cy@belfry.test", "Cy")
        r = call(c, cy, f"/channel-invites/{token}/join", {})
        assert r.status_code == 200 and r.json()["guest"] is False
        assert rsvc.odoo.invite_by_token(token)["state"] == "open"
        r = call(c, bob, f"/groups/{group}/card", method="GET")
        assert r.status_code == 200, r.text
        assert r.json() == {
            "id": card,
            "kind": "group",
            "started_by": {"name": "Bob", "pubkey": pk(bob)},
            "joined_by_scan": 53,
        }
        # guests keep their 30 days
        p = rsvc.odoo.search_read(
            "belfry.pass", [["guest_channel", "=", group]], ["date_end"], limit=1
        )
        end = (dt.datetime.now(dt.UTC).date() + dt.timedelta(days=30)).isoformat()
        assert p[0]["date_end"] == end


def test_who_may_not_claim(land):
    rsvc = land.rsvc
    card = land.card()
    with TestClient(create_app(rsvc)) as c:
        # nobody's key
        r = call(c, PrivateKey(), f"/cards/{card}/claim", FORM)
        assert r.status_code == 403 and r.json()["error"] == "no_pass"
        # an agent
        bob, bob_id = person(rsvc, "bob@belfry.test", "Bob")
        bot = PrivateKey()
        rsvc.odoo.create(
            "belfry.pass",
            {"partner_id": bob_id, "tier": "agent", "keycast_pubkey": pk(bot), "state": "active"},
        )
        r = call(c, bot, f"/cards/{card}/claim", FORM)
        assert r.status_code == 403 and r.json()["error"] == "agent"
        # a guest who only reads, and one who gave a name but no address
        reader = saved_guest(rsvc, email="", name="Guest")
        rows = rsvc.odoo.search_read("belfry.pass", [["keycast_pubkey", "=", pk(reader)]], ["id"])
        rsvc.odoo.write("belfry.pass", [rows[0]["id"]], {"guest_unnamed": True})
        r = call(c, reader, f"/cards/{card}/claim", FORM)
        assert r.status_code == 403 and r.json()["error"] == "guest_not_saved"
        named = saved_guest(rsvc, email="", name="Nell")
        r = call(c, named, f"/cards/{card}/claim", FORM)
        assert r.status_code == 403 and r.json()["error"] == "guest_not_saved"
        assert not land.of(9007)
        assert get(c, f"/cards/{card}").json()["state"] == "unclaimed"


# ── a guest is the steward of the group they started, and of that group only ────────────────
def test_a_saved_guest_starts_a_group_and_is_its_steward_only(land):
    rsvc, w = land.rsvc, land.w
    gus = saved_guest(rsvc)
    ada = land.ada
    # another group, which Gus is merely in
    w.put(ada, 9007, [["h", "pod-other"]])
    w.put(w.gkey, 39000, [["d", "pod-other"], ["name", "Other"]])
    w.put(w.gkey, 9000, [["h", "pod-other"], ["p", pk(gus), "member"]])
    card = land.card()
    with TestClient(create_app(rsvc)) as c:
        r = call(c, gus, f"/cards/{card}/claim", FORM)
        assert r.status_code == 200, r.text
        group = r.json()["group"]
        land.mirror(group)
        put = land.of(9000, group)[0]
        assert ["p", pk(gus), "admin"] in put["tags"] and ["role", "admin"] in put["tags"]
        assert ["guest", "2099-12-31"] in put["tags"]  # theirs to keep (no end while the card is)
        # still a guest: a guest pass for the group, no membership
        assert member_key(rsvc, pk(gus)) is None
        passes = rsvc.odoo.search_read(
            "belfry.pass",
            [["keycast_pubkey", "=", pk(gus)], ["state", "=", "active"]],
            ["tier", "guest_channel"],
        )
        assert {(p["tier"], p["guest_channel"]) for p in passes} == {
            ("guest", "garden"),
            ("guest", group),
        }
        # the steward of their group: its name, its roles, its card
        r = call(c, gus, "/group-meta", {"channel": group, "name": "Bread club"})
        assert r.status_code == 200, r.text
        r = call(c, gus, f"/groups/{group}/card", method="GET")
        assert r.status_code == 200 and r.json()["started_by"]["name"] == "Gus Guest"
        w.put(w.gkey, 9000, [["h", group], ["p", pk(ada), "member"]])
        r = call(c, gus, "/group-roles", {"channel": group, "pubkey": pk(ada), "role": "removed"})
        assert r.status_code != 403, r.text
        # of that group only
        r = call(c, gus, "/group-meta", {"channel": "pod-other", "name": "Mine now"})
        assert r.status_code == 403 and r.json()["error"] == "not_allowed"
        r = call(
            c, gus, "/group-roles", {"channel": "pod-other", "pubkey": pk(gus), "role": "admin"}
        )
        assert r.status_code == 403
        r = call(c, gus, "/groups/pod-other/card", method="GET")
        assert r.status_code == 403
        # and no rights in the belfry: guests still invite nobody
        r = call(c, gus, "/channel-invites", {"channel": group})
        assert r.status_code == 403 and r.json()["error"] == "guest"
        assert pk(gus) not in checkin._belfry_admins(rsvc)


# ── a visitor: e-mail, code, signed in, the group ───────────────────────────────────────────
def test_a_visitor_with_a_new_address_becomes_a_guest_and_the_steward(land):
    rsvc, w = land.rsvc, land.w
    card = land.card()
    with TestClient(create_app(rsvc)) as c:
        key = PrivateKey()
        r = call(c, key, f"/cards/{card}/claim-start", {"email": "not an address"})
        assert r.status_code == 422 and r.json()["field"] == "email"
        n_mail = len(mailer.SENT)
        r = call(c, key, f"/cards/{card}/claim-start", {"email": "vera@example.org"})
        assert r.status_code == 202 and r.json() == {"sent": True}
        assert len(mailer.SENT) == n_mail + 1
        # nothing is kept before the code is right
        assert not rsvc.odoo.search_read("res.partner", [["email", "=", "vera@example.org"]], [])
        assert not land.of(9007)
        code = _code()
        good = {"email": "vera@example.org", "code": code, "name": "Vera Visitor", **FORM}
        # the form is checked before the code is spent
        for body, field in (
            ({**good, "name": ""}, "name"),
            ({**good, "title": " "}, "title"),
            ({**good, "email": "nope"}, "email"),
        ):
            r = call(c, key, f"/cards/{card}/claim-verify", body)
            assert r.status_code == 422 and r.json()["field"] == field, r.text
        wrong = "000000" if code != "000000" else "111111"
        r = call(c, key, f"/cards/{card}/claim-verify", {**good, "code": wrong})
        assert r.status_code == 400 and r.json()["error"] == "wrong_code"
        assert r.json()["tries_left"] == 4
        # another browser's key can't use her code
        r = call(c, PrivateKey(), f"/cards/{card}/claim-verify", good)
        assert r.status_code == 410 and r.json()["error"] == "expired"
        assert not land.of(9007)
        r = call(c, key, f"/cards/{card}/claim-verify", good)
        assert r.status_code == 200, r.text
        out = r.json()
        group = out["group"]
        assert out["signed_in"] is True and out["bunker_url"].startswith("bunker://")
        assert re.fullmatch(r"[0-9a-f]{64}", out["pubkey"]) and out["npub"].startswith("npub1")
        assert set(out) <= {"signed_in", "bunker_url", "pubkey", "npub", "nip05", "group"}
        # a saved guest: her name, her proven address, a guest pass of her group with no end
        partner = rsvc.odoo.search_read(
            "res.partner", [["email", "=", "vera@example.org"]], ["name"]
        )
        assert [p["name"] for p in partner] == ["Vera Visitor"]
        passes = rsvc.odoo.search_read(
            "belfry.pass", [["partner_id", "=", partner[0]["id"]]], ["tier", "state", "date_end"]
        )
        end = "2099-12-31"
        assert [(p["tier"], p["state"], p["date_end"]) for p in passes] == [
            ("guest", "active", end)
        ]
        assert guests._saved_email(rsvc, out["pubkey"]) and member_key(rsvc, out["pubkey"]) is None
        # the steward of her group
        put = land.of(9000, group)
        assert len(put) == 1 and ["p", out["pubkey"], "admin"] in put[0]["tags"]
        assert ["guest", end] in put[0]["tags"] and ["name", "Vera Visitor"] in put[0]["tags"]
        assert out["pubkey"] in checkin.group_state(rsvc, group).admins
        assert [e["content"] for e in land.of(9, group)] == [
            "Vera Visitor started this group from a printed card"
        ]
        assert rsvc.odoo.card_get(card)["state"] == "claimed"
        # the code is used
        r = call(c, key, f"/cards/{card}/claim-verify", good)
        assert r.status_code == 409 and r.json()["error"] == "claimed"
        assert len(land.of(9007)) == 1 and w.grp.events


def test_the_session_is_bound_to_the_key_that_signed(land, monkeypatch):
    rsvc = land.rsvc
    bound = []
    real = rsvc.keycast.create_bunker

    def create(token, label, client_pubkey=None):
        bound.append(client_pubkey)
        return real(token, label, client_pubkey)

    monkeypatch.setattr(rsvc.keycast, "create_bunker", create)
    with TestClient(create_app(rsvc)) as c:
        key, r = visitor_claim(c, land.card(), "vera@example.org")
        assert r.status_code == 200, r.text
        assert bound == [pk(key)]
        # the same address on another card, from another browser: the same account, signed in
        key2, r2 = visitor_claim(c, land.card(), "vera@example.org", name="Someone Else")
        assert r2.status_code == 200, r2.text
        assert r2.json()["pubkey"] == r.json()["pubkey"] and r2.json()["signed_in"] is True
        assert r2.json()["bunker_url"].startswith("bunker://") and "nip05" in r2.json()
        assert bound == [pk(key), pk(key2)]
        people = rsvc.odoo.search_read(
            "res.partner", [["email", "=", "vera@example.org"]], ["name"]
        )
        assert [p["name"] for p in people] == ["Vera Visitor"]  # one account, her name kept
        # a guest still, and the steward of both groups
        g1, g2 = r.json()["group"], r2.json()["group"]
        assert g1 != g2
        for g in (g1, g2):
            assert r.json()["pubkey"] in checkin.group_state(rsvc, g).admins
        tags = land.of(9000, g2)[0]["tags"]
        assert any(t[0] == "guest" for t in tags)


def test_a_visitor_whose_address_is_a_members_starts_it_as_that_member(land):
    rsvc = land.rsvc
    with TestClient(create_app(rsvc)) as c:
        _key, r = visitor_claim(c, land.card(), "vera@example.org")
        me = r.json()["pubkey"]
        assert roles.make_member(rsvc, me, roles.default_role(rsvc), 0)
        assert member_key(rsvc, me)
        _key2, r2 = visitor_claim(c, land.card(), "vera@example.org")
        assert r2.status_code == 200, r2.text
        assert r2.json()["pubkey"] == me
        tags = land.of(9000, r2.json()["group"])[0]["tags"]
        assert ["p", me, "admin"] in tags and not any(t[0] == "guest" for t in tags)


def test_an_account_with_its_own_key_and_an_agent_are_refused(land, monkeypatch):
    rsvc = land.rsvc
    # Bob's key is his own here (the signer holds none for him)
    person(rsvc, "bob@belfry.test", "Bob")
    card = land.card()
    with TestClient(create_app(rsvc)) as c:
        monkeypatch.setattr(guests, "_sign_in_as", lambda *a: None)
        _k, r = visitor_claim(c, card, "bob@belfry.test")
        assert r.status_code == 409 and r.json()["error"] == "own_key"
        monkeypatch.setattr(guests, "_is_agent", lambda svc, pubkey: True)
        _k, r = visitor_claim(c, card, "bob@belfry.test")
        assert r.status_code == 409 and r.json()["error"] == "agent"
        assert not land.of(9007) and get(c, f"/cards/{card}").json()["state"] == "unclaimed"


def test_a_key_linked_to_another_account_is_refused_and_nothing_stays(land, monkeypatch):
    from belfry_onboarding.clients.keycast import ClientKeyTaken

    rsvc = land.rsvc
    card = land.card()

    def taken(*a, **k):
        raise ClientKeyTaken("taken")

    with TestClient(create_app(rsvc)) as c:
        monkeypatch.setattr(rsvc.keycast, "create_bunker", taken)
        _k, r = visitor_claim(c, card, "vera@example.org")
        assert r.status_code == 409 and r.json()["error"] == "key_taken", r.text
        assert not land.of(9007) and rsvc.odoo.card_get(card)["state"] == "unclaimed"
        rows = rsvc.odoo.search_read("belfry.pass", [["tier", "=", "guest"]], ["state"])
        assert [p["state"] for p in rows] == ["revoked"]


def test_a_relay_that_refuses_leaves_no_card_claimed_and_no_guest(land, monkeypatch):
    rsvc = land.rsvc
    card = land.card()
    real = events.publish
    monkeypatch.setattr(
        events, "publish", lambda svc, kind, tags, content="": kind not in (9002, 9007)
    )
    with TestClient(create_app(rsvc)) as c:
        _k, r = visitor_claim(c, card, "vera@example.org")
        assert r.status_code == 503 and r.json()["error"] == "relay", r.text
        assert rsvc.odoo.card_get(card)["state"] == "unclaimed"
        rows = rsvc.odoo.search_read("belfry.pass", [["tier", "=", "guest"]], ["state"])
        assert [p["state"] for p in rows] == ["revoked"]
        # the relay is back: a new code, and it works
        monkeypatch.setattr(events, "publish", real)
        _k, r = visitor_claim(c, card, "vera@example.org")
        assert r.status_code == 200, r.text
        assert len(land.of(9007)) == 1


def test_two_people_race_for_a_card_and_one_group_is_made(land, monkeypatch):
    rsvc = land.rsvc
    card = land.card()
    both = threading.Barrier(2, timeout=10)
    real = cards._check_code

    def checked(*a):
        real(*a)
        both.wait()  # both codes are right before either takes the card

    monkeypatch.setattr(cards, "_check_code", checked)
    people = []
    for email, name in (("vera@example.org", "Vera"), ("walt@example.org", "Walt")):
        key = PrivateKey()
        cards.claim_start(rsvc, card, pk(key), {"email": email}, "198.51.100.7")
        people.append((key, {"email": email, "code": _code(), "name": name, **FORM}))
    results: list = []

    def run(key, body):
        try:
            results.append(cards.claim_verify(rsvc, card, pk(key), body))
        except ApiError as exc:
            results.append(exc)

    threads = [threading.Thread(target=run, args=p) for p in people]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    won = [r for r in results if isinstance(r, dict)]
    lost = [r for r in results if isinstance(r, ApiError)]
    assert len(won) == 1 and len(lost) == 1, results
    assert lost[0].status == 409 and lost[0].error == "claimed"
    assert lost[0].extra["group"] == won[0]["group"]
    assert lost[0].extra["invite"] == rsvc.odoo.card_get(card)["invite"]
    # the loser's group was never made, and neither was the loser
    assert len(land.of(9007)) == 1 and len(land.of(9000)) == 1
    assert len(rsvc.odoo.search_read("belfry.pass", [["tier", "=", "guest"]], ["id"])) == 1


# ── topics ──────────────────────────────────────────────────────────────────────────────────
def test_a_topic_card_makes_a_channel_named_by_its_hashtag(land):
    rsvc, w = land.rsvc, land.w
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    card = land.card("topic")
    land.channels.add("garden")
    w.put(w.gkey, 39000, [["d", "bikes"], ["name", "Bikes"]])
    w.put(land.ada, 9008, [["h", "gone"]])  # a deleted channel's id is not given again
    with TestClient(create_app(rsvc)) as c:
        states = {
            "bread": "free",
            "bread-2": "free",
            "bikes": "taken",
            "garden": "taken",
            "gone": "taken",
            "general": "taken",
            "hello": "taken",
            "postmaster": "taken",
            "pod-cast": "taken",
            "guest-12": "taken",
            "b": "invalid",
            "Bread": "invalid",
            "-bread": "invalid",
            "bread_": "invalid",
            "x" * 31: "invalid",
        }
        for name, state in states.items():
            assert get(c, f"/cards/hashtag/{name}").json() == {"state": state}, name
        for body in (FORM, {**FORM, "hashtag": "Not A Slug"}, {**FORM, "hashtag": "x"}):
            r = call(c, bob, f"/cards/{card}/claim", body)
            assert r.status_code == 422 and r.json()["field"] == "hashtag", r.text
        for name in ("bikes", "garden", "hello"):
            r = call(c, bob, f"/cards/{card}/claim", {**FORM, "hashtag": name})
            assert r.status_code == 409 and r.json()["error"] == "hashtag_taken", name
        assert not land.of(9007)
        r = call(c, bob, f"/cards/{card}/claim", {**FORM, "hashtag": "bread"})
        assert r.status_code == 200 and r.json() == {"group": "bread", "summary": "weekly"}
        # a channel as the chat's New channel makes one (no access tags), named by its hashtag,
        # the topic in words leading its description, announced in the town square
        by = ["by", pk(bob)]
        assert land.of(9007, "bread")[0]["tags"] == [["h", "bread"], by]
        assert land.of(9002, "bread")[0]["tags"] == [
            ["h", "bread"],
            ["name", "bread"],
            ["about", "Sourdough starters · Swap and feed"],
            by,
        ]
        assert by in land.of(9, "bread")[0]["tags"]
        said = land.of(9, "general")
        assert len(said) == 1 and ["channel", "bread"] in said[0]["tags"] and by in said[0]["tags"]
        head = "# New topic: #bread · Sourdough starters · Swap and feed"
        assert said[0]["content"].startswith(head)
        link = followers.chat_link(rsvc, "bread")
        assert not link or link in said[0]["content"]
        assert [e["content"] for e in land.of(9, "bread")] == [
            "Bob started this topic from a printed card"
        ]
        assert pk(bob) in checkin.group_state(rsvc, "bread").admins
        assert get(c, "/cards/hashtag/bread").json() == {"state": "taken"}
        got = get(c, f"/cards/{card}").json()
        assert got["kind"] == "topic" and got["group"] == "bread" and got["state"] == "claimed"
        # a second topic card can't take the same hashtag, for a visitor before the code is spent
        other = land.card("topic")
        key = PrivateKey()
        call(c, key, f"/cards/{other}/claim-start", {"email": "vera@example.org"})
        body = {"email": "vera@example.org", "code": _code(), "name": "Vera", **FORM}
        r = call(c, key, f"/cards/{other}/claim-verify", {**body, "hashtag": "bread"})
        assert r.status_code == 409 and r.json()["error"] == "hashtag_taken"
        r = call(c, key, f"/cards/{other}/claim-verify", {**body, "hashtag": "rye"})
        assert r.status_code == 200 and r.json()["group"] == "rye"


def _follows(rsvc, group):
    return [
        (r["frequency"], r["state"])
        for r in rsvc.odoo.search_read(
            "belfry.follower", [["channel", "=", group]], ["frequency", "state"]
        )
    ]


def test_whoever_starts_a_topic_follows_it_weekly_unless_they_pick_monthly(land):
    rsvc = land.rsvc
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    cy, _ = person(rsvc, "cy@belfry.test", "Cy")
    with TestClient(create_app(rsvc)) as c:
        card = land.card("topic")
        r = call(c, bob, f"/cards/{card}/claim", {**FORM, "hashtag": "bread", "summary": "daily"})
        assert r.status_code == 422 and r.json()["field"] == "summary"
        r = call(c, bob, f"/cards/{card}/claim", {**FORM, "hashtag": "bread"})
        assert r.json() == {"group": "bread", "summary": "weekly"}
        assert _follows(rsvc, "bread") == [("weekly", "active")]
        card = land.card("topic")
        r = call(c, cy, f"/cards/{card}/claim", {**FORM, "hashtag": "rye", "summary": "monthly"})
        assert r.json() == {"group": "rye", "summary": "monthly"}
        assert _follows(rsvc, "rye") == [("monthly", "active")]
        # a visitor with a code: their proven address follows it
        card = land.card("topic")
        _key, r = visitor_claim(
            c, card, "vera@example.org", **FORM, hashtag="oats", summary="monthly"
        )
        assert r.status_code == 200 and r.json()["summary"] == "monthly", r.text
        assert _follows(rsvc, "oats") == [("monthly", "active")]
        # a group card: nobody follows by e-mail
        card = land.card()
        r = call(c, bob, f"/cards/{card}/claim", FORM)
        assert r.status_code == 200 and "summary" not in r.json()
        assert not _follows(rsvc, r.json()["group"])


# ── retiring, archiving ─────────────────────────────────────────────────────────────────────
def test_retiring_a_card_stops_its_link(land):
    rsvc = land.rsvc
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    cy, _ = person(rsvc, "cy@belfry.test", "Cy")
    card = land.card()
    free = land.card()
    with TestClient(create_app(rsvc)) as c:
        group = call(c, bob, f"/cards/{card}/claim", FORM).json()["group"]
        land.mirror(group)
        token = get(c, f"/cards/{card}").json()["invite"]
        # not a member of the group, not a stranger; an unclaimed card: the belfry's admins only
        r = call(c, cy, f"/cards/{card}/retire", {})
        assert r.status_code == 403 and r.json()["error"] == "not_allowed"
        assert call(c, PrivateKey(), f"/cards/{card}/retire", {}).status_code == 403
        assert call(c, bob, f"/cards/{free}/retire", {}).status_code == 403
        assert call(c, cy, f"/groups/{group}/card", method="GET").status_code == 403
        assert call(c, bob, "/cards/ABCDE1/retire", {}).status_code == 404
        r = call(c, bob, f"/cards/{card}/retire", {})
        assert r.status_code == 200 and r.json() == {"state": "retired"}
        assert get(c, f"/cards/{card}").json() == {
            "kind": "group",
            "state": "retired",
            "title": "Sourdough starters",
        }
        assert rsvc.odoo.invite_by_token(token)["state"] == "revoked"
        r = call(c, PrivateKey(), f"/channel-invites/{token}/join", {"name": "Late"})
        assert r.status_code == 410, r.text
        assert call(c, bob, f"/groups/{group}/card", method="GET").status_code == 404
        r = call(c, cy, f"/cards/{card}/claim", FORM)
        assert r.status_code == 409 and r.json()["error"] == "retired"
        # a belfry admin retires a card nobody used
        assert call(c, land.ada, f"/cards/{free}/retire", {}).status_code == 200
        assert get(c, f"/cards/{free}").json() == {"kind": "group", "state": "retired", "title": ""}
        r = call(c, PrivateKey(), f"/cards/{free}/claim-start", {"email": "v@example.org"})
        assert r.status_code == 409 and r.json()["error"] == "retired"


def test_an_archived_group_says_so_and_takes_nobody(land):
    rsvc = land.rsvc
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    card = land.card()
    with TestClient(create_app(rsvc)) as c:
        group = call(c, bob, f"/cards/{card}/claim", FORM).json()["group"]
        land.mirror(group)
        token = get(c, f"/cards/{card}").json()["invite"]
        r = call(c, bob, "/group-meta", {"channel": group, "archived": True})
        assert r.status_code == 200, r.text
        assert get(c, f"/cards/{card}").json() == {
            "kind": "group",
            "state": "archived",
            "title": "Sourdough starters",
        }
        r = call(c, PrivateKey(), f"/channel-invites/{token}/join", {"name": "Late"})
        assert r.status_code == 409 and r.json()["error"] == "archived"
        # unarchived: the card works again
        time.sleep(1.05)  # the later 9002 wins: not in the same second
        r = call(c, bob, "/group-meta", {"channel": group, "archived": False})
        assert r.status_code == 200, r.text
        assert get(c, f"/cards/{card}").json()["state"] == "claimed"


# ── the console ─────────────────────────────────────────────────────────────────────────────
def test_the_console_page_is_for_admins(land):
    rsvc = land.rsvc
    _bob, bob_id = person(rsvc, "bob@belfry.test", "Bob")
    ok = {"kind": "group", "per_page": "4", "pages": "1"}
    with TestClient(create_app(rsvc), base_url="https://belfry.belfry.test") as c:
        r = c.get("/cards", headers=H, follow_redirects=False)
        assert r.status_code == 303 and "/login" in r.headers["location"]
        signed_in(c, rsvc, bob_id)
        r = c.get("/cards", headers=H)
        assert r.status_code == 403 and 'href="/cards"' not in r.text  # not in a member's menu
        assert c.get("/cards/batch/1.pdf", headers=H).status_code == 403
        assert post(c, "/cards/make", **ok).status_code == 303
        assert rsvc.odoo.card_list() == []
        signed_in(c, rsvc, land.ada_id)
        t = c.get("/cards", headers=H).text
        assert "Printed cards" in t and "No cards yet." in t and 'href="/cards"' in t
        # the board's form: two tiles, cards per page, pages with the total, the batch's name
        for words in (
            "Account required",
            "No account needed to post",
            "People who get to know each other. They join and say hi.",
            "People come and go, share what they know and follow by e-mail.",
            "Cards per page",
            "= 8 cards",
            "4 A6 on each A4",
            "1 to 50",
            "To find these cards in the list",
            "Generate PDF",
            "Cards made · 0",
        ):
            assert words in t, words
        for field in (
            'name="kind" value="group" checked',
            'name="kind" value="topic"',
            'name="per_page" value="1"',
            'name="per_page" value="2"',
            'name="per_page" value="4" checked',
            'name="pages" min="1" max="50" value="2"',
            'name="batch" maxlength="60" value="Batch 1"',
            'target="_blank"',
        ):
            assert field in t, field
        # without the csrf, an unknown kind, a wrong number per page, too many pages: nothing
        r = c.post("/cards/make", data=ok, headers=H, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/cards"
        for bad in (
            {**ok, "kind": "pod"},
            {**ok, "per_page": "3"},
            {**ok, "per_page": ""},
            {**ok, "pages": "51"},
            {**ok, "pages": "0"},
            {**ok, "pages": "many"},
        ):
            r = post(c, "/cards/make", **bad)
            assert r.status_code == 303 and r.headers["location"] == "/cards", bad
        assert rsvc.odoo.card_list() == []
        assert "Make 1 to 50 pages at a time." in c.get("/cards", headers=H).text


def test_generate_makes_a_batch_and_opens_its_sheets(land):
    rsvc = land.rsvc
    with TestClient(create_app(rsvc), base_url="https://belfry.belfry.test") as c:
        signed_in(c, rsvc, land.ada_id)
        # 2 pages of 4: eight group cards, named "Batch 1" when the field is left empty
        r = post(c, "/cards/make", kind="group", per_page="4", pages="2", batch="")
        b1 = cardsconsole.batches(rsvc)[0]
        assert r.status_code == 303 and r.headers["location"] == f"/cards/batch/{b1['key']}.pdf"
        assert (b1["name"], b1["kind"], b1["per_page"], len(b1["cards"])) == (
            "Batch 1",
            "group",
            4,
            8,
        )
        # the next one is Batch 2 by default (every batch of the belfry counts), or its own name
        assert 'value="Batch 2"' in c.get("/cards", headers=H).text
        post(c, "/cards/make", kind="topic", per_page="2", pages="3", batch="Market <day>")
        post(c, "/cards/make", kind="topic", per_page="1", pages="50", batch="Batch 3")
        found = {b["name"]: b for b in cardsconsole.batches(rsvc)}
        assert (found["Market <day>"]["per_page"], len(found["Market <day>"]["cards"])) == (2, 6)
        assert (found["Batch 3"]["per_page"], len(found["Batch 3"]["cards"])) == (1, 50)
        made = rsvc.odoo.card_list()
        assert len(made) == 64 and len({m["id"] for m in made}) == 64
        assert all(cards.ID.fullmatch(m["id"]) and m["state"] == "unclaimed" for m in made)
        assert all(m["made_by"] == land.ada_id for m in made)
        t = c.get("/cards", headers=H).text
        assert "Cards made · 64" in t and 'value="Batch 4"' in t
        assert "Market &lt;day&gt;" in t and "<day>" not in t
        for head in ("Card", "For", "Batch", "State", "Name", "Started by"):
            assert f"<th>{head}</th>" in t
        assert t.count("Not scanned yet") == 64


def _inside(box, x0, y0, x1, y1, slack=0.01):
    return (
        box[0] >= x0 - slack
        and box[1] >= y0 - slack
        and box[2] <= x1 + slack
        and box[3] <= y1 + slack
    )


PDF_WANT = {
    # (kind, per page): (the page in mm, columns, rows)
    ("group", 4): ((210.0, 297.0), 2, 2),
    ("group", 2): ((297.0, 210.0), 2, 1),
    ("group", 1): ((210.0, 297.0), 1, 1),
    ("topic", 4): ((297.0, 210.0), 2, 2),
    ("topic", 2): ((210.0, 297.0), 1, 2),
    ("topic", 1): ((297.0, 210.0), 1, 1),
}


@pytest.mark.parametrize("kind,per", sorted(PDF_WANT))
def test_the_pdf_keeps_its_margins_and_is_centred(kind, per, settings):
    """With a ruler: nothing within 8 mm of the paper's edge, equal cells at the card's own
    proportions, the block centred, cut lines between cells only, the QR code's quiet zone."""
    import types

    svc = types.SimpleNamespace(settings=settings)
    name = "Maison des Égalités à Łódź · Österreich"  # accents, and long enough to need care
    urls = [f"https://chat.belfry.brussels/g/{cards.new_id()}" for _ in range(per + 1)]
    data, sheet, lay = cardspdf.render(svc, kind, per, urls, name, "Batch 3")
    (pw, ph), cols, rows = PDF_WANT[(kind, per)]
    assert data.startswith(b"%PDF-") and lay["page"] == (pw, ph)
    assert (lay["cols"], lay["rows"]) == (cols, rows) and len(sheet.boxes) == 2  # per + 1 cards
    # the PDF's own pages are that size, in that orientation
    boxes = re.findall(rb"/MediaBox \[0 0 ([0-9.]+) ([0-9.]+)\]", data)
    assert boxes and all(
        abs(float(w) - pw * 72 / 25.4) < 0.1 and abs(float(h) - ph * 72 / 25.4) < 0.1
        for w, h in boxes
    )
    m = cardspdf.MARGIN
    assert m == 8.0
    cw, ch = lay["cell"]
    bw, bh = lay["block"]
    card = cardspdf.TOPIC if kind == "topic" else cardspdf.GROUP
    # equal cells at the card's proportions, as large as the printable area allows
    assert abs(cw / ch - card[0] / card[1]) < 1e-6
    assert abs(bw - cw * cols) < 1e-6 and abs(bh - ch * rows) < 1e-6
    assert bw <= pw - 2 * m + 1e-6 and bh <= ph - 2 * m + 1e-6
    assert abs(bw - (pw - 2 * m)) < 1e-6 or abs(bh - (ph - 2 * m)) < 1e-6
    # the block is centred: equal left / right and top / bottom margins
    left, top = lay["x0"], lay["y0"]
    right, bottom = pw - (left + bw), ph - (top + bh)
    assert abs(left - right) < 0.5 and abs(top - bottom) < 0.5
    assert left >= m - 1e-6 and top >= m - 1e-6
    for page, drawn in enumerate(sheet.boxes):
        cuts = [b for b in drawn if b[4] == "cut"]
        rest = [b for b in drawn if b[4] != "cut"]
        on_page = per if page == 0 else 1
        cells = [
            (
                left + (i % cols) * cw,
                top + (i // cols) * ch,
                left + (i % cols + 1) * cw,
                top + (i // cols + 1) * ch,
            )
            for i in range(on_page)
        ]
        # nothing at all within 8 mm of a paper edge
        for b in drawn:
            assert _inside(b, m, m, pw - m, ph - m), (page, b)
        # everything of a card is inside its own cell: nothing is clipped, nothing runs over
        for b in rest:
            assert any(_inside(b, *c) for c in cells), (page, b)
        # cut lines: between the cells, from one printable edge to the other; none at the edge
        assert len(cuts) == (cols - 1) + (rows - 1)
        for b in cuts:
            if b[3] - b[1] > b[2] - b[0]:  # down the page
                assert abs((b[0] + b[2]) / 2 - pw / 2) < 0.01
                assert abs(b[1] - m) < 0.01 and abs(b[3] - (ph - m)) < 0.01
            else:
                assert abs((b[1] + b[3]) / 2 - ph / 2) < 0.01
                assert abs(b[0] - m) < 0.01 and abs(b[2] - (pw - m)) < 0.01
        # every card: a logo, its QR code, and four modules of white around the code
        codes = [b for b in rest if b[4].startswith("qr:")]
        assert len(codes) == on_page and len([b for b in rest if b[4] == "logo"]) == on_page
        for q in codes:
            quiet = 4 * (q[2] - q[0]) / int(q[4][3:])
            zone = (q[0] - quiet, q[1] - quiet, q[2] + quiet, q[3] + quiet)
            for b in drawn:
                if b is q:
                    continue
                apart = b[2] <= zone[0] or b[0] >= zone[2] or b[3] <= zone[1] or b[1] >= zone[3]
                assert apart, (page, q, b)
            assert any(_inside(zone, *c) for c in cells)  # the white is the card's own


def test_the_pdfs_words_font_logo_and_file_name(settings, tmp_path):
    import types

    from fontTools.ttLib import TTFont

    svc = types.SimpleNamespace(settings=settings)
    # Inter, with the letters community names use
    for weight in ("Regular", "SemiBold", "Bold"):
        font = TTFont(cardspdf.FONTS / f"Inter-{weight}.ttf")
        assert "Inter" in str(font["name"].getDebugName(1))
        assert all(ord(ch) in font.getBestCmap() for ch in "Égalité Łódź Österreich ñ ç · # …")
    assert (cardspdf.FONTS / "OFL.txt").read_text().count("SIL Open Font License") >= 1
    # the board's words (uncompressed, to read them back)
    seen: list[str] = []
    real = cardspdf.Sheet.text

    def text(self, x, baseline, words, *a, **k):
        seen.append(words)
        return real(self, x, baseline, words, *a, **k)

    cardspdf.Sheet.text = text
    try:
        cardspdf.render(svc, "group", 4, ["https://chat.belfry.test/g/ABCDEF"], "Riverside Commons")
        group, seen[:] = list(seen), []
        cardspdf.render(svc, "topic", 4, ["https://chat.belfry.test/g/ABCDEF"], "Riverside Commons")
        topic, seen[:] = list(seen), []
        long = "A community with a very long name that would never fit on one line of a card"
        _data, sheet, lay = cardspdf.render(
            svc, "group", 4, ["https://chat.belfry.test/g/ABCDEF"], long
        )
        cut = seen[0]
    finally:
        cardspdf.Sheet.text = real
    assert group == [
        "Riverside Commons",
        "Group",
        " · people you get to know. Join and say hi.",
        "Group name:",
        "Description:",
        "Scan to join this group",
    ]
    assert topic == [
        "Riverside Commons",
        "Topic",
        " · come and go, share what you know.",
        "#",
        "About:",
        "No account required",
        "Scan to contribute",
        "to this topic",
    ]
    assert cut.endswith("…") and long.startswith(cut[:-1].rstrip())  # cut, never over the edge
    # neither the address nor the id is written on a card: they are in the QR code only
    assert not any("chat." in w or "ABCDEF" in w for w in group + topic)
    # the logo: the Arms unless the theme folder has its own
    data, ratio = cardspdf.logo(svc)
    assert data.startswith(b"<svg") and abs(ratio - 64 / 70) < 1e-9
    from PIL import Image

    Image.new("RGB", (300, 100), "#336699").save(tmp_path / "logo.png")
    settings.theme_dir = str(tmp_path)
    data, ratio = cardspdf.logo(svc)
    assert data.startswith(b"\x89PNG") and abs(ratio - 3.0) < 1e-9
    _pdf, sheet, lay = cardspdf.render(svc, "topic", 1, ["https://chat.belfry.test/g/ABCDEF"], "X")
    mark = next(b for b in sheet.boxes[0] if b[4] == "logo")
    assert abs((mark[2] - mark[0]) / (mark[3] - mark[1]) - 3.0) < 0.01
    (tmp_path / "logo.png").write_bytes(b"not a picture")  # one it can't read: the Arms again
    assert cardspdf.logo(svc)[0].startswith(b"<svg")
    # the file's name
    assert cardspdf.filename("Batch 3") == "Batch 3.pdf"
    assert cardspdf.filename('Market "day" / <b>été</b>\r\n') == "Market day btb.pdf"
    assert cardspdf.filename("../../etc") == "etc.pdf" and cardspdf.filename("…") == "cards.pdf"


def test_generate_pdf_answers_a_pdf_for_admins(land):
    rsvc = land.rsvc
    _bob, bob_id = person(rsvc, "bob@belfry.test", "Bob")
    with TestClient(create_app(rsvc), base_url="https://belfry.belfry.test") as c:
        signed_in(c, rsvc, land.ada_id)
        r = post(c, "/cards/make", kind="topic", per_page="2", pages="2", batch="Market / day")
        key = cardsconsole.batches(rsvc)[0]["key"]
        assert r.status_code == 303 and r.headers["location"] == f"/cards/batch/{key}.pdf"
        r = c.get(f"/cards/batch/{key}.pdf", headers=H)
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
        assert r.headers["content-disposition"] == 'inline; filename="Market day.pdf"'
        assert r.content.startswith(b"%PDF-") and r.content.rstrip().endswith(b"%%EOF")
        # two topic cards a page: upright A4 pages, two of them for four cards
        sizes = re.findall(rb"/MediaBox \[0 0 ([0-9.]+) ([0-9.]+)\]", r.content)
        assert sizes and all(float(w) < float(h) for w, h in sizes)
        assert len(re.findall(rb"/Type /Page\b(?!s)", r.content)) == 2
        for m in cardsconsole.batches(rsvc)[0]["cards"]:
            assert m["id"].encode() not in r.content
        # a batch made before "cards per page" existed is four to a sheet
        old = cardsconsole.make(rsvc, "group", "Old", 5, land.ada_id)
        r = c.get(f"/cards/batch/{old}.pdf", headers=H)
        assert r.status_code == 200 and len(re.findall(rb"/Type /Page\b(?!s)", r.content)) == 2
        for bad in ("12345.pdf", f"{key}", f"{key}.html", "x%3Cb%3E.pdf"):
            assert c.get(f"/cards/batch/{bad}", headers=H).status_code == 404, bad
        assert (
            c.get(f"/cards/print/{key}", headers=H).status_code == 404
        )  # the HTML sheets are gone
        signed_in(c, rsvc, bob_id)
        assert c.get(f"/cards/batch/{key}.pdf", headers=H).status_code == 403
        c.cookies.clear()
        r = c.get(f"/cards/batch/{key}.pdf", headers=H, follow_redirects=False)
        assert r.status_code == 303 and "/login" in r.headers["location"]


def test_the_list_names_groups_and_starters_and_reset_frees_a_card(land):
    rsvc = land.rsvc
    bob, _ = person(rsvc, "bob@belfry.test", "Bob Baker")
    gus = saved_guest(rsvc, name="Lou")
    key = cardsconsole.make(rsvc, "group", "", 3, land.ada_id)
    tkey = cardsconsole.make(rsvc, "topic", "Market day", 1, land.ada_id)
    found = {b["key"]: b for b in cardsconsole.batches(rsvc)}
    card, card2 = (m["id"] for m in found[key]["cards"][:2])
    topic = found[tkey]["cards"][0]["id"]
    with TestClient(create_app(rsvc), base_url="https://belfry.belfry.test") as c:
        signed_in(c, rsvc, land.ada_id)
        group = call(c, bob, f"/cards/{card}/claim", FORM).json()["group"]
        land.mirror(group)
        call(c, gus, f"/cards/{card2}/claim", {"title": "Bike rides"})
        r = call(c, bob, f"/cards/{topic}/claim", {"title": "Tools", "hashtag": "tool-library"})
        assert r.status_code == 200, r.text
        token = rsvc.odoo.card_get(card)["invite"]
        t = c.get("/cards", headers=H).text
        assert "Sourdough starters" in t and "Bike rides" in t and "#tool-library" in t
        assert group not in t  # a group by its name, never its id
        assert "<td>Bob Baker</td>" in t and "<td>Lou (guest steward)</td>" in t
        assert t.count(">Claimed<") == 3 and t.count("Not scanned yet") == 1
        assert f'action="/cards/{card}/reset"' in t and t.count(">Reset<") == 3
        assert f'href="/cards/batch/{key}.pdf"' in t  # the batch's name opens its PDF again
        assert call(c, bob, f"/cards/{card2}/retire", {}).status_code == 403
        assert call(c, gus, f"/cards/{card2}/retire", {}).status_code == 200
        t = c.get("/cards", headers=H).text
        assert t.count(">Retired<") == 1 and t.count(">Reset<") == 3
        n = len(land.w.grp.events)
        r = c.post(f"/cards/{card}/reset", data={}, headers=H, follow_redirects=False)
        assert rsvc.odoo.card_get(card)["state"] == "claimed"  # no csrf, no reset
        r = post(c, f"/cards/{card}/reset")
        assert r.status_code == 303
        got = rsvc.odoo.card_get(card)
        assert got["state"] == "unclaimed" and got["group"] is None and got["invite"] is None
        assert rsvc.odoo.invite_by_token(token)["state"] == "revoked"
        assert len(land.w.grp.events) == n  # the old group is untouched
        assert "The card is free again." in c.get("/cards", headers=H).text
        assert get(c, f"/cards/{card}").json() == {"kind": "group", "state": "unclaimed"}
        assert call(c, bob, f"/groups/{group}/card", method="GET").status_code == 404
        # and it starts another group
        r = call(c, bob, f"/cards/{card}/claim", {"title": "Second life"})
        assert r.status_code == 200 and r.json()["group"] != group
        assert post(c, "/cards/ABCDEF/reset").status_code == 303
        assert CSRF not in "".join(m["id"] for m in rsvc.odoo.card_list())


def test_an_older_table_gets_the_cards_per_page_column(tmp_path, settings):
    path = tmp_path / "old.sqlite"
    reg = Register(path, settings)
    reg._db.execute("DROP TABLE group_cards")
    reg._db.execute(
        "CREATE TABLE group_cards (id TEXT PRIMARY KEY, kind TEXT NOT NULL, batch TEXT NOT NULL,"
        " state TEXT NOT NULL, group_id TEXT, invite TEXT, title TEXT, made_by INTEGER,"
        " made_at INTEGER NOT NULL, claimed_by INTEGER, claimed_key TEXT, claimed_at INTEGER)"
    )
    reg._db.execute(
        "INSERT INTO group_cards (id, kind, batch, state, made_by, made_at)"
        " VALUES ('ABCDEF', 'group', 'First', 'unclaimed', 1, 5)"
    )
    reg._db.close()
    reg = Register(path, settings)
    assert reg.card_get("ABCDEF")["per_page"] == 4  # as they were printed
    assert reg.card_make(["GHJKMN"], "topic", "B", 1, 6, 2) == ["GHJKMN"]
    assert reg.card_get("GHJKMN")["per_page"] == 2


def test_an_invite_says_when_it_is_a_cards(land):
    rsvc = land.rsvc
    bob, bob_id = person(rsvc, "bob@belfry.test", "Bob")
    group_card, topic_card = land.card(), land.card("topic")
    with TestClient(create_app(rsvc)) as c:
        call(c, bob, f"/cards/{group_card}/claim", FORM)
        call(c, bob, f"/cards/{topic_card}/claim", {**FORM, "hashtag": "bread"})
        for card, kind in ((group_card, "group"), (topic_card, "topic")):
            token = rsvc.odoo.card_get(card)["invite"]
            assert c.get(f"/api/v1/channel-invites/{token}", headers=HOST).json()["card"] == kind
            # a visitor in guest mode, someone with an account: the same field in the join answer
            r = call(c, PrivateKey(), f"/channel-invites/{token}/join", {})
            assert r.status_code == 200 and r.json()["card"] == kind and r.json()["reading"]
            cy, _ = person(rsvc, f"cy-{kind}@belfry.test", "Cy")
            r = call(c, cy, f"/channel-invites/{token}/join", {})
            assert r.status_code == 200 and r.json()["card"] == kind
        # an ordinary guest link says nothing about a card
        row = rsvc.odoo.create_guest_invite(bob_id, pk(bob), "garden", 30, people=50)
        token = row["url"].rsplit("/", 1)[1]
        assert "card" not in c.get(f"/api/v1/channel-invites/{token}", headers=HOST).json()
        r = call(c, PrivateKey(), f"/channel-invites/{token}/join", {})
        assert r.status_code == 200 and "card" not in r.json()


def test_a_hashtag_is_never_free_on_a_relay_that_fails(land, monkeypatch):
    rsvc = land.rsvc
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    card = land.card("topic")
    land.channels.add("garden-1a2b")
    with TestClient(create_app(rsvc)) as c:
        # a channel's plain slug reaches it by mail: taken
        monkeypatch.setattr(
            mail, "channel_by_slug", lambda svc, n: "garden-1a2b" if n == "garden" else None
        )
        assert get(c, "/cards/hashtag/garden").json() == {"state": "taken"}
        assert get(c, "/cards/hashtag/bread").json() == {"state": "free"}

        def down(svc, name):
            raise OSError("relay unreachable")

        monkeypatch.setattr(mail, "channel_by_slug", down)
        r = get(c, "/cards/hashtag/bread")
        assert r.status_code == 503 and r.json()["error"] == "relay"
        r = call(c, bob, f"/cards/{card}/claim", {**FORM, "hashtag": "bread"})
        assert r.status_code == 503 and r.json()["error"] == "relay"
        assert not land.of(9007) and rsvc.odoo.card_get(card)["state"] == "unclaimed"
        # mail's own lookup still answers "no channel" there, as before
        monkeypatch.undo()


# ── the steward keeps their group; the link makes only so many accounts a day ───────────────
def test_a_guest_steward_does_not_lose_their_group_after_30_days(land, monkeypatch):
    from belfry_onboarding import core, register

    rsvc = land.rsvc
    card = land.card()
    with TestClient(create_app(rsvc)) as c:
        _key, r = visitor_claim(c, card, "vera@example.org")
        assert r.status_code == 200, r.text
        me, group = r.json()["pubkey"], r.json()["group"]
        token = rsvc.odoo.card_get(card)["invite"]
        # someone who joins by the card is a guest for 30 days, as ever
        r = call(c, PrivateKey(), f"/channel-invites/{token}/join", {"name": "Gil"})
        gil = r.json()["pubkey"]

    def passes(pubkey):
        return rsvc.odoo.search_read(
            "belfry.pass",
            [["keycast_pubkey", "=", pubkey], ["guest_channel", "=", group]],
            ["state", "date_end"],
        )

    assert [(p["state"], p["date_end"]) for p in passes(me)] == [("active", register.FOREVER)]
    assert ["guest", register.FOREVER] in land.of(9000, group)[0]["tags"]
    real = dt.datetime.now(dt.UTC).date()
    monkeypatch.setattr(core, "today", lambda: real + dt.timedelta(days=31))
    core.expire(rsvc)
    out = [e for e in land.of(9001, group)]
    assert [t for e in out for t in e["tags"] if t[0] == "p"] == [["p", gil]]  # Gil, not Vera
    assert passes(me)[0]["state"] == "active"
    assert me in checkin.group_state(rsvc, group).admins
    # the card is reset: her pass ends as any guest's, 30 days from now
    assert rsvc.odoo.card_reset(card)
    end = (real + dt.timedelta(days=30)).isoformat()
    assert [(p["state"], p["date_end"]) for p in passes(me)] == [("active", end)]
    monkeypatch.setattr(core, "today", lambda: real + dt.timedelta(days=30))
    core.expire(rsvc)
    assert passes(me)[0]["state"] == "active"
    monkeypatch.setattr(core, "today", lambda: real + dt.timedelta(days=31))
    core.expire(rsvc)
    assert passes(me)[0]["state"] == "expired"
    assert ["p", me] in land.of(9001, group)[-1]["tags"]


def test_retiring_also_puts_the_stewards_usual_end_back(land):
    from belfry_onboarding import register

    rsvc = land.rsvc
    gus = saved_guest(rsvc)
    card = land.card()
    with TestClient(create_app(rsvc)) as c:
        group = call(c, gus, f"/cards/{card}/claim", FORM).json()["group"]
        rows = rsvc.odoo.search_read(
            "belfry.pass", [["keycast_pubkey", "=", pk(gus)]], ["guest_channel", "date_end"]
        )
        end = (dt.datetime.now(dt.UTC).date() + dt.timedelta(days=30)).isoformat()
        assert {r["guest_channel"]: r["date_end"] for r in rows} == {
            "garden": end,  # the channel they were a guest of keeps its own end
            group: register.FOREVER,
        }
        assert call(c, gus, f"/cards/{card}/retire", {}).status_code == 200
        rows = rsvc.odoo.search_read(
            "belfry.pass", [["keycast_pubkey", "=", pk(gus)]], ["guest_channel", "date_end"]
        )
        assert {r["date_end"] for r in rows} == {end}


def test_a_cards_link_makes_only_so_many_new_accounts_a_day(land, monkeypatch, caplog):
    from belfry_onboarding import register

    rsvc = land.rsvc
    monkeypatch.setattr(register, "CARD_NEW_GUESTS_PER_DAY", 3)
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    card = land.card()
    with TestClient(create_app(rsvc)) as c:
        group = call(c, bob, f"/cards/{card}/claim", FORM).json()["group"]
        token = rsvc.odoo.card_get(card)["invite"]
        first = PrivateKey()
        assert call(c, first, f"/channel-invites/{token}/join", {}).status_code == 200
        for _ in range(2):
            r = call(c, PrivateKey(), f"/channel-invites/{token}/join", {})
            assert r.status_code == 200 and r.json()["reading"] is True
        # the fourth new account: refused as a used-up link is
        with caplog.at_level("WARNING", logger="belfry_onboarding.register"):
            for _ in range(3):
                r = call(c, PrivateKey(), f"/channel-invites/{token}/join", {})
                assert r.status_code == 410 and r.json()["error"] == "closed", r.text
        said = [m for m in caplog.messages if "new guest accounts in 24 hours" in m]
        assert len(said) == 1 and card in said[0]  # once an hour per card
        # the same device asking again gets its account; people with an account are never refused
        assert call(c, first, f"/channel-invites/{token}/join", {}).status_code == 200
        cy, _ = person(rsvc, "cy@belfry.test", "Cy")
        r = call(c, cy, f"/channel-invites/{token}/join", {})
        assert r.status_code == 200 and r.json()["guest"] is False
        gus = saved_guest(rsvc)
        r = call(c, gus, f"/channel-invites/{token}/join", {})
        assert r.status_code == 200 and r.json()["new"] is False
        # the link itself stays open, and the count lives in the register: a restart keeps it
        assert rsvc.odoo.invite_by_token(token)["state"] == "open"
        again = Register(rsvc.odoo.path, rsvc.settings)
        with pytest.raises(Exception, match="This invite is used"):
            again.join_guest(token, "", pk(PrivateKey()), True)
        # a day later there is room again
        for p in rsvc.odoo.search_read("belfry.pass", [["guest_channel", "=", group]], ["id"]):
            rsvc.odoo.write("belfry.pass", [p["id"]], {"create_date": "2026-01-01 00:00:00"})
        assert call(c, PrivateKey(), f"/channel-invites/{token}/join", {}).status_code == 200


# ── the form with the code mail, and the mail's Confirm button ──────────────────────────────
def _mail_parts(msg):
    text = msg.get_body(preferencelist=("plain",)).get_content()
    html = msg.get_body(preferencelist=("html",)).get_content()
    return text, html


def _confirm_mail():
    """The code (shown as 427 913) and the Confirm link's token of the last mail."""
    text, _html = _mail_parts(mailer.SENT[-1])
    code = re.search(r"\b(\d{3}) (\d{3})\b", text)
    token = re.search(r"\?confirm=([A-Za-z0-9_-]+)", text)
    return code.group(1) + code.group(2), token.group(1)


START = {"email": "vera@example.org", "name": "Vera Visitor", **FORM}


def test_the_mail_has_the_code_and_a_confirm_button(land):
    rsvc = land.rsvc
    card = land.card()
    with TestClient(create_app(rsvc)) as c:
        key = PrivateKey()
        for body, field in (
            ({**START, "name": ""}, "name"),
            ({**START, "title": ""}, "title"),
            ({**START, "email": "no"}, "email"),
        ):
            r = call(c, key, f"/cards/{card}/claim-start", body)
            assert r.status_code == 422 and r.json()["field"] == field, r.text
        evil = {**START, "name": "<b>Vera</b> & co", "title": "Bread <script>x</script>"}
        r = call(c, key, f"/cards/{card}/claim-start", evil)
        assert r.status_code == 202 and r.json() == {"sent": True}
        msg = mailer.SENT[-1]
        assert msg["Subject"] == "Confirm your group: Bread <script>x</script>"
        text, html = _mail_parts(msg)
        assert "Hi <b>Vera</b> & co," in text
        assert "to create the group Bread <script>x</script>, enter this code on the page" in text
        assert "Or confirm with one click:" in text
        assert "The code and the button work for 15 minutes." in text
        assert "Didn't scan a card? Ignore this e-mail: nothing happens." in text
        # the names are escaped in the HTML and never in the URL
        assert "<script>" not in html and "&lt;script&gt;" in html and "&lt;b&gt;Vera" in html
        url = re.search(r'href="([^"]+confirm=[^"]+)"', html).group(1)
        assert re.fullmatch(rf"https://chat\.[^/]+/g/{card}\?confirm=[A-Za-z0-9_-]{{43}}", url)
        assert "Vera" not in url and "Bread" not in url and ">Confirm</a>" in html
        _code_, token = _confirm_mail()
        # stored hashed, never as it is
        row = rsvc.odoo.card_claim_get(card, pk(key))
        assert row["state"] == "pending" and token not in str(row)
        assert row["form"]["name"] == "<b>Vera</b> & co"
        # nothing is made before the proof
        assert not land.of(9007)
        assert not rsvc.odoo.search_read("res.partner", [["email", "=", "vera@example.org"]], [])
        # the topic's mail
        topic = land.card("topic")
        r = call(c, key, f"/cards/{topic}/claim-start", {**START, "title": "", "hashtag": "rye"})
        assert r.status_code == 202, r.text
        assert mailer.SENT[-1]["Subject"] == "Confirm your topic: #rye"
        assert "to create the topic #rye, enter this code" in _mail_parts(mailer.SENT[-1])[0]


def test_the_code_with_the_stored_form(land):
    rsvc = land.rsvc
    card = land.card()
    with TestClient(create_app(rsvc)) as c:
        key = PrivateKey()
        r = call(c, key, f"/cards/{card}/claim-start", START)
        assert r.status_code == 202
        code, token = _confirm_mail()
        r = call(c, key, f"/cards/{card}/claim-verify", {"email": START["email"], "code": code})
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["signed_in"] is True and out["group"].startswith("pod-sourdough-starters-")
        assert ["name", "Sourdough starters"] in land.of(9002, out["group"])[0]["tags"]
        # one claim only: the button now answers expired, and so does the code
        r = call(c, PrivateKey(), f"/cards/{card}/claim-confirm", {"token": token})
        assert r.status_code == 410 and r.json()["error"] == "expired"
        r = call(c, key, f"/cards/{card}/claim-verify", {"email": START["email"], "code": code})
        assert r.status_code == 410
        assert len(land.of(9007)) == 1


def test_the_form_in_verify_wins_over_the_stored_one(land):
    card = land.card()
    with TestClient(create_app(land.rsvc)) as c:
        key = PrivateKey()
        call(c, key, f"/cards/{card}/claim-start", START)
        code, _token = _confirm_mail()
        body = {**START, "code": code, "title": "Another name"}
        r = call(c, key, f"/cards/{card}/claim-verify", body)
        assert r.status_code == 200, r.text
        assert ["name", "Another name"] in land.of(9002, r.json()["group"])[0]["tags"]


def test_confirm_on_the_same_device_signs_it_in(land, monkeypatch):
    rsvc = land.rsvc
    bound = []
    real = rsvc.keycast.create_bunker

    def create(token, label, client_pubkey=None):
        bound.append(client_pubkey)
        return real(token, label, client_pubkey)

    monkeypatch.setattr(rsvc.keycast, "create_bunker", create)
    card = land.card()
    with TestClient(create_app(rsvc)) as c:
        key = PrivateKey()
        call(c, key, f"/cards/{card}/claim-start", START)
        _code_, token = _confirm_mail()
        r = call(c, key, f"/cards/{card}/claim-confirm", {"token": token})
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["confirmed"] is True and out["same_device"] is True
        assert out["signed_in"] is True and out["bunker_url"].startswith("bunker://")
        assert out["group"] == rsvc.odoo.card_get(card)["group"]
        assert bound == [pk(key)]
        assert out["pubkey"] in checkin.group_state(rsvc, out["group"]).admins
        # handed out: the waiting page has nothing more to take, the link is used
        r = call(c, key, f"/cards/{card}/claim-poll")
        assert r.status_code == 410
        r = call(c, key, f"/cards/{card}/claim-confirm", {"token": token})
        assert r.status_code == 410 and r.json()["error"] == "expired"


def test_confirm_on_another_device_hands_the_session_to_the_waiting_page_once(land, monkeypatch):
    rsvc = land.rsvc
    bound = []
    real = rsvc.keycast.create_bunker

    def create(token, label, client_pubkey=None):
        bound.append(client_pubkey)
        return real(token, label, client_pubkey)

    monkeypatch.setattr(rsvc.keycast, "create_bunker", create)
    card = land.card()
    with TestClient(create_app(rsvc)) as c:
        key, phone = PrivateKey(), PrivateKey()
        r = call(c, key, f"/cards/{card}/claim-poll")
        assert r.status_code == 404
        call(c, key, f"/cards/{card}/claim-start", START)
        code, token = _confirm_mail()
        r = call(c, key, f"/cards/{card}/claim-poll")
        assert r.status_code == 202 and r.json() == {"pending": True}
        r = call(c, phone, f"/cards/{card}/claim-poll")
        assert r.status_code == 404
        r = call(c, phone, f"/cards/{card}/claim-confirm", {"token": "x" * 43})
        assert r.status_code == 410
        r = call(c, phone, f"/cards/{card}/claim-confirm", {"token": token})
        assert r.status_code == 200, r.text
        out = r.json()
        assert out == {"confirmed": True, "group": out["group"], "same_device": False}
        assert bound == [pk(key)]  # the session is the waiting page's, never the phone's
        r = call(c, key, f"/cards/{card}/claim-poll")
        assert r.status_code == 200, r.text
        got = r.json()
        assert got["signed_in"] is True and got["bunker_url"].startswith("bunker://")
        assert got["group"] == out["group"]
        assert call(c, key, f"/cards/{card}/claim-poll").status_code == 410
        # the session is forgotten once handed out
        assert rsvc.odoo.card_claim_get(card, pk(key))["result"] is None
        # the code works no more
        r = call(c, key, f"/cards/{card}/claim-verify", {"email": START["email"], "code": code})
        assert r.status_code == 410


def test_an_expired_claim_and_link(land, monkeypatch):
    rsvc = land.rsvc
    card = land.card()
    with TestClient(create_app(rsvc)) as c:
        key = PrivateKey()
        call(c, key, f"/cards/{card}/claim-start", START)
        _code_, token = _confirm_mail()
        later = time.time() + cards.CLAIM_SECONDS + 1
        monkeypatch.setattr(cards.time, "time", lambda: later)
        r = call(c, PrivateKey(), f"/cards/{card}/claim-confirm", {"token": token})
        assert r.status_code == 410 and r.json()["error"] == "expired"
        r = call(c, key, f"/cards/{card}/claim-poll")
        assert r.status_code == 410
        assert not land.of(9007)


def test_a_new_claim_start_replaces_the_link_before(land):
    card = land.card()
    with TestClient(create_app(land.rsvc)) as c:
        key = PrivateKey()
        call(c, key, f"/cards/{card}/claim-start", START)
        _c1, old = _confirm_mail()
        call(c, key, f"/cards/{card}/claim-start", START)
        _c2, new = _confirm_mail()
        assert old != new
        r = call(c, key, f"/cards/{card}/claim-confirm", {"token": old})
        assert r.status_code == 410
        r = call(c, key, f"/cards/{card}/claim-confirm", {"token": new})
        assert r.status_code == 200, r.text


def test_someone_else_claimed_meanwhile(land):
    rsvc = land.rsvc
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    card = land.card()
    with TestClient(create_app(rsvc)) as c:
        key = PrivateKey()
        call(c, key, f"/cards/{card}/claim-start", START)
        _code_, token = _confirm_mail()
        r = call(c, bob, f"/cards/{card}/claim", FORM)
        assert r.status_code == 200
        r = call(c, PrivateKey(), f"/cards/{card}/claim-confirm", {"token": token})
        assert r.status_code == 409 and r.json()["error"] == "claimed"
        assert r.json()["group"] == rsvc.odoo.card_get(card)["group"]
        assert len(land.of(9007)) == 1
        assert not rsvc.odoo.search_read("res.partner", [["email", "=", "vera@example.org"]], [])


def test_the_code_and_the_button_race_and_one_wins(land, monkeypatch):
    rsvc = land.rsvc
    card = land.card()
    key = pk(PrivateKey())
    cards.claim_start(rsvc, card, key, dict(START), "198.51.100.7")
    code, token = _confirm_mail()
    both = threading.Barrier(2, timeout=10)
    real = cards._refuse_taken
    mine = threading.local()

    def taken(c):
        real(c)
        if not getattr(mine, "waited", False):  # both are past their first check
            mine.waited = True
            both.wait()

    monkeypatch.setattr(cards, "_refuse_taken", taken)
    results: list = []

    def run(fn, *a):
        try:
            results.append(fn(*a))
        except ApiError as exc:
            results.append(exc)

    threads = [
        threading.Thread(
            target=run,
            args=(cards.claim_verify, rsvc, card, key, {"email": START["email"], "code": code}),
        ),
        threading.Thread(target=run, args=(cards.claim_confirm, rsvc, card, key, {"token": token})),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    won = [r for r in results if isinstance(r, dict)]
    lost = [r for r in results if isinstance(r, ApiError)]
    assert len(won) == 1 and len(lost) == 1, results
    assert lost[0].status == 410 and lost[0].error == "expired"
    assert len(land.of(9007)) == 1 and len(land.of(9000)) == 1


def test_a_topic_without_a_title_is_named_by_its_hashtag(land):
    rsvc = land.rsvc
    card = land.card("topic")
    with TestClient(create_app(rsvc)) as c:
        key = PrivateKey()
        body = {
            "email": "vera@example.org",
            "name": "Vera",
            "about": "Rye and spelt",
            "hashtag": "bread",
        }
        r = call(c, key, f"/cards/{card}/claim-start", body)
        assert r.status_code == 202, r.text
        _code_, token = _confirm_mail()
        r = call(c, key, f"/cards/{card}/claim-confirm", {"token": token})
        assert r.status_code == 200, r.text
        assert r.json()["group"] == "bread" and r.json()["summary"] == "weekly"
        assert land.of(9002, "bread")[0]["tags"][1:3] == [
            ["name", "bread"],
            ["about", "Rye and spelt"],
        ]
        said = land.of(9, "general")[0]["content"]
        assert said.startswith("# New topic: #bread · Rye and spelt")
        assert "Rye and spelt · " not in said
        assert _follows(rsvc, "bread")
        assert rsvc.odoo.card_get(card)["title"] == "bread"
