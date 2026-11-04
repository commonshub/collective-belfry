"""/monitor, /changelog and /proposals (community.py)."""

from __future__ import annotations

import json

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import community, mypass
from belfry_onboarding.app import create_app


@pytest.fixture
def belfry(services):
    services.settings.pass_page_oidc_client_secret = "pass-secret"
    services.settings.pass_page_secret = "p" * 32
    services.settings.belfry_nsec = PrivateKey().secret.hex()
    community._cache["data"] = None
    return services


@pytest.fixture
def web(belfry):
    with TestClient(create_app(belfry), base_url="https://testserver") as c:
        yield c


def member(odoo, name="Alice", state="active", custody=None) -> int:
    partner = odoo.add(
        "res.partner", name=name, belfry_member_number=f"M-{name}", belfry_key_custody=custody
    )
    odoo.add("belfry.pass", partner_id=partner, tier="week", state=state)
    return partner


def sign_in(web, services, partner: int) -> None:
    cookie = mypass._sign(services, {"kind": "session", "partner": partner}, 3600)
    web.cookies.set(mypass.SESSION_COOKIE, cookie)


# ── changelog ───────────────────────────────────────────────────────────────────────────────
def test_changelog_lists_changes_with_who_started_them(web, belfry, tmp_path):
    path = tmp_path / "changelog.json"
    path.write_text(
        json.dumps(
            {
                "repo": "https://github.com/commonshub/collective-belfry.git",
                "entries": [
                    {
                        "at": "2026-11-04T11:00:00+00:00",
                        "type": "operation",
                        "summary": "Settings: HOMEPAGE",
                        "by": ["Xavier via Claude"],
                        "proposal": "ab" * 32,
                    },
                    {
                        "at": "2026-11-03T09:00:00+00:00",
                        "type": "code",
                        "summary": "Add <the> homepage",
                        "by": ["Xavier Damman", "Claude Opus 5.5"],
                        "ref": "45ea6ab6a45a",
                    },
                ],
            }
        )
    )
    belfry.settings.changelog_path = str(path)
    r = web.get("/changelog")
    assert r.status_code == 200
    assert "Settings: HOMEPAGE" in r.text and "started by Xavier via Claude" in r.text
    assert "started by Xavier Damman, Claude Opus 5.5" in r.text
    assert "Add &lt;the&gt; homepage" in r.text
    assert 'href="https://github.com/commonshub/collective-belfry/commit/45ea6ab6a45a"' in r.text
    assert f'href="/proposals/{"ab" * 32}"' in r.text
    assert web.get("/changelog.json").json()["entries"][0]["summary"] == "Settings: HOMEPAGE"


def test_changelog_without_a_file(web, belfry, tmp_path):
    belfry.settings.changelog_path = str(tmp_path / "missing.json")
    assert "No changelog yet" in web.get("/changelog").text


# ── members only ────────────────────────────────────────────────────────────────────────────
def test_monitor_and_proposals_are_for_members(web, belfry, odoo):
    for path in ("/monitor", "/proposals"):
        r = web.get(path)
        assert r.status_code == 200 and 'href="/me/login"' in r.text
    assert web.get("/monitor/stream").status_code == 401
    assert web.get("/monitor/names?p=" + "a" * 64).status_code == 401
    # a closed pass is not enough
    sign_in(web, belfry, member(odoo, "Bob", state="revoked"))
    assert 'href="/me/login"' in web.get("/monitor").text


def test_monitor_page_for_a_member(web, belfry, odoo):
    sign_in(web, belfry, member(odoo))
    r = web.get("/monitor")
    assert r.status_code == 200 and "EventSource('/monitor/stream" in r.text


def test_monitor_names_from_profiles(web, belfry, odoo, relay):
    sign_in(web, belfry, member(odoo))
    pk = "c" * 64
    relay.events.append(
        {
            "id": "1" * 64,
            "pubkey": pk,
            "kind": 0,
            "created_at": 5,
            "tags": [],
            "content": json.dumps({"name": "Carol"}),
            "sig": "x",
        }
    )
    names = web.get(f"/monitor/names?p={pk},{'d' * 64},notakey").json()
    assert names[pk] == "Carol" and names["d" * 64].startswith("npub1")


