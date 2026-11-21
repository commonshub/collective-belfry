"""A belfry without Keycast and without Authentik (SIGNER=belfry, docs/own-signer.md phase 2).

These tests force that mode whatever signer the rest of the suite runs against: admins in the
register (stewards.py), no Authentik call anywhere, the steward pages' own sign-in, and Keycast's
sign-in for apps answered by Belfry (appsignin.py).
"""

from __future__ import annotations

import json
import urllib.parse

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import apps, appsignin, console, core, mypass, remoteapps, steward, stewards
from belfry_onboarding.app import build_services, create_app
from belfry_onboarding.clients.authentik import AuthentikClient
from belfry_onboarding.core import Services
from belfry_onboarding.settings import Settings

from .conftest import (
    AUTHENTIK_URL,
    ISSUER_URL,
    KEYCAST_URL,
    SIGNER_URL,
    VERIFIER_URL,
    signer_client,
)
from .fakes import FakeAuthentik, FakeSigner
from .test_connect import APP, VERIFIER, authorize, flow_of, sign_in

CONSOLE = "https://belfry.belfry.test"
APPS = {"Host": "apps.belfry.test"}
CALENDAR = "https://calendar.belfry.test/oauth/callback"


@pytest.fixture
def signer(router) -> FakeSigner:
    return FakeSigner(router, SIGNER_URL, KEYCAST_URL)


@pytest.fixture
def own(settings, odoo, signer, relay, whitelist, issuer, verifier, issuer_jwk, bridge_jwk):
    """Services of a belfry on its own signer: every use of Authentik raises."""
    from belfry_onboarding.clients.issuer import IssuerClient
    from belfry_onboarding.clients.verifier import VerifierClient

    s = settings
    s.signer, s.pass_signin = "belfry", "belfry"
    s.signer_token = "signer-token-for-tests"
    s.authentik_token = s.keycast_admin_token = s.keycast_service_token = ""
    s.nip05_domain = "belfry.test"
    s.pass_page_secret = "p" * 32
    s.steward_session_secret = "s" * 32
    s.belfry_nsec = PrivateKey().secret.hex()
    s.public_url = "https://pass.belfry.test"
    s.console_url = CONSOLE
    s.osv_apps = True
    stewards.check_settings(s)
    console._admins["at"] = 0.0
    apps.forget()
    return Services(
        odoo=odoo,
        authentik=stewards.NoAuthentik(),  # type: ignore[arg-type]
        keycast=signer_client("belfry"),
        settings=s,
        relay=relay,  # type: ignore[arg-type]
        whitelist=whitelist,
        issuer=IssuerClient(ISSUER_URL),
        verifier=VerifierClient(VERIFIER_URL),
        issuer_jwk=issuer_jwk,
        issuer_x5c=["MIIBfake"],
        bridge_jwk=bridge_jwk,
    )


@pytest.fixture
def people(own, odoo):
    """Ana, an admin, and Bob, a member; both made the way the console makes them."""
    out = {}
    for name in ("Ana", "Bob"):
        made = steward.add_steward(own, f"{name.lower()}@example.org", name)
        p = odoo.tables["belfry.pass"][made["pass"]["pass_id"]]
        out[name] = {
            "partner": p["partner_id"],
            "pubkey": p["keycast_pubkey"],
            "email": f"{name.lower()}@example.org",
            "name": name,
        }
    stewards.remove(own, "bob@example.org")
    console._admins["at"] = 0.0
    return out


@pytest.fixture
def web(own, people):
    with TestClient(create_app(own), base_url=CONSOLE) as c:
        c.people = people
        yield c


# ── what may start ──────────────────────────────────────────────────────────────────────────
def test_the_own_signer_and_authentiks_sign_in_do_not_go_together(tmp_path):
    def make(**more):
        return Settings(
            REGISTER_PATH=str(tmp_path / "r.sqlite"),
            HOOK_TOKEN="h",
            _env_file=None,
            **more,
        )

    with pytest.raises(ValueError, match="PASS_SIGNIN=belfry"):
        stewards.check_settings(make(SIGNER="belfry", PASS_SIGNIN="authentik"))
    with pytest.raises(ValueError, match="PASS_SIGNIN=belfry"):
        build_services(make(SIGNER="belfry", PASS_SIGNIN="authentik", SIGNER_TOKEN="t" * 32))
    # no Authentik, no Keycast: none of their tokens is needed
    stewards.check_settings(make(SIGNER="belfry", PASS_SIGNIN="belfry"))
    # with Keycast everything it needs must be there, as before
    with pytest.raises(ValueError, match="AUTHENTIK_TOKEN, KEYCAST_ADMIN_TOKEN"):
        stewards.check_settings(make(SIGNER="keycast"))
    # … and exactly as before: the variables had to be set and could be empty (a box whose
    # .env carries an empty token started then and starts now)
    stewards.check_settings(
        make(AUTHENTIK_TOKEN="", KEYCAST_ADMIN_TOKEN="", KEYCAST_SERVICE_TOKEN="")
    )
    with pytest.raises(ValueError, match="needs KEYCAST_SERVICE_TOKEN$"):
        stewards.check_settings(make(AUTHENTIK_TOKEN="", KEYCAST_ADMIN_TOKEN="b"))
    stewards.check_settings(
        make(AUTHENTIK_TOKEN="a", KEYCAST_ADMIN_TOKEN="b", KEYCAST_SERVICE_TOKEN="c")
    )


