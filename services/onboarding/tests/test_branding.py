"""The belfry's branding (branding.py): admins set the logo, cover and colours, kept in the
kind-0 profile of the belfry's own key, read back by anyone, shown in belfry.json and the mails."""

# ruff: noqa: F811
from __future__ import annotations

import json

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import apps, branding, mailer
from belfry_onboarding.app import create_app

from .test_guests import rsvc  # noqa: F401 — fixture
from .test_holder_invites import BASE, post

BELFRY = PrivateKey()
BELFRY_PK = BELFRY.public_key_xonly.format().hex()


class FakeRelay:
    url = "wss://relay.belfry.test"

    def __init__(self):
        self.events: list[dict] = []

    def query(self, filters, sign):
        f = filters[0]
        return [
            e
            for e in self.events
            if e["kind"] in f.get("kinds", [e["kind"]])
            and e["pubkey"] in f.get("authors", [e["pubkey"]])
        ]

    def publish(self, event, sign):
        self.events.append(event)


@pytest.fixture
def world(rsvc, monkeypatch):
    rsvc.settings.public_url = BASE
    rsvc.settings.belfry_nsec = BELFRY.secret.hex()
    rsvc.settings.community_name = "Belfry"
    relay = FakeRelay()
    relay.events.append(
        {
            "kind": 0,
            "pubkey": BELFRY_PK,
            "created_at": 10,
            "tags": [],
            "content": '{"name":"Belfry","about":"Our place"}',
        }
    )
    rsvc.relay = relay
    ada, bob = PrivateKey(), PrivateKey()
    monkeypatch.setattr(apps, "admin_pubkeys", lambda svc: {ada.public_key_xonly.format().hex()})
    branding._cache.update(at=0.0, profile={}, created_at=0)
    yield rsvc, relay, ada, bob
    branding._cache.update(at=0.0, profile={}, created_at=0)


def test_admins_brand_the_belfry_in_its_own_profile(world):
    svc, relay, ada, bob = world
    with TestClient(create_app(svc)) as c:
        assert c.get("/api/branding").json() == {
            "logo": "",
            "cover": "",
            "primary": "",
            "secondary": "",
            "name": "Belfry",
        }
        want = {
            "logo": "https://blossom.belfry.test/logo.png",
            "cover": "https://blossom.belfry.test/cover.jpg",
            "primary": "#E11D48",
            "secondary": "#0ea5e9",
        }
        assert post(c, bob, "/api/branding", want).status_code == 403  # not an admin
        assert post(c, ada, "/api/branding", {"primary": "red"}).status_code == 400
        assert post(c, ada, "/api/branding", {"logo": "http://x.test/a.png"}).status_code == 400
        r = post(c, ada, "/api/branding", want)
        assert r.status_code == 200, r.text
        # the belfry's key signed a new profile, the rest of it kept
        k0 = [e for e in relay.events if e["kind"] == 0]
        latest = max(k0, key=lambda e: e["created_at"])
        assert latest["pubkey"] == BELFRY_PK and latest["created_at"] > 10
        content = json.loads(latest["content"])
        assert content == {
            "name": "Belfry",
            "about": "Our place",
            "picture": want["logo"],
            "banner": want["cover"],
            "primary_color": "#e11d48",
            "secondary_color": "#0ea5e9",
        }
        got = c.get("/api/branding").json()
        assert got["primary"] == "#e11d48" and got["logo"] == want["logo"]
        assert c.get("/.well-known/belfry.json").json()["branding"]["cover"] == want["cover"]
        # "" clears one
        post(c, ada, "/api/branding", {"cover": ""})
        assert c.get("/api/branding").json()["cover"] == ""
        assert (
            json.loads(max(relay.events, key=lambda e: e["created_at"])["content"]).get("banner")
            is None
        )


def test_mails_carry_the_logo_and_the_primary_colour(world):
    svc, relay, ada, bob = world
    with TestClient(create_app(svc)) as c:
        post(
            c,
            ada,
            "/api/branding",
            {"logo": "https://blossom.belfry.test/logo.png", "primary": "#e11d48"},
        )
    mailer.SENT.clear()
    mailer.Mailer(svc.settings).send(
        "ana@example.org", "Hi", ["Hello"], ("https://x.test/", "Open")
    )
    page = mailer.SENT[-1].get_body(preferencelist=("html",)).get_content()
    assert 'src="https://blossom.belfry.test/logo.png"' in page
    assert "background:#e11d48" in page  # the button
    assert (
        "background:#fafafa" not in page and "border:1px solid" not in page
    )  # no grey page, no frame
