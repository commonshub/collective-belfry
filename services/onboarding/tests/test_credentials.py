"""Phase 3: claim offers, the Token Status List, type metadata, the door and the pass bridge."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest

from belfry_onboarding import core, credentials

from .conftest import BRIDGE_SECRET, HOOK_TOKEN, seed_paid_invoice


def pass_for(odoo, invoice_id):
    return next(p for p in odoo.tables["belfry.pass"].values() if p.get("invoice_id") == invoice_id)


def activate(services, odoo, **kw):
    ids = seed_paid_invoice(odoo, **kw)
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    return ids, pass_for(odoo, ids["invoice"])


# -- status list encoding (IETF draft, 1-bit example) ---------------------------------------------


def test_status_list_bits_follow_the_ietf_example():
    revoked = {0, 3, 4, 5, 7, 8, 9, 13, 15}
    lst, size = credentials.encode_status_list(revoked)
    data = credentials.decode_status_list(lst)
    assert data[:2].hex() == "b9a3"
    assert size % 8 == 0 and size >= credentials.STATUS_LIST_MIN_BITS
    assert [credentials.status_at(lst, i) for i in range(16)] == [
        1, 0, 0, 1, 1, 1, 0, 1, 1, 1, 0, 0, 0, 1, 0, 1
    ]  # fmt: skip
    assert credentials.status_at(lst, 70_000) == 0  # beyond the list: not revoked


def test_status_list_grows_with_the_highest_index():
    lst, size = credentials.encode_status_list({20_000})
    assert size > 20_000 and credentials.status_at(lst, 20_000) == 1


# -- activation and the claim page -----------------------------------------------------------------


def test_activation_sets_claim_token_and_status_index(services, odoo):
    _ids, p = activate(services, odoo)
    assert len(p["claim_token"]) >= 24
    assert p["credential_status_index"] == p["id"]


def test_claim_offer_carries_the_pass_claims(client, services, odoo, issuer):
    _ids, p = activate(services, odoo, tier="week")
    r = client.get(f"/claim/{p['claim_token']}/offer")
    assert r.status_code == 200
    body = r.json()
    assert body["credential_offer"].startswith("openid-credential-offer://")
    assert body["tier"] == "week" and body["date_start"] == "2027-01-25"
    assert len(issuer.offers) == 1
    overrides = issuer.offers[0]["runtimeOverrides"]
    data = overrides["credentialData"]
    assert data["pass_tier"] == "week"
    assert data["member_number"] == "OSV-0001"
    assert data["display_name"] == "Alice Example"
    assert data["npub"] == p["npub"]
    assert data["exp"] > data["nbf"] and data["iat"] <= data["nbf"] or data["nbf"] <= data["iat"]
    assert overrides["credentialStatus"] == {
        "status_list": {"idx": p["id"], "uri": "http://onboarding:8100/status/1"}
    }
    assert issuer.offers[0]["expiresInSeconds"] == 600
    # the HTML page mints a second offer and shows the QR code
    page = client.get(f"/claim/{p['claim_token']}")
    assert page.status_code == 200
    assert "data:image/svg+xml" in page.text and "Open in a wallet" in page.text
    assert len(issuer.offers) == 2


def test_claim_refuses_unknown_and_closed_passes(client, services, odoo):
    assert client.get("/claim/nonexistent-token-value/offer").status_code == 404
    assert client.get("/claim/short").status_code == 404
    _ids, p = activate(services, odoo)
    core.revoke_pass(services, p["id"], "steward")
    r = client.get(f"/claim/{p['claim_token']}/offer")
    assert r.status_code == 410
    assert "revoked" in r.json()["error"]


# -- status list endpoint --------------------------------------------------------------------------


def test_status_list_flips_the_bit_on_revocation(client, services, odoo, issuer_jwk):
    _ids, p = activate(services, odoo)
    other = odoo.add("res.partner", name="Bob", email="bob@example.com")
    _ids2, p2 = activate(services, odoo, partner_id=other, provider_reference="pi_2")

    def fetch():
        r = client.get("/status/1")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/statuslist+jwt")
        assert "max-age=300" in r.headers["cache-control"]
        header = jwt.get_unverified_header(r.text)
        assert header["typ"] == "statuslist+jwt" and header["x5c"] == ["MIIBfake"]
        public = jwt.algorithms.ECAlgorithm.from_jwk(
            json.dumps({k: v for k, v in issuer_jwk.items() if k != "d"})
        )
        payload = jwt.decode(r.text, public, algorithms=["ES256"], options={"verify_aud": False})
        assert payload["iss"] == "http://localhost:7005/openid4vci"
        assert payload["sub"] == "http://onboarding:8100/status/1"
        assert payload["status_list"]["bits"] == 1
        return payload["status_list"]["lst"]

    lst = fetch()
    assert credentials.status_at(lst, p["id"]) == 0
    assert credentials.status_at(lst, p2["id"]) == 0
    core.revoke_pass(services, p["id"], "refund")
    lst = fetch()
    assert credentials.status_at(lst, p["id"]) == 1
    assert credentials.status_at(lst, p2["id"]) == 0
    core.reactivate_pass(services, p["id"])
    assert credentials.status_at(fetch(), p["id"]) == 0


def test_type_metadata(client):
    r = client.get("/vct/village-pass/v1")
    assert r.status_code == 200
    doc = r.json()
    assert doc["vct"] == "https://belfry.test/vct/village-pass/v1"
    paths = [c["path"][0] for c in doc["claims"]]
    assert paths == ["pass_tier", "display_name", "member_number", "npub"]
    assert doc["claims"][0]["sd"] == "never"


# -- door ------------------------------------------------------------------------------------------


def test_door_asks_for_the_tier_only_and_reports_validity(client, verifier):
    r = client.post("/door/sessions")
    assert r.status_code == 200
    sid = r.json()["session_id"]
    assert r.json()["authorization_request_url"].startswith("openid4vp://")
    created = verifier.created[-1]["core_flow"]
    query = created["dcql_query"]["credentials"][0]
    assert query["meta"]["vct_values"] == ["https://belfry.test/vct/village-pass/v1"]
    assert query["claims"] == [{"path": ["pass_tier"]}]
    assert created["signed_request"] is True
    policies = created["policies"]["vc_policies"]
    assert "signature" in policies and "expiration" in policies and "not-before" in policies
    assert {
        "policy": "allowed-issuer",
        "allowed_issuer": "http://localhost:7005/openid4vci",
    } in policies
    assert {
        "policy": "credential-status",
        "argument": {"discriminator": "ietf", "value": 0},
    } in policies

    assert client.get(f"/door/sessions/{sid}").json()["status"] == "pending"
    verifier.succeed(sid, {"pass_tier": "week", "exp": 1_800_000_000, "nbf": 1})
    result = client.get(f"/door/sessions/{sid}").json()
    assert result["status"] == "valid" and result["tier"] == "week"
    assert result["valid_until"] == "2027-01-15"

    r2 = client.post("/door/sessions").json()
    verifier.fail(r2["session_id"], "credential-status")
    result = client.get(f"/door/sessions/{r2['session_id']}").json()
    assert result["status"] == "invalid" and result["failed"] == ["credential-status"]

    page = client.get("/door")
    assert page.status_code == 200 and "Door check" in page.text


# -- bridge ----------------------------------------------------------------------------------------


def pkce():
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    return verifier, challenge.decode()


def test_bridge_discovery_and_jwks(client, bridge_jwk):
    disc = client.get("/bridge/.well-known/openid-configuration").json()
    assert disc["issuer"] == "https://belfry.test/bridge"
    assert disc["authorization_endpoint"] == "https://belfry.test/bridge/authorize"
    assert disc["code_challenge_methods_supported"] == ["S256"]
    keys = client.get("/bridge/jwks").json()["keys"]
    assert keys[0]["kid"] == bridge_jwk["kid"] and "d" not in keys[0]


def test_bridge_login_end_to_end(client, verifier, bridge_jwk):
    code_verifier, challenge = pkce()
    r = client.get(
        "/bridge/authorize",
        params={
            "client_id": "belfry-authentik",
            "redirect_uri": "http://authentik.test/source/oauth/callback/pass-bridge/",
            "response_type": "code",
            "scope": "openid profile",
            "state": "st-1",
            "nonce": "n-1",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert r.status_code == 200 and 'data-sid="' in r.text
    sid = r.text.split('data-sid="', 1)[1].split('"', 1)[0]
    # the verifier session asks for member number, npub and tier, with the status check
    query = verifier.created[-1]["core_flow"]["dcql_query"]["credentials"][0]
    assert [c["path"][0] for c in query["claims"]] == ["member_number", "npub", "pass_tier"]
    assert any(
        isinstance(p, dict) and p.get("policy") == "credential-status"
        for p in verifier.created[-1]["core_flow"]["policies"]["vc_policies"]
    )

    assert client.get(f"/bridge/poll/{sid}").json() == {"status": "pending"}
    vsid = verifier.created and list(verifier.sessions)[-1]
    verifier.succeed(vsid, {"member_number": "OSV-0007", "npub": "npub1abc", "pass_tier": "full"})
    done = client.get(f"/bridge/poll/{sid}").json()
    assert done["status"] == "done"
    redirect = urlsplit(done["redirect"])
    assert redirect.netloc == "authentik.test"
    q = parse_qs(redirect.query)
    assert q["state"] == ["st-1"]
    code = q["code"][0]
    # polling again returns the same code (the page may poll once more before redirecting)
    assert client.get(f"/bridge/poll/{sid}").json()["redirect"] == done["redirect"]

    # token: wrong secret, then basic auth with PKCE
    bad = client.post(
        "/bridge/token",
        data={"grant_type": "authorization_code", "code": code, "client_id": "belfry-authentik",
              "client_secret": "nope", "code_verifier": code_verifier},
    )  # fmt: skip
    assert bad.status_code == 401
    basic = base64.b64encode(f"belfry-authentik:{BRIDGE_SECRET}".encode()).decode()
    wrong_pkce = client.post(
        "/bridge/token",
        headers={"Authorization": f"Basic {basic}"},
        data={"grant_type": "authorization_code", "code": code, "code_verifier": "wrong",
              "redirect_uri": "http://authentik.test/source/oauth/callback/pass-bridge/"},
    )  # fmt: skip
    assert wrong_pkce.status_code == 400 and wrong_pkce.json()["error"] == "invalid_grant"
    # a failed exchange burns the code (it was popped); a login must start over
    again = client.post(
        "/bridge/token",
        headers={"Authorization": f"Basic {basic}"},
        data={"grant_type": "authorization_code", "code": code, "code_verifier": code_verifier},
    )
    assert again.status_code == 400


def test_bridge_tokens_and_userinfo(client, verifier, bridge_jwk):
    code_verifier, challenge = pkce()
    r = client.get(
        "/bridge/authorize",
        params={"client_id": "belfry-authentik", "redirect_uri": "http://authentik.test/cb",
                "response_type": "code", "code_challenge": challenge,
                "code_challenge_method": "S256", "nonce": "n-2"},
    )  # fmt: skip
    sid = r.text.split('data-sid="', 1)[1].split('"', 1)[0]
    verifier.succeed(list(verifier.sessions)[-1], {"member_number": "OSV-0009", "npub": "npub1x"})
    code = parse_qs(urlsplit(client.get(f"/bridge/poll/{sid}").json()["redirect"]).query)["code"][0]
    tokens = client.post(
        "/bridge/token",
        data={"grant_type": "authorization_code", "code": code, "client_id": "belfry-authentik",
              "client_secret": BRIDGE_SECRET, "code_verifier": code_verifier,
              "redirect_uri": "http://authentik.test/cb"},
    ).json()  # fmt: skip
    assert tokens["token_type"] == "Bearer"
    public = jwt.algorithms.ECAlgorithm.from_jwk(
        json.dumps({k: v for k, v in bridge_jwk.items() if k != "d"})
    )
    claims = jwt.decode(
        tokens["id_token"], public, algorithms=["ES256"], audience="belfry-authentik"
    )
    assert claims["iss"] == "https://belfry.test/bridge"
    assert claims["sub"] == "OSV-0009" and claims["preferred_username"] == "osv-0009"
    assert claims["npub"] == "npub1x" and claims["nonce"] == "n-2"
    info = client.get(
        "/bridge/userinfo", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert info.json() == {"sub": "OSV-0009", "member_number": "OSV-0009",
                           "preferred_username": "osv-0009", "npub": "npub1x"}  # fmt: skip
    assert client.get("/bridge/userinfo").status_code == 401
    # a code is single use
    reuse = client.post(
        "/bridge/token",
        data={"grant_type": "authorization_code", "code": code, "client_id": "belfry-authentik",
              "client_secret": BRIDGE_SECRET, "code_verifier": code_verifier},
    )  # fmt: skip
    assert reuse.status_code == 400


def test_bridge_refuses_a_presentation_without_member_number(client, verifier):
    r = client.get(
        "/bridge/authorize",
        params={"client_id": "belfry-authentik", "redirect_uri": "http://authentik.test/cb",
                "response_type": "code", "state": "s"},
    )  # fmt: skip
    sid = r.text.split('data-sid="', 1)[1].split('"', 1)[0]
    verifier.succeed(list(verifier.sessions)[-1], {"pass_tier": "day"})
    res = client.get(f"/bridge/poll/{sid}").json()
    assert res["status"] == "failed" and "error=access_denied" in res["redirect"]
    assert "state=s" in res["redirect"]


def test_bridge_rejects_unknown_client(client):
    r = client.get("/bridge/authorize", params={"client_id": "x", "redirect_uri": "http://a/b",
                                                "response_type": "code"})  # fmt: skip
    assert r.status_code == 400 and "invalid_client" in r.text
    assert client.get(f"/bridge/poll/{secrets.token_urlsafe(8)}").status_code == 404


def test_admin_revoke_endpoint(client, services, odoo):
    _ids, p = activate(services, odoo)
    r = client.post(f"/admin/passes/{p['id']}/revoke?token={HOOK_TOKEN}")
    assert r.json() == {"result": "revoked", "pass_id": p["id"]}
    assert pass_for(odoo, _ids["invoice"])["state"] == "revoked"
    assert client.post(f"/admin/passes/{p['id']}/revoke?token=bad").status_code == 401


@pytest.mark.parametrize("tier,start,end", [("day", "2027-01-25", "2027-01-26")])
def test_pass_credential_data_validity(tier, start, end):
    import datetime as dt

    data = credentials.pass_credential_data(
        tier, "A", "OSV-1", "npub1", dt.date.fromisoformat(start), dt.date.fromisoformat(end), now=1
    )
    assert data["nbf"] == 1  # bought before the event: valid from now, the event start is later
    assert dt.datetime.fromtimestamp(data["exp"], dt.UTC).date().isoformat() == end
    late = credentials.pass_credential_data(
        tier, "A", "OSV-1", "npub1", dt.date.fromisoformat(start), dt.date.fromisoformat(end),
        now=2_000_000_000,
    )  # fmt: skip
    assert late["nbf"] == int(dt.datetime(2027, 1, 25, tzinfo=dt.UTC).timestamp())


def test_door_refuses_a_pass_signed_by_another_issuer_certificate(client, verifier):
    sid = client.post("/door/sessions").json()["session_id"]
    verifier.succeed(sid, {"pass_tier": "week", "exp": 1_800_000_000}, leaf="MIIBsomebodyElse")
    result = client.get(f"/door/sessions/{sid}").json()
    assert result == {
        "status": "invalid",
        "verifier_status": "SUCCESSFUL",
        "failed": ["issuer-certificate"],
    }


def test_bridge_refuses_a_pass_signed_by_another_issuer_certificate(client, verifier):
    r = client.get(
        "/bridge/authorize",
        params={"client_id": "belfry-authentik", "redirect_uri": "http://authentik.test/cb",
                "response_type": "code", "state": "s"},
    )  # fmt: skip
    sid = r.text.split('data-sid="', 1)[1].split('"', 1)[0]
    verifier.succeed(
        list(verifier.sessions)[-1], {"member_number": "OSV-0001"}, leaf="MIIBsomebodyElse"
    )
    res = client.get(f"/bridge/poll/{sid}").json()
    assert res["status"] == "failed" and res["failed"] == ["issuer-certificate"]


def test_issuer_pinning_helpers():
    assert credentials.issued_by_belfry({}, ["MIIBfake"]) is False
    assert credentials.issued_by_belfry({"signed": "not-a-jwt"}, ["MIIBfake"]) is False
    assert (
        credentials.issued_by_belfry({"signed": "eyJ4NWMiOlsiTUlJQmZha2UiXX0.e30.c2ln"}, [])
        is False
    )
