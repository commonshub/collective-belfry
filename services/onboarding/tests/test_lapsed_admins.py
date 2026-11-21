"""An admin whose pass lapsed still signs in on a belfry on its own signer (Friedger's follow-up
on #8: after the switch both admins of the test belfry were locked out, every pass having
closed with the event), and add-steward's pass outlives the event with Odoo too."""

from __future__ import annotations

import datetime as dt

from belfry_onboarding import apps, connect, console, core, mypass, steward

from .test_own_mode import own, people, signer  # noqa: F401 — fixtures


def lapse(odoo, partner: int) -> None:
    for p in odoo.tables["belfry.pass"].values():
        if p["partner_id"] == partner:
            p["state"] = "expired"


def test_an_admin_whose_pass_lapsed_still_signs_in(own, people, odoo):
    ana, bob = people["Ana"], people["Bob"]
    lapse(odoo, ana["partner"])
    lapse(odoo, bob["partner"])
    console._admins["at"] = 0.0
    assert apps.member_by_email(own, ana["email"]) is None  # no active pass
    found = apps.signin_member(own, ana["email"])
    assert found == {**found, "partner": ana["partner"], "pubkey": ana["pubkey"]}
    # the sign-in sends the code (it used to answer 404 "No member … has this address")
    f: dict = {"session": "c" * 64}
    assert connect.send_code(own, f, ana["email"]) == {"to": ana["email"]}
    # the console lets the admin in, as an admin
    sess = {"partner": ana["partner"], "csrf": "x"}
    me = console.viewer(own, sess)
    assert me and me["admin"] is True
    assert ana["pubkey"] in apps.admin_pubkeys(own)
    # a member who is no admin stays out without a pass
    assert apps.signin_member(own, bob["email"]) is None
    assert console.viewer(own, {"partner": bob["partner"], "csrf": "x"}) is None


def test_with_keycast_a_lapsed_pass_signs_nobody_in(own, people, odoo):
    own.settings.signer = "keycast"  # admins sign in through Authentik there
    lapse(odoo, people["Ana"]["partner"])
    assert apps.signin_member(own, people["Ana"]["email"]) is None


def test_a_steward_pass_ends_with_the_role_not_with_the_event(services, odoo, monkeypatch):
    """With an Odoo register add-steward's pass took the event's dates: after the event, a pass
    that had ended the day it was opened (Friedger: pass 103, from 2026-11-21 to 2026-11-14)."""
    monkeypatch.setattr(core, "today", lambda: dt.date(2027, 3, 1))  # after EVENT_END
    made = steward.steward_pass(services, "sam@example.org", "Sam")
    p = odoo.tables["belfry.pass"][made["pass_id"]]
    assert p["state"] == "active"
    assert str(p["date_end"]) == core.MEMBERSHIP_END.isoformat()
    assert str(p["date_start"]) >= "2027-03-01"
    assert mypass.partner_pass(services, p["partner_id"])["state"] == "active"
