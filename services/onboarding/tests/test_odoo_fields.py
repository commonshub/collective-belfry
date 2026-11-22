"""Fields only Belfry's own register has are never asked of Odoo (Friedger's review of #8: the
migration's ``_tiers`` read ``guest_agent`` from Odoo, which answers ``Invalid field``; so did
roles, admins and guests). The fake Odoo refuses any field the add-on lacks (fakes.check_fields),
so this class of bug fails here instead of on a belfry."""

from __future__ import annotations

import pytest

from belfry_onboarding import core, guests, migration, roles, usernames
from belfry_onboarding.clients.odoo import OdooError
from belfry_onboarding.holder_invites import ApiError

from .fakes import ADDON_FIELDS

KEY = "ab" * 32


def test_the_fake_odoo_knows_the_add_ons_fields_and_refuses_others(odoo):
    assert {"partner_id", "tier", "keycast_pubkey", "steward_pass"} <= ADDON_FIELDS["belfry.pass"]
    assert "belfry_steward" in ADDON_FIELDS["res.partner"]
    with pytest.raises(OdooError, match="Invalid field"):
        odoo.search_read("belfry.pass", [], ["partner_id", "guest_agent"])
    with pytest.raises(OdooError, match="Invalid field"):
        odoo.search_read("belfry.pass", [["guest_channel", "=", "x"]], ["partner_id"])
    with pytest.raises(OdooError, match="Invalid field"):
        odoo.read("res.partner", [1], ["belfry_no_such_field"])
    odoo.search_read("res.partner", [], ["name", "email", "belfry_steward"])  # standard + add-on


def test_pass_fields_leave_the_registers_own_fields_out_with_odoo(services):
    assert core.pass_fields(services, "tier", "guest_agent", "agent_owner") == ["tier"]


def test_nothing_asks_odoo_for_a_field_only_the_register_has(services, odoo):
    pid = odoo.add("res.partner", name="A", email="a@example.org")
    odoo.add("belfry.pass", partner_id=pid, tier="full", state="active", keycast_pubkey=KEY)
    assert migration._tiers(services) == {}
    assert guests._is_agent(services, KEY) is False
    with pytest.raises(ApiError):  # no guest pass: not a member this way, but no OdooError
        roles.make_member(services, "cd" * 32, roles.ADMIN)
    usernames.backfill(services)
    # the closing of a guest pass asks whether it was an agent's: not of Odoo
    pass_id = odoo.add("belfry.pass", partner_id=pid, tier="full", state="revoked")
    core._suspend_unless_other_active(
        services, {"id": pass_id, "tier": "guest", "partner_id": [pid, "A"]}, "test"
    )


def test_a_cohosts_kind_is_read_without_register_only_fields(services, odoo):
    from belfry_onboarding import cohost

    pid = odoo.add("res.partner", name="C", email="c@example.org")
    odoo.add("belfry.pass", partner_id=pid, tier="full", state="active", keycast_pubkey=KEY)
    assert cohost._member(services, KEY) == "member"
