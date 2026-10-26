"""Volunteers: the shift page (/shifts/<secret>), activation with the third shift, the Shifts
tab's API (/admin/shifts) and the steward pages' shifts."""

from __future__ import annotations

import datetime as dt

from belfry_onboarding import core

from .test_steward import login as steward_login
from .test_steward import stewarding, web  # noqa: F401 - fixtures are used by name

AUTH = {"authorization": "Bearer info-token"}


def _shifts(odoo, n=4):
    return [
        odoo.add(
            "belfry.shift",
            name=f"Bar {i}",
            start=f"2027-01-2{5 + i} 09:00:00",
            end=f"2027-01-2{5 + i} 12:00:00",
            capacity=2,
            signups=[],
        )
        for i in range(n)
    ]


def _volunteer(odoo):
    partner = odoo.add("res.partner", name="Vic Volunteer", email="vic@example.org")
    pass_id = odoo.add(
        "belfry.pass",
        partner_id=partner,
        tier="volunteer",
        state="pending",
        volunteer_token="v" * 43,
    )
    return partner, pass_id


def test_third_shift_activates_the_volunteer_pass(web, odoo, services):  # noqa: F811
    shifts = _shifts(odoo)
    partner, pass_id = _volunteer(odoo)
    page = web.get(f"/shifts/{'v' * 43}")
    assert page.status_code == 200
    assert "0 of 3" in page.text and "Bar 0" in page.text and "10:00" in page.text, "Brussels time"
    for i, shift in enumerate(shifts[:3]):
        r = web.post(f"/shifts/{'v' * 43}/{shift}/signup", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].endswith("msg=active" if i == 2 else "msg=signup")
    p = odoo.tables["belfry.pass"][pass_id]
    assert p["state"] == "active"
    assert (p["date_start"], p["date_end"]) == ("2027-01-25", "2027-02-07"), "two weeks"
    assert p["login_setup_url"].startswith("https://belfry.test/signin/")
    start, end = dt.date(2027, 1, 25), dt.date(2027, 2, 7)
    allowance = core.token_allowance(services, "volunteer", None, start, end)
    assert allowance == 14 * services.settings.tokens_per_day
    page = web.get(f"/shifts/{'v' * 43}?msg=active")
    assert "is <strong>active</strong>" in page.text
    assert web.get("/shifts/unknown-token-unknown-token-xx").status_code == 404


def test_full_shift_is_refused_on_the_page(web, odoo):  # noqa: F811
    (shift,) = _shifts(odoo, 1)
    odoo.tables["belfry.shift"][shift]["signups"] = [901, 902]
    _volunteer(odoo)
    r = web.post(f"/shifts/{'v' * 43}/{shift}/signup", follow_redirects=False)
    assert "err=This+shift+is+full" in r.headers["location"]


def test_shifts_api_roles(web, odoo, services, monkeypatch):  # noqa: F811
    services.settings.pass_info_token = "info-token"
    stewards: list[str] = []
    monkeypatch.setattr(services.authentik, "group_member_emails", lambda name: stewards)
    _shifts(odoo, 2)
    partner, pass_id = _volunteer(odoo)
    visitor = odoo.add("res.partner", name="Val Visitor", email="val@example.org")
    assert web.get(f"/admin/shifts?partner={partner}").status_code == 401
    pending = web.get(f"/admin/shifts?partner={partner}", headers=AUTH).json()
    assert pending == {"role": None, "shifts": []}, "the tab comes with the active pass"
    odoo.tables["belfry.pass"][pass_id]["state"] = "active"
    board = web.get(f"/admin/shifts?partner={partner}", headers=AUTH).json()
    assert board["role"] == "volunteer" and len(board["shifts"]) == 2
    shift = board["shifts"][0]["id"]
    r = web.post(f"/admin/shifts/{shift}/signup?partner={partner}", headers=AUTH).json()
    assert r == {"result": "ok", "count": 1, "min_shifts": 3}
    assert web.get(f"/admin/shifts?partner={visitor}", headers=AUTH).json()["role"] is None
    stewards.append("Val@example.org")
    steward = web.get(f"/admin/shifts?partner={visitor}", headers=AUTH).json()
    assert steward["role"] == "steward" and steward["shifts"][0]["taken"] == 1
    refused = web.post(f"/admin/shifts/{shift}/signup?partner={visitor}", headers=AUTH).json()
    assert refused["result"] == "refused"


def test_stewards_add_shifts_and_volunteers(web, odoo, stewarding):  # noqa: F811
    csrf = steward_login(web)
    r = web.post(
        "/steward/shifts",
        data={
            "csrf": csrf,
            "name": "Night watch",
            "day": "2027-01-26",
            "start": "22:00",
            "end": "02:00",
            "place": "Gate",
            "capacity": "3",
        },
        follow_redirects=False,
    )
    assert "shift-added" in r.headers["location"]
    shift = next(iter(odoo.tables["belfry.shift"].values()))
    assert (shift["start"], shift["end"]) == ("2027-01-26 21:00:00", "2027-01-27 01:00:00")
    page = web.get("/steward").text
    assert "id='shifts'" in page and "Night watch" in page and "Tue 26 Jan 22:00" in page
    r = web.post(
        "/steward/volunteers",
        data={"csrf": csrf, "name": "Vic", "email": "vic@x.org"},
        follow_redirects=False,
    )
    assert "volunteer-added" in r.headers["location"]
    web.post(
        "/apply",
        data={"name": "Wes", "email": "wes@example.org", "motivation": "Help", "kind": "volunteer"},
    )
    app_id = next(iter(odoo.tables["belfry.application"]))
    assert "Accept as volunteer" in web.get("/steward").text
    r = web.post(
        f"/steward/applications/{app_id}/volunteer", data={"csrf": csrf}, follow_redirects=False
    )
    assert "application-volunteer" in r.headers["location"]
    assert odoo.tables["belfry.application"][app_id]["state"] == "invited"
