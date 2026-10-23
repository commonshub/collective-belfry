"""Authentik participants group: gates the Keycast OIDC login, derived from active passes."""

from __future__ import annotations

import datetime as dt

import pytest

from belfry_onboarding import core

from .conftest import seed_paid_invoice


def pass_for(odoo, invoice_id):
    return next(p for p in odoo.tables["belfry.pass"].values() if p["invoice_id"] == invoice_id)


def test_activation_adds_the_participant_once(services, odoo, authentik):
    ids = seed_paid_invoice(odoo)
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    user_pk = pass_for(odoo, ids["invoice"])["authentik_user_id"]
    assert authentik.members() == {user_pk}
    # webhook replays return early and make no further group calls
    core.activate_pass(services, ids["invoice"])
    assert [c[0] for c in authentik.group_calls] == ["add_user"]


def test_close_and_reactivate_follow_the_active_passes(services, odoo, authentik, monkeypatch):
    monkeypatch.setattr(core, "today", lambda: dt.date(2027, 1, 28))
    first = seed_paid_invoice(odoo, tier="day")  # ends 2027-01-26
    core.activate_pass(services, first["invoice"])
    second = seed_paid_invoice(odoo, tier="full", partner_id=first["partner"])
    odoo.get("payment.transaction", second["transaction"])["provider_reference"] = "pi_2"
    core.activate_pass(services, second["invoice"])
    bob = odoo.add("res.partner", name="Bob Example", email="bob@example.com")
    other = seed_paid_invoice(odoo, tier="week", partner_id=bob)
    odoo.get("payment.transaction", other["transaction"])["provider_reference"] = "pi_3"
    core.activate_pass(services, other["invoice"])
    alice_pk = pass_for(odoo, first["invoice"])["authentik_user_id"]
    bob_pk = pass_for(odoo, other["invoice"])["authentik_user_id"]
    assert alice_pk != bob_pk
    assert authentik.members() == {alice_pk, bob_pk}

    # the day pass expires, but Alice still holds the full pass
    core.expire(services)
    assert authentik.members() == {alice_pk, bob_pk}

    # a steward revokes Bob's pass: Keycast suspended and out of the group
    bob_pass = pass_for(odoo, other["invoice"])["id"]
    assert core.revoke_pass(services, bob_pass, "steward") == "revoked"
    assert authentik.members() == {alice_pk}

    # revoking again re-evaluates and stays consistent
    core.revoke_pass(services, pass_for(odoo, other["invoice"])["id"], "steward")
    assert authentik.members() == {alice_pk}

    assert core.reactivate_pass(services, pass_for(odoo, other["invoice"])["id"]) == "reactivated"
    assert authentik.members() == {alice_pk, bob_pk}


def test_two_partners_sharing_an_authentik_user_keep_it_while_one_pass_is_active(
    services, odoo, authentik
):
    # seed_paid_invoice gives every new partner the same e-mail, so the second partner is
    # matched to the first partner's Authentik user
    first = seed_paid_invoice(odoo)
    core.activate_pass(services, first["invoice"])
    second = seed_paid_invoice(odoo)
    odoo.get("payment.transaction", second["transaction"])["provider_reference"] = "pi_2"
    core.activate_pass(services, second["invoice"])
    user_pk = pass_for(odoo, first["invoice"])["authentik_user_id"]
    assert pass_for(odoo, second["invoice"])["authentik_user_id"] == user_pk

    core.revoke_pass(services, pass_for(odoo, first["invoice"])["id"], "refund")
    assert authentik.members() == {user_pk}
    core.revoke_pass(services, pass_for(odoo, second["invoice"])["id"], "refund")
    assert authentik.members() == set()


def test_missing_group_fails_activation_until_it_exists(services, odoo, authentik):
    group = authentik.groups.pop("belfry-participants")
    ids = seed_paid_invoice(odoo)
    with pytest.raises(RuntimeError, match="belfry-participants"):
        core.activate_pass(services, ids["invoice"])
    assert pass_for(odoo, ids["invoice"])["state"] == "pending"
    authentik.groups["belfry-participants"] = group
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    assert authentik.members() == {pass_for(odoo, ids["invoice"])["authentik_user_id"]}


def test_sync_backfills_and_removes_strays(services, odoo, authentik):
    ids = seed_paid_invoice(odoo)
    core.activate_pass(services, ids["invoice"])
    member = pass_for(odoo, ids["invoice"])["authentik_user_id"]
    # an activation from before the group existed, and someone added by hand
    authentik.groups["belfry-participants"]["members"] = set()
    stray = authentik.users[-1]["pk"] + 1
    authentik.users.append({"pk": stray, "username": "stray", "attributes": {}})
    authentik.groups["belfry-participants"]["members"].add(stray)

    assert core.sync_participants(services) == {"members": 1, "added": [member], "removed": [stray]}
    assert authentik.members() == {member}
    assert core.sync_participants(services) == {"members": 1, "added": [], "removed": []}


def test_sync_is_best_effort_unless_strict(services, authentik, caplog):
    authentik.groups.clear()
    assert core.sync_participants(services) is None
    assert "participants group not synced" in caplog.text
    with pytest.raises(RuntimeError):
        core.sync_participants(services, strict=True)


def test_startup_syncs_the_group(services, odoo, authentik):
    from fastapi.testclient import TestClient

    from belfry_onboarding.app import create_app

    ids = seed_paid_invoice(odoo)
    core.activate_pass(services, ids["invoice"])
    authentik.groups["belfry-participants"]["members"] = set()
    with TestClient(create_app(services)):
        pass
    assert authentik.members() == {pass_for(odoo, ids["invoice"])["authentik_user_id"]}
