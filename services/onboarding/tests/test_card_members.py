"""Printed cards whose people become members (docs/cards.md §6, CARD-35): the batch's setting
and its default, a member card's claim and joins (with and without an e-mail), a guests' card
unchanged, the one-time migration of the card guests who came before, and the console's radio."""

# ruff: noqa: F811
from __future__ import annotations

import sqlite3

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import cards, cardsconsole, guests, joined, roles
from belfry_onboarding.app import create_app
from belfry_onboarding.core import m2o_id
from belfry_onboarding.places import member_key
from belfry_onboarding.register import Register

from .test_cards import FORM, HOST, get, land, saved_guest, visitor_claim  # noqa: F401
from .test_guests import _code, published, rsvc  # noqa: F401 — fixtures
from .test_roles import call, person, pk, world  # noqa: F401 — fixtures
from .test_sales_console import H, post, signed_in


@pytest.fixture
def lines(monkeypatch) -> list[str]:
    """The town square's join lines (joined.py), by key."""
    out: list[str] = []
    monkeypatch.setattr(
        joined, "announce", lambda svc, pubkey, name, agent=False: out.append(pubkey) or True
    )
    return out


def tiers(rsvc, pubkey: str) -> set[str]:
    rows = rsvc.odoo.search_read(
        "belfry.pass", [["keycast_pubkey", "=", pubkey], ["state", "=", "active"]], ["tier"]
    )
    return {r["tier"] for r in rows}


def claimed(land, kind="group", join_as="member", **form):
    """A card of KIND claimed by Bob (a member) → (card id, group, its link's token)."""
    bob = getattr(land, "bob", None)
    if bob is None:
        land.bob, _ = person(land.rsvc, "bob@belfry.test", "Bob")
    card = land.card(kind, join_as=join_as)
    with TestClient(create_app(land.rsvc)) as c:
        r = call(c, land.bob, f"/cards/{card}/claim", form or FORM)
        assert r.status_code == 200, r.text
    return card, r.json()["group"], land.rsvc.odoo.card_get(card)["invite"]


def reading_join(land, c, token):
    """Open a card's link with no name; our key then stands in for the guest's identity (the
    signer holds the real one) → (our key, its hex)."""
    got = call(c, PrivateKey(), f"/channel-invites/{token}/join", {})
    assert got.status_code == 200 and got.json()["reading"] is True, got.text
    me = PrivateKey()
    p = land.rsvc.odoo.search_read(
        "belfry.pass", [["keycast_pubkey", "=", got.json()["pubkey"]]], ["id", "partner_id"]
    )[0]
    land.rsvc.odoo.write("belfry.pass", [p["id"]], {"keycast_pubkey": pk(me)})
    stand_in(land, m2o_id(p["partner_id"]), pk(me))
    return me, pk(me)


def stand_in(land, partner: int, pubkey: str) -> None:
    """The signer holds PUBKEY as that contact's identity (our key stands in for it)."""
    users = land.keycast.users
    users.setdefault(f"odoo-partner-{partner}", {"username": f"guest-{partner}"})
    users[f"odoo-partner-{partner}"]["pubkey"] = pubkey
    land.keycast.status[pubkey] = "active"


def known_guest(land, **kw) -> PrivateKey:
    """``saved_guest``, with the signer knowing their key."""
    key = saved_guest(land.rsvc, **kw)
    row = land.rsvc.odoo.search_read(
        "belfry.pass", [["keycast_pubkey", "=", pk(key)]], ["partner_id"]
    )
    stand_in(land, m2o_id(row[0]["partner_id"]), pk(key))
    return key


def save(c, key, email, name="Nina Newcomer"):
    r = call(c, key, "/guest/save", {"email": email, "name": name})
    assert r.status_code == 202, r.text
    return call(c, key, "/guest/save/verify", {"email": email, "code": _code(), "name": name})


