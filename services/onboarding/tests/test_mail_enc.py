"""E-mail in format 3 (docs/encrypted.md §10, §24; mail.py, enc.py): the belfry as a sender of
incoming mail (a person, a private group's inbox card) and as a person of a member's outgoing
mail. Every event the belfry writes is read back with the apps' rules (enc.py's port of the
web's core.ts, checked against the shared vectors in test_enc.py)."""

# ruff: noqa: F811 — the fixtures m and inbox come from test_mail
from __future__ import annotations

import json
import time

import pytest
from coincurve import PrivateKey

from belfry_onboarding import enc, followers, mail
from belfry_onboarding.sealed import sign

from .test_mail import (  # noqa: F401 — fixtures
    ANA,
    ANA_PUB,
    BELFRY,
    BELFRY_PUB,
    dsn_of,
    inbox,
    m,
    message,
)

DEV = PrivateKey(b"\x05" * 32)  # Ana's phone
DEV_PUB = DEV.public_key_xonly.format().hex()
GROUPS = PrivateKey(b"\x06" * 32)
GROUPS_PUB = GROUPS.public_key_xonly.format().hex()


def pub(k: PrivateKey) -> str:
    return k.public_key_xonly.format().hex()


def device_list(account: str, devices: list[str], n: int = 1, prev: str | None = None, at=None):
    tags = [["p", account], ["n", str(n)], *([["prev", prev]] if prev else [])]
    tags += [["device", d] for d in sorted(devices)]
    at = int(time.time()) - 3600 if at is None else at
    return sign(BELFRY.secret, {"kind": 8570, "created_at": at, "tags": tags, "content": ""})


@pytest.fixture
def m3(m):
    m.settings.encrypted_format = 3
    m.settings.groups_api_nsec = GROUPS.secret.hex()
    enc.forget_keys()
    yield m
    enc.forget_keys()


def mine(m, kind):
    return [e for e in m.relay.events if e["kind"] == kind]


# ── in: a person ────────────────────────────────────────────────────────────────────────────
def test_mail_to_a_member_is_encrypted_for_their_devices(m3):
    lst = device_list(ANA_PUB, [DEV_PUB])
    m3.relay.events.append(lst)
    code, _ = mail.deliver(
        m3, message(), "203.0.113.5", "h", "bob@example.org", ["ana@belfry.test"]
    )
    assert code == 250
    assert mine(m3, 1059) == []  # never the old way
    [env] = mine(m3, 8571)
    [msg] = mine(m3, 8572)
    assert env["pubkey"] not in (BELFRY_PUB,) and msg["pubkey"] != BELFRY_PUB  # one-time keys
    assert ["p", ANA_PUB] in env["tags"] and ["p", ANA_PUB] in msg["tags"]
    assert ["silent", "1"] not in msg["tags"]
    # Ana's phone reads it as the apps do
    devices = lambda a: enc.SERVICE if a == BELFRY_PUB else enc.chain_of([lst], BELFRY_PUB, a)  # noqa: E731
    _, kr = enc.open_people_envelope(DEV.secret, env)
    assert kr["pubkey"] == BELFRY_PUB and enc.accept_key_rumor(kr, ANA_PUB, devices) == "ok"
    assert enc.alias(mail.tag(kr, "K"), ANA_PUB) == mail.tag(env, "d") == mail.tag(msg, "d")
    seal, rumor = enc.open_people_message(kr["content"], msg)
    assert enc.accept_people_message(msg, seal, rumor, enc.people_of(kr), enc.SERVICE) == "ok"
    assert rumor["kind"] == 1301 and rumor["pubkey"] == BELFRY_PUB
    assert enc.people_of(rumor) == enc.people_id([BELFRY_PUB, ANA_PUB])  # what dm.ts files
    assert mail.tag(rumor, "mail-from") == "bob@example.org"
    assert "Subject: Hello" in rumor["content"]
    # the next mail, same list: the same key, no new envelope
    raw = message(subject="Again")
    code, _ = mail.deliver(m3, raw, "203.0.113.5", "h", "bob@example.org", ["ana@belfry.test"])
    assert code == 250 and len(mine(m3, 8571)) == 1 and len(mine(m3, 8572)) == 2
    # a new device: a new key for both
    m3.relay.events.append(
        device_list(ANA_PUB, [DEV_PUB, "e" * 64], 2, lst["id"], int(time.time()))
    )
    code, _ = mail.deliver(m3, raw, "203.0.113.5", "h", "bob@example.org", ["ana@belfry.test"])
    assert code == 250 and len(mine(m3, 8571)) == 2
    assert set(json.loads(mine(m3, 8571)[-1]["content"])) == {DEV_PUB, "e" * 64}


