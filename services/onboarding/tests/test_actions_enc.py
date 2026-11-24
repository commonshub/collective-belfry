"""Buttons in private groups, format 3 (docs/encrypted.md §24; actions.py, enc.py): the copy of a
press reaches the groups key as an 8572 on the community relay, is opened and judged as the apps
judge it, and the answer goes into the group as an 8572 by the groups key that the members'
devices open and accept."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from coincurve import PrivateKey

from belfry_onboarding import actions, enc, followers
from belfry_onboarding.sealed import sign, with_id

BELFRY = PrivateKey(b"\x11" * 32)
GROUPS = PrivateKey(b"\x12" * 32)
ANA = PrivateKey(b"\x13" * 32)
DEV = PrivateKey(b"\x14" * 32)  # Ana's phone


def pub(k: PrivateKey) -> str:
    return k.public_key_xonly.format().hex()


BELFRY_PUB, GROUPS_PUB, ANA_PUB, DEV_PUB = pub(BELFRY), pub(GROUPS), pub(ANA), pub(DEV)
GROUP = "pod-garden-7f3a"
BUTTON = ["button", "go", "Publish", "primary", "belfry:thing"]


class GroupsRelay:
    url = "ws://groups.test"

    def __init__(self):
        self.events: list[dict] = []

    def publish(self, e, sign_):
        self.events.append(e)

    def query(self, filters, sign_):
        return []


@pytest.fixture
def world(services, monkeypatch):
    s = services.settings
    s.belfry_nsec = BELFRY.secret.hex()
    s.groups_api_nsec = GROUPS.secret.hex()
    s.encrypted_format = 3
    services.relay.require_auth = False
    lst = sign(
        BELFRY.secret,
        {
            "kind": 8570,
            "created_at": int(time.time()) - 3600,
            "tags": [["p", ANA_PUB], ["n", "1"], ["device", DEV_PUB]],
            "content": "",
        },
    )
    services.relay.events.append(lst)
    groups = GroupsRelay()
    monkeypatch.setattr(followers, "_relay", lambda svc: (groups, lambda t: sign(GROUPS.secret, t)))
    monkeypatch.setattr(followers, "_meta_tags", lambda svc, g: {"d", "private", "closed"})
    members = {ANA_PUB}
    monkeypatch.setattr(enc, "group_members", lambda svc, g: set(members))
    enc.forget_keys()
    calls: list[dict] = []

    async def thing(p):
        calls.append(p.press)
        return actions.Result("ok", "Done.", closed=True)

    answerer = actions.Answerer(
        services, groups, lambda t: sign(GROUPS.secret, t), handlers={"thing": thing}
    )
    yield services, groups, answerer, calls, members, lst
    enc.forget_keys()


def press_copy(svc, button=BUTTON, author=ANA_PUB, sealer=DEV, at=None):
    """What the web sends (buttons.ts pressToGroupsKey): the press rumor's tags without the
    author's p, ["p", groups key], ["e", press id, "", "press"], the button, ["k", kind];
    content = the message as JSON with the files' keys taken out; then encryptFor([gk])."""
    at = int(time.time()) if at is None else at
    message = {
        "id": "m" * 64,  # as opened in the group; never recomputed by the belfry
        "pubkey": "b" * 64,
        "created_at": at - 60,
        "kind": 9,
        "content": "Approve?",
        "tags": [["h", GROUP], ["imeta", "url https://x/y", "m image/png"], button],
    }
    press_id = "e" * 64
    rumor = with_id(
        {
            "kind": 1985,
            "pubkey": author,
            "created_at": at,
            "content": json.dumps(message),
            "tags": [
                ["L", actions.PRESS_NS],
                ["l", button[1], actions.PRESS_NS],
                ["e", message["id"]],
                ["h", GROUP],
                ["p", GROUPS_PUB],
                ["e", press_id, "", "press"],
                button,
                ["k", "9"],
            ],
        }
    )
    lst = enc.chain_of(svc.relay.events, BELFRY_PUB, ANA_PUB)
    k, K = enc.new_key()
    people = sorted([(GROUPS_PUB, None), (ANA_PUB, enc.current(lst).id)], key=lambda p: p[0])
    kr = enc.key_rumor(pub(sealer), k, people, at)
    svc.relay.events += enc.people_envelopes(
        sealer.secret, kr, [(GROUPS_PUB, [GROUPS_PUB]), (ANA_PUB, [DEV_PUB])]
    )
    msgs = enc.people_message(sealer.secret, rumor, K, [GROUPS_PUB, ANA_PUB], ANA_PUB)
    svc.relay.events += msgs
    return next(m for m in msgs if ["p", GROUPS_PUB] in m["tags"]), press_id, message


