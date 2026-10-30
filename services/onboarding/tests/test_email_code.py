"""An app's own key proves an e-mail address: /api/email-code, signed with NIP-98."""

from __future__ import annotations

import time

from coincurve import PrivateKey

from .test_holder_invites import BASE, get, nip98, post


def key_hex(key: PrivateKey) -> str:
    return key.public_key_xonly.format().hex()


def test_code_by_mail_then_bound(services, odoo, client):
    services.settings.public_url = BASE
    key = PrivateKey()
    r = post(client, key, "/api/email-code", {"email": "dana@example.org", "app": "Village demo"})
    assert r.status_code == 202, r.text
    assert r.json()["state"] == "sent"
    sent = next(iter(odoo.tables["belfry.key.code"].values()))
    assert (sent["pubkey"], sent["app"]) == (key_hex(key), "Village demo")

    r = post(client, key, "/api/email-code/verify", {"email": "dana@example.org", "code": "000000"})
    assert r.status_code == 422
    assert r.json() == {
        "error": "wrong_code",
        "message": "That code is not right.",
        "tries_left": 4,
    }

    code = odoo.mailed_codes["dana@example.org"]
    r = post(client, key, "/api/email-code/verify", {"email": "dana@example.org", "code": code})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "bound" and r.json()["member"] is False

    # /api/me now names the address, still without a pass
    me = get(client, key, "/api/me").json()
    assert (me["email"], me["member"], me["passes"]) == ("dana@example.org", False, [])

    # the same request again is another event (an event is accepted once)
    again = {"email": "dana@example.org", "code": code}
    r = post(client, key, "/api/email-code/verify", again, created_at=int(time.time()) - 1)
    assert r.status_code == 410 and r.json()["error"] == "expired"


def test_other_key_and_taken_key(services, odoo, client):
    services.settings.public_url = BASE
    key = PrivateKey()
    odoo.key_code_answer = {"state": "other_key", "email": "a@example.org", "npub": "npub1other"}
    r = post(client, key, "/api/email-code/verify", {"email": "a@example.org", "code": "123456"})
    assert r.status_code == 409
    assert (r.json()["error"], r.json()["npub"]) == ("other_key", "npub1other")
    odoo.key_code_answer = {"state": "key_taken"}
    r = post(
        client,
        key,
        "/api/email-code/verify",
        {"email": "a@example.org", "code": "123456"},
        created_at=int(time.time()) - 1,
    )
    assert r.status_code == 409 and r.json()["error"] == "key_taken"


def test_limits_and_bad_input(services, odoo, client):
    services.settings.public_url = BASE
    key = PrivateKey()
    now = int(time.time())
    for i in range(5):
        r = post(client, key, "/api/email-code", {"email": "e@example.org"}, created_at=now - i)
        assert r.status_code == 202
    r = post(client, key, "/api/email-code", {"email": "e@example.org"}, created_at=now - 5)
    assert r.status_code == 429 and r.json()["error"] == "too_many"

    assert post(client, key, "/api/email-code", {"email": "nope"}).status_code == 422
    assert post(client, key, "/api/email-code", {"mail": "x@example.org"}).status_code == 422
    assert post(client, key, "/api/email-code", ["x@example.org"]).status_code == 400
    r = post(client, key, "/api/email-code/verify", {"email": "e@example.org"})
    assert r.status_code == 422  # no code


def test_needs_a_signature_by_the_key(services, client):
    services.settings.public_url = BASE
    assert client.post("/api/email-code", json={"email": "f@example.org"}).status_code == 401
    # signed for another path
    key = PrivateKey()
    r = post(client, key, "/api/email-code", {"email": "f@example.org"})
    assert r.status_code == 202
    body = b'{"email": "f@example.org"}'
    auth = nip98(key, BASE + "/api/invites", "POST", body)
    r = client.post(
        "/api/email-code",
        content=body,
        headers={"Authorization": auth, "Content-Type": "application/json"},
    )
    assert r.status_code == 401