def test_a_member_with_no_list_or_no_device_gets_451(m3):
    code, _ = mail.deliver(
        m3, message(), "203.0.113.5", "h", "bob@example.org", ["ana@belfry.test"]
    )
    assert code == 451 and mine(m3, 8572) == [] and mine(m3, 1059) == []
    m3.relay.events.append(device_list(ANA_PUB, []))
    code, _ = mail.deliver(
        m3, message(), "203.0.113.5", "h", "bob@example.org", ["ana@belfry.test"]
    )
    assert code == 451 and mine(m3, 8572) == [] and mine(m3, 1059) == []


def test_format_1_mail_is_as_before(m):
    code, _ = mail.deliver(m, message(), "203.0.113.5", "h", "bob@example.org", ["ana@belfry.test"])
    assert code == 250 and len(mine(m, 1059)) == 1 and mine(m, 8572) == []


# ── out: a member's mail to the belfry key ──────────────────────────────────────────────────
def outgoing(
    m, signer: PrivateKey = DEV, email_id="o1", publish_envelopes=True, sealer: PrivateKey = None
):
    """What the web sends: a key made by Ana's device for {belfry, Ana}, its envelopes, then a
    copy for each (dm.ts deliver → encryptFor(rumor, [belfry, me]))."""
    lst = enc.chain_of(mine(m, 8570), BELFRY_PUB, ANA_PUB)
    raw = (
        "From: Ana <ana@belfry.test>\r\nTo: bob@example.org\r\n"
        "Subject: Re: Hello\r\n\r\nThanks!\r\n"
    )
    rumor = {
        "kind": 1301,
        "pubkey": ANA_PUB,
        "created_at": int(time.time()),
        "content": raw,
        "tags": [
            ["email-id", email_id],
            ["mail-from", "ana@belfry.test"],
            ["rcpt-to", "bob@example.org"],
            ["p", BELFRY_PUB],
        ],
    }
    k, K = enc.new_key()
    people = sorted([(BELFRY_PUB, None), (ANA_PUB, enc.current(lst).id)], key=lambda p: p[0])
    kr = enc.key_rumor(pub(signer), k, people, int(time.time()))
    envs = enc.people_envelopes(
        signer.secret, kr, [(BELFRY_PUB, [BELFRY_PUB]), (ANA_PUB, [DEV_PUB])]
    )
    sealer = sealer or signer
    msgs = enc.people_message(sealer.secret, rumor, K, [BELFRY_PUB, ANA_PUB], ANA_PUB)
    if publish_envelopes:
        m.relay.events += envs
    m.relay.events += msgs
    return next(x for x in msgs if ["p", BELFRY_PUB] in x["tags"])


def test_a_members_encrypted_mail_goes_out_and_is_reported(m3, monkeypatch):
    m3.relay.events.append(device_list(ANA_PUB, [DEV_PUB]))
    sent = []
    monkeypatch.setattr(
        mail, "smtp_send", lambda svc, frm, rcpts, data: sent.append((frm, rcpts)) or (250, "OK")
    )
    msg = outgoing(m3)
    mail.take_wrap(m3, msg)
    assert sent == [("ana@belfry.test", ["bob@example.org"])]
    # the report: a 7679 to Ana's account key (no lb), as the apps read it
    assert dsn_of(m3, "o1")["status"] == "delivered"
    [report] = mine(m3, 7679)
    assert not any(t[0] == "lb" for t in report["tags"])
    mail.take_wrap(m3, msg)  # again after a restart: not sent twice
    assert len(sent) == 1


def test_a_seal_by_a_key_not_on_the_list_is_refused(m3, monkeypatch):
    m3.relay.events.append(device_list(ANA_PUB, [DEV_PUB]))
    monkeypatch.setattr(mail, "smtp_send", lambda *a: pytest.fail("must not send"))
    with pytest.raises(ValueError):
        mail.handle_encrypted(m3, outgoing(m3, signer=PrivateKey()))
    # Ana's own account key as the seal's signer (not on her list): refused too
    with pytest.raises(ValueError):
        mail.handle_encrypted(m3, outgoing(m3, signer=ANA, email_id="o2"))
    # a key Ana's phone made, a message sealed by a key on none of her lists and dated after
    # her newest list: her list may be behind (§20), so it waits, and is never sent meanwhile
    with pytest.raises(mail.Later):
        mail.handle_encrypted(m3, outgoing(m3, email_id="o4", sealer=PrivateKey()))
    assert mine(m3, 7679) == []