# ── the setting and its default ─────────────────────────────────────────────────────────────
def test_a_new_batch_makes_members_cards_unless_it_says_guests(land):
    key = cardsconsole.make(land.rsvc, "group", "B", 2, land.ada_id)
    batch = next(b for b in cardsconsole.batches(land.rsvc) if b["key"] == key)
    assert batch["join_as"] == "member"
    assert {c["join_as"] for c in batch["cards"]} == {"member"}
    key = cardsconsole.make(land.rsvc, "topic", "G", 1, land.ada_id, join_as="guest")
    batch = next(b for b in cardsconsole.batches(land.rsvc) if b["key"] == key)
    assert batch["join_as"] == "guest" and batch["cards"][0]["join_as"] == "guest"
    with pytest.raises(ValueError):
        land.rsvc.odoo.card_make(["ABCDEF"], "group", "B", 1, 1, 4, "admin")


def test_cards_made_before_the_setting_are_members_cards(tmp_path, settings):
    path = tmp_path / "old.sqlite"
    Register(path, settings)._db.close()
    db = sqlite3.connect(path)
    db.execute("DROP TABLE group_cards")
    db.execute(
        "CREATE TABLE group_cards (id TEXT PRIMARY KEY, kind TEXT NOT NULL, batch TEXT NOT NULL,"
        " state TEXT NOT NULL, group_id TEXT, invite TEXT, title TEXT, made_by INTEGER,"
        " made_at INTEGER NOT NULL, claimed_by INTEGER, claimed_key TEXT, claimed_at INTEGER,"
        " per_page INTEGER NOT NULL DEFAULT 4)"
    )
    db.execute(
        "INSERT INTO group_cards VALUES ('ABCDEF','topic','B','claimed',"
        "'bread','tok',NULL,1,1,NULL,NULL,NULL,4)"
    )
    db.commit()
    db.close()
    reg = Register(path, settings)
    assert reg.card_get("ABCDEF")["join_as"] == "member"
    assert cards.joins_as_member(reg.card_get("ABCDEF"))


# ── a member card ───────────────────────────────────────────────────────────────────────────
def test_a_visitor_who_starts_a_members_card_is_a_member_and_its_steward(land, lines):
    rsvc = land.rsvc
    card = land.card(join_as="member")
    with TestClient(create_app(rsvc)) as c:
        _key, r = visitor_claim(c, card, "vera@example.org")
        assert r.status_code == 200, r.text
        out = r.json()
        group = out["group"]
        assert out["signed_in"] is True and out["member"] is True
        assert member_key(rsvc, out["pubkey"]) is not None
        assert tiers(rsvc, out["pubkey"]) == {"member"}  # no guest pass left
        assert roles.role_of(rsvc, out["pubkey"]) == roles.default_role(rsvc)
        assert out["pubkey"] in lines  # the join line in the town square
        put = land.of(9000, group)
        assert len(put) == 1 and ["p", out["pubkey"], "admin"] in put[0]["tags"]
        assert not [t for t in put[0]["tags"] if t[0] == "guest"]
        assert rsvc.odoo.card_get(card)["state"] == "claimed"


def test_a_saved_guest_who_starts_a_members_card_becomes_a_member(land, lines):
    rsvc = land.rsvc
    gus = known_guest(land)
    card = land.card(join_as="member")
    with TestClient(create_app(rsvc)) as c:
        r = call(c, gus, f"/cards/{card}/claim", FORM)
        assert r.status_code == 200, r.text
        assert r.json()["member"] is True
    assert tiers(rsvc, pk(gus)) == {"member"} and pk(gus) in lines
    put = land.of(9000, r.json()["group"])
    assert ["p", pk(gus), "admin"] in put[0]["tags"]
    assert not [t for t in put[0]["tags"] if t[0] == "guest"]


