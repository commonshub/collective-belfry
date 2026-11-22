"""Choosing a username while joining: /api/v1/username/check (usernames.py) and a member link's
last step, /api/v1/welcome, with the welcome mail that waits for it (welcome.py)."""

# ruff: noqa: F811
from __future__ import annotations

import datetime as dt
import json

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import community, core, mail, mailer, usernames, welcome
from belfry_onboarding.app import create_app
from belfry_onboarding.events import Limiter
from belfry_onboarding.places import member_key

from .test_guests import published, rsvc  # noqa: F401 — fixtures
from .test_roles import _saved_reader, call, person, pk, world  # noqa: F401 — fixtures

JANE = "jane.doe@example.org"


@pytest.fixture
def town(world, monkeypatch):
    """A belfry with its own sign-in, an admin (Ada) and one channel, #garden."""
    rsvc = world[0]
    monkeypatch.setattr(mail, "channel_id", lambda svc, name: name if name == "garden" else None)
    rsvc.settings.pass_signin = "belfry"
    usernames._taken.clear()
    usernames.CHECKS._seen.clear()
    yield world
    usernames._taken.clear()
    usernames.CHECKS._seen.clear()


def check(c, key, **body):
    r = call(c, key, "/username/check", body)
    assert r.status_code == 200, r.text
    return r.json()


def welcomes(address):
    return [
        m.get_body(preferencelist=("plain",)).get_content()
        for m in mailer.SENT
        if m["To"] == address and str(m["Subject"]).startswith("Welcome to ")
    ]


def test_candidates_are_the_first_name_then_first_last_then_numbers():
    assert usernames.candidates("Lina Moreau")[:4] == ["lina", "lina-moreau", "lina-2", "lina-3"]
    assert usernames.candidates("Ana María Pérez")[:3] == ["ana", "ana-perez", "ana-2"]
    assert usernames.candidates("Lina")[:2] == ["lina", "lina-2"]
    assert usernames.candidates("李") == []
    assert all(len(c) <= usernames.MAX for c in usernames.candidates("x" * 40 + " y"))
    assert [usernames.problem(n) for n in ("", "a" * 31, "a b", "-a", "admin", "a", "a.b_c-d")] == [
        "short",
        "long",
        "chars",
        "chars",
        "reserved",
        None,
        None,
    ]


def test_checking_a_username(town):
    rsvc, _p, keycast, _ada, _ada_id, _s = town
    anyone, bob = PrivateKey(), PrivateKey()  # a key with no account at all may ask
    keycast.users["lina"] = {"pubkey": "ab" * 32, "username": "lina"}
    keycast.users["bob"] = {"pubkey": pk(bob), "username": "bob"}
    with TestClient(create_app(rsvc)) as c:
        assert c.post("/api/username/check", json={"username": "x"}).status_code == 401
        assert check(c, anyone, username=" Nora ") == {
            "username": "nora",
            "state": "available",
            "suggestions": [],
        }
        # taken: suggestions from the name when sent, else from the username itself
        assert check(c, anyone, username="Lina") == {
            "username": "lina",
            "state": "taken",
            "suggestions": ["lina-2", "lina-3", "lina-4"],
        }
        got = check(c, anyone, username="lina", name="Lina Moreau")
        assert got["state"] == "taken"
        assert got["suggestions"] == ["lina-moreau", "lina-2", "lina-3"]
        keycast.users["lm"] = {"pubkey": "cd" * 32, "username": "lina-moreau"}
        usernames._taken.clear()
        got = check(c, anyone, username="lina", name="Lina Moreau")
        assert got["suggestions"] == ["lina-2", "lina-3", "lina-4"]
        # a channel's name is taken too (it has a mailbox)
        assert check(c, anyone, username="garden")["state"] == "taken"
        # what can't be a username, and why
        for bad, reason in (
            ("", "short"),
            ("a" * 31, "long"),
            ("li na", "chars"),
            (".lina", "chars"),
            ("admin", "reserved"),
            ("postmaster", "reserved"),
        ):
            got = check(c, anyone, username=bad)
            assert (got["state"], got["reason"]) == ("invalid", reason), bad
        assert check(c, anyone, username="Hello", name="Nora New")["suggestions"][0] == "nora"
        # their own username is theirs to keep
        assert check(c, bob, username="bob")["state"] == "available"
        assert check(c, anyone, username="bob")["state"] == "taken"
        # a name alone: the default and the others
        assert check(c, anyone, name="Nora New") == {
            "suggested": "nora",
            "suggestions": ["nora", "nora-new", "nora-2"],
        }
        assert check(c, anyone, name="Lina Moreau", username="")["suggested"] == "lina-2"
        assert check(c, anyone, name="李") == {"suggested": "", "suggestions": []}
        r = call(c, anyone, "/username/check", {})
        assert r.status_code == 400 and r.json()["error"] == "missing"


