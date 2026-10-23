"""Steward pages (/steward): login through Authentik, CSRF, invites, mark paid, CLI."""

from __future__ import annotations

import re
import urllib.parse

import httpx
import pytest
from fastapi.testclient import TestClient

from belfry_onboarding import core, steward
from belfry_onboarding.app import create_app

from .conftest import AUTHENTIK_URL, seed_paid_invoice


@pytest.fixture
def stewarding(services, router):
    """Steward pages configured; Authentik's token and userinfo endpoints mocked."""
    services.settings.steward_oidc_client_secret = "steward-secret"
    services.settings.steward_session_secret = "s" * 32
    user = {"email": "sam@belfry.test", "name": "Steward Sam", "groups": ["belfry-stewards"]}
    seen: dict = {}

    def token(request: httpx.Request) -> httpx.Response:
        seen["token"] = urllib.parse.parse_qs(request.content.decode())
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"access_token": "at-1", "token_type": "Bearer"})

    router.post(f"{AUTHENTIK_URL}/application/o/token/").mock(side_effect=token)
    router.get(f"{AUTHENTIK_URL}/application/o/userinfo/").mock(
        side_effect=lambda request: httpx.Response(200, json=user)
    )
    return {"user": user, "seen": seen}


@pytest.fixture
def web(services):
    # PUBLIC_URL is https, so the cookies are Secure: talk https to the test server.
    with TestClient(create_app(services), base_url="https://testserver") as c:
        yield c


def login(web) -> str:
    start = web.get("/steward/login", follow_redirects=False)
    assert start.status_code == 303
    location = start.headers["location"]
    assert location.startswith("https://login.belfry.test/application/o/authorize/?")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
    assert query["client_id"] == ["belfry-steward"]
    assert query["redirect_uri"] == ["https://belfry.test/steward/callback"]
    assert query["code_challenge_method"] == ["S256"]
    back = web.get(
        "/steward/callback",
        params={"code": "c-1", "state": query["state"][0]},
        follow_redirects=False,
    )
    assert back.status_code == 303, back.text
    assert back.headers["location"] == "/steward"
    page = web.get("/steward")
    assert page.status_code == 200
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def test_disabled_without_secrets(web):
    assert web.get("/steward", follow_redirects=False).status_code == 503


def test_login_exchanges_code_with_pkce_and_client_secret(web, stewarding):
    login(web)
    seen = stewarding["seen"]
    assert seen["token"]["code"] == ["c-1"]
    assert seen["token"]["code_verifier"][0]
    assert seen["auth"].startswith("Basic ")


def test_unauthenticated_is_sent_to_login(web, stewarding):
    r = web.get("/steward", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/steward/login"


def test_non_steward_is_refused(web, stewarding):
    stewarding["user"]["groups"] = ["belfry-participants"]
    start = web.get("/steward/login", follow_redirects=False)
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(start.headers["location"]).query)["state"]
    r = web.get(
        "/steward/callback", params={"code": "c", "state": state[0]}, follow_redirects=False
    )
    assert r.status_code == 403
    assert "not a steward" in r.text
    assert "belfry_steward=" not in r.headers.get("set-cookie", "")


def test_callback_with_wrong_state_is_refused(web, stewarding):
    web.get("/steward/login", follow_redirects=False)
    r = web.get(
        "/steward/callback", params={"code": "c", "state": "forged"}, follow_redirects=False
    )
    assert r.status_code == 400


def test_create_invite_needs_csrf(web, stewarding, odoo):
    csrf = login(web)
    r = web.post("/steward/invites", data={"note": "for carol", "csrf": "wrong"})
    assert r.status_code == 403
    assert odoo.tables["belfry.invite"] == {}
    r = web.post(
        "/steward/invites", data={"note": "for carol", "csrf": csrf}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/steward?created=1"
    invite = odoo.get("belfry.invite", 1)
    assert invite["steward_email"] == "sam@belfry.test" and invite["note"] == "for carol"
    page = web.get(r.headers["location"])
    assert invite["url"] in page.text and "Invite created" in page.text


def test_revoke_invite(web, stewarding, odoo):
    csrf = login(web)
    web.post("/steward/invites", data={"note": "x", "csrf": csrf})
    r = web.post("/steward/invites/1/revoke", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert odoo.get("belfry.invite", 1)["state"] == "revoked"


def test_mark_paid_activates_the_pass(web, stewarding, services, odoo, authentik, keycast):
    assert services.settings.allow_manual_payments is False
    ids = seed_paid_invoice(odoo, payment_state="not_paid", with_transaction=False)
    pass_id = odoo.add(
        "belfry.pass",
        partner_id=ids["partner"],
        tier="week",
        invoice_id=ids["invoice"],
        state="pending",
    )
    csrf = login(web)
    page = web.get("/steward")
    assert f"/steward/passes/{pass_id}/mark-paid" in page.text
    r = web.post(
        f"/steward/passes/{pass_id}/mark-paid", data={"csrf": csrf}, follow_redirects=False
    )
    assert r.status_code == 303 and "msg=paid" in r.headers["location"]
    p = odoo.get("belfry.pass", pass_id)
    assert p["state"] == "active"
    assert p["manual_paid_by"] == "sam@belfry.test"
    assert keycast.preloads == 1


def test_manual_payment_without_steward_mark_stays_refused(services, odoo):
    ids = seed_paid_invoice(odoo, payment_state="paid", with_transaction=False)
    assert core.activate_pass(services, ids["invoice"]) == "not_paid"


def test_steward_mark_counts_even_with_a_failed_card_attempt(services, odoo, authentik, keycast):
    ids = seed_paid_invoice(odoo, payment_state="paid", tx_state="error")
    odoo.add(
        "belfry.pass",
        partner_id=ids["partner"],
        tier="week",
        invoice_id=ids["invoice"],
        state="pending",
        manual_paid_by="sam@belfry.test",
    )
    assert core.activate_pass(services, ids["invoice"]) == "activated"


def test_add_steward_creates_or_reuses_the_user(services, authentik):
    authentik.groups["belfry-stewards"] = {"pk": "0000-stewards", "members": set()}
    first = steward.add_steward(services, "sam@belfry.test", "Steward Sam")
    assert first["created"] is True
    again = steward.add_steward(services, "sam@belfry.test")
    assert again == {**first, "created": False}
    assert authentik.members("belfry-stewards") == {first["authentik_user"]}
    with pytest.raises(ValueError):
        steward.add_steward(services, "not-an-address")


def test_concurrent_activation_runs_once(services, odoo, authentik, keycast, monkeypatch):
    """Mark paid and Odoo's invoice-paid webhook arrive together: one activation, one link."""
    import threading
    import time

    ids = seed_paid_invoice(odoo, payment_state="paid", with_transaction=False)
    pass_id = odoo.add(
        "belfry.pass",
        partner_id=ids["partner"],
        tier="week",
        invoice_id=ids["invoice"],
        state="pending",
        manual_paid_by="sam@belfry.test",
    )
    links = []
    login_link = core._login_link

    def slow_login_link(svc, user_id):
        links.append(user_id)
        time.sleep(0.05)  # widen the window between reading the pass and writing it
        return login_link(svc, user_id)

    monkeypatch.setattr(core, "_login_link", slow_login_link)
    results = []

    def run():
        results.append(core.activate_pass(services, ids["invoice"]))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["activated", "already_active"]
    assert len(links) == 1
    assert odoo.get("belfry.pass", pass_id)["state"] == "active"