# ── proposals ───────────────────────────────────────────────────────────────────────────────
def test_a_member_proposes_supports_and_comments(web, belfry, odoo, relay, keycast):
    alice = member(odoo)
    sign_in(web, belfry, alice)
    r = web.post(
        "/proposals",
        data={"title": "A tool library", "body": "Share drills."},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/proposals?msg=sent"
    note = relay.events[-1]
    belfry_pk = community.belfry_pubkey(belfry.settings)
    assert (
        note["kind"] == 1 and ["t", "proposal"] in note["tags"] and ["p", belfry_pk] in note["tags"]
    )
    assert ["subject", "A tool library"] in note["tags"] and "Share drills." in note["content"]
    assert keycast.preload_bodies[-1]["vine_id"] == f"odoo-partner-{alice}"

    page = web.get("/proposals").text
    assert "A tool library" in page and "Your proposal is published." not in page
    assert f'href="/proposals/{note["id"]}"' in page

    assert web.post(f"/proposals/{note['id']}/support", follow_redirects=False).status_code == 303
    reaction = relay.events[-1]
    assert reaction["kind"] == 7 and reaction["content"] == "+"
    assert ["e", note["id"]] in reaction["tags"] and ["p", note["pubkey"]] in reaction["tags"]

    web.post(f"/proposals/{note['id']}/comment", data={"body": "I have a saw."})
    reply = relay.events[-1]
    assert reply["kind"] == 1 and ["e", note["id"], "", "root"] in reply["tags"]

    detail = web.get(f"/proposals/{note['id']}").text
    assert "Share drills." in detail and "I have a saw." in detail
    assert "1 support:" in detail and "You support this" in detail
    # a comment is not a proposal of its own
    assert web.get("/proposals").text.count('class="item"') == 1


def test_a_member_with_their_own_key_is_told_how_to_post(web, belfry, odoo, relay):
    sign_in(web, belfry, member(odoo, custody="self"))
    page = web.get("/proposals").text
    assert "You hold your own key" in page and 'action="/proposals"' not in page
    before = len(relay.events)
    web.post("/proposals", data={"title": "x"})
    assert len(relay.events) == before


def test_proposals_refuse_other_origins(web, belfry, odoo, relay):
    sign_in(web, belfry, member(odoo))
    before = len(relay.events)
    web.post("/proposals", data={"title": "x"}, headers={"Origin": "https://evil.example"})
    assert len(relay.events) == before


def test_unknown_proposal(web, belfry, odoo):
    sign_in(web, belfry, member(odoo))
    assert web.get("/proposals/" + "f" * 64).status_code == 404


def test_monitor_stream_answers_auth_and_forwards_events(belfry, monkeypatch):
    import asyncio

    import websockets.asyncio.client as wsclient

    sent: list = []

    class FakeWS:
        def __init__(self):
            self.inbox = [json.dumps(["AUTH", "challenge-1"])]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send(self, raw):
            msg = json.loads(raw)
            sent.append(msg)
            if msg[0] == "AUTH":
                self.inbox.append(json.dumps(["OK", msg[1]["id"], True, ""]))
            elif msg[0] == "REQ" and any(m[0] == "AUTH" for m in sent):
                event = {
                    "id": "e" * 64,
                    "pubkey": "a" * 64,
                    "kind": 1,
                    "created_at": 1,
                    "tags": [],
                    "content": "hi",
                    "sig": "s",
                }
                self.inbox.append(json.dumps(["EVENT", "monitor", event]))

        async def recv(self):
            while not self.inbox:
                await asyncio.sleep(0.01)
            return self.inbox.pop(0)

    monkeypatch.setattr(wsclient, "connect", lambda url, **kw: FakeWS())
    belfry.settings.relay_url = "ws://relay:8080"

    class Req:
        async def is_disconnected(self):
            return False

    async def first_event():
        gen = community.relay_stream(belfry, {"limit": 50}, Req())
        async for chunk in gen:
            if chunk.startswith("data: "):
                await gen.aclose()
                return json.loads(chunk[6:])

    event = asyncio.run(first_event())
    assert event["content"] == "hi"
    auth = next(m for m in sent if m[0] == "AUTH")[1]
    assert auth["kind"] == 22242 and auth["pubkey"] == community.belfry_pubkey(belfry.settings)
    assert ["challenge", "challenge-1"] in auth["tags"]
