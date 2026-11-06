"""Sign in to an app as name@domain (sessions.py): by NIP-17 DM and over HTTPS."""

from __future__ import annotations

import json

import pytest
from coincurve import PrivateKey

from belfry_onboarding import apps, nostr, sessions, steward

from .test_holder_invites import BASE, post


@pytest.fixture
def member(services, odoo, authentik, keycast):
    services.settings.public_url = BASE
    services.settings.belfry_nsec = PrivateKey().secret.hex()
    authentik.groups["belfry-stewards"] = {"pk": "0000-stewards", "members": set()}
    made = steward.add_steward(services, "ana@example.org", "Ana")
    p = odoo.tables["belfry.pass"][made["pass"]["pass_id"]]
    vine = f"odoo-partner-{p['partner_id']}"
    keycast.users[vine]["username"] = "ana"
    return {
        "pubkey": p["keycast_pubkey"],
        "email": "ana@example.org",
        "partner": p["partner_id"],
        "name": "Ana",
    }


CHAT = "https://chat.example.org/"
CAL = "https://calendar.example.org/app"


def approve(services, member, uri, name=""):
    apps.forget()
    apps.decide(services, member, uri, name, True)


def nip05(services, name="ana"):
    return f"{name}@{services.settings.nip05_domain}"


def test_unwrap_and_wrap_round_trip():
    a, b = PrivateKey(), PrivateKey()
    bpk = b.public_key_xonly.format().hex()
    w = sessions.wrap_dm(a.secret, bpk, "hello", [["client", "Chat"]])
    assert w["kind"] == 1059 and ["p", bpk] in w["tags"]
    assert w["pubkey"] != a.public_key_xonly.format().hex()
    sender, rumor = sessions.unwrap(b.secret, w)
    assert sender == a.public_key_xonly.format().hex() and rumor["content"] == "hello"
    with pytest.raises(ValueError):
        sessions.unwrap(PrivateKey().secret, w)


def test_sign_in_by_dm(services, odoo, keycast, member, client):
    approve(services, member, CHAT, "Chat")
    belfry = PrivateKey(bytes.fromhex(services.settings.belfry_nsec))
    session = PrivateKey()
    spk = session.public_key_xonly.format().hex()
    s = client.app.state.sessions

    def say(text, tags=()):
        w = sessions.wrap_dm(
            session.secret, belfry.public_key_xonly.format().hex(), text, list(tags)
        )
        sender, rumor = sessions.unwrap(belfry.secret, w)
        assert sender == spk
        text, tags = s.answer_dm(sender, rumor)
        # the reply is readable by the session key only
        _, back = sessions.unwrap(session.secret, sessions.wrap_dm(belfry.secret, spk, text, tags))
        return back["content"], back["tags"]

    text, tags = say(
        json.dumps({"nip05": nip05(services), "app": CHAT}),
    )
    assert ["belfry", "code_sent"] in tags and "a•••@example.org" in text
    code = odoo.mailed_codes["ana@example.org"]
    text, tags = say("000000")
    assert ["belfry", "error"] in tags and "not right" in text
    text, tags = say(f"Here it is: {code}")
    assert ["belfry", "open"] in tags
    bunker = next(t[1] for t in tags if t[0] == "bunker")
    assert bunker == keycast.bunkers[-1]["bunker_url"] and keycast.bunkers[-1]["app_name"] == "Chat"
    assert keycast.bunkers[-1]["pubkey"] == member["pubkey"]
    # the session key never lands in the registry as the member's key
    assert odoo.tables["res.partner"][member["partner"]].get("belfry_verified_pubkey") != spk


def test_sign_in_over_https(services, odoo, keycast, member, client):
    approve(services, member, CAL)
    key = PrivateKey()
    r = post(client, key, "/api/session", {"nip05": nip05(services), "app": CAL})
    assert r.status_code == 200 and r.json()["state"] == "code_sent"
    assert post(client, key, "/api/session/verify", {"code": "111111"}).status_code == 422
    code = odoo.mailed_codes["ana@example.org"]
    r = post(client, key, "/api/session/verify", {"code": code})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["state"] == "open" and data["npub"] == nostr.hex_to_npub(member["pubkey"])
    assert data["bunker_url"].startswith("bunker://")
    # another key can't use this request
    assert post(client, PrivateKey(), "/api/session/verify", {"code": code}).status_code == 410


def test_unknown_names_and_other_domains(services, member, client):
    key = PrivateKey()
    r = post(client, key, "/api/session", {"nip05": nip05(services, "nobody"), "app": CHAT})
    assert r.status_code == 404 and r.json()["error"] == "unknown"
    r = post(client, key, "/api/session", {"nip05": "ana@elsewhere.example", "app": CHAT})
    assert r.status_code == 404
    r = post(client, key, "/api/session", {"nip05": nip05(services), "app": "Chat"})
    assert r.status_code == 422 and r.json()["error"] == "app_address"
    r = post(client, key, "/api/session", {"nip05": "not an address"})
    assert r.status_code == 422
    assert client.post("/api/session", json={"nip05": nip05(services)}).status_code == 401


def test_an_app_nobody_approved_gets_no_session(
    services, odoo, keycast, relay, authentik, member, client
):
    # Bob is a member, not an admin: the right code sends the admins a request, signed by Bob
    made = steward.add_steward(services, "bob@example.org", "Bob")
    bob_pk = next(u["pk"] for u in authentik.users if u.get("email") == "bob@example.org")
    authentik.groups["belfry-stewards"]["members"].discard(bob_pk)
    from belfry_onboarding import console

    console._admins.update(at=0.0)
    apps.forget()
    bob_pid = odoo.tables["belfry.pass"][made["pass"]["pass_id"]]["partner_id"]
    keycast.users[f"odoo-partner-{bob_pid}"]["username"] = "bob"
    assert not apps.is_admin(services, {"email": "bob@example.org"})
    key = PrivateKey()
    r = post(client, key, "/api/session", {"nip05": nip05(services, "bob"), "app": CHAT})
    assert r.status_code == 200
    r = post(client, key, "/api/session/verify", {"code": odoo.mailed_codes["bob@example.org"]})
    assert r.status_code == 403 and r.json()["error"] == "app_not_approved"
    req = [e for e in relay.events if e["kind"] == apps.REQUEST]
    assert len(req) == 1 and ["r", CHAT] in req[0]["tags"]