def test_checks_are_limited_per_key(town, monkeypatch):
    rsvc = town[0]
    monkeypatch.setattr(usernames, "CHECKS", Limiter(2, 60))
    key = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        for name in ("a", "b"):
            assert call(c, key, "/username/check", {"username": name}).status_code == 200
        r = call(c, key, "/username/check", {"username": "c"})
        assert r.status_code == 429 and r.json()["error"] == "too_many"
        # another key from the same address of the network: that one counts too
        r = call(c, PrivateKey(), "/username/check", {"username": "c"})
        assert r.status_code == 429


def member_link(c, ada):
    r = call(c, ada, "/channel-invites", {"channel": "garden", "as": "member"})
    return r.json()["url"].rsplit("/", 1)[1]


def profiles(rsvc, pubkey):
    found = [e for e in rsvc.relay.events if e["kind"] == 0 and e["pubkey"] == pubkey]
    return sorted(found, key=lambda e: e["created_at"], reverse=True)


def test_a_member_link_ends_with_a_name_and_a_username(town, monkeypatch):
    rsvc, _p, keycast, ada, _ada_id, _s = town

    def query(svc, filters):  # the relay's answer for a profile; nothing else is asked here
        if filters[0].get("kinds") != [0]:
            return []
        return profiles(svc, filters[0]["authors"][0])[:1]

    monkeypatch.setattr(community, "query", query)
    keycast.users["lina"] = {"pubkey": "ab" * 32, "username": "lina"}
    with TestClient(create_app(rsvc)) as c:
        me, r = _saved_reader(rsvc, keycast, c, member_link(c, ada), JANE, None)
        assert r.status_code == 200 and r.json()["member"] is True, r.text
        row = member_key(rsvc, pk(me))
        partner = core.m2o_id(row["partner_id"])
        # a member, and the welcome mail waits for the name and username they pick
        assert welcomes(JANE) == []
        welcome.tick(rsvc)
        assert welcomes(JANE) == [], "not in the first ten minutes"
        # their profile so far, with a picture and a line about them
        before = {"name": "Jane Doe", "picture": "https://x/p.png", "about": "Gardener"}
        rsvc.relay.events.append(
            {
                "kind": 0,
                "pubkey": pk(me),
                "created_at": 2_000_000_000,
                "tags": [],
                "content": json.dumps(before),
            }
        )
        body = {"name": " Jane  van Dam ", "username": "Jane"}
        assert c.post("/api/welcome", json=body).status_code == 401
        r = call(c, me, "/welcome", {"name": "Jane"})
        assert r.status_code == 400 and r.json()["error"] == "missing"
        r = call(c, me, "/welcome", {"name": "Jane", "username": "admin"})
        assert r.status_code == 422 and r.json()["error"] == "username_invalid"
        r = call(c, me, "/welcome", {"name": "Lina Moreau", "username": "lina"})
        assert r.status_code == 409 and r.json()["error"] == "username_taken"
        assert r.json()["suggestions"] == ["lina-moreau", "lina-2", "lina-3"]
        assert welcomes(JANE) == []
        r = call(c, me, "/welcome", body)
        assert r.status_code == 200, r.text
        assert r.json() == {
            "name": "Jane van Dam",
            "username": "jane",
            "nip05": "jane@belfry.test",
        }
        assert rsvc.odoo.read_one("res.partner", partner, ["name"])["name"] == "Jane van Dam"
        names = {u["pubkey"]: u["username"] for u in keycast.users.values()}
        assert names[pk(me)] == "jane"
        # the profile: the name and the address; its picture and about stay
        latest = profiles(rsvc, pk(me))[0]
        assert latest["created_at"] == 2_000_000_001  # after the one it replaces
        assert json.loads(latest["content"]) == {
            "name": "Jane van Dam",
            "display_name": "Jane van Dam",
            "nip05": "jane@belfry.test",
            "picture": "https://x/p.png",
            "about": "Gardener",
        }
        # the welcome mail left with the address they picked, once
        [text] = welcomes(JANE)
        assert text.startswith("Hi Jane,\n\nYou're a member now.")
        assert "Your address here is jane@belfry.test." in text
        assert "https://belfry.sh/android" in text and "https://belfry.sh/iphone" in text
        assert f"We send a 6-digit code to this e-mail address, {JANE}, each time" in text
        # again within the first day (they changed their mind): fine, and no second mail
        r = call(c, me, "/welcome", {"name": "Jane van Dam", "username": "jane-vd"})
        assert r.status_code == 200 and r.json()["username"] == "jane-vd"
        _old(rsvc, partner, minutes=11)
        welcome.tick(rsvc)
        assert len(welcomes(JANE)) == 1
        # after a day, with a name of their choice: the profile screen
        _old(rsvc, partner, minutes=25 * 60)
        r = call(c, me, "/welcome", {"name": "Jane", "username": "jane"})
        assert r.status_code == 409 and r.json()["error"] == "already_set"
        # …unless their username is still a default one
        keycast.users[f"odoo-partner-{partner}"]["username"] = "0002"
        r = call(c, me, "/welcome", {"name": "Jane", "username": "jane"})
        assert r.status_code == 200 and r.json()["nip05"] == "jane@belfry.test"
        assert len(welcomes(JANE)) == 1


