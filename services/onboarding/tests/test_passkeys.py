"""Passkeys (passkeys.py, #issues 2026-11-15): a member adds one, signs in with it in place of the
e-mail code; assertions only from the belfry's own apps, never from the apex (member websites);
removing one needs a fresh assertion; synced passkeys (sign count 0) keep working."""

# ruff: noqa: F811
from __future__ import annotations

import base64
import hashlib
import json
import struct

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from belfry_onboarding import passkeys
from belfry_onboarding.app import create_app

from .test_guests import published, rsvc  # noqa: F401 — fixtures
from .test_roles import call, person, world  # noqa: F401 — fixtures

RP = "belfry.test"
CHAT = "https://chat.belfry.test"


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Authenticator:
    """A software passkey: ES256, user verified, sign count COUNT (0 = like a synced passkey)."""

    def __init__(self, count: int = 0):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cid = hashlib.sha256(str(id(self)).encode()).digest()[:16]
        self.count = count

    def _cose(self) -> bytes:
        n = self.key.public_key().public_numbers()
        return cbor2.dumps(
            {1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")}
        )

    def _auth_data(self, attested: bool) -> bytes:
        flags = (
            0x01 | 0x04 | (0x40 if attested else 0)
        )  # user present, user verified, attested data
        data = hashlib.sha256(RP.encode()).digest() + bytes([flags]) + struct.pack(">I", self.count)
        if attested:
            data += b"\0" * 16 + struct.pack(">H", len(self.cid)) + self.cid + self._cose()
        return data

    def register(self, options: dict, origin: str = CHAT) -> dict:
        client = json.dumps(
            {"type": "webauthn.create", "challenge": options["challenge"], "origin": origin}
        ).encode()
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": self._auth_data(True)})
        return {
            "id": b64(self.cid),
            "rawId": b64(self.cid),
            "type": "public-key",
            "response": {"clientDataJSON": b64(client), "attestationObject": b64(att)},
            "clientExtensionResults": {},
        }

    def assert_(self, options: dict, origin: str = CHAT) -> dict:
        if self.count:
            self.count += 1
        client = json.dumps(
            {"type": "webauthn.get", "challenge": options["challenge"], "origin": origin}
        ).encode()
        auth = self._auth_data(False)
        sig = self.key.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "id": b64(self.cid),
            "rawId": b64(self.cid),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64(client),
                "authenticatorData": b64(auth),
                "signature": b64(sig),
                "userHandle": None,
            },
            "clientExtensionResults": {},
        }


@pytest.fixture
def members(world):
    rsvc, _p, _k, ada, ada_id, _s = world
    rsvc.settings.nip05_domain = RP
    passkeys._pending.clear()
    return rsvc, ada, ada_id


def test_a_member_adds_a_passkey_and_signs_in_with_it(members):
    rsvc, ada, ada_id = members
    a = Authenticator()
    with TestClient(create_app(rsvc)) as c:
        assert call(c, ada, "/passkeys", {}).json() == {"passkeys": [], "email": True}
        opts = call(c, ada, "/passkeys", {"options": "add"}).json()
        assert (
            opts["rp"]["id"] == RP
            and opts["authenticatorSelection"]["userVerification"] == "required"
        )
        r = call(c, ada, "/passkeys", {"add": a.register(opts), "label": "My phone"})
        assert r.status_code == 200, r.text
        assert [p["label"] for p in r.json()["passkeys"]] == ["My phone"]
    # signing in: the account it proves
    o = passkeys.sign_in_options(rsvc, "flow1")
    assert passkeys.authenticate(rsvc, "in:flow1", a.assert_(o)) == ada_id
    # a challenge is used once
    with pytest.raises(passkeys.PasskeyError):
        passkeys.authenticate(rsvc, "in:flow1", a.assert_(o))


def test_an_assertion_from_the_apex_is_refused(members):
    """https://<domain> serves member-proposed websites: no assertion from there counts."""
    rsvc, ada, ada_id = members
    a = Authenticator()
    with TestClient(create_app(rsvc)) as c:
        opts = call(c, ada, "/passkeys", {"options": "add"}).json()
        assert call(c, ada, "/passkeys", {"add": a.register(opts)}).status_code == 200
    assert "https://belfry.test" not in passkeys.origins(rsvc)
    o = passkeys.sign_in_options(rsvc, "f2")
    with pytest.raises(passkeys.PasskeyError) as e:
        passkeys.authenticate(rsvc, "in:f2", a.assert_(o, origin="https://belfry.test"))
    assert e.value.code == "refused"
    o = passkeys.sign_in_options(rsvc, "f3")
    assert (
        passkeys.authenticate(rsvc, "in:f3", a.assert_(o, origin="https://pass.belfry.test"))
        == ada_id
    )


def test_sign_counts_synced_passkeys_and_regressions(members):
    rsvc, ada, ada_id = members
    synced, counted = Authenticator(0), Authenticator(5)
    with TestClient(create_app(rsvc)) as c:
        for a in (synced, counted):
            opts = call(c, ada, "/passkeys", {"options": "add"}).json()
            assert call(c, ada, "/passkeys", {"add": a.register(opts)}).status_code == 200
    for i in range(2):  # always 0: fine every time
        o = passkeys.sign_in_options(rsvc, f"s{i}")
        assert passkeys.authenticate(rsvc, f"in:s{i}", synced.assert_(o)) == ada_id
    o = passkeys.sign_in_options(rsvc, "c1")
    assert passkeys.authenticate(rsvc, "in:c1", counted.assert_(o)) == ada_id
    counted.count = 2  # a cloned authenticator going back
    o = passkeys.sign_in_options(rsvc, "c2")
    with pytest.raises(passkeys.PasskeyError):
        passkeys.authenticate(rsvc, "in:c2", counted.assert_(o))


def test_removing_needs_a_fresh_assertion(members):
    rsvc, ada, ada_id = members
    a, b = Authenticator(), Authenticator()
    with TestClient(create_app(rsvc)) as c:
        for x in (a, b):
            opts = call(c, ada, "/passkeys", {"options": "add"}).json()
            call(c, ada, "/passkeys", {"add": x.register(opts)})
        listed = call(c, ada, "/passkeys", {}).json()["passkeys"]
        r = call(c, ada, "/passkeys", {"remove": listed[0]["id"], "confirm": {}})
        assert r.status_code in (401, 410), r.text  # no fresh proof
        conf = call(c, ada, "/passkeys", {"options": "confirm"}).json()
        r = call(c, ada, "/passkeys", {"remove": listed[0]["id"], "confirm": b.assert_(conf)})
        assert r.status_code == 200 and len(r.json()["passkeys"]) == 1


def test_both_sign_in_pages_offer_a_passkey(members):
    from belfry_onboarding import passlogin

    rsvc, _ada, _ada_id = members
    html = passlogin.page(rsvc, passlogin.new_flow())
    assert "data-passkey" in html and "Sign in with a passkey" in html and "/passkey/verify" in html
