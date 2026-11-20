"""Steward pages (/steward): login with Belfry's session, CSRF, invites, mark paid, CLI."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from belfry_onboarding import core, steward
from belfry_onboarding.app import create_app

from .conftest import seed_paid_invoice, sign_in


@pytest.fixture
def stewarding(services, odoo):
    """Steward pages configured; Sam is a steward in Odoo."""
    services.settings.steward_session_secret = "s" * 32
    sam = odoo.add("res.partner", name="Steward Sam", email="sam@belfry.test", belfry_steward=True)
    return {"partner": sam}


@pytest.fixture
def web(services):
    # PUBLIC_URL is https, so the cookies are Secure: talk https to the test server.
    with TestClient(create_app(services), base_url="https://testserver") as c:
        yield c


def login(web, partner_id: int | None = None) -> str:
    start = web.get("/steward/login", follow_redirects=False)
    assert start.status_code == 303
    assert start.headers["location"] == "/login?next=%2Fsteward%2Flogin"
    odoo = web.app.state.services.odoo
    sam = partner_id or next(
        p["id"] for p in odoo.tables["res.partner"].values() if p.get("belfry_steward")
    )
    sign_in(web, sam)
    back = web.get("/steward/login", follow_redirects=False)
    assert back.status_code == 303, back.text
    assert back.headers["location"] == "/steward"
    page = web.get("/steward")
    assert page.status_code == 200
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def test_disabled_without_secrets(web):
    assert web.get("/steward", follow_redirects=False).status_code == 503


def test_login_goes_through_belfrys_sign_in(web, stewarding):
    csrf = login(web)
    assert csrf


def test_unauthenticated_is_sent_to_login(web, stewarding):
    r = web.get("/steward", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/steward/login"


def test_non_steward_is_refused(web, stewarding, odoo):
    alice = odoo.add("res.partner", name="Alice", email="alice@example.org")
    sign_in(web, alice)
    r = web.get("/steward/login", follow_redirects=False)
    assert r.status_code == 403
    assert "not a steward" in r.text
    assert "belfry_steward=" not in r.headers.get("set-cookie", "")


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


def test_mark_paid_activates_the_pass(web, stewarding, services, odoo, signer):
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
    assert signer.creates == 1


def test_manual_payment_without_steward_mark_stays_refused(services, odoo):
    ids = seed_paid_invoice(odoo, payment_state="paid", with_transaction=False)
    assert core.activate_pass(services, ids["invoice"]) == "not_paid"


def test_steward_mark_counts_even_with_a_failed_card_attempt(services, odoo, signer):
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


def test_add_steward_creates_or_reuses_the_contact(services, odoo):
    first = steward.add_steward(services, "sam@belfry.test", "Steward Sam")
    assert first["created"] is True
    again = steward.add_steward(services, "sam@belfry.test")
    assert again == {
        **first,
        "created": False,
        "pass": {**first["pass"], "result": "already_active"},
    }
    assert odoo.tables["res.partner"][first["partner_id"]]["belfry_steward"] is True
    assert core.steward_emails(services) == ["sam@belfry.test"]
    with pytest.raises(ValueError):
        steward.add_steward(services, "not-an-address")


def test_concurrent_activation_runs_once(services, odoo, signer, monkeypatch):
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

    def slow_login_link(svc):
        links.append(svc)
        time.sleep(0.05)  # widen the window between reading the pass and writing it
        return login_link(svc)

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
