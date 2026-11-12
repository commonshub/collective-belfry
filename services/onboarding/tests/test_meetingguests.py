"""People a meeting's host adds by name (meetingguests.py, Xavier 2026-11-12): an identity for
them, held by the host when there is no address, handed over by that host alone."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from belfry_onboarding import core, guests, holder_invites, mailer, mentions, webthreads
from belfry_onboarding.app import create_app
from belfry_onboarding.register import Register

from .test_webthreads import FakeRelay, GK, MEMBER
from belfry_onboarding import followers

HOST, OTHER, NEW = "c" * 64, "d" * 64, "9" * 64
CH = "ev-2026-abc123"


@pytest.fixture
def world(services, tmp_path, settings, monkeypatch):
    mailer.SENT.clear()
    services.odoo = Register(tmp_path / "register.sqlite", settings)
    services.settings.public_url = "https://belfry.test"
    relay = FakeRelay()
    relay.events += [
        {"id": "g0", "kind": 39000, "pubkey": GK, "created_at": 1, "tags": [["d", CH], ["name", "Plan"]], "content": ""},
        {"id": "g1", "kind": 39001, "pubkey": GK, "created_at": 1, "tags": [["d", CH], ["p", HOST]], "content": ""},
    ]
    monkeypatch.setattr(followers, "_relay", lambda svc: (relay, relay.sign))
    who = {"caller": HOST}
    monkeypatch.setattr(holder_invites, "authenticate", lambda *a: who["caller"])
    made: list = []

    def ensure(svc, partner, number, own_pubkey=None):
        made.append(partner["id"])
        return NEW, "npub1new", ""

    monkeypatch.setattr(core, "_ensure_keycast_user", ensure)
    reg = services.odoo
    for key in (HOST, OTHER, MEMBER):
        pid = reg._create("res.partner", {"name": key[:3], "email": f"{key[:3]}@example.org"})
        reg._create("belfry.pass", {"partner_id": pid, "tier": "member", "state": "active", "keycast_pubkey": key})
    yield services, relay, who, made


def joins(relay):
    return [e for e in relay.events if e["kind"] == 9000 and ["h", CH] in e["tags"]]


def test_a_name_only_identity_held_by_the_host(world):
    svc, relay, who, made = world
    with TestClient(create_app(svc)) as c:
        r = c.post("/api/meeting-guests", json={"channel": CH, "name": "Grandma Rose"})
        assert r.status_code == 200, r.text
        assert r.json() == {"pubkey": NEW, "held": True}
        pid = made[0]
        assert svc.odoo.read_one("res.partner", pid, ["name"])["name"] == "Grandma Rose"
        p = svc.odoo.search_read("belfry.pass", [["partner_id", "=", pid]], ["state", "guest_channel", "guest_inviter", "keycast_pubkey"])
        assert (p[0]["state"], p[0]["guest_channel"], p[0]["guest_inviter"]) == ("active", CH, HOST)
        j = joins(relay)
        assert j and ["p", NEW, "member"] in j[-1]["tags"] and ["unsaved", "1"] in j[-1]["tags"]
        assert ["name", "Grandma Rose"] in j[-1]["tags"]
        assert mentions.prefs(svc, pid)["f"] == "off"
        # only the host who made it hands it over
        who["caller"] = OTHER
        r = c.post("/api/meeting-guests/transfer", json={"pubkey": NEW, "email": "rose@example.org"})
        assert r.status_code == 409 and r.json()["error"] == "not_yours"
        who["caller"] = HOST
        r = c.post("/api/meeting-guests/transfer", json={"pubkey": NEW, "email": "ccc@example.org"})
        assert r.status_code == 409 and r.json()["error"] == "taken"  # someone's address already
        r = c.post("/api/meeting-guests/transfer", json={"pubkey": NEW, "email": "Rose@Example.org"})
        assert r.status_code == 200 and r.json() == {"transferred": True}
        assert svc.odoo.read_one("res.partner", pid, ["email"])["email"] == "rose@example.org"
        assert mailer.SENT[-1]["To"] == "rose@example.org"
        saved = [e for e in relay.events if e["kind"] == 1985 and ["l", "saved", "belfry/guest"] in e["tags"]]
        assert saved and ["p", NEW] in saved[-1]["tags"]
        r = c.post("/api/meeting-guests/transfer", json={"pubkey": NEW, "email": "other@example.org"})
        assert r.status_code == 409 and r.json()["error"] == "has_email"


def test_only_hosts_and_members(world):
    svc, relay, who, made = world
    with TestClient(create_app(svc)) as c:
        who["caller"] = OTHER  # a member, not a host of this meeting
        r = c.post("/api/meeting-guests", json={"channel": CH, "name": "Rose"})
        assert r.status_code == 403 and r.json()["error"] == "not_host"
        who["caller"] = NEW  # nobody
        r = c.post("/api/meeting-guests", json={"channel": CH, "name": "Rose"})
        assert r.status_code == 403
        who["caller"] = HOST
        assert c.post("/api/meeting-guests", json={"channel": "general", "name": "Rose"}).status_code == 422
        assert c.post("/api/meeting-guests", json={"channel": CH, "name": ""}).status_code == 422
        assert c.post("/api/meeting-guests", json={"channel": "ev-2026-zzzzzz", "name": "R"}).status_code == 404
        assert not made


def test_with_an_address_the_same_pipes_as_the_web(world, monkeypatch):
    svc, relay, who, made = world
    seen = []
    monkeypatch.setattr(webthreads, "web_identity", lambda svc, ch, name, email: seen.append((ch, name, email)) or {"partner": 1, "pubkey": NEW})
    with TestClient(create_app(svc)) as c:
        r = c.post("/api/meeting-guests", json={"channel": CH, "name": "Rose", "email": "Rose@Example.org"})
        assert r.status_code == 200 and r.json() == {"pubkey": NEW}
        assert seen == [(CH, "Rose", "rose@example.org")]
        # a member's address: they stay themselves and are put in the meeting's chat
        monkeypatch.setattr(webthreads, "web_identity", lambda *a: {"partner": 2, "pubkey": OTHER})
        r = c.post("/api/meeting-guests", json={"channel": CH, "name": "Dee", "email": "ddd@example.org"})
        assert r.json() == {"pubkey": OTHER, "member": True}
        assert ["p", OTHER, "member"] in joins(relay)[-1]["tags"]
    assert guests  # imported for the monkeypatch targets
