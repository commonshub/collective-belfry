"""Applying to join (/apply) and answering on the steward pages."""

from __future__ import annotations

from .test_steward import login as steward_login
from .test_steward import stewarding, web  # noqa: F401 - fixtures are used by name


def test_apply_form_and_submit(web, odoo, authentik):  # noqa: F811
    page = web.get("/apply")
    assert page.status_code == 200
    assert 'action="/apply"' in page.text and 'name="kind" value="volunteer"' in page.text
    r = web.post(
        "/apply",
        data={
            "name": "Carol",
            "email": "carol@example.org",
            "motivation": "Mesh networks.",
            "kind": "volunteer",
        },
    )
    assert r.status_code == 200 and "Thank you" in r.text
    row = next(iter(odoo.tables["belfry.application"].values()))
    assert (row["name"], row["kind"], row["motivation"]) == ("Carol", "volunteer", "Mesh networks.")


def test_apply_refuses_missing_fields_bad_addresses_and_bots(web, odoo):  # noqa: F811
    r = web.post("/apply", data={"name": "A", "email": "a@b.c", "motivation": " "})
    assert r.status_code == 400 and "Please fill in" in r.text
    r = web.post("/apply", data={"name": "A", "email": "nope", "motivation": "x"})
    assert r.status_code == 400 and "valid e-mail" in r.text
    assert 'value="nope"' in r.text, "the form keeps what was typed"
    r = web.post("/apply", data={"name": "A", "email": "a@b.c", "motivation": "x", "website": "x"})
    assert r.status_code == 200
    assert not odoo.tables["belfry.application"]


def test_start_page_links_the_form(web):  # noqa: F811
    assert 'href="/apply"' in web.get("/").text


def test_stewards_invite_or_decline(web, odoo, stewarding):  # noqa: F811
    for name in ("Dan", "Eve"):
        web.post("/apply", data={"name": name, "email": f"{name}@example.org", "motivation": "Hi"})
    dan, eve = sorted(odoo.tables["belfry.application"])
    csrf = steward_login(web)
    page = web.get("/steward").text
    assert "id='applications'" in page and "Applications (2 new)" in page
    r = web.post(f"/steward/applications/{dan}/invite", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and "application-invite" in r.headers["location"]
    assert odoo.tables["belfry.application"][dan]["state"] == "invited"
    assert odoo.tables["belfry.application"][dan]["handled_by"] == "sam@belfry.test"
    r = web.post(
        f"/steward/applications/{eve}/decline", data={"csrf": csrf}, follow_redirects=False
    )
    assert odoo.tables["belfry.application"][eve]["state"] == "declined"
    again = web.post(
        f"/steward/applications/{eve}/invite", data={"csrf": csrf}, follow_redirects=False
    )
    assert "err=" in again.headers["location"]
    assert web.post(f"/steward/applications/{dan}/invite", data={"csrf": "bad"}).status_code == 403
    assert web.post(f"/steward/applications/{dan}/nope", data={"csrf": csrf}).status_code == 404
