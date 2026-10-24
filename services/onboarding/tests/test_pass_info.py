"""GET /admin/partners/<id>/pass: the pass for Keycast's "Your pass" card."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from belfry_onboarding.app import create_app


@pytest.fixture
def web(services):
    services.settings.pass_info_token = "info-token"
    with TestClient(create_app(services)) as c:
        yield c


def auth(token: str = "info-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_needs_the_token(web, odoo):
    partner = odoo.add("res.partner", name="Alice", email="alice@example.org")
    assert web.get(f"/admin/partners/{partner}/pass").status_code == 401
    assert web.get(f"/admin/partners/{partner}/pass", headers=auth("wrong")).status_code == 401


def test_disabled_without_token(services, odoo):
    with TestClient(create_app(services)) as c:
        assert c.get("/admin/partners/1/pass", headers=auth()).status_code == 503


def test_no_pass(web, odoo):
    partner = odoo.add("res.partner", name="Alice", email="alice@example.org")
    assert web.get(f"/admin/partners/{partner}/pass", headers=auth()).status_code == 404


def test_active_pass_wins_over_newer_ones(web, odoo):
    partner = odoo.add(
        "res.partner", name="Alice", email="alice@example.org", belfry_member_number="OSV-0007"
    )
    odoo.add(
        "belfry.pass",
        partner_id=partner,
        tier="week",
        state="active",
        date_start="2027-01-25",
        date_end="2027-01-31",
        npub="npub1alice",
        claim_token="c" * 32,
    )
    odoo.add("belfry.pass", partner_id=partner, tier="day", state="pending", claim_token=False)
    r = web.get(f"/admin/partners/{partner}/pass", headers=auth())
    assert r.status_code == 200
    assert r.json() == {
        "state": "active",
        "tier": "week",
        "tier_name": "Week pass",
        "date_start": "2027-01-25",
        "date_end": "2027-01-31",
        "member_number": "OSV-0007",
        "npub": "npub1alice",
        "claim_url": "https://belfry.test/claim/" + "c" * 32,
        "show_url": "https://belfry.test/me",
        "tokens": 0,
        "tokens_url": None,
    }


def test_no_claim_link_for_a_revoked_pass(web, odoo):
    partner = odoo.add("res.partner", name="Bob", email="bob@example.org")
    odoo.add("belfry.pass", partner_id=partner, tier="day", state="revoked", claim_token="d" * 32)
    info = web.get(f"/admin/partners/{partner}/pass", headers=auth()).json()
    assert info["state"] == "revoked" and info["claim_url"] is None and info["show_url"] is None
    assert info["member_number"] is None


def test_active_passes_for_the_vote_service(web, odoo):
    partner = odoo.add("res.partner", name="Alice", email="alice@example.org")
    active = odoo.add(
        "belfry.pass", partner_id=partner, tier="week", state="active", keycast_pubkey="aa" * 32
    )
    odoo.add(
        "belfry.pass", partner_id=partner, tier="day", state="revoked", keycast_pubkey="bb" * 32
    )
    odoo.add("belfry.pass", partner_id=partner, tier="day", state="active")  # no key yet
    assert web.get("/admin/active-passes").status_code == 401
    r = web.get("/admin/active-passes", headers=auth())
    assert r.status_code == 200
    assert r.json() == {"passes": [{"pass_id": active, "pubkey": "aa" * 32}]}
