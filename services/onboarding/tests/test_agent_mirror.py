"""A member's own agent follows its member into every channel (agents.mirror, #issues
2026-11-13: "the exact same access as the parent")."""

from __future__ import annotations

from types import SimpleNamespace

from belfry_onboarding import agents, followers, guests

ADA, BOT, EVE = "a" * 64, "b" * 64, "e" * 64


class Relay:
    adds: list[dict] = []

    def query(self, filters, sign):
        if filters[0]["kinds"] == [9000]:
            return self.adds
        return [
            {"tags": [["d", "garden"], ["p", ADA], ["p", EVE]]},
            {"tags": [["d", "kitchen"], ["p", ADA], ["p", BOT]]},  # in it already
            {"tags": [["d", "ev-meet"], ["p", ADA]]},  # a meeting: the same
            {"tags": [["d", "attic"], ["p", EVE]]},  # not Ada's
            {"tags": [["d", "porch"], ["p", BOT]]},  # Ada left; the bot came by mirroring
            {"tags": [["d", "shed"], ["p", BOT]]},  # Ada left; the bot was invited on its own
        ]


def test_an_agent_is_put_in_its_members_channels_only(monkeypatch):
    svc = SimpleNamespace(odoo=SimpleNamespace(agent_owners=lambda: {BOT: ADA}))
    sent: list[tuple[int, list]] = []
    monkeypatch.setattr(followers, "_relay", lambda s: (Relay(), lambda t: t))
    monkeypatch.setattr(guests, "_channel_event", lambda s, k, tags: sent.append((k, tags)) or True)
    Relay.adds = [
        {"created_at": 5, "tags": [["h", "porch"], ["p", BOT, "member"], ["mirror", "1"]]},
        {"created_at": 5, "tags": [["h", "shed"], ["p", BOT, "member"]]},
    ]
    assert agents.mirror(svc) == 3
    added = sorted(t[0][1] for k, t in sent if k == 9000)
    assert added == ["ev-meet", "garden"]
    assert all(["mirror", "1"] in t and ["agent", "1"] in t for k, t in sent if k == 9000)
    assert [t[0][1] for k, t in sent if k == 9001] == ["porch"]
    sent.clear()
    assert agents.mirror(svc, only=EVE) == 0 and not sent
