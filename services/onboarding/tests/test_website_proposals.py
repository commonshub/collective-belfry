"""A website proposal that passed goes live (website.passed_proposals / watch, #issues
2026-11-13): the ref is the author's signed 30023's, the pass is the latest status label, by a
belfry admin, not older than the proposal's latest version; each version goes live once."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from belfry_onboarding import apps, community, followers, relay, website, webthreads

ADMIN, AUTHOR, EVE = "a" * 64, "b" * 64, "e" * 64
NPUB = "npub1" + "q" * 58
COORD = f"30023:{AUTHOR}:proposal-x"


class Groups:
    def __init__(self):
        self.labels: list[dict] = []
        self.sent: list[dict] = []

    def query(self, filters, sign):
        f = filters[0]
        if f["kinds"] == [9]:
            return [{"kind": 9, "tags": [["h", "website"], ["proposal", "prop-x"], ["a", COORD]]}]
        if f["kinds"] == [1985]:
            return self.labels
        return []

    def publish(self, e, sign):
        self.sent.append(e)


class Main:
    versions: list[dict] = []

    def __init__(self, url):
        pass

    def query(self, filters, sign):
        assert filters[0]["authors"] == [AUTHOR]
        return self.versions


def version(at: int, ref: str = f"nsite:{NPUB}", vid: str = "v1") -> dict:
    return {
        "id": vid,
        "pubkey": AUTHOR,
        "created_at": at,
        "tags": [
            ["d", "proposal-x"],
            ["h", "prop-x"],
            ["t", "proposal"],
            ["t", "website"],
            ["website", ref],
            ["title", "New site"],
        ],
    }


def label(status: str, at: int, by: str = ADMIN) -> dict:
    return {
        "pubkey": by,
        "created_at": at,
        "tags": [
            ["h", "prop-x"],
            ["L", "belfry/proposal-status"],
            ["l", status, "belfry/proposal-status"],
            ["a", COORD],
        ],
    }


@pytest.fixture
def world(monkeypatch):
    g = Groups()
    svc = SimpleNamespace(
        settings=SimpleNamespace(relay_url="wss://relay.test", nip05_domain="belfry.test"),
        odoo=SimpleNamespace(),
    )
    monkeypatch.setattr(
        followers, "_relay", lambda s: (g, lambda t: {**t, "id": "x", "pubkey": "f" * 64})
    )
    monkeypatch.setattr(relay, "RelayPublisher", Main)
    monkeypatch.setattr(community, "belfry_signer", lambda s: lambda t: t)
    monkeypatch.setattr(community, "names_for", lambda s, keys: {ADMIN: "Ada"})
    monkeypatch.setattr(apps, "admin_pubkeys", lambda s: {ADMIN})
    monkeypatch.setattr(
        website,
        "parse_ref",
        lambda text, dom="": (AUTHOR, "") if text.startswith("nsite:npub1") else None,
    )
    webthreads._pending_mem.clear()
    return svc, g


def test_only_a_pass_by_an_admin_on_the_latest_version(world):
    svc, g = world
    Main.versions = [version(100)]
    assert website.passed_proposals(svc) == []  # no label yet
    g.labels = [label("passed", 200, by=EVE)]
    assert website.passed_proposals(svc) == []  # not an admin
    g.labels = [label("passed", 200), label("rejected", 300, by=AUTHOR)]
    assert website.passed_proposals(svc) == []  # a later label wins
    g.labels = [label("passed", 200)]
    got = website.passed_proposals(svc)
    assert [p["ref"] for p in got] == [(AUTHOR, "")] and got[0]["admin"] == ADMIN
    # a new version after the pass: not without a new pass
    Main.versions = [version(100), version(250, vid="v2")]
    assert website.passed_proposals(svc) == []
    Main.versions = [version(100, ref="https://evil.example/")]
    assert website.passed_proposals(svc) == []  # not an nsite


def test_watch_publishes_once_and_says_so(world, monkeypatch):
    svc, g = world
    Main.versions = [version(100)]
    g.labels = [label("passed", 200)]
    calls: list[tuple] = []

    async def publish(s, pk, name, admin):
        calls.append((pk, name, admin))
        return {"files": 3}

    monkeypatch.setattr(website, "publish_from", publish)
    assert asyncio.run(website.watch(svc)) == 1
    assert calls == [(AUTHOR, "", "Ada")]
    assert any("Live now" in e["content"] and ["h", "prop-x"] in e["tags"] for e in g.sent)
    assert asyncio.run(website.watch(svc)) == 0 and len(calls) == 1  # once
