"""pass.<domain>/: the landing page, and HTML instead of JSON for unknown pages in a browser."""

from __future__ import annotations


def test_home_page_links_the_apps(services, client):
    services.settings.apps_public_url = "https://apps.belfry.test"
    services.settings.chat_public_url = "https://chat.belfry.test"
    services.settings.calendar_public_url = ""
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Your Open Source Village pass" in r.text
    assert 'href="https://apps.belfry.test"' in r.text
    assert 'href="https://chat.belfry.test"' in r.text
    assert "Calendar" not in r.text  # no URL, no link


def test_unknown_page_is_html_for_browsers_and_json_for_clients(client):
    r = client.get("/nothing-here", headers={"Accept": "text/html,application/xhtml+xml"})
    assert r.status_code == 404
    assert "Page not found" in r.text
    assert r.headers["content-type"].startswith("text/html")
    r = client.get("/nothing-here", headers={"Accept": "application/json"})
    assert r.status_code == 404
    assert r.json() == {"detail": "Not Found"}


def test_api_errors_stay_json(client):
    # A route's own HTTPException (here: a missing hook token) keeps its JSON body.
    r = client.post("/hooks/odoo/invoice-paid", headers={"Accept": "text/html"}, json={})
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid token"}