def opened_answer(groups):
    env = next(e for e in groups.events if e["kind"] == 8571)
    msg = next(e for e in groups.events if e["kind"] == 8572)
    assert env["pubkey"] == GROUPS_PUB and msg["pubkey"] == GROUPS_PUB
    assert ["silent", "1"] in msg["tags"]
    key = enc.open_group_envelope(DEV.secret, env, GROUP)
    assert enc.accept_group_key(key, enc.SERVICE) == "ok"
    seal, rumor = enc.open_group_message(key.k, msg)
    assert enc.accept_group_message(msg, seal, rumor, GROUP, enc.SERVICE) == "ok"
    return rumor


def test_a_press_in_a_private_group_is_answered_into_the_group(world):
    svc, groups, answerer, calls, members, _ = world
    copy, press_id, message = press_copy(svc)
    result = asyncio.run(answerer.handle_copy(copy))
    assert result.status == "ok"
    assert calls[0]["id"] == press_id and calls[0]["pubkey"] == ANA_PUB
    assert calls[0]["message"] == message["id"] and calls[0]["group"] == GROUP
    answer = opened_answer(groups)
    assert answer["kind"] == 1985 and answer["pubkey"] == GROUPS_PUB
    tags = answer["tags"]
    assert ["L", actions.RESULT_NS] in tags and ["l", "ok", actions.RESULT_NS] in tags
    assert ["press", press_id] in tags and ["e", message["id"]] in tags
    assert ["h", GROUP] in tags and ["closed", "1"] in tags and answer["content"] == "Done."
    # once: the same copy again, or the press again in another copy
    n = len(groups.events)
    assert asyncio.run(answerer.handle_copy(copy)) is None
    copy2, _, _ = press_copy(svc)
    assert asyncio.run(answerer.handle_copy(copy2)) is None
    assert len(groups.events) == n and len(calls) == 1


def test_a_presser_outside_the_group_is_left(world):
    svc, groups, answerer, calls, members, _ = world
    members.clear()
    copy, _, _ = press_copy(svc)
    assert asyncio.run(answerer.handle_copy(copy)) is None
    assert calls == [] and groups.events == []


def test_what_is_not_a_belfry_button_or_not_signed_by_a_device_is_left(world, monkeypatch):
    svc, groups, answerer, calls, members, _ = world
    own = ["button", "go", "Going", "", ""]  # the author's button: not the belfry's
    copy, _, _ = press_copy(svc, button=own)
    assert asyncio.run(answerer.handle_copy(copy)) is None
    copy, _, _ = press_copy(svc, sealer=ANA)  # her account key is no device of hers
    assert asyncio.run(answerer.handle_copy(copy)) is None
    assert groups.events == [] and calls == []
    # a group that is not private: copies exist only for private groups
    monkeypatch.setattr(followers, "_meta_tags", lambda svc, g: {"d"})
    copy, _, _ = press_copy(svc)
    assert asyncio.run(answerer.handle_copy(copy)) is None and groups.events == []


def test_an_old_press_is_expired(world):
    svc, groups, answerer, calls, members, _ = world
    copy, _, _ = press_copy(svc)
    answerer.clock = lambda: time.time() + actions.MAX_AGE + 60
    result = asyncio.run(answerer.handle_copy(copy))
    assert result.code == "expired" and calls == []


def test_the_copies_are_listened_for_only_on_format_3(world):
    svc, groups, answerer, calls, members, _ = world
    svc.settings.relay_url = "ws://relay.test"
    job = actions.copies_job(svc, answerer)
    assert job is not None
    job.close()
    svc.settings.encrypted_format = 1
    assert actions.copies_job(svc, answerer) is None
