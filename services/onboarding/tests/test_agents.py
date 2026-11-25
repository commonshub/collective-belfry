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
        assert post(c, ada, "/api/agents", {}).json() == {"agents": [], "limit": 5}
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
        # with a limit of one, a second one is refused (five unless an admin sets it)
        rsvc.odoo.set_param("belfry.agents_per_member", "1")
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
        # no room for a second link with a limit of one
        rsvc.odoo.set_param("belfry.agents_per_member", "1")
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
        rsvc.odoo.set_param("belfry.agents_per_tier.member", "7")
        time.sleep(1.1)
        assert post(c, bob, "/api/agents", {}).json()["limit"] == 7
        # an admin has none: the answer says so, and `limit` stays the members' number
        time.sleep(1.1)
        mine = post(c, ada, "/api/agents", {}).json()
        assert mine["unlimited"] is True and mine["limit"] == 7


def test_a_member_brings_several_agents_and_names_them(world):
    """"I should be able to add more than one agent and I should be able to edit their name"
    (owner, 2026-11-25): five by default; the member's name for an agent, apart from its own
    profile, in the list, belfry.json and whois."""
    rsvc, published, left = world
    ada, ada_id = member(rsvc, "ada@belfry.test", "Ada")
    bob, _ = member(rsvc, "bob@belfry.test", "Bob")
    with TestClient(create_app(rsvc)) as c:
        bots = []
        for ch in ("garden", "kitchen", "cellar"):
            bot = guest_agent(c, rsvc, ada, ada_id, ch)
            time.sleep(1.1)
            r = post(c, ada, "/api/agents", {"pubkey": pk(bot), "mine": True})
            assert r.status_code == 200, r.text
            bots.append(bot)
        listed = post(c, ada, "/api/agents", {}).json()
        assert listed["limit"] == 5 and len(listed["agents"]) == 3
        first = next(a for a in listed["agents"] if a["pubkey"] == pk(bots[0]))
        assert first["name"] == "Bot" and first["profile_name"] == "Bot"
        assert first["given_name"] is None
        assert "agent_names" not in c.get("/.well-known/belfry.json").json()
        # Ada names one
        time.sleep(1.1)
        r = post(c, ada, "/api/agents", {"pubkey": pk(bots[0]), "name": "  Gardener  "})
        assert r.status_code == 200, r.text
        assert r.json() == {"pubkey": pk(bots[0]), "given_name": "Gardener", "name": "Gardener"}
        time.sleep(1.1)
        first = next(
            a
            for a in post(c, ada, "/api/agents", {}).json()["agents"]
            if a["pubkey"] == pk(bots[0])
        )
        assert first["name"] == "Gardener" and first["given_name"] == "Gardener"
        assert first["profile_name"] == "Bot"
        doc = c.get("/.well-known/belfry.json").json()
        assert doc["agent_names"] == {pk(bots[0]): "Gardener"}
        assert doc["agents"] == {pk(b): pk(ada) for b in bots}  # unchanged
        time.sleep(1.1)
        r = post(c, bob, "/api/whois", {"pubkeys": [pk(b) for b in bots] + [pk(ada)]})
        assert r.json()["agent_names"] == {pk(bots[0]): "Gardener"}
        time.sleep(1.1)
        r = post(c, bob, "/api/whois", {"directory": True})
        assert r.json()["agent_names"] == {pk(bots[0]): "Gardener"}
        # not Bob's agent; not an agent; nobody's key; bad names
        time.sleep(1.1)
        r = post(c, bob, "/api/agents", {"pubkey": pk(bots[1]), "name": "Mine now"})
        assert r.status_code == 403 and r.json()["error"] == "not_owner"
        r = post(c, ada, "/api/agents", {"pubkey": pk(bob), "name": "Bob"})
        assert r.status_code == 409 and r.json()["error"] == "not_agent"
        r = post(c, ada, "/api/agents", {"pubkey": "1" * 64, "name": "X"})
        assert r.status_code == 404
        for bad in ("x" * 61, "a\nb", "a\u202eb", 7, None):
            time.sleep(1.1)
            r = post(c, ada, "/api/agents", {"pubkey": pk(bots[1]), "name": bad})
            assert r.status_code == 422 and r.json()["error"] == "invalid_name", bad
        time.sleep(1.1)
        r = post(c, ada, "/api/agents", {"pubkey": pk(bots[1]), "name": "x" * 60})
        assert r.status_code == 200
        # clearing: back to its profile name
        time.sleep(1.1)
        r = post(c, ada, "/api/agents", {"pubkey": pk(bots[0]), "name": " "})
        assert r.json() == {"pubkey": pk(bots[0]), "given_name": None, "name": "Bot"}
        assert c.get("/.well-known/belfry.json").json()["agent_names"] == {pk(bots[1]): "x" * 60}
        # removing an agent forgets its name
        time.sleep(1.1)
        r = post(c, ada, "/api/agents", {"pubkey": pk(bots[1]), "mine": False})
        assert r.status_code == 200
        assert "agent_names" not in c.get("/.well-known/belfry.json").json()
        assert rsvc.odoo.param(agents.NAME_PARAM + pk(bots[1])) == ""


