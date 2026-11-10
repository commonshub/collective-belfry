"""A member's own agents (agents.py): a guest agent made permanent, a link for a new one, the
limit, and the agent leaving with its member."""

# ruff: noqa: F811
from __future__ import annotations

import time

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import agents, apps, core, mail, steward
from belfry_onboarding.app import create_app

from .test_guests import admin_with_key, published, rsvc  # noqa: F401 — fixtures
from .test_holder_invites import BASE, nip98, post


def pk(key: PrivateKey) -> str:
    return key.public_key_xonly.format().hex()


@pytest.fixture
def world(rsvc, published, monkeypatch):  # noqa: F811
    rsvc.settings.public_url = BASE
    monkeypatch.setattr(mail, "channel_exists", lambda svc, ch: True)
    monkeypatch.setattr(apps, "admin_pubkeys", lambda svc: set())
    left: list[str] = []
    monkeypatch.setattr(agents, "out_of_channels", lambda svc, key: left.append(key) or 0)
    # the members of these tests have no account in the (fake) signer to suspend
    monkeypatch.setattr(core, "_keycast_pubkey_for", lambda svc, row: None)
    return rsvc, published, left


def member(rsvc, email: str, name: str) -> tuple[PrivateKey, int]:
    made = steward.steward_pass(rsvc, email, name)
    key = PrivateKey()
    rsvc.odoo.write(
        "belfry.pass", [made["pass_id"]], {"keycast_pubkey": pk(key), "state": "active"}
    )
    row = rsvc.odoo.read_one("belfry.pass", made["pass_id"], ["partner_id"])
    return key, core.m2o_id(row["partner_id"])


def guest_agent(c, rsvc, inviter: PrivateKey, owner: int, channel="garden") -> PrivateKey:
    row = rsvc.odoo.create_guest_invite(owner, pk(inviter), channel, 30)
    token = row["url"].rsplit("/", 1)[1]
    bot = PrivateKey()
    r = post(c, bot, f"/api/guest-invites/{token}/accept", {"name": "Bot"})
    assert r.status_code == 200, r.text
    return bot


def test_a_guest_agent_becomes_its_members_own_and_leaves_with_them(world):
    rsvc, published, left = world
    ada, ada_id = member(rsvc, "ada@belfry.test", "Ada")
    bob, _ = member(rsvc, "bob@belfry.test", "Bob")
    with TestClient(create_app(rsvc)) as c:
        bot = guest_agent(c, rsvc, ada, ada_id)
        assert pk(bot) not in core.active_pubkeys(rsvc)
        assert post(c, ada, "/api/agents", {}).json() == {"agents": [], "limit": 1}
        # not Bob's to take: Ada invited it
        r = post(c, bob, "/api/agents", {"pubkey": pk(bot), "mine": True})
        assert r.status_code == 409 and "invited" in r.json()["message"]
        r = post(c, ada, "/api/agents", {"pubkey": pk(bot), "mine": True})
        assert r.status_code == 200, r.text
        assert [a["pubkey"] for a in r.json()["agents"]] == [pk(bot)]
        # a member now: on the relay, no guest pass left, told to the channel, still in it
        assert pk(bot) in core.active_pubkeys(rsvc)
        tiers = [
            p["tier"]
            for p in rsvc.odoo.search_read(
                "belfry.pass",
                [["keycast_pubkey", "=", pk(bot)], ["state", "=", "active"]],
                ["tier"],
            )
        ]
        assert tiers == ["agent"]
        assert any(k == 1985 and ["l", "member", "belfry/guest"] in t for k, t in published)
        assert not any(k == 9001 for k, t in published)
        assert rsvc.odoo.agent_owners() == {pk(bot): pk(ada)}
        assert c.get("/.well-known/belfry.json").json()["agents"] == {pk(bot): pk(ada)}
        # one agent each: a second one is refused
        time.sleep(1.1)
        bot2 = guest_agent(c, rsvc, ada, ada_id, "kitchen")
        r = post(c, ada, "/api/agents", {"pubkey": pk(bot2), "mine": True})
        assert r.status_code == 409 and r.json()["error"] == "limit"
        # an agent can't invite, and can't have agents
        r = post(c, bot, "/api/channel-invites", {"channel": "garden"})
        assert r.status_code == 403 and r.json()["error"] == "agent"
        assert post(c, bot, "/api/agents", {}).status_code == 403
    # Ada's membership closes: her agent goes too, out of its channels
    mine = rsvc.odoo.search_read(
        "belfry.pass", [["keycast_pubkey", "=", pk(ada)], ["state", "=", "active"]], ["id"]
    )
    core.revoke_pass(rsvc, mine[0]["id"], "test")
    assert pk(bot) not in core.active_pubkeys(rsvc)
    assert pk(bot) in left and pk(ada) in left
    assert rsvc.odoo.agent_owners() == {}


