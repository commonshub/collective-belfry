"""Sign in with <domain> for apps (connect.py) and app approvals as Nostr events (apps.py)."""

from __future__ import annotations

import base64
import hashlib
import urllib.parse

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import apps, console, steward
from belfry_onboarding.app import create_app

from .fakes import flash_of

APP = "https://chat.example.org/app/"
VERIFIER = "v" * 64
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
)


@pytest.fixture
def web(services, odoo, authentik, keycast, relay):
    s = services.settings
    s.pass_page_oidc_client_secret = "pass-secret"
    s.pass_page_secret = "p" * 32
    s.belfry_nsec = PrivateKey().secret.hex()
    s.public_url = "https://pass.belfry.test"
    s.console_url = "https://belfry.belfry.test"
    authentik.groups["belfry-stewards"] = {"pk": "0000-stewards", "members": set()}
    people = {}
    for name in ("Ana", "Bob"):
        made = steward.add_steward(services, f"{name.lower()}@example.org", name)
        p = odoo.tables["belfry.pass"][made["pass"]["pass_id"]]
        people[name] = {
            "partner": p["partner_id"],
            "pubkey": p["keycast_pubkey"],
            "email": f"{name.lower()}@example.org",
            "name": name,
        }
    # Bob is a member, not an admin
    bob_pk = next(u["pk"] for u in authentik.users if u.get("email") == "bob@example.org")
    authentik.groups["belfry-stewards"]["members"].discard(bob_pk)
    console._admins["at"] = 0.0
    apps.forget()
    with TestClient(create_app(services), base_url="https://belfry.belfry.test") as c:
        c.people = people
        yield c


def authorize(c, **extra):
    q = {
        "client_id": "Your Bank (official)",
        "redirect_uri": APP,
        "state": "st8",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        **extra,
    }
    return c.get("/connect/authorize?" + urllib.parse.urlencode(q), follow_redirects=False)


def flow_of(page: str) -> dict[str, str]:
    return {
        "flow": page.split('data-flow="')[1].split('"')[0],
        "csrf": page.split('data-csrf="')[1].split('"')[0],
    }


def sign_in(c, odoo, email):
    page = authorize(c).text
    f = flow_of(page)
    r = c.post("/connect/send", json={**f, "email": email})
    assert r.status_code == 200, r.text
    r = c.post("/connect/verify", json={**f, "code": odoo.mailed_codes[email]})
    assert r.status_code == 200, r.text
    return f, r


def test_a_member_asks_the_admins(web, odoo, relay):
    page = authorize(web).text
    # never text from the URL: the app is shown by its address, not the name it gives itself
    assert "Your Bank" not in page and "chat.example.org/app" in page
    assert "isn't approved yet" in page and "Other ways to sign in" in page
    f = flow_of(page)
    assert web.post("/connect/send", json={**f, "email": "nobody@example.org"}).status_code == 404
    web.post("/connect/send", json={**f, "email": "bob@example.org"})
    wrong = web.post("/connect/verify", json={**f, "code": "000000"})
    assert wrong.status_code == 422 and "not right" in wrong.json()["message"]
    r = web.post("/connect/verify", json={**f, "code": odoo.mailed_codes["bob@example.org"]})
    assert r.json() == {"next": "pending", "others": 0}
    assert console.COOKIE in r.cookies  # this browser is now known
    req = [e for e in relay.events if e["kind"] == apps.REQUEST]
    assert len(req) == 1 and req[0]["pubkey"] == web.people["Bob"]["pubkey"]  # signed by Bob
    assert ["r", APP] in req[0]["tags"] and not apps.is_approved(web.app.state.services, APP)
    # asking again doesn't publish a second request
    f2, r2 = sign_in(web, odoo, "bob@example.org")
    assert r2.json()["next"] == "pending"
    assert len([e for e in relay.events if e["kind"] == apps.REQUEST]) == 1


