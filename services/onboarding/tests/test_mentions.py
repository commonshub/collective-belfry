"""@mentions by e-mail (mentions.py): a daily digest of the messages naming you, weekly or off
from its footer."""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from belfry_onboarding import core, followers, mailer, mentions, steward
from belfry_onboarding.app import create_app
from belfry_onboarding.nostr import hex_to_npub
from belfry_onboarding.register import Register

ANA, BOB = "a" * 64, "b" * 64
TZ = ZoneInfo("Europe/Brussels")


class FakeRelay:
    def __init__(self):
        self.events: list[dict] = []

    def query(self, filters, sign):
        f = filters[0]
        out = []
        for e in self.events:
            if e["kind"] not in f.get("kinds", [e["kind"]]):
                continue
            if "authors" in f and e["pubkey"] not in f["authors"]:
                continue
            if "#p" in f and not any(t[0] == "p" and t[1] in f["#p"] for t in e["tags"]):
                continue
            if e["created_at"] < f.get("since", 0):
                continue
            out.append(e)
        return out


@pytest.fixture
def world(services, tmp_path, settings, monkeypatch):
    mailer.SENT.clear()
    services.odoo = Register(tmp_path / "register.sqlite", settings)
    services.settings.nip05_domain = "belfry.test"
    services.settings.public_url = "https://pass.belfry.test"
    relay = FakeRelay()
    monkeypatch.setattr(followers, "_relay", lambda svc: (relay, lambda t: t))
    monkeypatch.setattr(followers, "channel_label", lambda svc, ch: f"#{ch}")
    ids = {}
    for email, name, key in (("ana@example.org", "Ana", ANA), ("bob@example.org", "Bob", BOB)):
        made = steward.steward_pass(services, email, name)
        services.odoo.write("belfry.pass", [made["pass_id"]], {"keycast_pubkey": key})
        row = services.odoo.read_one("belfry.pass", made["pass_id"], ["partner_id"])
        ids[name] = core.m2o_id(row["partner_id"])
    relay.events.append(
        {"kind": 0, "pubkey": ANA, "created_at": 1, "tags": [], "content": '{"name": "Ana"}'}
    )
    return services, relay, ids


def mention(relay, at: int, channel="garden", eid="e1"):
    relay.events.append(
        {
            "id": eid,
            "kind": 9,
            "pubkey": ANA,
            "created_at": at,
            "tags": [["h", channel], ["p", BOB]],
            "content": f"Can you water the plants, nostr:{hex_to_npub(BOB)}?",
        }
    )


def test_a_daily_digest_of_mentions(world):
    svc, relay, ids = world
    morning = dt.datetime(2026, 11, 10, 9, 0, tzinfo=TZ)  # a Tuesday
    mention(relay, int(morning.timestamp()) - 3600)
    mailer.SENT.clear()
    assert mentions.morning(svc, morning)["due"] == 2
    assert len(mailer.SENT) == 1  # built (the tests have no mail provider to send it)
    msg = mailer.SENT[-1]
    text = msg.get_body(preferencelist=("plain",)).get_content()
    assert msg["To"] == "bob@example.org" and "1 mention on" in msg["Subject"]
    assert "#garden (https://chat.belfry.test/#/c/garden)" in text
    assert "Ana: Can you water the plants, @Bob?" in text
    assert "Reply: https://chat.belfry.test/#/c/garden/m/e1" in text
    page = msg.get_body(preferencelist=("html",)).get_content()
    assert "background:#fafafa" not in page and "Open the chat" not in page  # no frame, no button
    assert 'href="https://chat.belfry.test/#/c/garden"' in page and ">#garden</a>" in page
    assert 'href="https://chat.belfry.test/#/c/garden/m/e1"' in page and "Reply →" in page
    assert "Weekly instead: https://pass.belfry.test/mentions/" in text
    # once a day only
    assert mentions.morning(svc, morning + dt.timedelta(hours=2)) is None
    # seen: the channel opened after the mention → not mailed
    nxt = morning + dt.timedelta(days=1)
    mention(relay, int(nxt.timestamp()) - 7200, eid="e2")
    relay.events.append(
        {
            "kind": 30078,
            "pubkey": BOB,
            "created_at": int(nxt.timestamp()) - 3600,
            "tags": [["d", "seen:garden"], ["h", "garden"]],
            "content": "",
        }
    )
    mailer.SENT.clear()
    mentions.morning(svc, nxt)
    assert not mailer.SENT
    # weekly: nothing on a Thursday
    mentions.save(svc, ids["Bob"], f="weekly")
    thu = morning + dt.timedelta(days=2)
    mention(relay, int(thu.timestamp()) - 600, channel="kitchen", eid="e3")
    assert mentions.morning(svc, thu)["due"] == 1  # Ana only (Bob is weekly)


