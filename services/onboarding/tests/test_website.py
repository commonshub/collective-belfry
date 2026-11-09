"""The website as a conversation (website.py): #website, previews, publishing a shared preview."""

from __future__ import annotations

import time

from coincurve import PrivateKey
from fastapi.responses import Response
from fastapi.testclient import TestClient

from belfry_onboarding import homepage, website
from belfry_onboarding.app import create_app
from belfry_onboarding.nostr import hex_to_npub

from .test_guests import admin_with_key, rsvc  # noqa: F401 — fixtures
from .test_holder_invites import BASE, nip98

AUTHOR = PrivateKey().public_key_xonly.format().hex()
NPUB = hex_to_npub(AUTHOR)


def test_a_message_names_an_nsite():
    assert website.parse_ref(f"new version: nsite:{NPUB}/v2 please") == (AUTHOR, "v2")
    assert website.parse_ref(f"see https://preview.belfry.test/p/{NPUB}@draft") == (AUTHOR, "draft")
    assert website.parse_ref(f"https://preview.belfry.test/p/{NPUB}") == (AUTHOR, "")
    assert website.parse_ref(f"https://{NPUB}.nsite.lol/") == (AUTHOR, "")
    assert website.parse_ref("no site here https://example.org") is None


def test_previews_pick_a_site_and_serve_its_paths(rsvc, monkeypatch):  # noqa: F811
    seen = {}

    class FakeSite:
        async def nsite(self, request, path):
            seen["path"] = path
            return Response(f"page {path}", media_type="text/html")

    monkeypatch.setattr(
        website, "site_of", lambda svc, pk, name: (seen.update(pk=pk, name=name), FakeSite())[1]
    )
    rsvc.settings.nip05_domain = "belfry.test"
    with TestClient(create_app(rsvc)) as c:
        host = {"Host": "preview.belfry.test"}
        assert c.get("/style.css", headers=host).status_code == 404  # no site chosen yet
        r = c.get(f"/p/{NPUB}@draft", headers=host, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/"
        c.cookies.set(website.PREVIEW_COOKIE, f"{NPUB}@draft")
        r = c.get("/style.css", headers=host)
        assert r.text == "page /style.css" and seen == {
            "pk": AUTHOR,
            "name": "draft",
            "path": "/style.css",
        }
        assert r.headers["x-robots-tag"] == "noindex"


def test_only_admins_publish_and_every_update_is_logged(rsvc, monkeypatch):  # noqa: F811
    rsvc.settings.public_url = BASE
    rsvc.settings.nip05_domain = "belfry.test"
    rsvc.settings.homepage = "nsite"
    rsvc.settings.belfry_nsec = "1" * 64
    key, _ = admin_with_key(rsvc)  # a member; admin only once in the stewards' e-mails
    published, logged = {}, []
    monkeypatch.setattr(
        website, "files_of", lambda *a: _async(({"/index.html": b"<h1>v2</h1>"}, ""))
    )
    monkeypatch.setattr(
        homepage,
        "publish",
        lambda settings, files, title, by, note: (
            published.update(files=files, by=by, note=note) or {"manifest": "m1"}
        ),
    )
    monkeypatch.setattr(website, "changelog", lambda svc, text: logged.append(text))
    from belfry_onboarding import console

    admins: set[str] = set()
    monkeypatch.setattr(console, "admin_emails", lambda svc: admins)
    body = f'{{"ref": "Here: nsite:{NPUB}"}}'.encode()
    url = BASE + "/api/website/publish"
    with TestClient(create_app(rsvc)) as c:

        def h():
            return {
                "Authorization": nip98(key, url, "POST", body),
                "Content-Type": "application/json",
            }

        assert c.post("/api/website/publish", content=body, headers=h()).status_code == 403
        admins.add("admin@belfry.test")
        time.sleep(1.1)
        r = c.post("/api/website/publish", content=body, headers=h())
        assert r.status_code == 200, r.text
        assert (
            published["files"] == {"/index.html": b"<h1>v2</h1>"}
            and "Ada Admin" in published["note"]
        )
        assert logged and "Website updated" in logged[-1] and "Ada Admin" in logged[-1]
        assert f"https://preview.belfry.test/p/{NPUB}" in logged[-1]
        time.sleep(1.1)
        bad = b'{"ref": "nothing to publish"}'
        assert (
            c.post(
                "/api/website/publish",
                content=bad,
                headers={
                    "Authorization": nip98(key, url, "POST", bad),
                    "Content-Type": "application/json",
                },
            ).status_code
            == 422
        )


def test_the_channel_says_how_to_propose(rsvc):  # noqa: F811
    rsvc.settings.nip05_domain = "belfry.test"
    text = website.about(rsvc)
    assert (
        "https://belfry.test" in text
        and "https://preview.belfry.test/p/" in text
        and "agent" in text
    )


async def _async(v):
    return v
