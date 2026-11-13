"""A member's own agent follows its member into every channel (agents.mirror, #issues
2026-11-13: "the exact same access as the parent")."""

from __future__ import annotations

from types import SimpleNamespace

from belfry_onboarding import agents, followers, guests

ADA, BOT, EVE = "a" * 64, "b" * 64, "e" * 64


class Relay:
    def query(self, filters, sign):
        assert filters[0]["kinds"] == [39002]
        return [
            {"tags": [["d", "garden"], ["p", ADA], ["p", EVE]]},
            {"tags": [["d", "kitchen"], ["p", ADA], ["p", BOT]]},  # in it already
            {"tags": [["d", "ev-meet"], ["p", ADA]]},  # a meeting: the same
            {"tags": [["d", "attic"], ["p", EVE]]},  # not Ada's
        ]


def test_an_agent_is_put_in_its_members_channels_only(monkeypatch):
    svc = SimpleNamespace(odoo=SimpleNamespace(agent_owners=lambda: {BOT: ADA}))
    sent: list[tuple[int, list]] = []
    monkeypatch.setattr(followers, "_relay", lambda s: (Relay(), lambda t: t))
    monkeypatch.setattr(guests, "_channel_event", lambda s, k, tags: sent.append((k, tags)) or True)
    assert agents.mirror(svc) == 2
    assert sorted(t[0][1] for k, t in sent) == ["ev-meet", "garden"]
    assert all(k == 9000 and ["p", BOT, "member"] in t and ["agent", "1"] in t for k, t in sent)
    sent.clear()
    assert agents.mirror(svc, only=EVE) == 0 and not sent
