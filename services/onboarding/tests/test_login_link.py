"""Account-setup links: minted through authentik's recovery-link API at activation and on
request, stored on the pass for Odoo to mail (Phase 2 login)."""

from __future__ import annotations

import httpx
import pytest

from belfry_onboarding import core

from .conftest import HOOK_TOKEN, seed_paid_invoice


def pass_for(odoo, invoice_id):
    return next(p for p in odoo.tables["belfry.pass"].values() if p.get("invoice_id") == invoice_id)


def test_activation_stores_a_public_link_once(services, odoo, authentik):
    ids = seed_paid_invoice(odoo)
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    p = pass_for(odoo, ids["invoice"])
    assert authentik.recovery_links == [(p["authentik_user_id"], "days=7")]
    # authentik answered with its own host; the stored link uses the public origin and keeps
    # the flow path and token
    assert (
        p["login_setup_url"]
        == "https://login.belfry.test/if/flow/belfry-account-setup/?flow_token=tok001"
    )
    assert p["login_setup_expires_at"] > p["activated_at"]
    # a replayed webhook returns early: no second link, the stored one stays valid
    assert core.activate_pass(services, ids["invoice"]) == "already_active"
    assert len(authentik.recovery_links) == 1


def test_public_url_with_a_path_prefix(services, odoo, authentik):
    services.settings.authentik_public_url = "https://belfry.test/auth/"
    ids = seed_paid_invoice(odoo)
    core.activate_pass(services, ids["invoice"])
    assert pass_for(odoo, ids["invoice"])["login_setup_url"] == (
        "https://belfry.test/auth/if/flow/belfry-account-setup/?flow_token=tok001"
    )


def test_missing_recovery_flow_keeps_the_pass_pending_until_retry(services, odoo, authentik):
    authentik.recovery_flow_missing = True
    ids = seed_paid_invoice(odoo)
    with pytest.raises(httpx.HTTPStatusError):
        core.activate_pass(services, ids["invoice"])
    assert pass_for(odoo, ids["invoice"])["state"] == "pending"
    assert "login_setup_url" not in pass_for(odoo, ids["invoice"])
    authentik.recovery_flow_missing = False
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    assert pass_for(odoo, ids["invoice"])["login_setup_url"].endswith("flow_token=tok001")


def test_send_login_link_replaces_the_link_of_an_active_pass(services, odoo, authentik):
    ids = seed_paid_invoice(odoo)
    core.activate_pass(services, ids["invoice"])
    p = pass_for(odoo, ids["invoice"])
    first = p["login_setup_url"]

    assert core.send_login_link(services, p["id"]) == "sent"
    p = pass_for(odoo, ids["invoice"])
    assert p["login_setup_url"] != first
    assert p["login_setup_url"].endswith("flow_token=tok002")
    assert [pk for pk, _ in authentik.recovery_links] == [p["authentik_user_id"]] * 2


def test_send_login_link_refuses_passes_that_are_not_active(services, odoo, authentik):
    ids = seed_paid_invoice(odoo)
    pending = odoo.add("belfry.pass", partner_id=ids["partner"], tier="day", state="pending")
    assert core.send_login_link(services, pending) == "not_active"
    core.activate_pass(services, ids["invoice"])
    p = pass_for(odoo, ids["invoice"])
    core.revoke_pass(services, p["id"], "steward")
    assert core.send_login_link(services, p["id"]) == "not_active"
    assert core.send_login_link(services, 9999) == "not_found"
    assert len(authentik.recovery_links) == 1  # only the activation minted one


def test_send_login_link_falls_back_to_the_partner_user(services, odoo, authentik):
    ids = seed_paid_invoice(odoo)
    core.activate_pass(services, ids["invoice"])
    p = pass_for(odoo, ids["invoice"])
    user_pk = p["authentik_user_id"]
    p["authentik_user_id"] = False  # an older pass row without the audit column
    assert core.send_login_link(services, p["id"]) == "sent"
    assert authentik.recovery_links[-1][0] == user_pk


def test_admin_endpoint(client, odoo, services, authentik):
    ids = seed_paid_invoice(odoo)
    core.activate_pass(services, ids["invoice"])
    pass_id = pass_for(odoo, ids["invoice"])["id"]
    assert client.post(f"/admin/passes/{pass_id}/login-link?token=wrong").status_code == 401
    r = client.post(f"/admin/passes/{pass_id}/login-link?token={HOOK_TOKEN}")
    assert r.status_code == 200
    assert r.json() == {"result": "sent", "pass_id": pass_id}
    assert pass_for(odoo, ids["invoice"])["login_setup_url"].endswith("tok002")
    r = client.post(f"/admin/passes/424242/login-link?token={HOOK_TOKEN}")
    assert r.json() == {"result": "not_found", "pass_id": 424242}
