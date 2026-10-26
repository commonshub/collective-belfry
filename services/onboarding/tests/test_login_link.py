"""Sign-in links (Phase 2 login, reworked 2026-10-26): the pass carries Belfry's own one-time
secret (``/signin/<secret>``) for Odoo to mail; spending it mints the authentik recovery link and
redirects there. The welcome page after paying has a secret of its own, so using one never makes
the other useless (authentik keeps one recovery token per user)."""

from __future__ import annotations

import httpx
import pytest

from belfry_onboarding import core

from .conftest import HOOK_TOKEN, seed_paid_invoice


def pass_for(odoo, invoice_id):
    return next(p for p in odoo.tables["belfry.pass"].values() if p.get("invoice_id") == invoice_id)


def test_activation_stores_a_belfry_link_once(services, odoo, authentik):
    ids = seed_paid_invoice(odoo)
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    p = pass_for(odoo, ids["invoice"])
    assert p["login_setup_url"] == f"https://belfry.test/signin/{p['signin_token']}"
    assert len(p["signin_token"]) >= 40
    assert p["login_setup_expires_at"] > p["activated_at"]
    assert authentik.recovery_links == [], "authentik's link is minted only when the link is used"
    # a replayed webhook returns early: the stored link stays valid
    assert core.activate_pass(services, ids["invoice"]) == "already_active"
    assert pass_for(odoo, ids["invoice"])["login_setup_url"] == p["login_setup_url"]


def test_using_the_link_redirects_to_authentik_once(client, services, odoo, authentik):
    ids = seed_paid_invoice(odoo)
    core.activate_pass(services, ids["invoice"])
    p = pass_for(odoo, ids["invoice"])
    path = f"/signin/{p['signin_token']}"
    page = client.get(path)
    assert page.status_code == 200
    assert f'action="{path}"' in page.text, "the page's script sends the POST (scanners do not)"
    assert authentik.recovery_links == [], "opening the page spends nothing"
    r = client.post(path, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == (
        "https://login.belfry.test/if/flow/belfry-account-setup/?flow_token=tok001"
    )
    assert authentik.recovery_links == [(p["authentik_user_id"], "minutes=10")]
    again = client.post(path, follow_redirects=False)
    assert again.status_code == 410
    assert "used already" in again.text
    assert len(authentik.recovery_links) == 1
    assert client.post("/signin/not-a-real-token-at-all-xxxxxxxx").status_code == 410


def test_welcome_page_and_mail_link_are_independent(client, services, odoo, authentik):
    ids = seed_paid_invoice(odoo)
    core.activate_pass(services, ids["invoice"])
    p = pass_for(odoo, ids["invoice"])
    p["welcome_token"] = "w" * 43
    page = client.get(f"/welcome/{'w' * 43}")
    assert page.status_code == 200
    assert "Welcome, Alice!" in page.text and "Open the apps" in page.text
    r = client.post(f"/welcome/{'w' * 43}", follow_redirects=False)
    assert r.status_code == 303 and "flow_token=tok001" in r.headers["location"]
    page = client.get(f"/welcome/{'w' * 43}")
    assert "Open the apps" not in page.text, "the welcome page signs in once"
    # the mail's link still works
    r = client.post(f"/signin/{p['signin_token']}", follow_redirects=False)
    assert r.status_code == 303 and "flow_token=tok002" in r.headers["location"]
    assert client.get("/welcome/unknown").status_code == 404


def test_welcome_page_waits_for_the_activation(client, odoo):
    ids = seed_paid_invoice(odoo)
    odoo.add(
        "belfry.pass",
        partner_id=ids["partner"],
        tier="day",
        state="pending",
        invoice_id=ids["invoice"],
        welcome_token="p" * 43,
    )
    page = client.get(f"/welcome/{'p' * 43}")
    assert page.status_code == 200
    assert "Open the apps" not in page.text
    assert client.post(f"/welcome/{'p' * 43}", follow_redirects=False).status_code == 410


def test_missing_recovery_flow_fails_at_use_not_at_activation(client, services, odoo, authentik):
    authentik.recovery_flow_missing = True
    ids = seed_paid_invoice(odoo)
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    p = pass_for(odoo, ids["invoice"])
    with pytest.raises(httpx.HTTPStatusError):
        client.post(f"/signin/{p['signin_token']}", follow_redirects=False)


def test_send_login_link_replaces_the_link_of_an_active_pass(services, odoo):
    ids = seed_paid_invoice(odoo)
    core.activate_pass(services, ids["invoice"])
    p = pass_for(odoo, ids["invoice"])
    first = p["login_setup_url"]
    p["signin_used_at"] = "2026-10-26 12:00:00"
    assert core.send_login_link(services, p["id"]) == "sent"
    p = pass_for(odoo, ids["invoice"])
    assert p["login_setup_url"] != first
    assert p["login_setup_url"] == f"https://belfry.test/signin/{p['signin_token']}"
    assert p["signin_used_at"] is False


def test_send_login_link_refuses_passes_that_are_not_active(services, odoo):
    ids = seed_paid_invoice(odoo)
    pending = odoo.add("belfry.pass", partner_id=ids["partner"], tier="day", state="pending")
    assert core.send_login_link(services, pending) == "not_active"
    core.activate_pass(services, ids["invoice"])
    p = pass_for(odoo, ids["invoice"])
    core.revoke_pass(services, p["id"], "steward")
    assert core.send_login_link(services, p["id"]) == "not_active"
    assert core.send_login_link(services, 9999) == "not_found"


def test_admin_endpoint(client, odoo, services):
    ids = seed_paid_invoice(odoo)
    core.activate_pass(services, ids["invoice"])
    pass_id = pass_for(odoo, ids["invoice"])["id"]
    first = pass_for(odoo, ids["invoice"])["login_setup_url"]
    assert client.post(f"/admin/passes/{pass_id}/login-link?token=wrong").status_code == 401
    r = client.post(f"/admin/passes/{pass_id}/login-link?token={HOOK_TOKEN}")
    assert r.status_code == 200
    assert r.json() == {"result": "sent", "pass_id": pass_id}
    assert pass_for(odoo, ids["invoice"])["login_setup_url"] != first
    r = client.post(f"/admin/passes/424242/login-link?token={HOOK_TOKEN}")
    assert r.json() == {"result": "not_found", "pass_id": 424242}