def test_a_guest_of_one_channel_is_brought_into_another(world):
    rsvc, published, left = world
    ada, ada_id = member(rsvc, "ada@belfry.test", "Ada")
    with TestClient(create_app(rsvc)) as c:
        bot = guest_agent(c, rsvc, ada, ada_id, "garden")
        r = post(c, ada, "/api/channel-guests", {"channel": "android", "pubkey": pk(bot)})
        assert r.status_code == 200 and r.json()["added"] is True, r.text
        # the app's own background rounds (agents.mirror, watchers) may publish around this call:
        # find the 9000 for the bot in android rather than trusting the last event
        bot_pk = pk(bot)
        adds = [
            t
            for k, t in published
            if k == 9000 and ["h", "android"] in t and any(x[:2] == ["p", bot_pk] for x in t)
        ]
        assert adds, published[-3:]
        tags = adds[-1]
        assert ["agent", "1"] in tags and ["invited-by", pk(ada)] in tags
        time.sleep(1.1)
        r = post(c, ada, "/api/channel-guests", {"channel": "android", "pubkey": pk(bot)})
        assert r.json()["already"] is True
        time.sleep(1.1)
        r = post(c, ada, "/api/channel-guests", {"channel": "android", "pubkey": pk(ada)})
        assert r.json()["member"] is True
        time.sleep(1.1)
        r = post(c, bot, "/api/channel-guests", {"channel": "x", "pubkey": pk(ada)})
        assert r.status_code == 403


def test_an_agent_with_only_the_link_learns_everything(world):
    """The member pastes one line to their AI ("Join … as my agent: read <agent_url> and follow
    it"): that page alone says how to make and keep a key, accept, sign NIP-98, which relays,
    how to read and post, where the skills are, and that it is the member's agent, never them."""
    rsvc, _published, _left = world
    rsvc.settings.console_url = "https://belfry.belfry.test"
    rsvc.settings.groups_relay_public_url = "wss://groups-relay.belfry.test"
    rsvc.settings.community_relay_public_url = "wss://relay.belfry.test"
    ada, _ = member(rsvc, "ada@belfry.test", "Ada Admin")
    with TestClient(create_app(rsvc)) as c:
        r = post(c, ada, "/api/channel-invites", {"channel": "general", "agent": True})
        url = r.json()["agent_url"]
        assert url.startswith("https://belfry.belfry.test/invite/") and url.endswith(".md")
        token = url.rsplit("/", 1)[1].removesuffix(".md")
        md = c.get(f"/invite/{token}.md")
        assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown")
        text = md.text
        accept = f"https://belfry.belfry.test/api/v1/guest-invites/{token}/accept"
        for needed in (
            "Join Open Source Village as Ada Admin's agent",
            "never as Ada Admin",  # it presents itself as the member's agent
            "Keep the secret key",
            accept,
            '"kind": 27235',
            '["payload", "<sha256 hex',
            "[0, pubkey, created_at, kind, tags, content]",
            "wss://groups-relay.belfry.test",
            "wss://relay.belfry.test",  # a member's agent reads the community relay too
            "kind 22242",
            '"#h": ["general"]',
            "kind 9, content your text",
            "https://belfry.belfry.test/skills/",
            "/.well-known/belfry.json",
            "You may not",
            '"member": true',
        ):
            assert needed in text, needed
        # bad tokens: 404; a used link: 410
        assert c.get("/invite/nope.md").status_code == 404
        bot = PrivateKey()
        body = b'{"name": "Ada\'s agent"}'
        r = c.post(
            f"/api/v1/guest-invites/{token}/accept",
            content=body,
            headers={
                "Host": "belfry.belfry.test",
                "Authorization": nip98(bot, accept, "POST", body),
                "Content-Type": "application/json",
            },
        )
        assert r.status_code == 200, r.text
        assert c.get(f"/invite/{token}.md").status_code == 410


