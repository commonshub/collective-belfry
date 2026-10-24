"""Reports from the community apps: /admin/reports, the hidden list, the steward pages."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from belfry_onboarding.app import create_app

from .test_steward import login, stewarding  # noqa: F401 - the fixture is used by name

REPORTER = "aa" * 32
AUTHOR = "bb" * 32
PHOTO = "cc" * 32


@pytest.fixture
def web(services, odoo, authentik):
    services.settings.pass_info_token = "info-token"
    alice = odoo.add("res.partner", name="Alice", email="alice@example.org")
    bob = odoo.add("res.partner", name="Bob", email="bob@example.org")
    odoo.add("belfry.pass", partner_id=alice, state="active", tier="week", keycast_pubkey=REPORTER)
    odoo.add("belfry.pass", partner_id=bob, state="active", tier="day", keycast_pubkey=AUTHOR)
    authentik.users.append(
        {"pk": 7, "username": "sam", "email": "sam@belfry.test", "attributes": {}}
    )
    authentik.users.append(
        {"pk": 8, "username": "kim", "email": "kim@belfry.test", "attributes": {}}
    )
    authentik.groups["belfry-stewards"] = {"pk": "0000-stewards", "members": {7, 8}}
    with TestClient(create_app(services), base_url="https://testserver") as c:
        yield c


AUTH = {"Authorization": "Bearer info-token"}


def body(**kw):
    return {
        "reporter_pubkey": REPORTER,
        "app": "photos",
        "target_kind": "photo",
        "target_ref": PHOTO,
        "target_pubkey": AUTHOR,
        "reason": "spam",
        "note": "selling things",
        "target_url": f"https://photos.belfry.test/#/p/{PHOTO}",
        "target_excerpt": "Buy now",
        **kw,
    }


def test_needs_the_token(web):
    assert web.post("/admin/reports", json=body()).status_code == 401
    assert web.get("/admin/moderation/hidden").status_code == 401


def test_report_is_stored_and_mailed_to_the_stewards(web, odoo):
    r = web.post("/admin/reports", json=body(), headers=AUTH)
    assert r.status_code == 200, r.text
    report = odoo.get("belfry.report", r.json()["id"])
    assert report["target_ref"] == PHOTO and report["reason"] == "spam"
    assert report["mailed_to"] == ["kim@belfry.test", "sam@belfry.test"]
    assert report["target_pass_id"] and report["reporter_pass_id"]


@pytest.mark.parametrize(
    "change",
    [
        {"reason": "boring"},
        {"app": "chat"},
        {"target_ref": "not-a-ref"},
        {"target_pubkey": "zz"},
        {"target_pubkey": REPORTER},  # one's own content
        {"note": ["not", "text"]},
    ],
)
def test_bad_reports_are_refused(web, change):
    assert web.post("/admin/reports", json=body(**change), headers=AUTH).status_code == 400


def test_reporter_needs_an_active_pass(web):
    r = web.post("/admin/reports", json=body(reporter_pubkey="dd" * 32), headers=AUTH)
    assert r.status_code == 403


def test_offer_coordinates_and_profiles_are_references(web):
    for kind, ref in (("listing", f"30402:{AUTHOR}:mug-1"), ("profile", AUTHOR)):
        r = web.post(
            "/admin/reports",
            json=body(app="market", target_kind=kind, target_ref=ref),
            headers=AUTH,
        )
        assert r.status_code == 200, r.text


def test_steward_hides_and_the_apps_learn_it(web, odoo, stewarding):  # noqa: F811
    report_id = web.post("/admin/reports", json=body(), headers=AUTH).json()["id"]
    csrf = login(web)
    page = web.get("/steward").text
    assert "id='reports'" in page and "Reports (1 open)" in page
    assert "Buy now" in page and "Note: selling things" in page and "Bob" in page
    r = web.post(f"/steward/reports/{report_id}/hide", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/steward?msg=report-hide"
    assert odoo.get("belfry.report", report_id)["state"] == "hidden"
    assert web.get("/admin/moderation/hidden", headers=AUTH).json() == {"hidden": [PHOTO]}
    assert re.search(r"Reopen", web.get("/steward").text)
    web.post(f"/steward/reports/{report_id}/reopen", data={"csrf": csrf})
    assert web.get("/admin/moderation/hidden", headers=AUTH).json() == {"hidden": []}


def test_steward_actions_need_csrf_and_a_known_action(web, stewarding):  # noqa: F811
    report_id = web.post("/admin/reports", json=body(), headers=AUTH).json()["id"]
    csrf = login(web)
    assert web.post(f"/steward/reports/{report_id}/hide", data={"csrf": "x"}).status_code == 403
    assert web.post(f"/steward/reports/{report_id}/delete", data={"csrf": csrf}).status_code == 404