def test_mail_whose_key_or_list_is_not_there_yet_is_tried_again(m3, monkeypatch):
    m3.relay.events.append(device_list(ANA_PUB, [DEV_PUB]))
    monkeypatch.setattr(mail, "smtp_send", lambda *a: pytest.fail("must not send"))
    with pytest.raises(mail.Later):
        mail.handle_encrypted(m3, outgoing(m3, publish_envelopes=False))
    later = []
    monkeypatch.setattr(mail, "_later", lambda s, call: later.append(s))
    mail.take_wrap(m3, outgoing(m3, publish_envelopes=False, email_id="o3"))
    assert later == [mail.RETRY_AFTER[0]]


def test_a_message_not_for_the_belfry_is_dropped(m3):
    m3.relay.events.append(device_list(ANA_PUB, [DEV_PUB]))
    msg = outgoing(m3)
    other = next(x for x in mine(m3, 8572) if x is not msg)
    with pytest.raises(ValueError):
        mail.handle_encrypted(m3, other)


# ── in: a private group's shared inbox ──────────────────────────────────────────────────────
class GroupsRelay:
    url = "ws://groups.test"

    def __init__(self):
        self.events: list[dict] = []

    def publish(self, e, sign_):
        self.events.append(e)

    def query(self, filters, sign_):
        return []


@pytest.fixture
def private(m3, inbox, monkeypatch):
    relay = GroupsRelay()
    m3.relay.events.append(device_list(ANA_PUB, [DEV_PUB]))
    monkeypatch.setattr(followers, "_relay", lambda svc: (relay, lambda t: sign(GROUPS.secret, t)))
    monkeypatch.setattr(followers, "_meta_tags", lambda svc, ch: {"d", "name", "private", "closed"})
    monkeypatch.setattr(enc, "group_members", lambda svc, g: {ANA_PUB, GROUPS_PUB})
    monkeypatch.setattr(
        mail.drops,
        "upload",
        lambda svc, files: [
            ["imeta", f"url https://blossom.test/{i}"] for i, _ in enumerate(files)
        ],
    )
    monkeypatch.setattr(mail, "_original", lambda svc, raw: [])
    return relay


def test_an_inbox_card_in_a_private_group_is_encrypted_by_the_groups_key(m3, inbox, private):
    from .test_mail import _gmail_with_picture_and_pdf

    code, _ = mail.deliver(
        m3, _gmail_with_picture_and_pdf(), "203.0.113.5", "h", "", ["garden@belfry.test"]
    )
    assert code == 250
    assert inbox == []  # nothing in clear
    [env, msg] = private.events
    assert env["kind"] == 8571 and env["pubkey"] == GROUPS_PUB and mail.tag(env, "h") == "garden"
    assert (
        msg["kind"] == 8572
        and msg["pubkey"] == GROUPS_PUB
        and mail.tag(msg, "d") == mail.tag(env, "d")
    )
    assert set(json.loads(env["content"])) == {DEV_PUB}  # the groups key keeps no entry
    key = enc.open_group_envelope(DEV.secret, env, "garden")
    assert key.maker == GROUPS_PUB and enc.accept_group_key(key, enc.SERVICE) == "ok"
    assert enc.device_set(enc.entry_keys([env])) == key.set
    seal, rumor = enc.open_group_message(key.k, msg)
    assert enc.accept_group_message(msg, seal, rumor, "garden", enc.SERVICE) == "ok"
    assert rumor["kind"] == 9 and mail.tag(rumor, "mail-from") == "xavi@example.org"
    files = [t for t in rumor["tags"] if t[0] == "imeta"]
    assert len(files) == 2 and all(any(p.startswith("decryption-key ") for p in t) for t in files)
    assert all(any(p.startswith("ox ") for p in t) for t in files)
    assert any("m image/png" in t for t in files)


def test_a_members_mail_to_a_private_group_without_inbox_is_refused(
    m3, inbox, private, monkeypatch
):
    monkeypatch.setattr(mail, "inbox_on", lambda svc, ch: False)
    raw = message(to="garden@belfry.test")
    code, _ = mail.deliver(m3, raw, "203.0.113.5", "h", "", ["garden@belfry.test"])
    assert code == 550 and private.events == [] and inbox == []