def test_the_footer_page_switches_with_a_button(world):
    svc, _, ids = world
    tok = mentions.token(svc, ids["Bob"])
    with TestClient(create_app(svc)) as c:
        page = c.get(f"/mentions/{tok}?f=weekly")
        assert page.status_code == 200 and "every day" in page.text  # a GET changes nothing
        assert mentions.prefs(svc, ids["Bob"])["f"] == "daily"
        r = c.post(f"/mentions/{tok}", data={"f": "weekly"}, follow_redirects=False)
        assert r.status_code in (302, 303)
        assert mentions.prefs(svc, ids["Bob"])["f"] == "weekly"
        assert c.get(f"/mentions/{ids['Bob']}.forged").status_code == 404
        assert c.post(f"/mentions/{tok}", data={"f": "hourly"}).status_code == 404
        assert re.search(r"once a week", c.get(f"/mentions/{tok}").text)


def test_a_digest_groups_per_channel_and_links_what_the_messages_say(world):
    """Xavier, 2026-11-11: a reply link under each message, the channel's name clickable, links,
    channels and #topics in messages clickable, the community named up top."""
    from belfry_onboarding.nostr import bech32_encode

    svc, relay, ids = world
    morning = dt.datetime(2026, 11, 10, 9, 0, tzinfo=TZ)
    at = int(morning.timestamp()) - 3600
    tlv = bytes([0, 7]) + b"general" + bytes([3, 4]) + (39000).to_bytes(4, "big")
    naddr = bech32_encode("naddr", tlv)
    mention(relay, at - 20, channel="garden", eid="g1")
    relay.events.append(
        {
            "id": "k1",
            "kind": 1111,
            "pubkey": ANA,
            "created_at": at - 10,
            "tags": [["h", "kitchen"], ["E", "root1"], ["p", BOB]],
            "content": f"See https://example.org/menu, and nostr:{naddr} for #soup <b>now</b>",
        }
    )
    mention(relay, at, channel="garden", eid="g2")
    mailer.SENT.clear()
    mentions.morning(svc, morning)
    msg = mailer.SENT[-1]
    page = msg.get_body(preferencelist=("html",)).get_content()
    text = msg.get_body(preferencelist=("plain",)).get_content()
    # grouped: garden (both), then kitchen
    assert page.index(">#garden</a>") < page.index("/m/g2") < page.index(">#kitchen</a>")
    assert page.count(">#garden</a>") == 1
    assert 'href="https://example.org/menu"' in page
    assert re.search(r'href="https://chat\.belfry\.test/#/c/general"[^>]*>#general</a>', page)
    assert re.search(r'href="https://chat\.belfry\.test/#/c/kitchen/t/soup"[^>]*>#soup</a>', page)
    assert "&lt;b&gt;now&lt;/b&gt;" in page and "<b>now</b>" not in page
    assert 'href="https://chat.belfry.test/#/c/kitchen/thread/root1"' in page
    assert "you were mentioned 3 times" in page and page.count("Reply →") == 3
    assert "Ana: See https://example.org/menu, and #general for #soup <b>now</b>" in text