def test_a_link_for_a_members_own_agent(world):
    rsvc, published, left = world
    rsvc.settings.console_url = "https://belfry.belfry.test"
    ada, ada_id = member(rsvc, "ada@belfry.test", "Ada")
    with TestClient(create_app(rsvc)) as c:
        r = post(c, ada, "/api/channel-invites", {"channel": "garden", "agent": True})
        assert r.status_code == 200, r.text
        assert r.json()["own_agent"] is True and r.json()["people"] == 1
        token = r.json()["agent_url"].rsplit("/", 1)[1].removesuffix(".md")
        md = c.get(f"/invite/{token}.md").text
        assert "as their own agent" in md and "guest for" not in md
        # a person can't use it
        person = PrivateKey()
        r = post(c, person, f"/api/channel-invites/{token}/join", {"name": "Pat"})
        assert r.status_code == 410 and r.json()["error"] == "for_agent"
        bot = PrivateKey()
        url = f"https://belfry.belfry.test/api/v1/guest-invites/{token}/accept"
        body = b'{"name": "Ada\'s helper"}'
        r = c.post(
            f"/api/v1/guest-invites/{token}/accept",
            content=body,
            headers={
                "Host": "belfry.belfry.test",
                "Authorization": nip98(bot, url, "POST", body),
                "Content-Type": "application/json",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["member"] is True and r.json()["until"] is None
        assert pk(bot) in core.active_pubkeys(rsvc)
        joined = [t for k, t in published if k == 9000][-1]
        assert ["p", pk(bot), "member"] in joined and ["agent", "1"] in joined
        assert not any(t[:1] == ["guest"] for t in joined)
        assert rsvc.odoo.invite_by_token(token)["state"] == "used"
        # no room for a second link
        time.sleep(1.1)
        r = post(c, ada, "/api/channel-invites", {"channel": "garden", "agent": True})
        assert r.status_code == 409
        # Ada removes it
        r = post(c, ada, "/api/agents", {"pubkey": pk(bot), "mine": False})
        assert r.status_code == 200 and r.json()["agents"] == []
        assert pk(bot) not in core.active_pubkeys(rsvc) and pk(bot) in left


def test_admins_set_how_many_agents_a_member_brings(world, monkeypatch):
    rsvc, published, left = world
    ada, ada_id = member(rsvc, "ada@belfry.test", "Ada")
    bob, _ = member(rsvc, "bob@belfry.test", "Bob")
    monkeypatch.setattr(apps, "admin_pubkeys", lambda svc: {pk(ada)})
    with TestClient(create_app(rsvc)) as c:
        assert post(c, bob, "/api/agents", {"limit": 3}).status_code == 403
        time.sleep(1.1)
        assert post(c, ada, "/api/agents", {"limit": 3}).json()["limit"] == 3
        assert post(c, bob, "/api/agents", {}).json()["limit"] == 3
        rsvc.odoo.set_param("belfry.agents_per_tier.member", "5")
        time.sleep(1.1)
        assert post(c, bob, "/api/agents", {}).json()["limit"] == 5


def test_a_guest_of_one_channel_is_brought_into_another(world):
    rsvc, published, left = world
    ada, ada_id = member(rsvc, "ada@belfry.test", "Ada")
    with TestClient(create_app(rsvc)) as c:
        bot = guest_agent(c, rsvc, ada, ada_id, "garden")
        r = post(c, ada, "/api/channel-guests", {"channel": "android", "pubkey": pk(bot)})
        assert r.status_code == 200 and r.json()["added"] is True, r.text
        kind, tags = published[-1]
        assert kind == 9000 and ["h", "android"] in tags and ["agent", "1"] in tags
        assert ["invited-by", pk(ada)] in tags
        time.sleep(1.1)
        r = post(c, ada, "/api/channel-guests", {"channel": "android", "pubkey": pk(bot)})
        assert r.json()["already"] is True
        time.sleep(1.1)
        r = post(c, ada, "/api/channel-guests", {"channel": "android", "pubkey": pk(ada)})
        assert r.json()["member"] is True
        time.sleep(1.1)
        r = post(c, bot, "/api/channel-guests", {"channel": "x", "pubkey": pk(ada)})
        assert r.status_code == 403