def test_an_admin_approves_and_the_app_gets_a_session(web, odoo, relay, keycast):
    f, r = sign_in(web, odoo, "ana@example.org")
    assert r.json() == {"next": "approve"}
    r = web.post("/connect/approve", json=f)
    assert r.status_code == 200 and r.json()["next"] == "in"
    to = urllib.parse.urlsplit(r.json()["to"])
    q = dict(urllib.parse.parse_qsl(to.query))
    assert to.geturl().startswith(APP) and q["state"] == "st8"
    ok = [e for e in relay.events if e["kind"] == apps.APPROVE]
    assert len(ok) == 1 and ok[0]["pubkey"] == web.people["Ana"]["pubkey"]  # signed by Ana
    body = {
        "grant_type": "authorization_code",
        "code": q["code"],
        "client_id": "Your Bank (official)",
        "redirect_uri": APP,
        "code_verifier": VERIFIER,
    }
    bad = web.post("/connect/token", json={**body, "code_verifier": "w" * 64})
    assert bad.status_code == 400  # and the code is spent
    f, r = sign_in(web, odoo, "ana@example.org")
    assert r.json()["next"] == "in"  # approved now: straight in
    code = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.json()["to"]).query))["code"]
    t = web.post(
        "/connect/token",
        json={**body, "code": code},
        headers={"Origin": "https://chat.example.org", "User-Agent": IPHONE},
    )
    assert t.status_code == 200, t.text
    assert (
        t.json()["bunker_url"].startswith("bunker://")
        and t.json()["pubkey"] == web.people["Ana"]["pubkey"]
    )
    assert t.headers["access-control-allow-origin"] == "https://chat.example.org"
    assert keycast.bunkers[-1]["app_name"] == "chat.example.org/app · Safari on iPhone"
    assert web.post("/connect/token", json={**body, "code": code}).status_code == 400  # single use


def test_members_go_straight_in_once_approved(web, odoo):
    services = web.app.state.services
    apps.decide(services, web.people["Ana"], APP, "Chat", True)
    page = authorize(web).text
    assert "Sign in to Chat" in page and 'class="badge ok"' not in page  # no badge once approved
    f, r = sign_in(web, odoo, "bob@example.org")
    assert r.json()["next"] == "in"
    # a known browser: one tap, and silent sign-in works
    page = authorize(web).text
    assert 'data-start="known"' in page and "Continue as Bob" in page
    r = web.post("/connect/continue", json=flow_of(page))
    assert r.json()["next"] == "in"
    silent = authorize(web, prompt="none")
    assert silent.status_code == 302 and "code=" in silent.headers["location"]
    web.cookies.clear()
    silent = authorize(web, prompt="none")
    assert "error=login_required" in silent.headers["location"]


def test_withdrawn_and_admin_only(web, odoo, relay):
    services = web.app.state.services
    apps.decide(services, web.people["Ana"], APP, "", True)
    with pytest.raises(PermissionError):
        apps.decide(services, web.people["Bob"], APP, "", False)
    apps.decide(services, web.people["Ana"], APP, "", False)
    assert not apps.is_approved(services, APP)
    # an approval by someone who isn't an admin doesn't count, even if it reaches the relay
    apps.publish(services, web.people["Bob"]["partner"], apps.APPROVE, "https://evil.example/", "")
    assert not apps.is_approved(services, "https://evil.example/")


def test_the_apps_tab(web, odoo, relay):
    sign_in(web, odoo, "bob@example.org")  # Bob asks; his browser is signed in
    page = web.get("/apps").text
    assert (
        "Waiting · 1" in page
        and "chat.example.org/app" in page
        and 'action="/apps/approve"' not in page
    )
    web.cookies.clear()
    sign_in(web, odoo, "ana@example.org")  # Ana's browser (she lands on approve; leave it)
    page = web.get("/apps").text
    assert 'action="/apps/approve"' in page and "Bob" in page
    csrf = page.split('name="csrf" value="')[1].split('"')[0]
    r = web.post(
        "/apps/approve", data={"csrf": csrf, "uri": APP, "name": "Chat"}, follow_redirects=False
    )
    assert "approved" in flash_of(r)
    assert apps.display_name(web.app.state.services, APP) == "Chat"
    page = web.get("/apps").text
    assert "Approved · 1" in page and "Withdraw" in page
    r = web.post("/apps/withdraw", data={"csrf": csrf, "uri": APP}, follow_redirects=False)
    assert "withdrawn" in flash_of(r) and not apps.is_approved(web.app.state.services, APP)


