"""What an app needs to connect: /.well-known/belfry.json, /api/me and CORS of /api/*."""

from __future__ import annotations

from coincurve import PrivateKey

from belfry_onboarding.nostr import hex_to_npub

from .test_holder_invites import BASE, get, holder, nip98


def test_discovery_document(services, client, monkeypatch):
    s = services.settings
    s.public_url, s.site_url = BASE, "https://belfry.test"
    s.odoo_public_url = "https://shop.belfry.test"
    s.community_relay_public_url = "wss://relay.belfry.test"
    s.groups_relay_public_url = "wss://groups-relay.belfry.test"
    s.bunker_relays = "wss://bunker.belfry.test"
    s.blossom_public_url = "https://blossom.belfry.test/"
    s.nip05_domain = "belfry.test"
    monkeypatch.setenv("COMMUNITY_APPS", "Chat|https://chat.belfry.test|Messages;Bad|ftp://x|")

    r = client.get("/.well-known/belfry.json", headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "*"
    doc = r.json()
    assert doc["belfry"] == 1 and doc["name"] == "Open Source Village"
    assert doc["domain"] == "belfry.test" and doc["site"] == "https://belfry.test"
    assert doc["relays"] == {
        "community": "wss://relay.belfry.test",
        "groups": "wss://groups-relay.belfry.test",
        "signer": ["wss://bunker.belfry.test"],
    }
    assert doc["blossom"] == "https://blossom.belfry.test"
    assert doc["api"]["me"] == BASE + "/api/me"
    assert doc["api"]["invite_offer"] == "https://shop.belfry.test/belfry/api/invites/{token}"
    assert doc["apps"] == [
        {"name": "Chat", "url": "https://chat.belfry.test", "description": "Messages"}
    ]
    assert doc["skills"]["steward"] == BASE + "/skills/belfry-steward.md"


def test_skills_are_served_by_name(services, client):
    services.settings.site_url = "https://belfry.test"
    r = client.get("/skills/belfry-steward.md")
    assert r.status_code == 200 and "steward" in r.text
    assert "<domain>" not in r.text  # filled in with the deployment's domain
    assert client.get("/skills/open-source-village.md").text == client.get("/skill.md").text
    assert client.get("/skills/nope.md").status_code == 404
    assert client.get("/skills/..%2Fsecrets.md").status_code == 404


def test_me_for_a_member(services, odoo, client):
    key, partner = holder(services, odoo)
    odoo.tables["res.partner"][partner]["belfry_member_number"] = "OSV-0042"
    services.settings.nip05_domain = "belfry.test"
    r = get(client, key, "/api/me")
    assert r.status_code == 200, r.text
    me = r.json()
    pubkey = key.public_key_xonly.format().hex()
    assert me["pubkey"] == pubkey and me["npub"] == hex_to_npub(pubkey)
    assert me["member"] is True and me["steward"] is False
    assert me["nip05"] == "osv-0042@belfry.test"
    assert [(p["state"], p["tier"]) for p in me["passes"]] == [("active", "week")]


def test_me_for_a_steward(services, odoo, client, authentik):
    key, _ = holder(services, odoo, authentik, steward_group=True)
    assert get(client, key, "/api/me").json()["steward"] is True


def test_me_for_an_order_with_ones_own_key_and_for_a_stranger(services, odoo, client):
    services.settings.public_url = BASE
    key = PrivateKey()
    partner = odoo.add("res.partner", name="Dana", email="dana@example.org")
    odoo.add(
        "belfry.pass",
        partner_id=partner,
        tier="day",
        state="pending",
        own_pubkey=key.public_key_xonly.format().hex(),
    )
    me = get(client, key, "/api/me").json()
    assert me["member"] is False and me["nip05"] is None
    assert [(x["state"], x["tier"]) for x in me["passes"]] == [("pending", "day")]

    stranger = get(client, PrivateKey(), "/api/me").json()
    assert stranger["member"] is False and stranger["passes"] == []


def test_me_needs_a_valid_signature(services, client):
    services.settings.public_url = BASE
    assert client.get("/api/me").status_code == 401
    key = PrivateKey()
    wrong_url = nip98(key, BASE + "/api/invites")
    r = client.get("/api/me", headers={"Authorization": wrong_url})
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"
    auth = nip98(key, BASE + "/api/me")
    assert client.get("/api/me", headers={"Authorization": auth}).status_code == 200
    assert client.get("/api/me", headers={"Authorization": auth}).status_code == 401  # replay


def test_cors_of_the_api_for_configured_origins(services, odoo, client):
    origin = "http://localhost:5173"
    preflight = {"Origin": origin, "Access-Control-Request-Method": "POST"}
    assert client.options("/api/invites", headers=preflight).status_code == 403  # off by default
    r = client.get("/api/me", headers={"Origin": origin})
    assert "access-control-allow-origin" not in r.headers

    services.settings.api_cors_origins = "https://app.test, " + origin
    r = client.options(
        "/api/invites", headers={**preflight, "Access-Control-Request-Headers": "authorization"}
    )
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == origin
    assert "Authorization" in r.headers["access-control-allow-headers"]
    # answers and errors alike, so the page can read why
    r = client.get("/api/me", headers={"Origin": origin})
    assert r.status_code == 401 and r.headers["access-control-allow-origin"] == origin
    r = client.get("/api/me", headers={"Origin": "https://elsewhere.test"})
    assert "access-control-allow-origin" not in r.headers
    # other paths are not affected
    r = client.get("/health", headers={"Origin": origin})
    assert "access-control-allow-origin" not in r.headers

    services.settings.api_cors_origins = "*"
    r = client.get("/api/me", headers={"Origin": "https://anywhere.test"})
    assert r.headers["access-control-allow-origin"] == "*"