def test_joining_a_members_card_with_an_e_mail_makes_a_member(land, lines):
    rsvc = land.rsvc
    _card, group, token = claimed(land)
    with TestClient(create_app(rsvc)) as c:
        # the join answer keeps a card's shape: no "as": "member", no email_needed
        info = c.get(f"/api/v1/channel-invites/{token}", headers=HOST).json()
        assert info["card"] == "group" and "member_role" not in info
        me, me_pk = reading_join(land, c, token)
        assert member_key(rsvc, me_pk) is None and tiers(rsvc, me_pk) == {"guest"}
        r = save(c, me, "nina@example.org")
        assert r.status_code == 200, r.text
        assert r.json()["saved"] is True and r.json()["member"] is True
        assert r.json()["role"] == roles.default_role(rsvc)
    assert tiers(rsvc, me_pk) == {"member"} and me_pk in lines
    # in the card's group: the 9000 with the name (the join line there)
    assert any(
        ["p", me_pk, "member"] in e["tags"] and ["name", "Nina Newcomer"] in e["tags"]
        for e in land.of(9000, group)
    )


def test_a_saved_guest_who_scans_a_members_card_becomes_a_member(land, lines):
    rsvc = land.rsvc
    _card, group, token = claimed(land)
    gus = known_guest(land)
    with TestClient(create_app(rsvc)) as c:
        r = call(c, gus, f"/channel-invites/{token}/join", {})
        assert r.status_code == 200, r.text
        assert r.json()["member"] is True and r.json()["guest"] is False
        assert r.json()["card"] == "group"
    assert tiers(rsvc, pk(gus)) == {"member"} and pk(gus) in lines
    assert any(["p", pk(gus), "member"] in e["tags"] for e in land.of(9000, group))


def test_a_topic_without_an_e_mail_stays_a_guest_until_it_saves_one(land, lines):
    rsvc = land.rsvc
    _card, group, token = claimed(land, "topic", hashtag="bread")
    with TestClient(create_app(rsvc)) as c:
        me, me_pk = reading_join(land, c, token)
        assert member_key(rsvc, me_pk) is None and me_pk not in lines
        # a member who scans it just gets the channel; their role doesn't change
        cy, _ = person(rsvc, "cy@belfry.test", "Cy")
        r = call(c, cy, f"/channel-invites/{token}/join", {})
        assert r.status_code == 200 and r.json()["guest"] is False and "member" not in r.json()
        # later the reader saves an address: a member then
        r = save(c, me, "rita@example.org", "Rita Reader")
        assert r.status_code == 200 and r.json()["member"] is True, r.text
    assert tiers(rsvc, me_pk) == {"member"} and me_pk in lines


def test_a_guests_card_keeps_people_guests(land, lines):
    rsvc = land.rsvc
    card = land.card(join_as="guest")
    with TestClient(create_app(rsvc)) as c:
        _key, r = visitor_claim(c, card, "vera@example.org")
        assert r.status_code == 200 and "member" not in r.json(), r.text
        assert member_key(rsvc, r.json()["pubkey"]) is None
        token = rsvc.odoo.card_get(card)["invite"]
        me, me_pk = reading_join(land, c, token)
        r = save(c, me, "nina@example.org")
        assert r.status_code == 200 and "member" not in r.json(), r.text
        gus = known_guest(land)
        r = call(c, gus, f"/channel-invites/{token}/join", {})
        assert r.status_code == 200 and "member" not in r.json()
    assert member_key(rsvc, me_pk) is None and member_key(rsvc, pk(gus)) is None
    assert not lines


def test_an_agent_stays_a_guest_on_a_members_card(land, monkeypatch):
    _card, _group, token = claimed(land)
    terms = guests.card_member_terms(land.rsvc, land.rsvc.odoo.invite_by_token(token))
    assert terms["member_role"] == roles.default_role(land.rsvc)
    assert terms["membership"] == "permanent"
    # the belfry's default role is Admin: a card still makes plain members
    monkeypatch.setattr(roles, "default_role", lambda svc: roles.ADMIN)
    assert guests.card_terms(land.rsvc, {"id": "X", "join_as": "member"})["member_role"] == (
        roles.MEMBER
    )
    assert guests.card_terms(land.rsvc, {"id": "X", "join_as": "guest"}) is None


