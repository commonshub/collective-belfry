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
    checkin,
    events,
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
        assert made[0]["tags"] == [["h", group], ["open"], ["public"]]
        assert meta[0]["tags"] == [
            ["h", group],
            ["name", "Sourdough starters"],
            ["about", "Swap and feed"],
            ["open"],
            ["public"],
        ]
        # announced in general exactly as the chat does
        said = [e for e in land.of(9, "general") if ["pod", group] in e["tags"]]
        assert len(said) == 1
        assert said[0]["content"] == "⬡ New group: Sourdough starters · Swap and feed"
        assert said[0]["tags"] == [["h", "general"], ["pod", group]]
        # Bob is in it, with the admin role; the first line names him, never the card
        put = land.of(9000, group)
        assert len(put) == 1 and ["p", pk(bob), "admin"] in put[0]["tags"]
        assert ["role", "admin"] in put[0]["tags"] and ["name", "Bob Baker"] in put[0]["tags"]
        assert not any(t[0] == "guest" for t in put[0]["tags"])
        st = checkin.group_state(rsvc, group)
        assert pk(bob) in st.admins and pk(bob) in st.members
        line = land.of(9, group)
        assert [e["content"] for e in line] == ["Bob Baker started this group from a printed card"]
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
        assert r.status_code == 200 and r.json() == {"group": "bread"}
        # a channel as the chat's New channel makes one: no access tags, no post in general
        assert land.of(9007, "bread")[0]["tags"] == [["h", "bread"]]
        assert land.of(9002, "bread")[0]["tags"] == [
            ["h", "bread"],
            ["name", "Sourdough starters"],
            ["about", "Swap and feed"],
        ]
        assert not land.of(9, "general")
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
    with TestClient(create_app(rsvc), base_url="https://belfry.belfry.test") as c:
        r = c.get("/cards", headers=H, follow_redirects=False)
        assert r.status_code == 303 and "/login" in r.headers["location"]
        signed_in(c, rsvc, bob_id)
        r = c.get("/cards", headers=H)
        assert r.status_code == 403 and 'href="/cards"' not in r.text  # not in a member's menu
        assert c.get("/cards/print/1", headers=H).status_code == 403
        assert post(c, "/cards/make", kind="group", count="4").status_code == 303
        assert rsvc.odoo.card_list() == []
        signed_in(c, rsvc, land.ada_id)
        t = c.get("/cards", headers=H).text
        assert "Printed cards" in t and "No cards yet." in t and 'href="/cards"' in t
        # without the csrf, an unknown kind, too many: nothing
        r = c.post("/cards/make", data={"kind": "group", "count": "4"}, headers=H)
        assert rsvc.odoo.card_list() == []
        for bad in (
            {"kind": "pod", "count": "4"},
            {"kind": "group", "count": "41"},
            {"kind": "group", "count": "0"},
            {"kind": "group", "count": "many"},
        ):
            assert post(c, "/cards/make", **bad).status_code == 303
        assert rsvc.odoo.card_list() == []
        assert "Make 1 to 40 cards at a time." in c.get("/cards", headers=H).text


def test_batches_the_print_view_and_reset(land):
    rsvc = land.rsvc
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    with TestClient(create_app(rsvc), base_url="https://belfry.belfry.test") as c:
        signed_in(c, rsvc, land.ada_id)
        r = post(c, "/cards/make", kind="group", count="6", batch="Spring <fair>")
        assert r.status_code == 303 and r.headers["location"] == "/cards"
        r = post(c, "/cards/make", kind="topic", count="5", batch="")
        made = rsvc.odoo.card_list()
        assert len(made) == 11 and len({m["id"] for m in made}) == 11
        assert all(cards.ID.fullmatch(m["id"]) and m["state"] == "unclaimed" for m in made)
        assert all(m["made_by"] == land.ada_id for m in made)
        found = cardsconsole.batches(rsvc)
        assert sorted((b["kind"], len(b["cards"])) for b in found) == [("group", 6), ("topic", 5)]
        groups = next(b for b in found if b["kind"] == "group")
        topics = next(b for b in found if b["kind"] == "topic")
        t = c.get("/cards", headers=H).text
        assert "The cards are made." in t and "Spring &lt;fair&gt;" in t and "<fair>" not in t
        assert "Topic cards, " in t and f"/cards/print/{groups['key']}" in t
        # the print view: four A6 cards a sheet, upright for groups
        r = c.get(f"/cards/print/{groups['key']}", headers=H)
        assert r.status_code == 200
        p = r.text
        assert "@page{size:A4 portrait;margin:0}" in p and p.count('class="sheet"') == 2
        assert p.count('class="card"') == 6 and p.count("<svg") == 6
        assert "width:105mm;height:148mm" in p
        assert p.count("Scan to start a group here") == 6
        assert p.count("chat.belfry.test/g/</p>") == 6
        for m in groups["cards"]:  # the id is in the QR only, never printed
            assert m["id"] not in p
        assert rsvc.settings.community_name in p
        # on their side for topics
        p = c.get(f"/cards/print/{topics['key']}", headers=H).text
        assert "@page{size:A4 landscape;margin:0}" in p and "width:148mm;height:105mm" in p
        assert p.count('class="sheet"') == 2 and p.count("Scan to start a topic here") == 5
        assert c.get("/cards/print/12345", headers=H).status_code == 404
        assert c.get("/cards/print/x%3Cb%3E", headers=H).status_code in (400, 404, 422)
        # the QR is the card's address
        import segno

        want = segno.make(f"https://chat.belfry.test/g/{groups['cards'][0]['id']}", error="q")
        assert cardsconsole.qr_svg(f"https://chat.belfry.test/g/{groups['cards'][0]['id']}") == (
            want.svg_inline(scale=4, border=4, dark="#000", light="#fff", omitsize=True)
        )
        assert want.error in ("Q", "H") and not want.is_micro
        # a claimed card shows its group's name; Reset frees it and leaves the group
        card = groups["cards"][0]["id"]
        group = call(c, bob, f"/cards/{card}/claim", FORM).json()["group"]
        land.mirror(group)
        token = rsvc.odoo.card_get(card)["invite"]
        t = c.get("/cards", headers=H).text
        assert "Sourdough starters" in t and "In use" in t and f"/cards/{card}/reset" in t
        assert group not in t  # the name, never the id
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
