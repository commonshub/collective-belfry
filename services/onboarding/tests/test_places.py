"""Places for profiles (places.py) and the admin role from the chat (admins.py)."""

from __future__ import annotations

import time

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import admins, apps, console, core, places, steward
from belfry_onboarding.app import create_app
from belfry_onboarding.register import Register

from .test_holder_invites import BASE, post


@pytest.fixture
def rsvc(services, tmp_path, settings):
    services.odoo = Register(tmp_path / "register.sqlite", settings)
    services.settings.public_url = BASE
    return services


def member(rsvc, email: str, name: str, tier: str | None = None) -> str:
    made = steward.steward_pass(rsvc, email, name)
    key = PrivateKey()
    vals = {"keycast_pubkey": key.public_key_xonly.format().hex()}
    if tier:
        vals["tier"] = tier
    rsvc.odoo.write("belfry.pass", [made["pass_id"]], vals)
    return key


def hexkey(key: PrivateKey) -> str:
    return key.public_key_xonly.format().hex()


def test_search_folds_accents_and_ranks_by_population(rsvc):
    found = places.search(rsvc, "brus")
    assert found[0]["id"] == "gn:2800866" and found[0]["name"] == "Brussels"
    assert found[0]["country"] == "BE" and found[0]["lat"] == 50.85
    assert [p["name"] for p in places.search(rsvc, "LIEGE")][:1] == ["Liège"]
    assert places.search(rsvc, "b") == []  # two characters at least
    assert len(places.search(rsvc, "san", limit=99)) == places.MAX_LIMIT
    # a word inside the name too, after the names that start with it
    assert any(p["name"] == "Rio de Janeiro" for p in places.search(rsvc, "janeiro"))


def test_the_api_answers_with_attribution_and_cors(rsvc):
    with TestClient(create_app(rsvc)) as c:
        r = c.get(
            "/api/places",
            params={"q": "brus"},
            headers={"Origin": f"https://chat.{rsvc.settings.nip05_domain}"},
        )
        assert r.status_code == 200
        assert r.json()["attribution"] == places.ATTRIBUTION
        assert r.json()["places"][0]["name"] == "Brussels"
        assert (
            r.headers["access-control-allow-origin"] == f"https://chat.{rsvc.settings.nip05_domain}"
        )


def test_a_member_adds_a_place_near_a_city_once(rsvc):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    body = {"name": " Moorsel ", "country": "be", "near": "gn:2800866"}
    with TestClient(create_app(rsvc)) as c:
        r = post(c, ada, "/api/places", body)
        assert r.status_code == 200, r.text
        p = r.json()["place"]
        assert p["id"] == "b:1" and p["name"] == "Moorsel" and p["country"] == "BE"
        assert (p["lat"], p["lon"], p["near"]) == (50.85, 4.35, "Brussels")
        assert "added_by" not in p
        time.sleep(1.1)
        again = post(c, ada, "/api/places", {**body, "name": "moorsel"})
        assert again.json()["place"]["id"] == "b:1"  # the same name and country: no copy
        assert places.search(rsvc, "moors")[0]["id"] == "b:1"
        time.sleep(1.1)
        assert post(c, ada, "/api/places", {**body, "country": "XX"}).status_code == 422
        time.sleep(1.1)
        assert post(c, ada, "/api/places", {**body, "near": "gn:0"}).status_code == 422
        time.sleep(1.1)
        assert post(c, ada, "/api/places", {**body, "name": "a\x00b"}).status_code == 422


def test_a_guest_can_not_add_a_place(rsvc):
    gus = member(rsvc, "gus@example.org", "Gus", tier="guest")
    with TestClient(create_app(rsvc)) as c:
        r = post(c, gus, "/api/places", {"name": "X", "country": "BE", "near": "gn:2800866"})
        assert r.status_code == 403 and r.json()["error"] == "not_member"


@pytest.fixture
def admin_set(rsvc, monkeypatch):
    """The stewards group as a set of e-mails; the relay sync recorded."""
    emails: set[str] = set()
    synced: list[int] = []
    monkeypatch.setattr(console, "admin_emails", lambda svc: emails)
    monkeypatch.setattr(
        console,
        "set_admin",
        lambda svc, email, on: emails.add(email) if on else emails.discard(email),
    )
    monkeypatch.setattr(core, "sync_relay_whitelist", lambda svc, *a, **k: synced.append(1))
    return emails, synced


def test_an_admin_gives_and_takes_the_admin_role(rsvc, admin_set):
    emails, synced = admin_set
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bob = member(rsvc, "bob@belfry.test", "Bob")
    emails.add("ada@belfry.test")
    with TestClient(create_app(rsvc)) as c:
        r = post(c, ada, admins.PATH, {"pubkey": hexkey(bob), "admin": True})
        assert r.status_code == 200, r.text
        assert sorted(r.json()["admins"]) == sorted([hexkey(ada), hexkey(bob)])
        assert synced and "bob@belfry.test" in emails
        time.sleep(1.1)
        r = post(c, bob, admins.PATH, {"pubkey": hexkey(ada), "admin": False})
        assert r.status_code == 200 and r.json()["admins"] == [hexkey(bob)]
        time.sleep(1.1)
        r = post(c, bob, admins.PATH, {"pubkey": hexkey(bob), "admin": False})
        assert r.status_code == 400 and r.json()["error"] == "last_admin"
        # no longer an admin: Ada can't give the role
        r = post(c, ada, admins.PATH, {"pubkey": hexkey(ada), "admin": True})
        assert r.status_code == 403
    apps.forget()


def test_unsaved_guests_and_unknown_keys_can_not_be_admins(rsvc, admin_set):
    """A guest needs an e-mail on their account first (then they become a member with the role:
    test_guests.test_an_admin_makes_a_saved_guest_an_admin)."""
    emails, _ = admin_set
    ada = member(rsvc, "ada@belfry.test", "Ada")
    gus = member(rsvc, "gus@example.org", "Gus", tier="guest")
    row = rsvc.odoo.search_read(
        "belfry.pass", [["keycast_pubkey", "=", hexkey(gus)]], ["partner_id"]
    )[0]
    rsvc.odoo.write("res.partner", [core.m2o_id(row["partner_id"])], {"email": False})
    emails.add("ada@belfry.test")
    with TestClient(create_app(rsvc)) as c:
        r = post(c, ada, admins.PATH, {"pubkey": hexkey(gus), "admin": True})
        assert r.status_code == 400 and r.json()["error"] == "guest_not_saved"
        time.sleep(1.1)
        r = post(c, ada, admins.PATH, {"pubkey": "ab" * 32, "admin": True})
        assert r.status_code == 400 and r.json()["error"] == "not_member"
        time.sleep(1.1)
        assert post(c, ada, admins.PATH, {"pubkey": "nope", "admin": True}).status_code == 400
    apps.forget()
