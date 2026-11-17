"""Buttons on messages (actions.py, docs/actions.md): the parser against the shared vectors, and
the belfry's answers to presses on belfry: buttons."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from belfry_onboarding import actions, apps, community, homepage, website

VECTORS = json.loads(
    (Path(__file__).resolve().parents[3] / "docs" / "actions-vectors.json").read_text()
)
ADMIN, AUTHOR, EVE, GROUPS = "a" * 64, "b" * 64, "e" * 64, "f" * 64
NPUB = "npub10j0emvtx36vh5h0dmd2e2zau7uh7vssx9mqnk2mn68agvknvecsqraxdux"
SITE_PK = "7c9f9db1668e997a5deddb55950bbcf72fe642062ec13b2b73d1fa865a6cce20"
NOW = 1_000_000


@pytest.mark.parametrize("case", VECTORS["buttons"], ids=lambda c: c["name"])
def test_buttons_vectors(case):
    assert actions.parse_buttons(case["tags"]) == case["buttons"]


def test_press_vector_is_a_press():
    v = VECTORS["press"]
    p = actions.press_of({**v["event"], "id": "p1", "pubkey": "P", "created_at": 5})
    assert p == {
        "id": "p1",
        "pubkey": "P",
        "button": v["button"],
        "message": v["message"]["id"],
        "group": v["message"]["group"],
        "created_at": 5,
    }


def test_fixed_sentences_match_the_vectors():
    for code, text in actions.FIXED.items():
        assert VECTORS["fixed_text"][code] == text


class Relay:
    def __init__(self, messages, answers=()):
        self.messages = {m["id"]: m for m in messages}
        self.answers = list(answers)
        self.sent: list[dict] = []

    def query(self, filters, sign):
        f = filters[0]
        if "ids" in f:
            return [self.messages[i] for i in f["ids"] if i in self.messages]
        if f.get("#L") == [actions.RESULT_NS]:
            return [a for a in self.answers + self.sent if a["tags"][2][1] in f["#e"]]
        return []

    def publish(self, e, sign):
        self.sent.append(e)


def sign(t):
    return {**t, "id": f"ans{t['created_at']}", "pubkey": GROUPS}


def message(mid="m1", tags=None, content="", kind=9, pubkey=AUTHOR, group="website"):
    return {
        "id": mid,
        "kind": kind,
        "pubkey": pubkey,
        "content": content,
        "tags": [["h", group], *(tags or [])],
    }


PUBLISH = ["button", "publish", "Approve and publish", "primary", "belfry:website-publish"]


def press(pid="p1", by=ADMIN, button="publish", mid="m1", group="website", at=NOW - 5):
    return {
        "id": pid,
        "pubkey": by,
        "kind": 1985,
        "created_at": at,
        "content": "",
        "tags": [
            ["L", "belfry/action"],
            ["l", button, "belfry/action"],
            ["e", mid],
            ["p", AUTHOR],
            ["h", group],
        ],
    }


def tag(e, k):
    return next((t[1] for t in e["tags"] if t[0] == k), None)


@pytest.fixture
def world(monkeypatch):
    svc = SimpleNamespace(settings=SimpleNamespace(nip05_domain="belfry.test"))
    published: list[tuple] = []

    async def publish_from(s, pubkey, name, admin):
        published.append((pubkey, name, admin))
        return {"files": 3}

    monkeypatch.setattr(website, "publish_from", publish_from)
    monkeypatch.setattr(apps, "admin_pubkeys", lambda s: {ADMIN})
    monkeypatch.setattr(community, "names_for", lambda s, keys: {ADMIN: "Ada"})
    return svc, published


def run(answerer, e):
    return asyncio.run(answerer.handle(e))


def make(svc, relay):
    return actions.Answerer(svc, relay, sign, clock=lambda: NOW, trusted={GROUPS})


def test_publish_by_an_admin_from_the_a_tag(world):
    svc, published = world
    r = Relay([message(tags=[["a", f"35128:{SITE_PK}:website"], PUBLISH])])
    out = run(make(svc, r), press())
    assert out.status == "ok" and out.closed and out.text == "Published by Ada."
    assert published == [(SITE_PK, "website", "Ada")]
    (ans,) = r.sent
    assert ans["kind"] == 1985 and ans["content"] == "Published by Ada."
    assert ["L", "belfry/action-result"] in ans["tags"]
    assert ["l", "ok", "belfry/action-result"] in ans["tags"]
    assert ["closed", "1"] in ans["tags"] and tag(ans, "code") is None
    assert tag(ans, "press") == "p1" and tag(ans, "e") == "m1"
    assert tag(ans, "p") == ADMIN and tag(ans, "h") == "website"


def test_publish_from_a_preview_link_in_the_text(world):
    svc, published = world
    text = f"New front page: https://preview.belfry.test/p/{NPUB}@website. Approve below."
    r = Relay([message(tags=[PUBLISH], content=text)])
    assert run(make(svc, r), press()).status == "ok"
    assert published == [(SITE_PK, "website", "Ada")]


def test_not_an_admin(world):
    svc, published = world
    r = Relay([message(tags=[["a", f"35128:{SITE_PK}:website"], PUBLISH])])
    out = run(make(svc, r), press(by=EVE))
    assert (out.status, out.code) == ("error", "not_allowed")
    assert r.sent[0]["content"] == "Only admins of this belfry can publish the website."
    assert ["code", "not_allowed"] in r.sent[0]["tags"]
    assert published == []


def test_no_target(world):
    svc, published = world
    r = Relay([message(tags=[PUBLISH], content="Look at belfry.test/website")])
    out = run(make(svc, r), press())
    assert out.code == "no_target"
    assert out.text == "This message doesn't point to a version of the website."
    assert published == []


def test_publish_failure_says_why(world, monkeypatch):
    svc, _ = world

    async def boom(*a):
        raise homepage.HomepageError("That site has no /index.html.")

    monkeypatch.setattr(website, "publish_from", boom)
    r = Relay([message(tags=[["a", f"35128:{SITE_PK}:website"], PUBLISH])])
    out = run(make(svc, r), press())
    assert out.code == "failed" and out.text == "Couldn't publish: that site has no /index.html."


def test_once_per_press_in_memory_and_on_the_relay(world):
    svc, published = world
    msg = message(tags=[["a", f"35128:{SITE_PK}:website"], PUBLISH])
    r = Relay([msg])
    a = make(svc, r)
    assert run(a, press()) is not None
    assert run(a, press()) is None  # seen
    assert len(r.sent) == 1
    # a restart: the answer is on the relay already
    r2 = Relay([msg], answers=r.sent)
    assert run(make(svc, r2), press()) is None and r2.sent == []
    # an answer by someone else doesn't count as answered
    forged = [{**r.sent[0], "pubkey": EVE}]
    r3 = Relay([msg], answers=forged)
    assert run(make(svc, r3), press()) is not None
    assert len(published) == 2  # a second press on the same version publishes again: fine


def test_expired(world):
    svc, published = world
    r = Relay([message(tags=[["a", f"35128:{SITE_PK}:website"], PUBLISH])])
    out = run(make(svc, r), press(at=NOW - actions.MAX_AGE - 1))
    assert out.code == "expired" and out.text == "That took too long. Try again."
    assert published == []


def test_unknown_belfry_action(world):
    svc, _ = world
    r = Relay([message(tags=[["button", "go", "Go", "", "belfry:fly"]])])
    out = run(make(svc, r), press(button="go"))
    assert out.code == "unknown_action"
    assert out.text == "This belfry doesn't know this button yet."


def test_authors_buttons_and_strangers_are_left_alone(world):
    svc, _ = world
    r = Relay(
        [
            message(tags=[["button", "yes", "Yes"], PUBLISH]),
            message("m2", tags=[PUBLISH], kind=7),
            message("m3", tags=[PUBLISH], group="general"),
        ]
    )
    a = make(svc, r)
    assert run(a, press(button="yes")) is None  # the author's button
    assert run(a, press("p2", button="nope")) is None  # no such button
    assert run(a, press("p3", mid="gone")) is None  # no such message
    assert run(a, press("p4", mid="m2")) is None  # not a message kind
    assert run(a, press("p5", mid="m3")) is None  # press in another group than the message
    assert run(a, {"kind": 1985, "id": "x", "pubkey": EVE, "tags": [["L", "other"]]}) is None
    assert r.sent == []


def test_rate_limit(world):
    svc, _ = world
    r = Relay([message(tags=[["button", "go", "Go", "", "belfry:fly"]])])
    a = make(svc, r)
    for i in range(actions.RATE):
        assert run(a, press(f"p{i}", button="go")).code == "unknown_action"
    out = run(a, press("last", button="go"))
    assert out.code == "failed" and out.text == "Too many tries. Wait a little."


def test_thread_reply_can_carry_buttons(world):
    svc, published = world
    r = Relay([message(kind=1111, tags=[["a", f"35128:{SITE_PK}:"], PUBLISH])])
    assert run(make(svc, r), press()).status == "ok"
    assert published == [(SITE_PK, "", "Ada")]


def test_handler_crash_still_answers(world):
    svc, _ = world

    async def crash(p):
        raise RuntimeError("x")

    r = Relay([message(tags=[["button", "go", "Go", "", "belfry:crash"]])])
    a = actions.Answerer(svc, r, sign, {"crash": crash}, clock=lambda: NOW, trusted={GROUPS})
    assert run(a, press(button="go")).code == "failed"
    assert r.sent[0]["content"] == "That didn't work. Try again."


# ── #website's description ──────────────────────────────────────────────────────────────────
class Meta:
    def __init__(self, about):
        self.about = about
        self.sent: list[dict] = []

    def query(self, filters, sign):
        if self.about is None:
            return []
        return [
            {
                "kind": 39000,
                "created_at": 1,
                "tags": [["d", "website"], ["name", "website"], ["about", self.about], ["open"]],
            }
        ]

    def publish(self, e, sign):
        self.sent.append(e)


def test_old_default_description_is_replaced_but_never_an_edited_one():
    svc = SimpleNamespace(settings=SimpleNamespace(nip05_domain="belfry.test"))
    old = website.OLD_ABOUTS[0].format(d="belfry.test")
    m = Meta(old)
    assert website.refresh_about(svc, m, lambda t: t) is True
    (e,) = m.sent
    assert e["kind"] == 9002
    assert e["tags"] == [["h", "website"], ["name", "website"], ["about", website.ABOUT], ["open"]]
    m = Meta("Our own words.")
    assert website.refresh_about(svc, m, lambda t: t) is False and m.sent == []
    m = Meta(None)
    assert website.refresh_about(svc, m, lambda t: t) is None and m.sent == []
    assert website.ABOUT.startswith("What goes on our public website")


def test_belfry_json_names_the_groups_key():
    from coincurve import PrivateKey

    from belfry_onboarding import discovery
    from belfry_onboarding.nostr import hex_to_npub

    key = PrivateKey()
    pk = key.public_key_xonly.format().hex()
    s = SimpleNamespace(groups_api_nsec=key.secret.hex())
    assert discovery._groups_key(s) == {"pubkey": pk, "npub": hex_to_npub(pk)}
    assert discovery._groups_key(SimpleNamespace(groups_api_nsec="")) == {}