def _old(rsvc, partner, minutes):
    then = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(minutes=minutes)
    for p in rsvc.odoo.search_read("belfry.pass", [["partner_id", "=", partner]], ["tier"]):
        if p["tier"] != "guest":
            rsvc.odoo.write(
                "belfry.pass", [p["id"]], {"activated_at": then.strftime("%Y-%m-%d %H:%M:%S")}
            )


def test_the_welcome_mail_leaves_by_itself_after_ten_minutes(town):
    rsvc, _p, keycast, ada, _ada_id, _s = town
    with TestClient(create_app(rsvc)) as c:
        me, r = _saved_reader(rsvc, keycast, c, member_link(c, ada), JANE, None)
        assert r.json()["member"] is True
    partner = core.m2o_id(member_key(rsvc, pk(me))["partner_id"])
    welcome.tick(rsvc)
    assert welcomes(JANE) == []
    _old(rsvc, partner, minutes=9)
    welcome.tick(rsvc)
    assert welcomes(JANE) == []
    _old(rsvc, partner, minutes=11)
    welcome.tick(rsvc)
    [text] = welcomes(JANE)
    # the name from the address, and the username the activation gave
    assert text.startswith("Hi Jane,") and "Your address here is jane-doe@belfry.test." in text
    welcome.tick(rsvc)
    assert len(welcomes(JANE)) == 1
    # the call, later: the name and username are set, no second mail
    with TestClient(create_app(rsvc)) as c:
        r = call(c, me, "/welcome", {"name": "Jane Doe", "username": "jane"})
        assert r.status_code == 200, r.text
    assert len(welcomes(JANE)) == 1


def test_guests_have_no_welcome_step(town):
    rsvc, _p, keycast, ada, _ada_id, _s = town
    with TestClient(create_app(rsvc)) as c:
        token = call(c, ada, "/channel-invites", {"channel": "garden"}).json()["url"]
        gus, r = _saved_reader(rsvc, keycast, c, token.rsplit("/", 1)[1], "gus@example.org", "Gus")
        assert r.status_code == 200 and member_key(rsvc, pk(gus)) is None
        r = call(c, gus, "/welcome", {"name": "Gus", "username": "gus"})
        assert r.status_code == 403 and r.json()["error"] == "not_member"
        r = call(c, PrivateKey(), "/welcome", {"name": "Gus", "username": "gus"})
        assert r.status_code == 403 and r.json()["error"] == "not_member"


def test_an_accounts_own_starting_name_may_be_sent_back(services, monkeypatch):
    """A member still called by their starting name ("osv-0007") who sends it back unchanged
    (a profile saved as it is) is not refused as if choosing a reserved name; anyone else is."""
    from belfry_onboarding import usernames as u
    from belfry_onboarding.holder_invites import ApiError

    mine, other = "aa" * 32, "bb" * 32
    monkeypatch.setattr(u, "holder", lambda svc, name: mine if name == "osv-0007" else "")
    assert u.require(services, "osv-0007", mine=mine) == "osv-0007"
    assert u.check(services, mine, {"username": "osv-0007"})["state"] == "available"
    with pytest.raises(ApiError) as exc:
        u.require(services, "osv-0007", mine=other)
    assert exc.value.status == 422
    assert u.check(services, other, {"username": "osv-0007"})["state"] == "invalid"