def test_nothing_reaches_authentik(own):
    with pytest.raises(RuntimeError, match="not part of this belfry"):
        own.authentik.group_member_emails("belfry-stewards")
    assert core.sync_participants(own, strict=True) is None


# ── admins live in the register ─────────────────────────────────────────────────────────────
def test_admins_are_people_flagged_in_the_register(own, odoo, people, signer):
    ana, bob = people["Ana"]["partner"], people["Bob"]["partner"]
    assert stewards.emails(own) == ["ana@example.org"]
    assert stewards.is_steward(own, ana) and not stewards.is_steward(own, bob)
    assert console.admin_emails(own) == {"ana@example.org"}
    # the membership was opened without any Authentik user, and none is remembered
    assert not odoo.tables["res.partner"][ana].get("belfry_authentik_user_id")
    assert all(not p.get("authentik_user_id") for p in odoo.tables["belfry.pass"].values())
    # the console's switch, both ways
    console.set_admin(own, "Bob@Example.org", True)
    assert stewards.emails(own) == ["ana@example.org", "bob@example.org"]
    console.set_admin(own, "bob@example.org", False)
    assert stewards.emails(own) == ["ana@example.org"]
    assert odoo.tables["belfry.pass"]  # removing the role leaves the membership
    # an Authentik id left from before a migration means nothing: it is never compared
    odoo.write("res.partner", [bob], {"belfry_authentik_user_id": ana})
    assert not stewards.is_steward(own, bob, ana)


def test_an_address_with_a_wildcard_matches_nobody_else(own, odoo, people):
    console.set_admin(own, "ana@example.org", False)
    assert stewards.emails(own) == []
    odoo.write("res.partner", [people["Ana"]["partner"]], {"belfry_steward": True})
    stewards.remove(own, "an_@example.org")  # Odoo's =ilike reads _ as "any character"
    stewards.remove(own, "%@example.org")
    assert stewards.emails(own) == ["ana@example.org"]


def test_authentiks_stewards_are_copied_once(own, odoo, people, router):
    ak = FakeAuthentik(router, AUTHENTIK_URL)
    for pk, email in ((7, "bob@example.org"), (8, "gone@example.org"), (9, "ana@example.org")):
        ak.users.append({"pk": pk, "username": email, "email": email, "attributes": {}})
    ak.groups["belfry-stewards"] = {"pk": "g", "members": {7, 8, 9}}
    client = AuthentikClient(AUTHENTIK_URL, "ak-token")
    assert stewards.import_from_authentik(own, client) == {
        "marked": ["bob@example.org"],
        "already": ["ana@example.org"],
        "missing": ["gone@example.org"],  # no person here: reported, not made
    }
    assert stewards.emails(own) == ["ana@example.org", "bob@example.org"]
    again = stewards.import_from_authentik(own, client)
    assert again["marked"] == [] and again["already"] == ["ana@example.org", "bob@example.org"]
    assert not odoo.search_read("res.partner", [["email", "=", "gone@example.org"]], ["id"])


# ── the steward pages sign in with the belfry's own sign-in ─────────────────────────────────
def test_steward_pages_take_the_pass_pages_session(own, people):
    def session(partner):
        return mypass._sign(own, {"kind": "session", "partner": partner}, 3600)

    with TestClient(create_app(own), base_url="https://pass.belfry.test") as c:
        r = c.get("/steward", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/steward/login"
        r = c.get("/steward/login", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/me/login"
        assert "belfry_pass_next=/steward/login" in r.headers["set-cookie"].replace('"', "")
        c.cookies.clear()
        c.cookies.set(mypass.SESSION_COOKIE, session(people["Bob"]["partner"]))
        r = c.get("/steward/login", follow_redirects=False)
        assert r.status_code == 403 and "not a steward" in r.text
        assert steward.SESSION_COOKIE not in r.headers.get("set-cookie", "")
        c.cookies.set(mypass.SESSION_COOKIE, session(people["Ana"]["partner"]))
        r = c.get("/steward/login", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/steward"
        assert c.get("/steward").status_code == 200


# ── Keycast's sign-in for apps, answered by Belfry ──────────────────────────────────────────
def code_for(web, odoo, email: str, redirect: str, client_id: str) -> str:
    page = authorize(web, redirect_uri=redirect, client_id=client_id).text
    f = flow_of(page)
    assert web.post("/connect/send", json={**f, "email": email}).status_code == 200
    r = web.post("/connect/verify", json={**f, "code": odoo.mailed_codes[email]})
    assert r.status_code == 200 and r.json()["next"] == "in", r.text
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.json()["to"]).query))["code"]


