"""The pass on the phone (/me, /check): Belfry's sign-in, renewing QR, door check."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from belfry_onboarding import mypass
from belfry_onboarding.app import create_app

from .conftest import sign_in

TODAY = dt.datetime.now(dt.UTC).date()


@pytest.fixture
def passing(services, router, odoo):
    """Pass page configured, one participant with an active pass."""
    services.settings.pass_page_secret = "p" * 32
    partner = odoo.add("res.partner", name="Alice", email="alice@example.org")
    pass_id = odoo.add(
        "belfry.pass",
        partner_id=partner,
        tier="week",
        state="active",
        date_start=str(TODAY - dt.timedelta(days=1)),
        date_end=str(TODAY + dt.timedelta(days=5)),
        claim_token="c" * 32,
    )
    return {"partner": partner, "pass": pass_id}


@pytest.fixture
def web(services):
    with TestClient(create_app(services), base_url="https://testserver") as c:
        yield c


def login(web) -> None:
    start = web.get("/me", follow_redirects=False)
    assert start.status_code == 303 and start.headers["location"] == "/me/login"
    start = web.get("/me/login", follow_redirects=False)
    assert start.headers["location"] == "/login?next=%2Fme"
    odoo = web.app.state.services.odoo
    partner = next(p["id"] for p in odoo.tables["res.partner"].values() if p["name"] == "Alice")
    sign_in(web, partner)


def test_disabled_without_secrets(web):
    assert web.get("/me", follow_redirects=False).status_code == 503
    assert web.get("/check/1.2.x").status_code == 503


def test_signed_in_participant_sees_a_qr(web, passing):
    login(web)
    page = web.get("/me")
    assert page.status_code == 200
    assert 'id="qr"' in page.text and "Week pass" in page.text
    assert "/claim/" + "c" * 32 in page.text  # the wallet is still offered
    assert page.headers["cache-control"] == "no-store"
    qr = web.get("/me/qr").json()
    assert qr["svg"].startswith("data:image/svg+xml") and qr["expires"] > 0


def test_a_forged_session_is_not_signed_in(web, passing):
    web.cookies.set("belfry_session", "forged")
    r = web.get("/me", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/me/login"


def test_revoked_pass_shows_no_qr(web, passing, odoo):
    login(web)
    odoo.get("belfry.pass", passing["pass"])["state"] = "revoked"
    page = web.get("/me")
    assert 'id="qr"' not in page.text and "revoked" in page.text
    assert web.get("/me/qr").status_code == 409


def test_token_round_trip_and_tampering(services, passing):
    token, exp = mypass.qr_token(services, 42, now=1000)
    assert mypass.read_qr_token(services, token, now=1000) == 42
    assert exp == 1000 + mypass.QR_TTL_SECONDS
    pid, e, mac = token.split(".")
    with pytest.raises(mypass.PassPageError):
        mypass.read_qr_token(services, f"43.{e}.{mac}", now=1000)
    with pytest.raises(mypass.PassPageError):
        mypass.read_qr_token(services, f"{pid}.{int(e) + 60}.{mac}", now=1000)
    with pytest.raises(mypass.PassPageError) as expired:
        mypass.read_qr_token(services, token, now=exp + 1)
    assert expired.value.status == 410


def test_door_check_valid_then_scanned_again(web, services, passing):
    token, _ = mypass.qr_token(services, passing["pass"])
    first = web.get(f"/check/{token}", headers={"accept": "application/json"}).json()
    assert first["valid"] is True and first["tier"] == "week" and first["scanned_before"] is None
    assert "name" not in first and "partner" not in first
    second = web.get(f"/check/{token}", headers={"accept": "application/json"}).json()
    assert second["valid"] is True and second["scanned_before"] is not None
    page = web.get(f"/check/{token}")
    assert "Valid pass" in page.text and "already scanned" in page.text
    assert "Alice" not in page.text


def test_door_check_reads_odoo_at_scan_time(web, services, passing, odoo):
    token, _ = mypass.qr_token(services, passing["pass"])
    odoo.get("belfry.pass", passing["pass"])["state"] = "revoked"
    r = web.get(f"/check/{token}", headers={"accept": "application/json"}).json()
    assert r["valid"] is False and "revoked" in r["reason"]


def test_door_check_dates(services, passing, odoo):
    row = odoo.get("belfry.pass", passing["pass"])
    assert mypass.check_pass(services, passing["pass"], today=TODAY)["valid"] is True
    late = mypass.check_pass(services, passing["pass"], today=TODAY + dt.timedelta(days=6))
    assert late["valid"] is False and "ended" in late["reason"]
    row["date_start"] = str(TODAY + dt.timedelta(days=2))
    early = mypass.check_pass(services, passing["pass"], today=TODAY)
    assert early["valid"] is False and "starts" in early["reason"]


def test_door_check_refuses_junk_and_forgeries(web, services, passing):
    assert "Not valid" in web.get("/check/hello").text
    token, _ = mypass.qr_token(services, passing["pass"])
    pid, e, mac = token.split(".")
    assert "Not valid" in web.get(f"/check/{pid}.{e}.{'A' * len(mac)}").text


def test_scan_log_forgets_expired_tokens():
    log = mypass.ScanLog()
    assert log.first_scan("t", exp=100, now=10) is None
    assert log.first_scan("t", exp=100, now=20) == 10
    assert log.first_scan("u", exp=300, now=200) is None  # "t" expired and is forgotten
    assert log.first_scan("t", exp=100, now=50) is None
