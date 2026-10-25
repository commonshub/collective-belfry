"""pass.<domain>/: the landing page, and HTML instead of JSON for unknown pages in a browser."""

from __future__ import annotations


def test_home_page_links_the_apps(services, client):
    services.settings.apps_public_url = "https://apps.belfry.test"
    services.settings.chat_public_url = "https://chat.belfry.test"
    services.settings.calendar_public_url = ""
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "How to join" in r.text and "Get an invite" in r.text
    assert 'href="https://apps.belfry.test"' in r.text
    assert 'href="/me"' in r.text and 'href="/feedback"' in r.text


def test_start_page_lives_on_the_site_url(services, client):
    services.settings.site_url = "https://belfry.test"
    r = client.get("/", follow_redirects=False)  # the test client's host is testserver
    assert r.status_code == 302 and r.headers["location"] == "https://belfry.test"
    r = client.get("/", headers={"Host": "belfry.test"})
    assert r.status_code == 200 and "How to join" in r.text


def test_test_run_notice_on_every_page(services, client):
    from belfry_onboarding import claim

    claim.TEST_NOTICE = "Test run: no real money."
    try:
        assert "Test run: no real money." in client.get("/").text
        assert "Test run: no real money." in client.get("/help").text
    finally:
        claim.TEST_NOTICE = ""


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


def test_page_errors_are_branded_for_browsers(client):
    # A GET a browser opens: the branded page with the route's status; API clients keep JSON.
    r = client.get("/admin/partners/1/pass", headers={"Accept": "text/html"})
    assert r.status_code in (401, 503)
    assert r.headers["content-type"].startswith("text/html")
    assert f"Error {r.status_code}" in r.text and "run by Belfry" in r.text
    r = client.get("/admin/partners/1/pass", headers={"Accept": "application/json"})
    assert "detail" in r.json()


def test_malformed_link_is_branded_for_browsers(client):
    r = client.get("/admin/partners/not-a-number/pass", headers={"Accept": "text/html"})
    assert r.status_code == 400 and "This request did not work" in r.text
    r = client.get("/admin/partners/not-a-number/pass")
    assert r.status_code == 422


def test_server_error_is_branded_for_browsers(services):
    from fastapi.testclient import TestClient

    from belfry_onboarding.app import create_app

    app = create_app(services)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/boom", headers={"Accept": "text/html"})
        assert r.status_code == 500 and "Something went wrong" in r.text
        r = client.get("/boom")
        assert r.status_code == 500 and r.json() == {"detail": "Internal Server Error"}
