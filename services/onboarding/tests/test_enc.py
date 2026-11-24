"""Format 3 rules (enc.py) against the shared vectors (tests/data/encrypted.json = the web chat's
vectors/encrypted.json, branch enc-v3, sha256 16efbbc8…42fe): what the server reads is what the
apps read, and what it writes is byte for byte what the web writes for the same keys, nonces and
dates."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from belfry_onboarding import enc
from belfry_onboarding.sealed import sign, with_id

V = json.loads((Path(__file__).parent / "data" / "encrypted.json").read_text())
B, K = V["belfry"], V["keys"]
AUX = bytes(32)


def test_the_vectors_are_the_reviewed_ones():
    raw = (Path(__file__).parent / "data" / "encrypted.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest().startswith("16efbbc8")


def sk(name: str) -> bytes:
    return hashlib.sha256(f"belfry-vector:{name}".encode()).digest()


def fixed(label: str, at: int, seal_at: int | None = None) -> enc.Fixed:
    return enc.Fixed(
        at=at,
        seal_at=at - 100 if seal_at is None else seal_at,
        one_time=sk("once:" + label),
        nonce=lambda i: hashlib.sha256(f"nonce:{label}:{i}".encode()).digest(),
        aux=AUX,
    )


def chain(events, account=K["sam"]):
    return enc.chain_of(events, B, account)


def chains(events):
    return lambda a: enc.SERVICE if a == B else chain(events, a)


def view(c):
    if c is None:
        return None
    cur = enc.current(c)
    return {
        "lists": [x.id for x in c.lists],
        "current": cur.id,
        "devices": cur.devices,
        "readers": cur.readers,
        "gaps": c.gaps,
        "fork": c.fork,
        "invalidAbove": c.invalid_above,
    }


# ── lists ───────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", sorted(V["lists"]["valid"]))
def test_lists_valid(name):
    c = V["lists"]["valid"][name]
    got = enc.parse_list(c["event"], B)
    assert (got is not None) == c["valid"]
    if got:
        assert (len(got.devices), len(got.readers)) == (c["devices"], c["readers"])


@pytest.mark.parametrize("i", range(len(V["lists"]["chains"])))
def test_lists_chains(i):
    c = V["lists"]["chains"][i]
    assert view(chain(c["events"], c["account"])) == c["expect"]


def test_invalid_above():
    c = chain(V["invalid_above"]["events"])
    assert c.invalid_above == V["invalid_above"]["invalid_above"]
    assert view(c) == V["invalid_above"]["expect"]


SIGNS = ["signs_at", "signs_at_gap", "signs_at_relisted", "signs_at_slow_clock", "signs_at_behind"]


@pytest.mark.parametrize("section", SIGNS)
def test_signs_at(section):
    c = chain(V[section]["lists"])
    for x in V[section]["cases"]:
        assert enc.signs_at(c, x["device"], x["at"]) == x["expect"], x


@pytest.mark.parametrize("section", ["listed_at", "listed_at_gap"])
def test_listed_at(section):
    c = chain(V[section]["lists"])
    for x in V[section]["cases"]:
        assert enc.listed_at(c, x["device"], x["at"]) == x["expect"], x


def test_group_key_slow_clock():
    for x in V["group_key_slow_clock"]:
        key = enc.GroupKey(
            "1" * 64, "a" * 64, "pod-garden-7f3a", x["maker"], x["account"], x["at"], "b" * 64
        )
        assert enc.accept_group_key(key, chain(x["lists"], x["account"]), x["now"]) == x["expect"]


# ── names ───────────────────────────────────────────────────────────────────────────────────
def test_alias():
    for x in V["alias"]:
        assert enc.alias(x["K"], x["account"]) == x["expect"]


def test_lists_hash():
    lh = V["lists_hash"]
    assert enc.lists_hash([(x["account"], x["id"]) for x in lh["lists"]]) == lh["expect"]


def test_people_of():
    for x in V["people_of"]:
        assert enc.people_of(x["rumor"]) == x["expect"], x["name"]


# ── people keys ─────────────────────────────────────────────────────────────────────────────
PK = V["people_key"]


@pytest.mark.parametrize("i", range(len(PK["open"])))
def test_people_envelope_open(i):
    x = PK["open"][i]
    try:
        o = enc.open_people_envelope(
            bytes.fromhex(V["secrets"][x["device"]]), PK["envelopes"][x["envelope"]]
        )
        got = o and {"rumor_id": o[1]["id"], "K": next(t[1] for t in o[1]["tags"] if t[0] == "K")}
    except ValueError:
        got = {"error": True}
    assert got == x["expect"]


@pytest.mark.parametrize("i", range(len(PK["accept"])))
def test_people_key_accept(i):
    x = PK["accept"][i]
    got = enc.accept_key_rumor(with_id(x["rumor"]), x["receiver"], chains(x["lists"]), x["now"])
    assert got == x["expect"], x["name"]


def test_people_envelopes_are_written_as_the_web_writes_them():
    c3 = chain(V["signs_at"]["lists"])
    envs = enc.people_envelopes(
        sk("d2"),
        PK["key_rumor"],
        [(K["sam"], enc.receivers(enc.current(c3))), (K["lina"], [K["c1"]])],
        fixed("env1", 3100),
    )
    assert envs == PK["envelopes"]


def test_the_key_rumor_is_written_as_the_web_writes_it():
    kr = PK["key_rumor"]
    lists = [t for t in kr["tags"] if t[0] == "list"]
    named = [
        (t[1], next((x[2] for x in lists if x[1] == t[1]), None)) for t in kr["tags"] if t[0] == "p"
    ]
    assert enc.key_rumor(kr["pubkey"], kr["content"], named, kr["created_at"]) == kr


# ── group keys ──────────────────────────────────────────────────────────────────────────────
GK = V["group_key"]


def test_group_envelopes_split_and_open():
    assert len(GK["envelopes"]) == GK["expect_events"]
    assert all(len(e["content"].encode()) <= GK["max_content_bytes"] for e in GK["envelopes"])
    for x in GK["find"]:
        if x["device"] not in V["secrets"]:
            continue
        got = None
        for i, e in enumerate(GK["envelopes"]):
            g = enc.open_group_envelope(bytes.fromhex(V["secrets"][x["device"]]), e, GK["group"])
            if g:
                got = {"envelope": i, "K": g.K, "maker": g.maker}
                break
        assert got == x["expect"], x["device"]
    with pytest.raises(ValueError):
        enc.open_group_envelope(
            bytes.fromhex(V["secrets"]["c1"]), GK["envelopes"][0], GK["wrong_group"]["group"]
        )


def test_group_set_hash():
    g = enc.open_group_envelope(bytes.fromhex(V["secrets"]["c1"]), GK["envelopes"][0], GK["group"])
    assert g.set == GK["expect_set"]
    assert (enc.device_set(enc.entry_keys(GK["envelopes"])) == g.set) == GK["expect_set_all_events"]
    first = enc.device_set(enc.entry_keys(GK["envelopes"][:1])) == g.set
    assert first == GK["expect_set_first_event_only"]
    for x in GK["accept"]:
        key = enc.GroupKey(g.k, g.K, g.group, x["maker"], g.account, x["at"], g.set)
        assert enc.accept_group_key(key, chain(x["lists"], x["account"]), x["now"]) == x["expect"]


def test_group_envelopes_are_written_as_the_web_writes_them():
    gdevs = [K["d2"], K["c1"], *[enc.pubkey_of(sk(f"g{i}")) for i in range(300)]]
    sigs = V["signs_at"]["lists"]
    l3 = next(e["id"] for e in sigs if ["n", "3"] in e["tags"])
    lc1 = next(
        e["id"] for e in V["people_key"]["accept"][0]["lists"] if ["p", K["lina"]] in e["tags"]
    )
    g = enc.open_group_envelope(bytes.fromhex(V["secrets"]["c1"]), GK["envelopes"][0], GK["group"])
    templates = enc.group_envelopes(
        sk("d2"),
        g.k,
        GK["group"],
        gdevs,
        enc.lists_hash([(K["sam"], l3), (K["lina"], lc1)]),
        fixed("genv", 3200),
    )
    assert [sign(sk("sam"), t, AUX) for t in templates] == GK["envelopes"]


# ── messages ────────────────────────────────────────────────────────────────────────────────
PM = V["people_messages"]


def test_people_messages_open():
    assert [m["tags"] for m in PM["messages"]] == PM["expect_tags"]
    for m, x in zip(PM["messages"], PM["open"], strict=True):
        seal, rumor = enc.open_people_message(PM["k"], m)
        assert {"seal_signer": seal["pubkey"], "rumor_id": rumor["id"]} == x["expect"]
    with pytest.raises(ValueError):
        enc.open_people_message("11" * 32, PM["messages"][1])


@pytest.mark.parametrize("i", range(len(PM["accept"])))
def test_people_message_accept(i):
    x = PM["accept"][i]
    seal, rumor = enc.open_people_message(PM["k"], x["message"])
    lists = V["signs_at"]["lists"] if x.get("lists_without_lina") else PK["accept"][0]["lists"]
    got = enc.accept_people_message(
        x["message"],
        seal,
        rumor,
        x.get("key_people") or PM["key_people"],
        chains(lists)(rumor["pubkey"]),
    )
    assert got == x["expect"], x["name"]


def test_people_messages_are_written_as_the_web_writes_them():
    got = enc.people_message(
        sk("d2"), PM["rumor"], PM["K"], [K["sam"], K["lina"]], K["sam"], fixed("m1", 3300)
    )
    assert got == PM["messages"]


GM = V["group_messages"]


@pytest.mark.parametrize("i", range(len(GM["cases"])))
def test_group_message_accept(i):
    x = GM["cases"][i]
    seal, rumor = enc.open_group_message(GM["k"], x["message"])
    assert (
        enc.accept_group_message(
            x["message"], seal, rumor, GM["group"], chain(V["signs_at"]["lists"])
        )
        == x["expect"]
    )
    if x.get("expect_tags"):
        assert x["message"]["tags"] == x["expect_tags"]


def test_group_messages_are_written_as_the_web_writes_them():
    gr = with_id(
        {
            "kind": 9,
            "pubkey": K["sam"],
            "created_at": 3400,
            "tags": [["h", GM["group"]]],
            "content": "Seeds are in",
        }
    )
    assert gr["id"] == GM["rumor_id"]
    t = enc.group_message(sk("d2"), gr, GM["K"], GM["group"], fixed("gm", 3400), [K["lina"]])
    assert sign(sk("sam"), t, AUX) == GM["cases"][0]["message"]


# ── the belfry as a sender, read back by the rules ──────────────────────────────────────────
def test_a_service_key_people_message_opens_and_is_accepted_like_the_webs():
    """The belfry sends to Lina (list LC1): her device c1 finds its entry, takes the key (the
    belfry is a service key, its own device) and accepts the message."""
    belfry = sk("belfry")
    assert enc.pubkey_of(belfry) == B
    lists = PK["accept"][0]["lists"]
    sent = enc.people_events(
        None,
        belfry,
        {"kind": 1301, "created_at": 4000, "tags": [["p", K["lina"]]], "content": "x"},
        [K["lina"]],
        chains(lists),
    )
    assert len(sent.envelopes) == 1 and len(sent.messages) == 1
    env, msg = sent.envelopes[0], sent.messages[0]
    assert ["p", K["lina"]] in env["tags"] and ["p", K["lina"]] in msg["tags"]
    assert ["silent", "1"] not in msg["tags"]
    _, kr = enc.open_people_envelope(bytes.fromhex(V["secrets"]["c1"]), env)
    assert enc.accept_key_rumor(kr, K["lina"], chains(lists)) == "ok"
    assert ["list", K["lina"], next(e["id"] for e in lists if ["p", K["lina"]] in e["tags"])] in kr[
        "tags"
    ]
    seal, rumor = enc.open_people_message(kr["content"], msg)
    assert rumor["pubkey"] == B
    assert enc.accept_people_message(msg, seal, rumor, enc.people_of(kr), enc.SERVICE) == "ok"
    assert enc.people_of(rumor) == enc.people_id([B, K["lina"]])


def test_a_person_with_no_list_or_no_device_is_held():
    lists = PK["accept"][0]["lists"]
    r = {"kind": 1301, "created_at": 4000, "tags": [], "content": "x"}
    with pytest.raises(enc.Held):
        enc.people_events(None, sk("belfry"), r, [K["other"]], chains(lists))
    empty = sign(
        sk("belfry"),
        {"kind": 8570, "created_at": 1, "tags": [["p", K["other"]], ["n", "1"]], "content": ""},
        AUX,
    )
    with pytest.raises(enc.Held):
        enc.people_events(None, sk("belfry"), r, [K["other"]], chains([*lists, empty]))