# ── the migration ───────────────────────────────────────────────────────────────────────────
def test_the_migration_makes_card_guests_with_an_address_members_once(land, lines):
    rsvc = land.rsvc
    # a guests' card, as every card was until §6: a steward visitor, a saved and a reading guest
    card = land.card(join_as="guest")
    with TestClient(create_app(rsvc)) as c:
        _key, r = visitor_claim(c, card, "vera@example.org")
        steward = r.json()["pubkey"]
        token = rsvc.odoo.card_get(card)["invite"]
        saved, saved_pk = reading_join(land, c, token)
        assert save(c, saved, "nina@example.org").status_code == 200
        _reader, reader_pk = reading_join(land, c, token)
    other = known_guest(land, email="olga@example.org", channel="garden")  # no card
    for k in (steward, saved_pk, reader_pk, pk(other)):
        assert member_key(rsvc, k) is None
    # what the column's default does to cards made before it
    rsvc.odoo._db.execute("UPDATE group_cards SET join_as = 'member'")
    made = cards.migrate_card_guests(rsvc)
    assert sorted(made) == sorted([steward, saved_pk])
    assert tiers(rsvc, steward) == {"member"} and tiers(rsvc, saved_pk) == {"member"}
    assert member_key(rsvc, reader_pk) is None  # reading: a member once they save an address
    assert member_key(rsvc, pk(other)) is None  # never came by a card
    assert rsvc.odoo.param(cards.MIGRATED)
    assert steward in lines and saved_pk in lines
    # once
    assert cards.migrate_card_guests(rsvc) == []


def test_the_migration_leaves_a_guests_card_alone_and_retries_after_a_failure(land, monkeypatch):
    rsvc = land.rsvc
    card = land.card(join_as="guest")
    with TestClient(create_app(rsvc)) as c:
        _key, r = visitor_claim(c, card, "vera@example.org")
    steward = r.json()["pubkey"]
    assert cards.migrate_card_guests(rsvc) == []  # a guests' card: nobody
    assert member_key(rsvc, steward) is None
    rsvc.odoo.set_param(cards.MIGRATED, "")
    rsvc.odoo._db.execute("UPDATE group_cards SET join_as = 'member'")
    monkeypatch.setattr(cards, "member_now", lambda svc, card, pubkey: False)
    assert cards.migrate_card_guests(rsvc) == [] and not rsvc.odoo.param(cards.MIGRATED)
    monkeypatch.undo()
    assert cards.migrate_card_guests(rsvc) == [steward]


# ── the console ─────────────────────────────────────────────────────────────────────────────
def test_the_console_asks_members_or_guests_and_shows_it(land):
    rsvc = land.rsvc
    with TestClient(create_app(rsvc), base_url="https://belfry.belfry.test") as c:
        signed_in(c, rsvc, land.ada_id)
        page = c.get("/cards", headers=H).text
        assert "People who join with these cards become" in page
        assert 'name="join_as" value="member" checked' in page
        assert 'name="join_as" value="guest">' in page
        base = {"kind": "group", "per_page": "1", "pages": "1"}
        r = post(c, "/cards/make", **base, batch="G", join_as="guest")
        assert r.status_code == 303 and "/batch/" in r.headers["location"], r.text
        r = post(c, "/cards/make", **base, batch="M")  # an older form: members
        assert r.status_code == 303 and "/batch/" in r.headers["location"]
        r = post(c, "/cards/make", **base, batch="X", join_as="admin")
        assert r.status_code == 303 and "/batch/" not in r.headers["location"]
        found = {b["name"]: b["join_as"] for b in cardsconsole.batches(rsvc)}
        assert found == {"G": "guest", "M": "member"}
        page = c.get("/cards", headers=H).text
        assert "<th>People become</th>" in page
        assert "<td>Guests</td>" in page and "<td>Members</td>" in page
