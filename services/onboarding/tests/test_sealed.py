"""Sealed conversations (sealed.py, docs/sealed.md): the shared vectors, and mail in and out."""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest
from coincurve import PrivateKey

from belfry_onboarding import apps, discovery, drops, mail, sealed
from belfry_onboarding.nip46check import conversation_key, nip44_decrypt

from .test_mail import ANA, ANA_PUB, BELFRY, BELFRY_PUB, message

ROOT = Path(__file__).resolve().parents[3]
VECTORS = ROOT / "docs" / "sealed-vectors.json"
TOOL = ROOT / "services" / "onboarding" / "tools" / "sealed_vectors.py"


def tool():
    spec = importlib.util.spec_from_file_location("sealed_vectors", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def v():
    return json.loads(VECTORS.read_text())


def secret(v, name: str) -> bytes:
    return bytes.fromhex(v["keys"][name]["secret"])


def chain_named(v, name: str | None):
    if name is None:
        return None
    case = next(c for c in v["chains"] if c["name"] == name)
    return sealed.chain(case["anchor"], case["rosters"])


def summary(c):
    if c is None:
        return {"valid_ids": [], "head": None, "devices": [], "lb": None, "ready": False}
    return {
        "valid_ids": c.valid_ids,
        "head": c.head.id,
        "devices": list(c.devices),
        "lb": c.lb,
        "ready": c.ready,
    }


# ── the vectors ─────────────────────────────────────────────────────────────────────────────
def test_the_vectors_file_is_what_the_tool_writes():
    """Deterministic: fixed keys, nonces, times. Regenerate with tools/sealed_vectors.py."""
    made = tool().text()
    assert made == tool().text()
    assert VECTORS.read_text() == made


def test_the_keys_are_what_they_say(v):
    for pair in v["keys"].values():
        assert sealed.pubkey_of(bytes.fromhex(pair["secret"])) == pair["pubkey"]


def test_every_chain_case(v):
    assert len(v["chains"]) >= 14
    assert len({c["name"] for c in v["chains"]}) == len(v["chains"])
    for case in v["chains"]:
        got = sealed.chain(case["anchor"], case["rosters"])
        assert summary(got) == case["expected"], case["name"]
        # the order the relay gives them in does not matter
        back = sealed.chain(case["anchor"], list(reversed(case["rosters"])))
        assert summary(back) == case["expected"], case["name"]


def test_every_wrap_case(v):
    assert len(v["wraps"]) >= 6
    for case in v["wraps"]:
        want = case["expected"]
        key = bytes.fromhex(case["letterbox_secret"])
        if not want["opens"]:
            with pytest.raises(ValueError):
                sealed.open_sealed(key, case["wrap"])
            continue
        seal, rumor = sealed.open_sealed(key, case["wrap"])
        events = case.get("author_chain_events")
        chain = (
            sealed.chain(events["anchor"], events["rosters"])
            if events
            else chain_named(v, case["author_chain"])
        )
        assert seal["pubkey"] == want["seal_pubkey"], case["name"]
        assert rumor == want["rumor"], case["name"]
        assert sealed.accept(chain, seal, rumor, case["belfry"]) is want["accepted"], case["name"]
        assert ["lb", sealed.pubkey_of(key)] in case["wrap"]["tags"]


def test_a_wrap_is_not_accepted_before_its_authors_chain_is_known(v):
    case = v["wraps"][0]
    seal, rumor = sealed.open_sealed(bytes.fromhex(case["letterbox_secret"]), case["wrap"])
    assert sealed.accept(None, seal, rumor, case["belfry"]) is False


def test_the_ceiling(v):
    c = v["ceiling"]
    assert c["max_rumor_json_bytes"] == sealed.MAX_RUMOR == 40_000
    for case in c["cases"]:
        r = dict(case["rumor"])
        r["content"] = r["content"]["repeat"] * r["content"]["times"]
        r = sealed.with_id(r)
        assert r["id"] == case["rumor_id"]
        assert len(sealed.rumor_json(r).encode()) == case["rumor_json_bytes"]
        assert sealed.fits(r) is case["may_seal"], case["name"]
        if not case["may_seal"]:
            with pytest.raises(ValueError):
                sealed.seal_to_letterbox(
                    secret(v, "alice_phone"),
                    r,
                    v["keys"]["bob"]["pubkey"],
                    v["keys"]["bob_lb1"]["pubkey"],
                )
    # a kind 8564 is packed until its own JSON would pass the ceiling
    t = tool()
    want = c["history_packing"]["expected"]
    k = {n: p["pubkey"] for n, p in v["keys"].items()}
    shared = [
        sealed.with_id(
            {
                "pubkey": k["alice" if i % 2 else "bob"],
                "created_at": t.T0 + i,
                "kind": 14,
                "tags": [["p", k["bob" if i % 2 else "alice"]]],
                "content": f'Message {i}: "quoted" \\ and é',
            }
        )
        for i in range(300)
    ]
    big = sealed.with_id({**shared[150], "content": "x" * 39_700})
    packed = sealed.pack_history(
        k["bob"], k["alice"], [*shared[:150], big, *shared[150:]], t.T0 + 800
    )
    assert [p["id"] for p in packed] == want["ids_of_8564"]
    assert [len(sealed.rumor_json(p).encode()) for p in packed] == want["json_bytes_per_8564"]
    assert [len(json.loads(p["content"])["rumors"]) for p in packed] == want["rumors_per_8564"]
    assert want["left_out"] == [big["id"]] and all(sealed.fits(p) for p in packed)


def test_every_hand_over_case(v):
    assert len(v["keys_handover"]) >= 3
    for case in v["keys_handover"]:
        seal, rumor = sealed.open_keys(bytes.fromhex(case["device_secret"]), case["wrap"])
        assert seal["pubkey"] == case["expected"]["giver"]
        assert rumor == case["expected"]["rumor"]
        chain = chain_named(v, case["chain"])
        got = sealed.accept_keys(case["account"], [chain], seal, rumor)
        assert got == case["expected"]["keys"], case["name"]
        for key in got:
            assert sealed.pubkey_of(bytes.fromhex(key["secret"])) == key["lb"]
        # not for another device, and not without the account's chain
        with pytest.raises(ValueError):
            sealed.open_keys(secret(v, "alice_phone"), case["wrap"])
        assert sealed.accept_keys(case["account"], [], seal, rumor) == []


def test_every_link_case(v):
    for case in v["link"]:
        req = case["request"]
        assert (
            sealed.link_proof(case["link_secret"], req["pubkey"])
            == case["expected"]["link_for_this_device"]
        )
        assert sealed.link_ok(case["link_secret"], req) is case["expected"]["valid"], case["name"]


def test_every_history_case(v):
    for case in v["history"]:
        seal, rumor = sealed.open_sealed(bytes.fromhex(case["letterbox_secret"]), case["wrap"])
        events = case["sharer_chain_events"]
        chain = sealed.chain(events["anchor"], events["rosters"])
        assert rumor["kind"] == sealed.HISTORY and rumor["pubkey"] == case["expected"]["sharer"]
        assert seal["pubkey"] == case["expected"]["seal_pubkey"]
        assert sealed.accept(chain, seal, rumor)
        got = [r["id"] for r in sealed.shared_rumors(rumor)]
        assert got == case["expected"]["added_ids"], case["name"]


def test_every_ready_case(v):
    r = v["ready"]
    for case in r["cases"]:
        got = sealed.may_be_ready(case["sessions"], case["roster_devices"], r["now"])
        assert got is case["expected"]["may_carry_ready"], case["name"]


def test_an_event_is_checked_by_its_hash_not_the_id_it_carries(v):
    case = next(c for c in v["chains"] if c["name"] == "a valid root")
    e = case["rosters"][0]
    assert sealed.valid_event(e)
    assert not sealed.valid_event({**e, "content": "x"})  # the old id and signature
    assert not sealed.valid_event({**e, "id": e["id"].upper()})
    assert not sealed.valid_event("nope") and not sealed.valid_event({})


def test_who_added_and_who_dropped_a_device(v):
    c = chain_named(v, "a device is removed, with a new letterbox")
    k = {n: p["pubkey"] for n, p in v["keys"].items()}
    assert c.added[k["alice_phone"]].id == c.root.id
    assert c.added[k["alice_laptop"]].n == 2
    assert c.added[k["alice_laptop"]].signer == k["alice_phone"]  # "Added from"
    assert c.dropped[k["alice_laptop"]].id == c.head.id and k["alice_phone"] not in c.dropped
    assert c.all_devices == {k["alice_phone"], k["alice_laptop"]}
    assert c.has_device(k["alice_laptop"], c.head.created_at)
    assert not c.has_device(k["alice_laptop"], c.head.created_at + 1)
    assert not c.has_device(k["mallory_phone"], 0)


# ── mail, with a letterbox ──────────────────────────────────────────────────────────────────
PHONE = PrivateKey(b"\x11" * 32)
LAPTOP = PrivateKey(b"\x12" * 32)
LB = PrivateKey(b"\x13" * 32)
STRANGER = PrivateKey(b"\x14" * 32)


def pub(key: PrivateKey) -> str:
    return key.public_key_xonly.format().hex()


@pytest.fixture(autouse=True)
def no_chain_kept():
    """The chain cache is the module's: nothing of one test reaches another (or another file)."""
    sealed._cache.clear()
    yield
    sealed._cache.clear()


@pytest.fixture
def m(services, odoo, keycast, monkeypatch):
    s = services.settings
    s.nip05_domain = "belfry.test"
    s.belfry_nsec = BELFRY.secret.hex()
    s.community_relay_public_url = "wss://relay.belfry.test"
    services.relay.require_auth = False
    mail._cache.clear()
    mail._counts.clear()
    sealed._cache.clear()
    names = {"ana": ANA_PUB}
    monkeypatch.setattr(services.keycast, "pubkey_for_name", lambda n: names.get(n))
    monkeypatch.setattr(mail, "active_pubkey", lambda svc, p: p == ANA_PUB)
    monkeypatch.setattr(mail, "channel_exists", lambda svc, c: c in {"garden"})
    monkeypatch.setattr(apps, "admin_pubkeys", lambda svc: {ANA_PUB})
    # Ana in the register and in the signer, as discovery.py's /api/me/sessions finds her
    partner = odoo.add(
        "res.partner", name="Ana", email="ana@example.org", belfry_member_number="M-1"
    )
    odoo.add("belfry.pass", partner_id=partner, tier="week", state="active", keycast_pubkey=ANA_PUB)
    keycast.users[f"odoo-partner-{partner}"] = {"pubkey": ANA_PUB, "username": "ana"}
    keycast.last_pubkey = ANA_PUB
    return services


def publish_chain(m, ready: bool, devices=(PHONE,)) -> None:
    """Ana's chain on the relay: a root by her phone, anchored by her account."""
    now = int(time.time())
    root = sealed.sign(
        PHONE.secret,
        {
            "kind": sealed.ROSTER,
            "created_at": now - 100,
            "tags": [
                ["p", ANA_PUB],
                ["n", "1"],
                ["lb", pub(LB)],
                ["device", pub(PHONE)],
                *([["ready"]] if ready and len(devices) == 1 else []),
            ],
            "content": "",
        },
    )
    events = [root]
    if len(devices) > 1:
        events.append(
            sealed.sign(
                PHONE.secret,
                {
                    "kind": sealed.ROSTER,
                    "created_at": now - 50,
                    "tags": [
                        ["p", ANA_PUB],
                        ["n", "2"],
                        ["prev", root["id"]],
                        ["lb", pub(LB)],
                        *[["device", pub(d)] for d in devices],
                        *([["ready"]] if ready else []),
                    ],
                    "content": "",
                },
            )
        )
    anchor = sealed.sign(
        ANA.secret,
        {
            "kind": sealed.ANCHOR,
            "created_at": now - 99,
            "tags": [["d", sealed.ANCHOR_D], ["root", root["id"]]],
            "content": "",
        },
    )
    m.relay.events += [anchor, *events]
    sealed._cache.clear()


def deliver(m, raw=None):
    return mail.deliver(
        m, raw or message(), "203.0.113.5", "mx.example.org", "bob@example.org", ["ana@belfry.test"]
    )


def the_wrap(m):
    [wrap] = [e for e in m.relay.events if e["kind"] == 1059]
    return wrap


def test_mail_to_a_member_with_a_ready_chain_is_sealed_to_their_letterbox(m):
    publish_chain(m, ready=True)
    assert deliver(m)[0] == 250
    wrap = the_wrap(m)
    assert ["p", ANA_PUB] in wrap["tags"] and ["lb", pub(LB)] in wrap["tags"]
    with pytest.raises(ValueError):
        mail.unwrap(ANA.secret, wrap)  # the account key (the signer) cannot open it
    seal, rumor = sealed.open_sealed(LB.secret, wrap)
    assert seal["pubkey"] == BELFRY_PUB == rumor["pubkey"] and seal["tags"] == []
    assert rumor["kind"] == 1301 and mail.tag(rumor, "mail-from") == "bob@example.org"
    assert "Subject: Hello" in rumor["content"]
    assert sealed.accept(None, seal, rumor, BELFRY_PUB)


def test_mail_to_a_member_whose_chain_is_not_ready_goes_as_before(m):
    publish_chain(m, ready=False)
    assert sealed.chain_for(m, ANA_PUB).ready is False
    assert deliver(m)[0] == 250
    wrap = the_wrap(m)
    assert wrap["tags"] == [["p", ANA_PUB]]
    assert mail.unwrap(ANA.secret, wrap)[1]["kind"] == 1301


def test_mail_to_a_member_without_a_chain_goes_as_before(m):
    assert sealed.chain_for(m, ANA_PUB) is None
    assert deliver(m)[0] == 250
    wrap = the_wrap(m)
    assert wrap["tags"] == [["p", ANA_PUB]]
    assert mail.unwrap(ANA.secret, wrap)[0] == BELFRY_PUB


def test_a_relay_that_does_not_answer_the_chain_question_does_not_stop_the_mail(m, monkeypatch):
    publish_chain(m, ready=True)

    def down(filters, sign):
        raise RuntimeError("relay down")

    monkeypatch.setattr(m.relay, "query", down)
    assert sealed.chain_for(m, ANA_PUB) is None  # never raises
    with pytest.raises(RuntimeError):
        sealed.chain_for(m, ANA_PUB, strict=True)
    assert deliver(m)[0] == 250
    assert the_wrap(m)["tags"] == [["p", ANA_PUB]]  # as before sealing


def test_the_chain_is_read_as_the_belfry_and_kept_a_minute(m, monkeypatch):
    publish_chain(m, ready=True)
    asked = []
    real = m.relay.query

    def query(filters, sign):
        probe = sign({"kind": 22242, "created_at": 0, "tags": [], "content": ""})
        asked.append((probe["pubkey"], filters))
        return real(filters, sign)

    monkeypatch.setattr(m.relay, "query", query)
    assert sealed.chain_for(m, ANA_PUB).lb == pub(LB)
    n = len(asked)
    assert n >= 2 and {who for who, _ in asked} == {BELFRY_PUB}
    assert all("since" not in f for _, fs in asked for f in fs)
    assert sealed.chain_for(m, ANA_PUB).lb == pub(LB) and len(asked) == n  # the cache
    assert sealed.chain_for(m, "nonsense") is None


def test_a_sealed_mail_too_big_for_a_seal_goes_to_blossom(m, monkeypatch):
    """A sealed rumor is at most 40 000 bytes of JSON: over it, the Blossom route, although the
    unsealed limit (60 kB of mail) is not reached."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    stored = {}

    def upload(svc, files):
        stored["blob"] = files[0][2]
        return [["imeta", "url https://blossom.belfry.test/abc", "m application/octet-stream"]]

    monkeypatch.setattr(drops, "upload", upload)
    publish_chain(m, ready=True)
    raw = message(body="line of text\r\n" * 3200)  # 45 kB, and more once the line ends are escaped
    assert 40_000 < len(raw) < mail.INLINE
    assert deliver(m, raw)[0] == 250
    seal, rumor = sealed.open_sealed(LB.secret, the_wrap(m))
    assert rumor["content"] == "" and mail.tag(rumor, "encryption-algorithm") == "aes-gcm"
    import base64

    key = base64.b64decode(mail.tag(rumor, "decryption-key"))
    nonce = base64.b64decode(mail.tag(rumor, "decryption-nonce"))
    assert AESGCM(key).decrypt(nonce, stored["blob"], None) == raw
    # a small one stays inline
    m.relay.events[:] = [e for e in m.relay.events if e["kind"] != 1059]
    stored.clear()
    assert deliver(m, message(body="x" * 20_000))[0] == 250
    assert sealed.open_sealed(LB.secret, the_wrap(m))[1]["content"] and not stored


# ── mail out, sealed by a device ────────────────────────────────────────────────────────────
def session(m, device: PrivateKey, name="Chat · Firefox on Linux") -> dict:
    return m.keycast.create_bunker("ucan-token", name, pub(device))


def out_wrap(signer: PrivateKey, email_id="d1", frm="ana@belfry.test", author=ANA_PUB):
    raw = f"From: Ana <{frm}>\r\nTo: bob@example.org\r\nSubject: Re: Hello\r\n\r\nThanks!\r\n"
    rumor = {
        "kind": 1301,
        "pubkey": author,
        "created_at": int(time.time()),
        "content": raw,
        "tags": [["email-id", email_id], ["mail-from", frm], ["rcpt-to", "bob@example.org"]],
    }
    # §7 Out: the wrap is the old kind (to the belfry's key, no lb); only the seal's signer differs
    return sealed._wrap(signer.secret, rumor, BELFRY_PUB, [["p", BELFRY_PUB]])


def dsn(m, email_id):
    [e] = [e for e in m.relay.events if e["kind"] == 7679 and ["r", email_id] in e["tags"]]
    assert e["pubkey"] == BELFRY_PUB and not any(t[0] == "p" for t in e["tags"])
    return e


def read_dsn(e, secret: bytes) -> dict:
    temp = mail.tag(e, "ephemeral-pubkey")
    return json.loads(nip44_decrypt(conversation_key(secret, bytes.fromhex(temp)), e["content"]))


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(
        mail, "smtp_send", lambda svc, frm, rcpts, data: out.append((frm, rcpts)) or (250, "OK")
    )
    return out


def test_a_device_seals_outgoing_mail_and_the_report_goes_to_the_letterbox(m, sent):
    publish_chain(m, ready=True)
    session(m, PHONE)
    mail.handle_wrap(m, out_wrap(PHONE))
    assert sent == [("ana@belfry.test", ["bob@example.org"])]
    e = dsn(m, "d1")
    assert ["lb", pub(LB)] in e["tags"]
    assert read_dsn(e, LB.secret)["status"] == "delivered"
    with pytest.raises(ValueError):
        read_dsn(e, ANA.secret)  # not for the account key any more
    mail.handle_wrap(m, out_wrap(PHONE))  # the relay sends it again after a restart
    assert len(sent) == 1


def test_a_device_of_a_chain_that_is_not_ready_may_seal_and_the_report_goes_as_before(m, sent):
    publish_chain(m, ready=False)
    session(m, PHONE)
    mail.handle_wrap(m, out_wrap(PHONE))
    assert len(sent) == 1
    e = dsn(m, "d1")
    assert not any(t[0] == "lb" for t in e["tags"])
    assert read_dsn(e, ANA.secret)["status"] == "delivered"


def test_the_account_key_still_seals_outgoing_mail(m, sent):
    publish_chain(m, ready=True)
    mail.handle_wrap(m, mail.gift_wrap(ANA.secret, json_rumor("a1"), BELFRY_PUB))
    assert len(sent) == 1
    assert read_dsn(dsn(m, "a1"), LB.secret)["status"] == "delivered"


def json_rumor(email_id):
    return {
        "kind": 1301,
        "pubkey": ANA_PUB,
        "created_at": int(time.time()),
        "content": "From: Ana <ana@belfry.test>\r\nTo: bob@example.org\r\n\r\nHi\r\n",
        "tags": [
            ["email-id", email_id],
            ["mail-from", "ana@belfry.test"],
            ["rcpt-to", "bob@example.org"],
        ],
    }


def refused(m, wrap) -> None:
    """Dropped like any wrap that is not its author's: no mail, no report."""
    with pytest.raises(ValueError):
        mail.handle_wrap(m, wrap)
    assert not [e for e in m.relay.events if e["kind"] == 7679]


def test_a_key_that_is_not_a_device_of_the_member_seals_nothing(m, sent):
    publish_chain(m, ready=True)
    session(m, PHONE)
    session(m, STRANGER)  # a live chat session, but no device of the chain
    refused(m, out_wrap(STRANGER))
    assert not sent


def test_a_device_without_a_chain_seals_nothing(m, sent):
    session(m, PHONE)
    refused(m, out_wrap(PHONE))
    assert not sent


def test_a_device_must_be_a_live_chat_session(m, sent, keycast):
    publish_chain(m, ready=True, devices=(PHONE, LAPTOP))
    refused(m, out_wrap(PHONE))  # in the chain, no session at all
    session(m, LAPTOP, "Notes · Firefox on Linux")  # another app's session with that key
    refused(m, out_wrap(LAPTOP))
    made = session(m, PHONE)
    assert sealed.live_devices(m, ANA_PUB) == {pub(PHONE)}
    m.keycast.revoke_session("ucan-token", made["bunker_pubkey"])
    refused(m, out_wrap(PHONE))  # signed out
    assert not sent
    session(m, PHONE, "Belfry for iPhone")
    mail.handle_wrap(m, out_wrap(PHONE))
    assert len(sent) == 1


def test_a_device_dropped_from_the_chain_seals_nothing(m, sent):
    """Only the head's devices send: a device of an earlier roster does not, whatever its date."""
    publish_chain(m, ready=True, devices=(PHONE, LAPTOP))
    now = int(time.time())
    head = sealed.chain_for(m, ANA_PUB).head
    m.relay.events.append(
        sealed.sign(
            PHONE.secret,
            {
                "kind": sealed.ROSTER,
                "created_at": now,
                "tags": [
                    ["p", ANA_PUB],
                    ["n", "3"],
                    ["prev", head.id],
                    ["lb", pub(PrivateKey(b"\x15" * 32))],
                    ["device", pub(PHONE)],
                ],
                "content": "",
            },
        )
    )
    sealed._cache.clear()
    session(m, LAPTOP)
    refused(m, out_wrap(LAPTOP))
    assert not sent


def test_a_device_speaks_only_for_its_own_account(m, sent):
    """Ana's phone seals a rumor that says it is someone else's."""
    publish_chain(m, ready=True)
    session(m, PHONE)
    other = pub(PrivateKey(b"\x16" * 32))
    refused(m, out_wrap(PHONE, author=other))
    assert not sent


def test_a_signer_or_relay_that_does_not_answer_leaves_the_mail_for_later(m, sent, monkeypatch):
    publish_chain(m, ready=True)
    session(m, PHONE)
    monkeypatch.setattr(m.keycast, "list_sessions", lambda token: 1 / 0)
    with pytest.raises(ZeroDivisionError):  # not a ValueError: logged, tried again later
        mail.handle_wrap(m, out_wrap(PHONE))
    assert not sent and not [e for e in m.relay.events if e["kind"] == 7679]


def test_a_device_answers_as_the_shared_inbox(m, sent, monkeypatch):
    posted = []
    monkeypatch.setattr(
        drops, "post", lambda svc, ch, text, tags, partner, client="": posted.append((ch, client))
    )
    monkeypatch.setattr(mail, "inbox_on", lambda svc, ch: ch == "garden")
    monkeypatch.setattr(mail, "group_admins", lambda svc, ch: {ANA_PUB})
    publish_chain(m, ready=True)
    session(m, PHONE)
    mail.handle_wrap(m, out_wrap(PHONE, "g1", frm="garden@belfry.test"))
    assert sent == [("garden@belfry.test", ["bob@example.org"])]
    assert posted == [("garden", "belfry-mail-out")]
    # and not for someone who is no admin of it
    monkeypatch.setattr(mail, "group_admins", lambda svc, ch: set())
    mail.handle_wrap(m, out_wrap(PHONE, "g2", frm="garden@belfry.test"))
    assert len(sent) == 1
    assert read_dsn(dsn(m, "g2"), LB.secret)["status"] == "failed"


# ── /api/me/sessions says which sessions are the belfry's own chat ──────────────────────────
def test_the_sessions_list_says_which_are_chat_sessions(m, odoo, keycast, client):
    from .test_holder_invites import get

    m.settings.public_url = "https://pass.belfry.test"
    key = PrivateKey()
    pk = pub(key)
    partner = odoo.add(
        "res.partner", name="Cleo", email="cleo@example.org", belfry_member_number="M-7"
    )
    odoo.add("belfry.pass", partner_id=partner, tier="week", state="active", keycast_pubkey=pk)
    keycast.users[f"odoo-partner-{partner}"] = {"pubkey": pk, "username": "cleo"}
    keycast.last_pubkey = pk
    return_names = [
        ("Chat · Firefox on Linux", True),
        ("Chat", True),
        ("Chat (guest)", True),
        ("Belfry for iPhone", True),
        ("Belfry for Android · Chrome on Android", True),
        ("Belfry app", True),
        ("Notes", False),
        ("Tickets", False),
        ("Setup page (first session)", False),
        ("chat.example.org · Safari on Mac", False),
    ]
    for name, _ in return_names:
        m.keycast.create_bunker("ucan-token", name, pub(PrivateKey()))
    r = get(client, key, "/api/me/sessions")
    assert r.status_code == 200, r.text
    rows = r.json()["sessions"]
    assert [s["chat"] for s in rows] == [want for _, want in return_names]
    # nothing that was there before changed or left (the phone apps decode the list strictly)
    assert set(rows[0]) == {
        "application_name",
        "redirect_origin",
        "bunker_pubkey",
        "client_pubkey",
        "created_at",
        "last_activity",
        "activity_count",
        "device",
        "chat",
    }
    assert rows[0]["application_name"] == "Chat" and rows[0]["device"] == "Firefox on Linux"


def test_a_session_opened_from_elsewhere_is_no_chat_session(m):
    chat = {"application_name": "Chat · Safari on Mac", "redirect_origin": ""}
    assert discovery.chat_session(m, chat)
    assert discovery.chat_session(m, {**chat, "redirect_origin": "https://chat.belfry.test"})
    assert discovery.chat_session(m, {**chat, "redirect_origin": None})
    assert not discovery.chat_session(m, {**chat, "redirect_origin": "https://evil.example"})
    assert not discovery.chat_session(m, {"application_name": None})


def test_no_other_app_gets_a_chat_sessions_name(m, monkeypatch):
    """An admin may name an approved app "Chat": its sessions are called by its address, so the
    signer's list never shows it as one of the belfry's own chat clients."""
    other = "https://chat.example.org/"
    named = {other: "Chat"}
    monkeypatch.setattr(apps, "display_name", lambda svc, uri: named.get(uri, "Chat"))
    assert apps.session_label(m, other) == "chat.example.org"
    assert apps.session_label(m, "https://chat.belfry.test/") == "Chat"
    assert apps.session_label(m, "sh.belfry.app://connect", "belfry-ios") == "Belfry for iPhone"
    assert apps.session_label(m, "sh.belfry.app://connect", "x") == "Belfry app"
    named[other] = "Notes"
    assert apps.session_label(m, other) == "Notes"