def test_bad_links_and_addresses(web):
    assert authorize(web, redirect_uri="http://chat.example.org/").status_code == 400
    assert authorize(web, code_challenge_method="plain").status_code == 400
    assert apps.app_uri("https://a.example/x?y=1#z") == "https://a.example/x"
    assert apps.app_uri("http://localhost:5173/") == "http://localhost:5173/"
    with pytest.raises(ValueError):
        apps.app_uri("javascript:alert(1)")


IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1"
)


def test_device_labels():
    from belfry_onboarding import device

    assert device.label(IPHONE) == "Safari on iPhone"
    android = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/130.0 Mobile Safari/537.36"
    assert device.label(android) == "Chrome on Android"
    mac = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:132.0) Gecko/20100101 Firefox/132.0"
    assert device.label(mac) == "Firefox on Mac"
    assert device.label("") == "" and device.name("Chat", "") == "Chat"
    assert device.split("Chat · Safari on iPhone") == ("Chat", "Safari on iPhone")
    assert device.split("Tea · Biscuits") == ("Tea · Biscuits", "")


def test_a_member_lists_and_ends_their_sessions(services, odoo, keycast, client):
    """GET /api/me/sessions and POST /api/me/sessions/revoke, NIP-98 with the member's key."""
    from .test_holder_invites import get, post

    services.settings.public_url = "https://pass.belfry.test"
    key = PrivateKey()
    pk = key.public_key_xonly.format().hex()
    partner = odoo.add(
        "res.partner", name="Cleo", email="cleo@example.org", belfry_member_number="M-7"
    )
    odoo.add("belfry.pass", partner_id=partner, tier="week", state="active", keycast_pubkey=pk)
    keycast.users[f"odoo-partner-{partner}"] = {"pubkey": pk, "username": "cleo"}
    keycast.last_pubkey = pk
    services.keycast.create_bunker("ucan-token", "Chat · Firefox on Linux")
    services.keycast.create_bunker("ucan-token", "Notes")
    r = get(client, key, "/api/me/sessions")
    assert r.status_code == 200, r.text
    rows = r.json()["sessions"]
    assert [s["application_name"] for s in rows] == ["Chat", "Notes"]
    assert [s["device"] for s in rows] == ["Firefox on Linux", ""]
    r = post(client, key, "/api/me/sessions/revoke", {"bunker_pubkey": rows[0]["bunker_pubkey"]})
    assert r.status_code == 200
    import time

    time.sleep(1.1)  # a NIP-98 authorization is single use; the next one needs another second
    assert [
        s["application_name"] for s in get(client, key, "/api/me/sessions").json()["sessions"]
    ] == ["Notes"]
    # the address comes from the signer's username, not the member number
    assert get(client, key, "/api/me").json()["nip05"].startswith("cleo@")
    assert (
        post(
            client, PrivateKey(), "/api/me/sessions/revoke", {"bunker_pubkey": "a" * 64}
        ).status_code
        == 403
    )


def test_an_app_of_this_belfry_is_shown_by_its_path(web):
    services = web.app.state.services
    services.settings.nip05_domain = "belfry.test"
    own = "https://apps.belfry.test/chat/"
    apps.decide(services, web.people["Ana"], own, "", True)
    page = authorize(web, redirect_uri=own).text
    chip = page.split('<div class="chip">')[1].split("</h1>")[0]
    assert "<b>/chat</b>" in chip and "Sign in to /chat" in chip
    assert "apps.belfry.test" not in chip and 'class="badge' not in chip