def test_a_member_lets_their_own_agent_read(world, monkeypatch):
    """docs/encrypted.md §11: POST /api/agents {pubkey, reads} by the agent's own member; the
    agent becomes a reader on the member's device list, outside the 20 devices."""
    from belfry_onboarding import devices

    rsvc, published, left = world
    ada, ada_id = member(rsvc, "ada@belfry.test", "Ada")
    bob, _ = member(rsvc, "bob@belfry.test", "Bob")
    refreshed: list[str] = []
    monkeypatch.setattr(
        devices, "schedule", lambda svc, account, delay=0.0: refreshed.append(account)
    )
    with TestClient(create_app(rsvc)) as c:
        bot = guest_agent(c, rsvc, ada, ada_id)
        # a guest agent, not yet Ada's own: not an agent's pass
        r = post(c, ada, "/api/agents", {"pubkey": pk(bot), "reads": True})
        assert r.status_code == 409 and r.json()["error"] == "not_agent"
        time.sleep(1.1)
        assert post(c, ada, "/api/agents", {"pubkey": pk(bot), "mine": True}).status_code == 200
        time.sleep(1.1)
        r = post(c, bob, "/api/agents", {"pubkey": pk(bot), "reads": True})
        assert r.status_code == 403 and r.json()["error"] == "not_owner"
        r = post(c, ada, "/api/agents", {"pubkey": "1" * 64, "reads": True})
        assert r.status_code == 404
        r = post(c, ada, "/api/agents", {"pubkey": pk(bot), "reads": "yes"})
        assert r.status_code == 400
        time.sleep(1.1)
        r = post(c, ada, "/api/agents", {"pubkey": pk(bot), "reads": True})
        assert r.status_code == 200 and r.json() == {"pubkey": pk(bot), "reads": True}
        assert refreshed == [pk(ada)]
        assert post(c, ada, "/api/agents", {}).json()["agents"][0]["reads"] is True
        # the list's entries: Ada's devices, then the agent as a reader
        phone = "d" * 64
        rows = [{"client_pubkey": phone, "name": "Chat · Safari on iPhone"}]
        monkeypatch.setattr(devices, "sessions_of", lambda svc, a: (rows, "t"))
        monkeypatch.setattr("belfry_onboarding.discovery.chat_session", lambda *a: True)
        devices._stores.clear()
        assert devices.entries_for(rsvc, pk(ada)) == [(phone,), (pk(bot), "reader")]
        event = devices.event_for(pk(ada), 1, None, devices.entries_for(rsvc, pk(ada)), 1)
        assert ["device", pk(bot), "reader"] in event["tags"]
        # an own-key member: their key as self, and the reader beside it
        monkeypatch.setattr(devices, "sessions_of", lambda svc, a: (None, None))
        assert devices.entries_for(rsvc, pk(ada)) == [(pk(ada), "self"), (pk(bot), "reader")]
        time.sleep(1.1)
        r = post(c, ada, "/api/agents", {"pubkey": pk(bot), "reads": False})
        assert r.json() == {"pubkey": pk(bot), "reads": False}
        assert devices.entries_for(rsvc, pk(ada)) == [(pk(ada), "self")]
        assert devices.readers_of(rsvc, pk(bob)) == []