def tokens_for(web, odoo, email: str, redirect: str, client_id: str) -> dict:
    body = {
        "grant_type": "authorization_code",
        "code": code_for(web, odoo, email, redirect, client_id),
        "client_id": client_id,
        "redirect_uri": redirect,
        "code_verifier": VERIFIER,
    }
    r = web.post("/api/oauth/token", json=body, headers=APPS)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    assert web.post("/api/oauth/token", json=body, headers=APPS).status_code == 400  # once
    return r.json()


def call(web, access: str, method: str, params: list):
    return web.post(
        "/api/nostr",
        json={"method": method, "params": params},
        headers={**APPS, "Authorization": f"Bearer {access}"},
    )


def event(kind: int, *tags: list[str]) -> dict:
    return {"kind": kind, "created_at": 1_800_000_000, "tags": list(tags), "content": "x"}


def test_the_apps_address_hands_over_to_the_belfrys_sign_in(web):
    q = "client_id=belfry-calendar&redirect_uri=x&state=s&code_challenge=c"
    r = web.get(f"/api/oauth/authorize?{q}", headers=APPS, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == f"{CONSOLE}/connect/authorize?{q}"  # our own, fixed address
    r = web.get(
        "/api/auth/oidc/logout?return_to=https://evil.example/",
        headers=APPS,
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].startswith(f"{CONSOLE}/connect/logout?")


def test_with_keycast_these_paths_are_not_belfrys(own, people):
    own.settings.signer = "keycast"
    with TestClient(create_app(own), base_url=CONSOLE) as c:
        for path in ("/api/oauth/token", "/api/oauth/revoke", "/api/nostr"):
            assert c.post(path, json={}, headers=APPS).status_code == 404
        assert c.get("/api/oauth/authorize", headers=APPS).status_code == 404
        assert c.get("/api/auth/oidc/logout", headers=APPS).status_code == 404


def test_an_app_of_the_suite_signs_in_and_signs(web, odoo, signer, own):
    assert apps.own_app(own, CALENDAR) and not apps.own_app(own, APP)
    own.settings.osv_apps = False  # the suite does not run here: its hosts are nobody's
    assert not apps.own_app(own, CALENDAR)
    own.settings.osv_apps = True
    t = tokens_for(web, odoo, "bob@example.org", CALENDAR, "belfry-calendar")
    bob = web.people["Bob"]["pubkey"]
    assert t["bunker_url"].startswith("bunker://") and t["pubkey"] == bob
    assert t["token_type"] == "Bearer" and t["expires_in"] == 3600
    assert signer.bunkers[-1]["scope"] == "own"
    assert call(web, t["access_token"], "get_public_key", []).json() == {"result": bob}
    # one of the belfry's own apps: what the chat signs, it may sign (moderation, AUTH, NIP-98)
    for e in (
        event(1),
        event(9000, ["h", "general"]),
        event(22242, ["relay", "wss://relay.belfry.test"]),
        event(27235, ["u", "https://belfry.belfry.test/api/v1/whois"]),
    ):
        r = call(web, t["access_token"], "sign_event", [e])
        assert r.status_code == 200 and r.json()["result"]["pubkey"] == bob, r.text
    as_text = call(web, t["access_token"], "sign_event", [json.dumps(event(1))])
    assert as_text.status_code == 200
    other = {**event(1), "pubkey": web.people["Ana"]["pubkey"]}
    assert call(web, t["access_token"], "sign_event", [other]).status_code == 403
    peer = web.people["Ana"]["pubkey"]
    call(web, t["access_token"], "nip44_encrypt", [peer, "hello"])
    assert ("nip44_encrypt", bob) in signer.rpc  # done by the signer, as Bob (the fake has no key)
    assert call(web, t["access_token"], "nip44_encrypt", [peer]).status_code == 400
    # the calendar is not the wallet: it derives nothing
    assert call(web, t["access_token"], "derive_secret", ["cashu-wallet-v1"]).status_code == 403
    assert call(web, t["access_token"], "export_key", []).status_code == 400
    # only the wallet the belfry serves, only its purposes
    w = tokens_for(web, odoo, "bob@example.org", "https://wallet.belfry.test/oauth/callback", "x")
    secret = call(web, w["access_token"], "derive_secret", ["cashu-wallet-v1"])
    assert secret.status_code == 200 and len(secret.json()["result"]) == 64
    assert call(web, w["access_token"], "derive_secret", ["anything-else"]).status_code == 403


def test_an_outside_app_is_limited_in_what_it_may_have_signed(web, odoo, signer, own):
    apps.decide(own, web.people["Ana"], APP, "Notes", True)
    t = tokens_for(web, odoo, "bob@example.org", APP, "notes")
    assert signer.bunkers[-1]["scope"] == "outside"  # the signer limits the bunker the same way
    access = t["access_token"]
    for ok in (
        event(1),
        event(7, ["e", "0" * 64]),
        event(27235, ["u", "https://api.example.org/x"], ["method", "GET"]),
        event(22242, ["relay", "wss://relay.example.org"], ["challenge", "c"]),
    ):
        assert call(web, access, "sign_event", [ok]).status_code == 200, ok
    for refused in (
        event(27235, ["u", "https://belfry.belfry.test/api/v1/admins"], ["method", "POST"]),
        event(27235, ["u", "https://anything.belfry.test/"]),
        event(27235),
        event(22242, ["relay", "wss://relay.belfry.test"], ["challenge", "c"]),
        event(22242, ["challenge", "c"]),
        event(24133, ["p", "0" * 64]),
        event(9000, ["h", "general"]),
        event(9005, ["h", "general"]),
        event(9021, ["h", "general"]),
        event(0),
    ):
        r = call(web, access, "sign_event", [refused])
        assert r.status_code == 403 and r.json()["error"] == "forbidden", refused
    assert call(web, access, "derive_secret", ["cashu-wallet-v1"]).status_code == 403
    # the same answers as remoteapps.py gives a remote app: one rule set
    hosts = remoteapps.own_hosts(own.settings)
    assert not remoteapps.allowed(None, True, "sign_event", event(9000), hosts)


def test_one_rule_set_for_the_signer_and_the_service():
    """The signer (limits.rs) and remoteapps.py are held to the same vectors: the events an
    outside app never gets signed (app data and labels under the belfry's names among them),
    and decrypting only with the member's consent (``methods``; ``decrypt`` is the flag)."""
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "services/signer/vectors/limits.json"
    data = json.loads(path.read_text())
    hosts = remoteapps.Own(
        data["own"]["domain"],
        frozenset(data["own"]["api"]),
        frozenset(data["own"]["relays"]),
        frozenset(data["own"]["blossom"]),
    )
    assert len(data["cases"]) >= 73 and len(data["methods"]) >= 7
    assert not (path.parent / "limits-next.json").exists(), "one file for both"
    for case in data["cases"]:
        allowed = remoteapps.allowed(None, True, "sign_event", case["event"], hosts)
        assert allowed is not case["refused"], case["name"]
    for case in data["methods"]:
        allowed = remoteapps.allowed(None, case["decrypt"], case["method"], None, hosts)
        assert allowed is not case["refused"], case["name"]


def test_an_outside_app_does_not_read_or_write_the_belfrys_own(web, odoo, signer, own):
    apps.decide(own, web.people["Ana"], APP, "Notes", True)
    t = tokens_for(web, odoo, "bob@example.org", APP, "notes")
    access, peer = t["access_token"], web.people["Ana"]["pubkey"]
    for refused in (
        event(30078, ["d", "belfry:sealed"]),
        event(30078, ["d", "belfry:channel-order"]),
        event(1985, ["L", "belfry/pin"], ["l", "pinned", "belfry/pin"], ["e", "0" * 64]),
        event(1985, ["l", "passed", "belfry/proposal-status"]),
    ):
        assert call(web, access, "sign_event", [refused]).status_code == 403, refused
    for fine in (
        event(30078, ["d", "notes:settings"]),
        event(1985, ["L", "com.example"], ["l", "good", "com.example"]),
        event(5, ["e", "0" * 64]),
    ):
        assert call(web, access, "sign_event", [fine]).status_code == 200, fine
    # it writes to people; reading what was written to the member needs their word, per app
    assert call(web, access, "nip44_encrypt", [peer, "hello"]).status_code != 403
    assert ("nip44_encrypt", web.people["Bob"]["pubkey"]) in signer.rpc  # reached the signer
    for method in ("nip44_decrypt", "nip04_decrypt"):
        r = call(web, access, method, [peer, "ciphertext"])
        assert r.status_code == 403 and r.json()["error"] == "forbidden"
    assert ("nip44_decrypt", web.people["Bob"]["pubkey"]) not in signer.rpc
    grant = appsignin.store(own).by("access", appsignin.mark(own.settings, "access", access))
    assert grant["decrypt"] == 0 and grant["scope"] == "outside"
    # the belfry's own apps read, as the chat does
    mine = tokens_for(web, odoo, "bob@example.org", CALENDAR, "belfry-calendar")
    call(web, mine["access_token"], "nip44_decrypt", [peer, "ciphertext"])
    assert ("nip44_decrypt", web.people["Bob"]["pubkey"]) in signer.rpc


def test_a_malformed_client_key_does_not_spend_the_code(web, odoo, signer, own):
    body = {
        "grant_type": "authorization_code",
        "code": code_for(web, odoo, "bob@example.org", CALENDAR, "belfry-calendar"),
        "client_id": "belfry-calendar",
        "redirect_uri": CALENDAR,
        "code_verifier": VERIFIER,
    }
    r = web.post("/api/oauth/token", json={**body, "client_pubkey": "not-a-key"}, headers=APPS)
    assert r.status_code == 400
    assert web.post("/api/oauth/token", json=body, headers=APPS).status_code == 200  # still good


def test_the_native_app_gets_its_silent_answer_at_its_own_address(web, odoo, own):
    """prompt=none: an address in the registry (served here, approved, or the phone apps')
    gets ``error=login_required`` at that address when nobody is signed in; any other address
    gets a page and no redirect."""
    native = "sh.belfry.app://connect"
    r = authorize(web, prompt="none", redirect_uri=native)
    assert r.status_code == 302
    assert r.headers["location"].startswith(native + "?error=login_required")
    r = authorize(web, prompt="none")  # APP: nobody approved it
    assert r.status_code == 403 and "location" not in r.headers
    apps.decide(own, web.people["Ana"], APP, "Notes", True)
    r = authorize(web, prompt="none")
    assert r.status_code == 302 and r.headers["location"].startswith(APP + "?error=login_required")


def test_belfry_json_names_a_sign_in_page_that_exists(web, own):
    doc = web.get("/.well-known/belfry.json", headers={"Host": "belfry.test"}).json()
    assert doc["sign_in"]["login"] == "https://pass.belfry.test/me/login"  # no Authentik here
    assert "keycast" in doc["sign_in"]  # the field stays: the apps' sign-in answers there


def test_a_suspended_members_name_still_resolves_as_with_keycast(web, own, signer):
    """Keycast answers nostr.json from ``users.username`` whatever the status (its
    ``find_pubkey_by_username`` has no status in its WHERE, at fe9c646): a suspended account
    keeps its name and its address keeps resolving. The own signer's names do the same."""
    bob = web.people["Bob"]
    own.keycast.set_username(f"acct:odoo-partner-{bob['partner']}", "bob")
    own.keycast.set_user_status(bob["pubkey"], "suspended", "pass closed")
    r = web.get("/.well-known/nostr.json", params={"name": "bob"}, headers={"Host": "belfry.test"})
    assert r.status_code == 200 and r.json() == {"names": {"bob": bob["pubkey"]}}


def test_refresh_tokens_work_once_and_a_copy_ends_the_sign_in(web, odoo, signer, own, monkeypatch):
    t = tokens_for(web, odoo, "bob@example.org", CALENDAR, "belfry-calendar")

    def refresh(token, **more):
        body = {"grant_type": "refresh_token", "refresh_token": token, **more}
        return web.post("/api/oauth/token", json=body, headers=APPS)

    # a token is good for what it is: neither opens the other's door
    assert call(web, t["refresh_token"], "get_public_key", []).status_code == 401
    assert refresh(t["access_token"]).status_code == 400
    assert refresh(t["refresh_token"], client_id="another-app").status_code == 400
    second = refresh(t["refresh_token"], client_id="belfry-calendar")
    assert second.status_code == 200, second.text
    new = second.json()
    assert "bunker_url" not in new and new["refresh_token"] != t["refresh_token"]
    assert call(web, t["access_token"], "get_public_key", []).status_code == 401  # replaced
    assert call(web, new["access_token"], "get_public_key", []).status_code == 200
    # an hour later the access token is over, the refresh token still works
    now = appsignin._now()
    monkeypatch.setattr(appsignin, "_now", lambda: now + appsignin.ACCESS_SECONDS + 1)
    assert call(web, new["access_token"], "get_public_key", []).status_code == 401
    third = refresh(new["refresh_token"]).json()
    assert call(web, third["access_token"], "get_public_key", []).status_code == 200
    # thirty days without a refresh: over
    monkeypatch.setattr(appsignin, "_now", lambda: now + 40 * 86400)
    assert refresh(third["refresh_token"]).status_code == 400
    monkeypatch.setattr(appsignin, "_now", lambda: now + appsignin.ACCESS_SECONDS + 2)
    # the first refresh token again (someone kept a copy): the whole sign-in ends, here and
    # in the signer
    live = [b for b in signer.bunkers if not b.get("revoked")]
    assert refresh(t["refresh_token"]).status_code == 400
    assert refresh(third["refresh_token"]).status_code == 400
    assert call(web, third["access_token"], "get_public_key", []).status_code == 401
    assert len([b for b in signer.bunkers if not b.get("revoked")]) == len(live) - 1


def test_revoking_and_signing_out_end_it_on_the_server(web, odoo, signer, own):
    t = tokens_for(web, odoo, "bob@example.org", CALENDAR, "belfry-calendar")
    u = tokens_for(web, odoo, "bob@example.org", CALENDAR, "belfry-calendar")
    r = web.post("/api/oauth/revoke", json={"token": t["refresh_token"]}, headers=APPS)
    assert r.status_code == 200
    assert web.post("/api/oauth/revoke", json={"token": "unknown"}, headers=APPS).status_code == 200
    assert call(web, t["access_token"], "get_public_key", []).status_code == 401
    assert call(web, u["access_token"], "get_public_key", []).status_code == 200  # another device
    # signed out by the member (the Devices list): the token is worth nothing either
    bob = web.people["Bob"]["partner"]
    for s in console.my_devices(own, bob):
        console.revoke_device(own, bob, s["bunker_pubkey"])
    assert console.my_devices(own, bob) == []
    assert call(web, u["access_token"], "get_public_key", []).status_code == 401
    body = {"grant_type": "refresh_token", "refresh_token": u["refresh_token"]}
    assert web.post("/api/oauth/token", json=body, headers=APPS).status_code == 400


def test_an_app_that_only_signs_over_http_keeps_its_sign_in(web, odoo, signer, own):
    """The wallet never sends a NIP-46 connect. Its sign-in is a connection of kind ``http``
    in the signer: it stays when nobody connects (only its secret ends), carries the app's
    origin, still signs, is listed once and can be signed out."""
    wallet = "https://wallet.belfry.test/oauth/callback"
    t = tokens_for(web, odoo, "bob@example.org", wallet, "x")
    bob = web.people["Bob"]["partner"]
    assert signer.bunkers[-1]["kind"] == "http"
    assert signer.bunkers[-1]["origin"] == "https://wallet.belfry.test"
    assert signer.bunkers[-1]["decrypt"] is False  # nothing asks the member yet
    before = console.my_devices(own, bob)
    assert len(before) == 1  # one sign-in, one row: the signer's connection
    assert signer.expire_unclaimed() == 0  # nobody connected within the window: it stays
    from belfry_onboarding import community

    found = community.member_token(own, bob)
    listed = own.keycast.list_sessions(found[1])
    assert [s["redirect_origin"] for s in listed] == ["https://wallet.belfry.test"]
    r = call(web, t["access_token"], "sign_event", [event(1)])
    assert r.status_code == 200 and r.json()["result"]["pubkey"] == web.people["Bob"]["pubkey"]
    assert call(web, t["access_token"], "derive_secret", ["cashu-wallet-v1"]).status_code == 200
    body = {"grant_type": "refresh_token", "refresh_token": t["refresh_token"]}
    new = web.post("/api/oauth/token", json=body, headers=APPS)
    assert new.status_code == 200, new.text
    # still listed for the member, under the same id and name, and not twice
    after = console.my_devices(own, bob)
    assert [s["bunker_pubkey"] for s in after] == [before[0]["bunker_pubkey"]]
    assert after[0]["application_name"] == before[0]["application_name"]
    assert after[0]["created_at"].endswith("Z") and after[0]["client_pubkey"] is None
    # and revocable from that list
    console.revoke_device(own, bob, after[0]["bunker_pubkey"])
    assert console.my_devices(own, bob) == []
    assert call(web, new.json()["access_token"], "get_public_key", []).status_code == 401
    body = {"grant_type": "refresh_token", "refresh_token": new.json()["refresh_token"]}
    assert web.post("/api/oauth/token", json=body, headers=APPS).status_code == 400


def test_signing_in_by_name_gives_an_outside_app_an_outside_session(web, odoo, signer, own):
    """POST /api/v1/session (name@domain and a code, sessions.py): the signer's limits follow
    the app as they do in the browser's sign-in; an admin's approval does not lift them."""
    from .test_holder_invites import post

    own.keycast.set_username(f"odoo-partner-{web.people['Bob']['partner']}", "bob")
    apps.decide(own, web.people["Ana"], APP, "Notes", True)

    def sign_in_by_name(app, **more):
        key = PrivateKey()
        with TestClient(create_app(own), base_url="https://pass.belfry.test") as c:
            r = post(c, key, "/api/session", {"nip05": "bob@belfry.test", "app": app, **more})
            assert r.status_code == 200, r.text
            code = odoo.mailed_codes["bob@example.org"]
            r = post(c, key, "/api/session/verify", {"code": code})
            assert r.status_code == 200 and r.json()["state"] == "open", r.text
        return key.public_key_xonly.format().hex()

    key = sign_in_by_name(APP)
    assert signer.bunkers[-1]["scope"] == "outside"
    assert signer.bunkers[-1]["client_pubkey"] == key
    sign_in_by_name("sh.belfry.app://connect", client="belfry-android")
    assert signer.bunkers[-1]["scope"] == "own"  # the phone app is the belfry's own


def test_a_member_who_left_or_an_app_no_longer_approved(web, odoo, signer, own):
    apps.decide(own, web.people["Ana"], APP, "Notes", True)
    t = tokens_for(web, odoo, "bob@example.org", APP, "notes")
    assert call(web, t["access_token"], "get_public_key", []).status_code == 200
    apps.decide(own, web.people["Ana"], APP, "", False)
    assert call(web, t["access_token"], "get_public_key", []).status_code == 401
    c = tokens_for(web, odoo, "bob@example.org", CALENDAR, "belfry-calendar")
    for p in odoo.tables["belfry.pass"].values():
        if p["partner_id"] == web.people["Bob"]["partner"]:
            p["state"] = "revoked"
    assert call(web, c["access_token"], "sign_event", [event(1)]).status_code == 401


def test_a_logout_ends_the_browsers_session_on_the_server(web, odoo, own):
    sign_in(web, odoo, "bob@example.org")
    cookie = web.cookies.get(console.COOKIE)
    claims = console.read_session(own, cookie)
    assert claims and claims["sid"] and claims["partner"] == web.people["Bob"]["partner"]
    r = web.get("/connect/logout?return_to=https://evil.example/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"  # never to an unknown address
    assert console.read_session(own, cookie) is None  # a copy of the cookie opens nothing
    # the console's own Log out does the same
    sign_in(web, odoo, "bob@example.org")
    cookie = web.cookies.get(console.COOKIE)
    assert console.read_session(own, cookie)
    web.get("/logout", follow_redirects=False)
    assert console.read_session(own, cookie) is None
    # back to an app of ours, and only then
    sign_in(web, odoo, "bob@example.org")
    r = web.get("/connect/logout?return_to=https://calendar.belfry.test/", follow_redirects=False)
    assert r.headers["location"] == "https://calendar.belfry.test/"


def test_a_silent_sign_in_never_redirects_to_an_address_nobody_approved(web, odoo, own):
    r = authorize(web, prompt="none")  # APP: not approved, nobody signed in
    assert r.status_code == 403 and "location" not in r.headers
    assert "Your Bank" not in r.text  # nor is the link's text shown
    sign_in(web, odoo, "bob@example.org")  # known browser, still not approved
    r = authorize(web, prompt="none")
    assert r.status_code == 403 and "location" not in r.headers
    web.cookies.clear()
    # an app in the registry does get its answer
    r = authorize(web, prompt="none", redirect_uri=CALENDAR)
    assert r.status_code == 302 and r.headers["location"].startswith(CALENDAR + "?error=login_")
    # and no code without PKCE S256
    assert authorize(web, redirect_uri=CALENDAR, code_challenge_method="plain").status_code == 400
    assert authorize(web, redirect_uri=CALENDAR, code_challenge="short").status_code == 400


def test_an_app_that_names_its_client_key_gets_a_session_bound_to_it(web, odoo, signer, own):
    key = PrivateKey().public_key_xonly.format().hex()

    def exchange(email, **more):
        body = {
            "grant_type": "authorization_code",
            "code": code_for(web, odoo, email, CALENDAR, "belfry-calendar"),
            "client_id": "belfry-calendar",
            "redirect_uri": CALENDAR,
            "code_verifier": VERIFIER,
            **more,
        }
        return web.post("/api/oauth/token", json=body, headers=APPS)

    assert exchange("bob@example.org", client_pubkey=key).status_code == 200
    assert signer.bunkers[-1]["client_pubkey"] == key  # nothing waits for a first connect
    assert exchange("bob@example.org", client_pubkey="not-a-key").status_code == 400
    taken = exchange("ana@example.org", client_pubkey=key)  # one client key, one identity
    assert taken.status_code == 409 and taken.json()["error_description"] == "client_key_taken"
    assert exchange("bob@example.org").status_code == 200
    assert signer.bunkers[-1]["client_pubkey"] is None  # bound by the first connect, in 10 min


def test_tokens_are_kept_as_marks_under_a_key_per_kind(own, web, odoo):
    t = tokens_for(web, odoo, "bob@example.org", CALENDAR, "belfry-calendar")
    s = own.settings
    assert appsignin.key_for(s, "token/access") != appsignin.key_for(s, "token/refresh")
    row = appsignin.store(own).by("access", appsignin.mark(s, "access", t["access_token"]))
    assert row and row["refresh"] == appsignin.mark(s, "refresh", t["refresh_token"])
    kept = json.dumps(row)
    assert t["access_token"] not in kept and t["refresh_token"] not in kept
    assert appsignin.mark(s, "access", t["refresh_token"]) != row["refresh"]


def test_sessions_carry_their_apps_origin(web, odoo, signer, own):
    """The signer keeps where a session was opened from and gives it back as
    ``redirect_origin`` (as Keycast did), so a chat session is told by its origin again."""
    from belfry_onboarding import community, discovery

    from .test_holder_invites import post

    own.keycast.set_username(f"odoo-partner-{web.people['Bob']['partner']}", "bob")
    key = PrivateKey()
    with TestClient(create_app(own), base_url="https://pass.belfry.test") as c:
        r = post(
            c,
            key,
            "/api/session",
            {"nip05": "bob@belfry.test", "app": "sh.belfry.app://connect", "client": "belfry-ios"},
        )
        assert r.status_code == 200, r.text
        r = post(c, key, "/api/session/verify", {"code": odoo.mailed_codes["bob@example.org"]})
        assert r.status_code == 200, r.text
    assert signer.bunkers[-1]["origin"] == "sh.belfry.app://connect"
    assert signer.bunkers[-1]["kind"] == "nip46"  # the phone app talks NIP-46
    found = community.member_token(own, web.people["Bob"]["partner"])
    row = own.keycast.list_sessions(found[1])[-1]
    assert row["redirect_origin"] == "sh.belfry.app://connect"
    assert discovery.chat_session(own, row)
    # an app that took a chat name is still not the chat when its origin is elsewhere
    assert not discovery.chat_session(
        own, {"application_name": "Chat", "redirect_origin": "https://evil.example"}
    )
    assert discovery.chat_session(own, {"application_name": "Chat", "redirect_origin": ""})
    assert apps.origin_of("https://notes.example.org/cb?x=1") == "https://notes.example.org"
    assert apps.origin_of("http://localhost:5173/cb") == "http://localhost:5173"
    assert apps.origin_of("not an address") == ""


def test_a_signer_or_register_failure_is_a_502_the_apps_browser_can_read(
    web, odoo, signer, own, monkeypatch
):
    """Errors from underneath (httpx, the signer's own refusals) answer 502 signer_unavailable
    with the CORS headers on /api/oauth/token and /api/nostr, never a bare 500."""
    import httpx

    from belfry_onboarding import appsignin

    t = tokens_for(web, odoo, "bob@example.org", CALENDAR, "belfry-calendar")
    origin = {**APPS, "Origin": "https://calendar.belfry.test"}

    def down(*_a, **_k):
        raise httpx.ConnectError("register down")

    monkeypatch.setattr(appsignin, "check_live", down)
    r = web.post(
        "/api/nostr",
        json={"method": "get_public_key", "params": []},
        headers={**origin, "Authorization": f"Bearer {t['access_token']}"},
    )
    assert r.status_code == 502 and r.json()["error"] == "signer_unavailable"
    assert r.headers["access-control-allow-origin"] == "https://calendar.belfry.test"
    monkeypatch.setattr(appsignin, "refresh", down)
    r = web.post(
        "/api/oauth/token",
        json={"grant_type": "refresh_token", "refresh_token": t["refresh_token"]},
        headers=origin,
    )
    assert r.status_code == 502 and r.headers["access-control-allow-origin"]


def test_a_table_that_cannot_be_written_never_stops_a_logout_or_a_revoke(own, monkeypatch):
    import sqlite3

    from belfry_onboarding import appsignin

    def broken(_svc):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(appsignin, "store", broken)
    appsignin.end_session(own, {"sid": "s" * 16, "exp": 2_000_000_000})  # logged, not raised
    assert appsignin.session_revoked(own, 1, "ab" * 32) == 0
