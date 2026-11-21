"""The lines the belfry posts in #website (website.py, "the messages in #website"): a short first
line, screenshots (URLs + NIP-92 imeta), ``---``, then the details with the short preview link
preview.<domain>/v/<code>; screenshots and a summary from the API, a proposal or a button."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from belfry_onboarding import website, webthreads
from belfry_onboarding.app import create_app
from belfry_onboarding.nostr import hex_to_npub

from .test_actions import PUBLISH, Relay, make, message, press
from .test_actions import world as aworld  # noqa: F401 — fixture
from .test_guests import admin_with_key, rsvc  # noqa: F401 — fixtures
from .test_holder_invites import BASE, nip98
from .test_website_paths import AUTHOR, Main, notes, passed, props, pub, version  # noqa: F401 — fixtures

SHA = "9" * 64
SHA2 = "8" * 64


def svc_(dom="b.test"):
    webthreads._pending_mem.clear()
    return SimpleNamespace(settings=SimpleNamespace(nip05_domain=dom), odoo=SimpleNamespace())


# ── short preview links ─────────────────────────────────────────────────────────────────────
def test_a_short_link_is_made_once_per_site_and_read_back():
    svc = svc_()
    code = website.short_code(svc, AUTHOR, "website")
    assert len(code) == 8 and set(code) <= set(website.CODE_ALPHABET)
    assert website.short_code(svc, AUTHOR, "website") == code  # reused
    other = website.short_code(svc, AUTHOR, "")
    assert other != code
    assert website.resolve_code(svc, code) == (AUTHOR, "website")
    assert website.resolve_code(svc, "zzzzzzzz") is None and website.resolve_code(svc, "x") is None
    assert website.preview_url(svc, AUTHOR, "website") == f"https://preview.b.test/v/{code}"
    assert json.loads(webthreads._pending_mem[website.PREVIEW_LINKS_PARAM]) == {
        code: [AUTHOR, "website"],
        other: [AUTHOR, ""],
    }
    # parse_ref reads it, on this belfry's preview host only
    assert website.parse_ref(f"look https://preview.b.test/v/{code} ok", "b.test", svc) == (
        AUTHOR,
        "website",
    )
    assert website.parse_ref(f"https://preview.b.test/v/{code}/ios/", "b.test", svc) == (
        AUTHOR,
        "website",
    )
    assert website.parse_ref(f"https://preview.evil.test/v/{code}", "b.test", svc) is None
    assert website.parse_ref("https://preview.b.test/v/aaaaaaaa", "b.test", svc) is None
    assert website.parse_ref(f"https://preview.b.test/v/{code}", "b.test") is None  # no svc
    # the long links still work, and win when both are there
    npub = hex_to_npub("c" * 64)
    assert website.parse_ref(
        f"https://preview.b.test/v/{code} https://preview.b.test/p/{npub}@x", "b.test", svc
    ) == ("c" * 64, "x")


def test_the_preview_host_serves_a_short_link_like_the_long_one(rsvc, monkeypatch):  # noqa: F811
    rsvc.settings.nip05_domain = "belfry.test"
    code = website.short_code(rsvc, AUTHOR, "shots")
    with TestClient(create_app(rsvc)) as c:
        host = {"Host": "preview.belfry.test"}
        r = c.get(f"/v/{code}", headers=host, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/"
        cookie = r.headers["set-cookie"]  # the same cookie as /p/<npub>@shots sets
        assert cookie.startswith(f'{website.PREVIEW_COOKIE}="{hex_to_npub(AUTHOR)}@shots"')
        r = c.get(f"/v/{code}/ios/screenshots/", headers=host, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/ios/screenshots/"
        # an unknown code: the same 404 as no site at all
        nothing = c.get("/style.css", headers=host)
        r = c.get("/v/aaaaaaaa", headers=host, follow_redirects=False)
        assert r.status_code == 404 and r.text == nothing.text and "set-cookie" not in r.headers


# ── the "Website updated" line ──────────────────────────────────────────────────────────────
def test_the_update_line_is_short_then_screenshots_then_folded_details(pub, monkeypatch):  # noqa: F811
    svc, state = pub
    tags_seen = []
    monkeypatch.setattr(
        website,
        "changelog",
        lambda s, text, tags=None: (state["logged"].append(text), tags_seen.append(tags)),
    )
    out = asyncio.run(
        website.publish_from(
            svc,
            AUTHOR,
            "website",
            "Ada",
            ["/ios/screenshots/"],
            summary="  New onboarding screens  ",
            screenshots=[
                {
                    "url": f"https://blossom.b.test/{SHA}.png",
                    "alt": "/ios/ on a phone",
                    "dim": "390x844",
                },
                {"url": "/ios/screenshots/a.png"},  # a file of the version: its Blossom URL
            ],
        )
    )
    code = website.short_code(svc, AUTHOR, "website")
    a_sha = website.hashlib.sha256(b"new a").hexdigest()
    assert state["logged"][-1] == (
        "**Website updated** by Ada: New onboarding screens\n"
        f"https://blossom.b.test/{SHA}.png\n"
        f"https://blossom.b.test/{a_sha}.png\n"
        "---\n"
        "2 files: 1 added, 1 changed, 1 removed, only /ios/screenshots/. Version by Bo.\n"
        f"Live: https://b.test · Preview: https://preview.b.test/v/{code}"
    )
    assert tags_seen[-1] == [
        [
            "imeta",
            f"url https://blossom.b.test/{SHA}.png",
            "m image/png",
            "dim 390x844",
            "alt /ios/ on a phone",
        ],
        [
            "imeta",
            f"url https://blossom.b.test/{a_sha}.png",
            "m image/png",
            "alt Screenshot of the website",
        ],
    ]
    assert out["summary"] == "New onboarding screens" and len(out["screenshots"]) == 2
    assert out["preview"].endswith(f"/v/{code}") and "/p/npub1" in out["preview_long"]


@pytest.mark.parametrize(
    "shots",
    [
        [{"url": "https://evil.test/" + SHA + ".png"}],
        [{"url": f"http://blossom.b.test/{SHA}.png"}],
        [{"url": f"https://blossom.b.test/{SHA}.exe"}],
        [{"url": f"https://blossom.b.test/{SHA}.png", "m": "text/html"}],
        [{"url": f"https://blossom.b.test/{SHA}.png", "dim": "big"}],
        [{"url": f"https://blossom.b.test/{SHA}.png", "alt": "two\nlines"}],
        [{"url": f"https://blossom.b.test/{SHA}.png", "alt": "x" * 201}],
        [{"url": f"https://blossom.b.test/{SHA}.png", "onclick": "x"}],
        [{"url": "/ios/../secret.png"}],
        [{"url": "/ios/screenshots/"}],
        [f"https://blossom.b.test/{SHA}.png"],
        {"url": f"https://blossom.b.test/{SHA}.png"},
        [{"url": f"https://blossom.b.test/{SHA}.png"}] * 7,
    ],
)
def test_bad_screenshots_are_refused_before_anything_is_copied(pub, shots):  # noqa: F811
    svc, state = pub
    with pytest.raises(website.ScreenshotsError):
        asyncio.run(website.publish_from(svc, AUTHOR, "website", "Ada", screenshots=shots))
    assert state["proposal"].fetched == [] and state["published"] == {}


def test_a_screenshot_path_must_be_a_file_of_the_website(pub):  # noqa: F811
    svc, state = pub
    with pytest.raises(website.ScreenshotsError):
        asyncio.run(
            website.publish_from(
                svc,
                AUTHOR,
                "website",
                "Ada",
                ["/ios/screenshots/"],
                screenshots=[{"url": "/ios/screenshots/nope.png"}],
            )
        )
    assert state["published"] == {}


@pytest.mark.parametrize("summary", ["x" * 201, "one\ntwo", 5])
def test_a_summary_is_one_short_line(pub, summary):  # noqa: F811
    svc, state = pub
    with pytest.raises(website.SummaryError):
        asyncio.run(website.publish_from(svc, AUTHOR, "website", "Ada", summary=summary))
    assert state["published"] == {}
    assert website.clean_summary("x" * 200) == "x" * 200 and website.clean_summary("  ") is None


def test_screenshots_and_summary_come_from_the_proposal_or_message(pub):  # noqa: F811
    svc, state = pub
    source = {
        "kind": 30023,
        "content": f"See https://blossom.b.test/{SHA2}.webp and https://evil.test/{SHA}.png",
        "tags": [
            ["summary", "Fresh screenshots"],
            ["imeta", f"url https://blossom.b.test/{SHA}.png", "m image/png", "alt phone"],
            ["imeta", f"url https://evil.test/{SHA}.png"],  # not this belfry's: left out
            *[["imeta", f"url https://blossom.b.test/{i:064x}.jpg"] for i in range(1, 9)],
        ],
    }
    out = asyncio.run(
        website.publish_from(svc, AUTHOR, "website", "Ada", ["/ios/screenshots/"], source=source)
    )
    assert out["summary"] == "Fresh screenshots"
    assert len(out["screenshots"]) == website.MAX_SHOTS
    assert out["screenshots"][0] == f"https://blossom.b.test/{SHA}.png"
    assert all("evil" not in u for u in out["screenshots"])
    assert state["logged"][-1].startswith("**Website updated** by Ada: Fresh screenshots\n")
    # given ones win; an empty list means none
    out = asyncio.run(
        website.publish_from(
            svc,
            AUTHOR,
            "website",
            "Ada",
            ["/ios/screenshots/"],
            summary="Mine",
            screenshots=[],
            source=source,
        )
    )
    assert out["summary"] == "Mine" and out["screenshots"] == []
    # content URLs count when there is no imeta; a too long summary tag is ignored
    only_text = {"content": f"https://blossom.b.test/{SHA2}.webp", "tags": [["summary", "y" * 300]]}
    out = asyncio.run(
        website.publish_from(svc, AUTHOR, "website", "Ada", ["/ios/screenshots/"], source=only_text)
    )
    assert out["screenshots"] == [f"https://blossom.b.test/{SHA2}.webp"] and out["summary"] == ""
    assert state["logged"][-1].startswith("**Website updated** by Ada\n")


# ── the note before approval, and the sources passed on ─────────────────────────────────────
def test_the_note_before_approval_has_the_same_shape(props):  # noqa: F811
    svc, g, state = props
    v = version(paths=["/ios/screenshots/"])
    v["tags"] += [
        ["summary", "New onboarding screens"],
        ["imeta", f"url https://blossom.belfry.test/{SHA}.png", "m image/png", "alt phone"],
    ]
    Main.versions = [v]
    assert asyncio.run(website.announce(svc)) == 1
    code = website.short_code(svc, AUTHOR, "")
    assert g.sent[-1]["content"] == (
        "**This changes only /ios/screenshots/**: New onboarding screens\n"
        f"https://blossom.belfry.test/{SHA}.png\n"
        "---\n"
        "This changes only /ios/screenshots/: 1 file added, 0 changed, 0 removed. "
        "The rest of the website stays as it is.\n"
        f"Preview: https://preview.belfry.test/v/{code}"
    )
    assert [
        "imeta",
        f"url https://blossom.belfry.test/{SHA}.png",
        "m image/png",
        "alt phone",
    ] in g.sent[-1]["tags"]
    assert notes(g)[-1].startswith("This changes only /ios/screenshots/: 1 file added")


def test_a_passed_proposal_hands_its_version_to_publish(props, monkeypatch):  # noqa: F811
    svc, g, state = props
    Main.versions = [version(at=int(time.time()) - 10)]
    g.labels = [passed()]
    got = []

    async def publish(s, pk, name, admin, scope=None, **k):
        got.append(k)
        return {"files": 1}

    monkeypatch.setattr(website, "publish_from", publish)
    assert asyncio.run(website.watch(svc)) == 1
    assert got[0]["source"]["id"] == "v1"


def test_the_button_hands_its_message_to_publish(aworld, monkeypatch):  # noqa: F811
    svc, _ = aworld
    got = []

    async def publish(s, pk, name, admin, paths=None, **k):
        got.append(k)
        return {"files": 1}

    monkeypatch.setattr(website, "publish_from", publish)
    msg = message(tags=[["a", f"35128:{'c' * 64}:website"], ["summary", "S"], PUBLISH])
    out = asyncio.run(make(svc, Relay([msg])).handle(press()))
    assert out.status == "ok"
    assert got[0]["source"]["id"] == msg["id"]


# ── the API ─────────────────────────────────────────────────────────────────────────────────
def test_the_api_takes_screenshots_and_a_summary(rsvc, monkeypatch):  # noqa: F811
    from belfry_onboarding import console

    rsvc.settings.public_url = BASE
    rsvc.settings.nip05_domain = "belfry.test"
    key, _ = admin_with_key(rsvc)
    monkeypatch.setattr(console, "admin_emails", lambda svc: {"admin@belfry.test"})
    calls = []

    async def publish_from(s, pk, name, admin, paths=None, **k):
        calls.append(k)
        return {"files": 1, "paths": paths or []}

    monkeypatch.setattr(website, "publish_from", publish_from)
    npub = hex_to_npub("c" * 64)
    code = website.short_code(rsvc, "d" * 64, "website")
    url = BASE + "/api/website/publish"
    with TestClient(create_app(rsvc)) as c:

        def post(data: dict):
            body = json.dumps(data).encode()
            time.sleep(1.1)  # NIP-98: one request a second per key
            return c.post(
                "/api/website/publish",
                content=body,
                headers={
                    "Authorization": nip98(key, url, "POST", body),
                    "Content-Type": "application/json",
                },
            )

        shot = {"url": f"https://blossom.belfry.test/{SHA}.png", "alt": "home", "dim": "1280x800"}
        r = post({"ref": f"nsite:{npub}", "summary": "New home", "screenshots": [shot]})
        assert r.status_code == 200, r.text
        assert calls[-1]["summary"] == "New home"
        assert calls[-1]["screenshots"] == [{**shot, "m": "image/png"}]
        r = post({"ref": f"nsite:{npub}", "screenshots": [{"url": "https://evil.test/x.png"}]})
        assert r.status_code == 422 and r.json()["error"] == "invalid_screenshots"
        r = post({"ref": f"nsite:{npub}", "summary": "x" * 201})
        assert r.status_code == 422 and r.json()["error"] == "invalid_summary"
        assert len(calls) == 1
        # the short link names a site too
        r = post({"ref": f"https://preview.belfry.test/v/{code}"})
        assert r.status_code == 200 and len(calls) == 2


def test_without_a_summary_the_title_is_used_unless_it_is_a_default(pub):  # noqa: F811
    svc, state = pub
    svc.settings.community_name = "Example Commons"

    def summary(tags):
        return asyncio.run(
            website.publish_from(
                svc, AUTHOR, "website", "Ada", ["/ios/screenshots/"], source={"tags": tags}
            )
        )["summary"]

    assert summary([["title", "Website: /ios/screenshots/ — new onboarding screens"]]) == (
        "/ios/screenshots/ — new onboarding screens"
    )
    assert summary([["title", "New screens"], ["summary", "Better"]]) == "Better"
    assert summary([["subject", "New website: a fresh home page"]]) == "a fresh home page"
    for default in ("", "Website", "website", "Example Commons", "Proposal", "Website: "):
        assert summary([["title", default]]) == "", default
    assert summary([["title", "x" * 201]]) == ""  # too long: no summary, never cut
    assert summary([["title", "two\nlines"]]) == ""
    assert state["logged"][-1].startswith("**Website updated** by Ada\n---\n")
