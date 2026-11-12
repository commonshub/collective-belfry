"""POST /api/whois: member, agent or guest per key; belfry.json revalidates (ETag)."""

# ruff: noqa: F811
from __future__ import annotations

from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding.app import create_app

from .test_agents import guest_agent, member, pk, world  # noqa: F401 — fixtures
from .test_guests import admin_with_key, published, rsvc  # noqa: F401 — fixtures
from .test_holder_invites import post


def test_whois_tells_members_guests_and_agents_apart(world):
    rsvc, _published, _left = world
    ada, ada_id = member(rsvc, "ada@belfry.test", "Ada")
    stranger = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        bot = guest_agent(c, rsvc, ada, ada_id)
        keys = [pk(ada), pk(bot), pk(stranger)]
        r = post(c, ada, "/api/whois", {"pubkeys": keys})
        assert r.status_code == 200, r.text
        assert r.json()["people"] == {pk(ada): "member", pk(bot): "guest"}
        # roles apart (roles.py): members only, "people" keeps its three values
        assert r.json()["roles"] == {pk(ada): "member"}
        # the guest may ask too; a key with no pass may not
        assert post(c, bot, "/api/whois", {"pubkeys": [pk(ada)]}).status_code == 200
        assert post(c, stranger, "/api/whois", {"pubkeys": [pk(ada)]}).status_code == 403
        assert post(c, ada, "/api/whois", {"pubkeys": ["nope"]}).status_code == 400
        # Ada makes it her own agent: an agent now, not a guest
        assert post(c, ada, "/api/agents", {"pubkey": pk(bot), "mine": True}).status_code == 200
        r = post(c, ada, "/api/whois", {"pubkeys": [pk(bot)]})
        assert r.json() == {"people": {pk(bot): "agent"}, "roles": {}}


def test_belfry_json_revalidates(world):
    rsvc, _published, _left = world
    with TestClient(create_app(rsvc)) as c:
        r = c.get("/.well-known/belfry.json")
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
        etag = r.headers["etag"]
        again = c.get("/.well-known/belfry.json", headers={"If-None-Match": etag})
        assert again.status_code == 304 and again.headers["etag"] == etag
