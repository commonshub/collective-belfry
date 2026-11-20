"""Belfry's own sign-in (2026-11-19): the login page, the apps' sign-in with the same OAuth shape
Keycast had, the account page, NIP-05 names and the credential issuer's OpenID provider."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.parse

import jwt
import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import core, session
from belfry_onboarding.app import create_app
from belfry_onboarding.blossom import event_id

from .conftest import ISSUER_OIDC_SECRET, seed_paid_invoice, sign_in

CHAT = "https://chat.belfry.test/oauth/callback"
WALLET = "https://wallet.belfry.test/oauth/callback"


@pytest.fixture
def web(services):
    services.settings.oauth_clients = f"belfry-chat={CHAT},belfry-wallet={WALLET}"
    services.settings.derive_secrets = "belfry-wallet:cashu-wallet-v1|cashu-nutzap-v1"
    services.settings.pass_page_secret = "p" * 32
    with TestClient(create_app(services), base_url="https://belfry.test") as c:
        yield c


@pytest.fixture
def member(services, odoo):
    ids = seed_paid_invoice(odoo, tier="week")
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    return ids["partner"]


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


# -- the login page -------------------------------------------------------------------------------


def test_login_by_code_and_by_link(web, odoo, member):
    page = web.get("/login?next=/me")
    assert page.status_code == 200 and 'action="/login/email"' in page.text
    assert "Sign in with your pass" in page.text
    sent = web.post("/login/email", data={"email": "alice@example.com", "next": "/me"})
    assert sent.status_code == 200 and "Check your mail" in sent.text
    (_model, address, code, secret) = odoo.mails[-1]
    assert address == "alice@example.com"
    wrong = web.post("/login/code", data={"email": address, "code": "000000", "next": "/me"})
    assert wrong.status_code == 422 and "4 tries left" in wrong.text
    ok = web.post(
        "/login/code", data={"email": address, "code": code, "next": "/me"}, follow_redirects=False
    )
    assert ok.status_code == 303 and ok.headers["location"] == "/me"
    found = session.decode(web.app.state.services.settings, ok.cookies[session.COOKIE])
    assert (found.partner_id, found.method) == (member, "email")
    assert "httponly" in ok.headers["set-cookie"].lower()
    assert "secure" in ok.headers["set-cookie"].lower()

    web.cookies.clear()
    web.post("/login/email", data={"email": "alice@example.com", "next": "/account"})
    secret = odoo.mails[-1][3]
    assert "document.getElementById" in web.get(f"/login/link/{secret}").text
    used = web.post(f"/login/link/{secret}", follow_redirects=False)
    assert used.status_code == 303 and used.headers["location"] == "/account"
    again = web.post(f"/login/link/{secret}", follow_redirects=False)
    assert again.status_code == 410 and "used already" in again.text


def test_the_root_takes_a_signed_in_participant_to_the_account(web, member):
    assert web.get("/", follow_redirects=False).status_code == 200
    sign_in(web, member)
    assert web.get("/", follow_redirects=False).headers["location"] == "/account"


def test_unknown_addresses_look_the_same_and_next_stays_on_belfry(web, odoo):
    r = web.post(
        "/login/email", data={"email": "nobody@example.org", "next": "https://evil.example/"}
    )
    assert r.status_code == 200 and "Check your mail" in r.text
    assert odoo.mails == []
    # an outside next is replaced by the account page
    assert 'name="next" value="/account"' in r.text
    settings = web.app.state.services.settings
    assert session.safe_next(settings, "//evil.example/") == "/account"
    assert session.safe_next(settings, "https://chat.belfry.test/x") == "https://chat.belfry.test/x"
    assert session.safe_next(settings, "/api/oauth/authorize?a=1") == "/api/oauth/authorize?a=1"


def test_logout_ends_the_session_and_returns_to_an_app(web, member):
    sign_in(web, member)
    r = web.get(
        "/api/auth/oidc/logout",
        params={"return_to": "https://chat.belfry.test/"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "https://chat.belfry.test/"
    assert (
        f'{session.COOKIE}=""' in r.headers["set-cookie"] or "Max-Age=0" in r.headers["set-cookie"]
    )
    r = web.get("/logout", params={"return_to": "https://evil.example/"}, follow_redirects=False)
    assert r.headers["location"] == "/login?logged_out=1"


def test_pass_sign_in(web, odoo, verifier, member):
    number = odoo.get("res.partner", member)["belfry_member_number"]
    page = web.get("/login/pass", params={"next": "/me"})
    assert page.status_code == 200 and 'data-sid="' in page.text
    sid = page.text.split('data-sid="', 1)[1].split('"', 1)[0]
    query_ = verifier.created[-1]["core_flow"]["dcql_query"]["credentials"][0]
    assert [c["path"][0] for c in query_["claims"]] == ["member_number", "npub", "pass_tier"]
    assert web.get(f"/login/pass/poll/{sid}").json() == {"status": "pending"}
    verifier.succeed(list(verifier.sessions)[-1], {"member_number": number, "pass_tier": "week"})
    done = web.get(f"/login/pass/poll/{sid}").json()
    assert done == {"status": "done", "redirect": f"/login/pass/done/{sid}"}
    # another browser (without the attempt's cookie) cannot finish it
    other = TestClient(web.app, base_url="https://belfry.test")
    assert other.get(done["redirect"], follow_redirects=False).status_code == 400
    r = web.get(done["redirect"], follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/me"
    found = session.decode(web.app.state.services.settings, r.cookies[session.COOKIE])
    assert (found.partner_id, found.method) == (member, "pass")


def test_pass_sign_in_refuses_another_issuer(web, verifier, member):
    page = web.get("/login/pass")
    sid = page.text.split('data-sid="', 1)[1].split('"', 1)[0]
    verifier.succeed(list(verifier.sessions)[-1], {"member_number": "OSV-0001"}, leaf="MIIBother")
    assert web.get(f"/login/pass/poll/{sid}").json() == {
        "status": "failed",
        "failed": ["issuer-certificate"],
    }


# -- the apps' sign-in ----------------------------------------------------------------------------


def authorize(web, client_id="belfry-chat", redirect_uri=CHAT, **extra):
    verifier, challenge = pkce()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "policy:belfry-event",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "st",
        **extra,
    }
    return web.get("/api/oauth/authorize", params=params, follow_redirects=False), verifier


def test_silent_sign_in_needs_a_session(web, member):
    r, _ = authorize(web, prompt="none")
    assert r.status_code == 302
    assert query(r.headers["location"]) == {"error": "login_required", "state": "st"}
    r, _ = authorize(web)
    assert r.status_code == 303 and r.headers["location"].startswith(
        "/login?next=%2Fapi%2Foauth%2Fauthorize"
    )


def test_own_app_signs_in_and_gets_a_bunker_and_tokens(web, signer, member):
    sign_in(web, member)
    r, verifier = authorize(web, prompt="none")
    assert r.status_code == 302 and r.headers["location"].startswith(CHAT + "?")
    code = query(r.headers["location"])["code"]
    assert query(r.headers["location"])["state"] == "st"
    wrong = web.post(
        "/api/oauth/token",
        json={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": "belfry-chat",
            "code_verifier": "x",
        },
    )
    assert wrong.status_code == 400, "a failed exchange burns the code"
    r, verifier = authorize(web, prompt="none")
    code = query(r.headers["location"])["code"]
    tokens = web.post(
        "/api/oauth/token",
        json={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": "belfry-chat",
            "redirect_uri": CHAT,
            "code_verifier": verifier,
        },
        headers={"Origin": "https://chat.belfry.test"},
    )
    assert tokens.status_code == 200, tokens.text
    assert tokens.headers["access-control-allow-origin"] == "*"
    body = tokens.json()
    assert body["bunker_url"].startswith("bunker://")
    account = signer.accounts[f"odoo-partner-{member}"]
    assert body["pubkey"] == account["pubkey"]
    conn = next(iter(signer.live(f"odoo-partner-{member}")))
    assert (conn["app"], conn["keep"], conn["derive"]) == ("belfry-chat", 5, [])

    # the revoke ends the connection; the code is single use
    assert web.post("/api/oauth/revoke", json={"token": body["refresh_token"]}).json() == {
        "ok": True
    }
    assert signer.live(f"odoo-partner-{member}") == []
    again = web.post(
        "/api/oauth/token",
        json={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": "belfry-chat",
            "code_verifier": verifier,
        },
    )
    assert again.status_code == 400


def test_the_wallet_signs_and_derives_over_http_rpc(web, signer, member):
    sign_in(web, member)
    r, verifier = authorize(web, client_id="belfry-wallet", redirect_uri=WALLET)
    code = query(r.headers["location"])["code"]
    body = web.post(
        "/api/oauth/token",
        json={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": "belfry-wallet",
            "code_verifier": verifier,
        },
    ).json()
    auth = {"Authorization": f"Bearer {body['access_token']}"}

    def rpc(method, params):
        return web.post("/api/nostr", json={"method": method, "params": params}, headers=auth)

    assert rpc("get_public_key", []).json() == {"result": body["pubkey"]}
    signed = rpc(
        "sign_event", [{"kind": 10019, "content": "", "tags": [], "pubkey": body["pubkey"]}]
    )
    assert signed.json()["result"]["sig"] == "fake-sig"
    assert len(rpc("derive_secret", ["cashu-wallet-v1"]).json()["result"]) == 64
    refused = rpc("derive_secret", ["something-else"])
    assert refused.status_code == 403
    assert rpc("nip44_encrypt", ["ab" * 32, "hi"]).json() == {"result": "enc:hi"}
    assert rpc("melt", []).status_code == 400
    assert web.post("/api/nostr", json={"method": "get_public_key"}).status_code == 401
    # refresh, then the app's logout revokes: both the RPC and the refresh stop
    fresh = web.post(
        "/api/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": body["refresh_token"]},
    ).json()
    assert fresh["access_token"] and fresh["refresh_token"]
    web.post(
        "/api/oauth/revoke", json={"token": body["refresh_token"], "client_id": "belfry-wallet"}
    )
    assert rpc("sign_event", [{"kind": 1, "content": "x"}]).status_code == 403
    assert rpc("get_public_key", []).status_code == 403
    gone = web.post(
        "/api/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": body["refresh_token"]},
    )
    assert gone.status_code == 400


def test_outside_apps_see_the_consent_screen(web, signer, member):
    sign_in(web, member)
    notes = "https://notes.example/callback"
    r, _ = authorize(web, client_id="village-notes", redirect_uri=notes, prompt="none")
    assert query(r.headers["location"])["error"] == "consent_required"
    r, verifier = authorize(web, client_id="village-notes", redirect_uri=notes)
    assert r.status_code == 200 and "notes.example" in r.text
    form = r.text.split('name="consent" value="', 1)[1].split('"', 1)[0]
    deny = web.post(
        "/api/oauth/consent", data={"consent": form, "answer": "deny"}, follow_redirects=False
    )
    assert query(deny.headers["location"])["error"] == "access_denied"
    allow = web.post(
        "/api/oauth/consent", data={"consent": form, "answer": "allow"}, follow_redirects=False
    )
    code = query(allow.headers["location"])["code"]
    body = web.post(
        "/api/oauth/token",
        json={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": "village-notes",
            "code_verifier": verifier,
        },
    ).json()
    conn = signer.live(f"odoo-partner-{member}")[-1]
    assert conn["derive"] == [] and conn["keep"] is None and "notes.example" in conn["label"]
    assert body["bunker_url"].startswith("bunker://")
    # allowed once and still signed in: back without the consent screen, also silently
    r, _ = authorize(web, client_id="village-notes", redirect_uri=notes, prompt="none")
    assert r.status_code == 302 and "code" in query(r.headers["location"])
    # its logout may return to it now (as Keycast's did for an app signed in to)
    back = web.get(
        "/api/auth/oidc/logout",
        params={"return_to": "https://notes.example/"},
        follow_redirects=False,
    )
    assert back.headers["location"] == "https://notes.example/"
    # once its connection is revoked, the app asks again
    web.post("/api/oauth/revoke", json={"token": body["refresh_token"]})
    sign_in(web, member)
    r, _ = authorize(web, client_id="village-notes", redirect_uri=notes, prompt="none")
    assert query(r.headers["location"])["error"] == "consent_required"
    # an http redirect elsewhere than localhost, or a registered app with another address: refused
    r, _ = authorize(web, client_id="x", redirect_uri="http://evil.example/cb")
    assert r.status_code == 400
    r, _ = authorize(web, client_id="belfry-chat", redirect_uri="https://evil.example/cb")
    assert r.status_code == 400


def test_closed_passes_and_own_keys_get_no_signer(web, odoo, signer, member, services):
    sign_in(web, member)
    signer.accounts[f"odoo-partner-{member}"]["custody"] = "own"
    r, _ = authorize(web, prompt="none")
    assert query(r.headers["location"]) == {
        "error": "access_denied",
        "error_description": "self_custody",
        "state": "st",
    }
    r, _ = authorize(web)
    assert r.status_code == 403 and "Use your own signer" in r.text
    signer.accounts[f"odoo-partner-{member}"]["custody"] = "derived"
    pass_id = next(
        p["id"] for p in odoo.tables["belfry.pass"].values() if p["partner_id"] == member
    )
    core.revoke_pass(services, pass_id, "steward")
    r, _ = authorize(web, prompt="none")
    assert query(r.headers["location"])["error"] == "login_required"


# -- the account page -----------------------------------------------------------------------------


def test_account_page_lists_apps_and_connections(web, signer, member, services):
    services.settings.community_apps = (
        "Chat|https://chat.belfry.test|Talk;Wallet|https://wallet.belfry.test|Tokens"
    )
    assert (
        web.get("/account", follow_redirects=False).headers["location"] == "/login?next=%2Faccount"
    )
    sign_in(web, member)
    page = web.get("/account")
    assert page.status_code == 200
    assert "Your pass" in page.text and "Community apps" in page.text and "Chat" in page.text
    assert "osv-0001@belfry.local" in page.text
    made = web.post("/account/connections", data={"label": "Amethyst"})
    assert made.status_code == 200 and "bunker://" in made.text
    conn = signer.live(f"odoo-partner-{member}")[0]
    assert (conn["app"], conn["label"]) == ("outside", "Amethyst")
    assert "Amethyst" in web.get("/account").text
    removed = web.post(
        "/account/connections/revoke", data={"id": conn["id"]}, follow_redirects=False
    )
    assert removed.headers["location"] == "/account?notice=removed"
    assert signer.live(f"odoo-partner-{member}") == []
    # posts from another site are refused
    other = web.post(
        "/account/connections",
        data={"label": "x"},
        headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert other.status_code == 303 and signer.live(f"odoo-partner-{member}") == []


def test_key_export_needs_a_fresh_sign_in(web, member):
    sign_in(web, member, auth_time=int(time.time()) - 3600)
    page = web.get("/account")
    assert "Sign in again to unlock" in page.text and "/account/export" not in page.text
    stale = web.post("/account/export", follow_redirects=False)
    assert stale.status_code == 303 and "fresh=1" in stale.headers["location"]
    sign_in(web, member)
    shown = web.post("/account/export")
    assert shown.status_code == 200 and "nsec1fake" in shown.text


def signed_challenge(key: PrivateKey, challenge: str, kind: int = 27235) -> dict:
    event = {
        "pubkey": key.public_key_xonly.format().hex(),
        "created_at": int(time.time()),
        "kind": kind,
        "tags": [["challenge", challenge]],
        "content": "",
    }
    event["id"] = event_id(event)
    event["sig"] = key.sign_schnorr(bytes.fromhex(event["id"])).hex()
    return event


def test_moving_to_an_own_key(web, odoo, signer, member, whitelist):
    sign_in(web, member)
    challenge = web.get("/account/self-custody/challenge").json()
    assert challenge["kind"] == 27235
    key = PrivateKey()
    new = key.public_key_xonly.format().hex()
    bad = web.post("/account/self-custody", json={"event": signed_challenge(key, "1.forged")})
    assert bad.status_code == 422
    r = web.post(
        "/account/self-custody", json={"event": signed_challenge(key, challenge["challenge"])}
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"outcome": "moved_to_own_key", "pubkey": new}
    account = signer.accounts[f"odoo-partner-{member}"]
    assert (account["pubkey"], account["custody"]) == (new, "own")
    p = next(p for p in odoo.tables["belfry.pass"].values() if p["partner_id"] == member)
    assert p["nostr_pubkey"] == new
    assert odoo.get("res.partner", member)["belfry_key_custody"] == "self"
    assert new in whitelist.config_path.read_text()
    assert "You hold this key yourself" in web.get("/account").text


# -- NIP-05 and the issuer's OpenID provider ------------------------------------------------------


def test_nip05_answers_a_name_only(web, odoo, member):
    pubkey = next(p for p in odoo.tables["belfry.pass"].values() if p["partner_id"] == member)[
        "nostr_pubkey"
    ]
    r = web.get("/.well-known/nostr.json", params={"name": "OSV-0001"})
    assert r.json()["names"] == {"osv-0001": pubkey}
    assert r.headers["access-control-allow-origin"] == "*"
    assert web.get("/.well-known/nostr.json").json() == {"names": {}}
    assert web.get("/.well-known/nostr.json", params={"name": "nobody"}).json() == {"names": {}}


def test_the_issuer_signs_participants_in_through_belfry(web, member, bridge_jwk):
    cb = "http://localhost:7005/openid4vci/external/oauth/callback"
    verifier, challenge = pkce()
    params = {
        "client_id": "belfry-issuer",
        "redirect_uri": cb,
        "response_type": "code",
        "scope": "openid",
        "state": "s1",
        "nonce": "n1",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    first = web.get("/oidc/authorize", params=params, follow_redirects=False)
    assert first.status_code == 303 and first.headers["location"].startswith(
        "/login?next=%2Foidc%2Fauthorize"
    )
    sign_in(web, member)
    r = web.get("/oidc/authorize", params=params, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith(cb + "?")
    code = query(r.headers["location"])["code"]
    bad = web.post(
        "/oidc/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": "belfry-issuer",
            "client_secret": "no",
        },
    )
    assert bad.status_code == 401
    basic = base64.b64encode(f"belfry-issuer:{ISSUER_OIDC_SECRET}".encode()).decode()
    tokens = web.post(
        "/oidc/token",
        headers={"Authorization": f"Basic {basic}"},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": cb,
            "code_verifier": verifier,
        },
    ).json()
    public = jwt.algorithms.ECAlgorithm.from_jwk(
        json.dumps({k: v for k, v in bridge_jwk.items() if k != "d"})
    )
    claims = jwt.decode(tokens["id_token"], public, algorithms=["ES256"], audience="belfry-issuer")
    assert claims["iss"] == "https://belfry.test/oidc" and claims["sub"] == "OSV-0001"
    assert claims["nonce"] == "n1"
    info = web.get("/oidc/userinfo", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert info.json()["member_number"] == "OSV-0001"
    other = dict(params, redirect_uri="https://evil.example/cb")
    assert web.get("/oidc/authorize", params=other, follow_redirects=False).status_code == 400
    assert (
        web.get("/oidc/.well-known/openid-configuration").json()["issuer"]
        == "https://belfry.test/oidc"
    )
