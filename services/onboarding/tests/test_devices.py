"""Device lists, kind 8570 (docs/encrypted.md §3, §16–§21; devices.py), and
GET /api/sessions/removed (§16.6)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import community, devices
from belfry_onboarding.app import create_app
from belfry_onboarding.sealed import valid_event

from .test_holder_invites import BASE, nip98, post

BELFRY = PrivateKey(b"\x31" * 32)
GROUPS = PrivateKey(b"\x32" * 32)
VECTORS = json.loads((Path(__file__).parent / "data" / "encrypted-lists.json").read_text())


def pub(key: PrivateKey) -> str:
    return key.public_key_xonly.format().hex()


BELFRY_PUB = pub(BELFRY)


# ── the rules a reader applies (the web's vectors, written again here from the contract) ────
def check_list(event: dict, belfry: str, account: str, prev: dict | None) -> list[str]:
    """Whether EVENT is a valid list ``n`` of ACCOUNT following PREV, as an app reads it, and
    as the belfry must write it: sorted plain devices, ≤ 20, ``self`` alone and only the
    account's own key, ``prev`` = the id of the list before exactly when n > 1. The device
    keys, or raises AssertionError."""
    assert event["kind"] == 8570 and event["pubkey"] == belfry and event["content"] == ""
    assert valid_event(event)
    tags = event["tags"]
    assert [t for t in tags if t[0] == "p"] == [["p", account]]
    [n] = [t[1] for t in tags if t[0] == "n"]
    assert n.isdigit() and int(n) >= 1 and str(int(n)) == n
    prevs = [t[1] for t in tags if t[0] == "prev"]
    if prev is None:
        assert n == "1" and prevs == []
    else:
        assert int(n) == int(next(t[1] for t in prev["tags"] if t[0] == "n")) + 1
        assert prevs == [prev["id"]]
    entries = [t for t in tags if t[0] == "device"]
    keys = [t[1] for t in entries]
    assert len(set(keys)) == len(keys)
    assert all(len(k) == 64 and int(k, 16) >= 0 for k in keys)
    plain = [t[1] for t in entries if len(t) == 2]
    selfs = [t for t in entries if len(t) > 2 and t[2] == "self"]
    assert plain == sorted(plain) and len(plain) <= 20
    if selfs:
        assert entries == [["device", account, "self"]]
    assert all(len(t) == 2 or t[2] in ("self", "reader") for t in entries)
    return keys


@pytest.mark.parametrize("name", sorted(VECTORS["valid"]))
def test_the_readers_rule_is_the_vectors(name):
    """The server's own reading (devices.parse) agrees with every list case of the web's
    vectors."""
    case = VECTORS["valid"][name]
    event = case["event"]
    got = devices.parse(event, VECTORS["belfry"])
    assert (got is not None) == case["valid"], name
    if got is not None:
        assert len(got["devices"]) + int(got["self"]) == case["devices"]
        assert len(got["readers"]) == case["readers"]


@pytest.mark.parametrize("i", range(len(VECTORS["chains"])))
def test_the_current_list_is_the_vectors(i, services, monkeypatch):
    """relay_latest picks the list the apps call current: the highest n, the lower id on a tie."""
    case = VECTORS["chains"][i]
    services.relay.events = list(case["events"])
    services.settings.belfry_nsec = "00" * 31 + "01"  # any key: the vectors' belfry is fixed
    monkeypatch.setattr(devices, "_belfry", lambda svc: _VectorKey())
    got = devices.relay_latest(services, case["account"])
    if case["expect"] is None:
        assert got is None
    else:
        assert got["id"] == case["expect"]["current"]


class _VectorKey:
    """Stands in for the belfry key in the vector tests: its pubkey is the vectors' belfry (the
    fake relay signs nothing it checks)."""

    secret = b"\x01" * 32

    class public_key_xonly:  # noqa: N801
        @staticmethod
        def format():
            return bytes.fromhex(VECTORS["belfry"])


# ── a belfry with a member ──────────────────────────────────────────────────────────────────
@pytest.fixture
def m(services, odoo, keycast, monkeypatch):
    s = services.settings
    s.belfry_nsec = BELFRY.secret.hex()
    s.groups_api_nsec = GROUPS.secret.hex()
    s.public_url = BASE
    s.nip05_domain = "belfry.test"
    s.device_lists = True
    monkeypatch.setattr(devices, "PACE_SECONDS", 0.0)
    monkeypatch.setattr(devices, "INLINE", True)
    devices._stores.clear()
    devices._tokens.clear()
    devices.install(services)
    return services


def member(m, odoo, keycast, name="Ana", custody=None) -> tuple[PrivateKey, int, str]:
    """(account key, partner, signer token) of a member with an active pass."""
    key = PrivateKey()
    pk = pub(key)
    partner = odoo.add(
        "res.partner",
        name=name,
        email=f"{name.lower()}@example.org",
        belfry_member_number=f"M-{name}",
        **({"belfry_key_custody": custody} if custody else {}),
    )
    odoo.add("belfry.pass", partner_id=partner, tier="week", state="active", keycast_pubkey=pk)
    if custody != "self":
        keycast.users[f"odoo-partner-{partner}"] = {"pubkey": pk, "username": name.lower()}
    found = community.member_token(m, partner)
    return key, partner, (found[1] if found else "")


def lists(m, account: str) -> list[dict]:
    return sorted(
        [e for e in m.relay.events if e["kind"] == 8570 and ["p", account] in e["tags"]],
        key=lambda e: int(next(t[1] for t in e["tags"] if t[0] == "n")),
    )


def chain_of(m, account: str) -> list[list[str]]:
    """Every list of ACCOUNT checked in order; the device keys of each."""
    out, prev = [], None
    for e in lists(m, account):
        out.append(check_list(e, BELFRY_PUB, account, prev))
        prev = e
    return out


def device(m, token: str, name="Chat · Firefox on Linux") -> tuple[PrivateKey, dict]:
    key = PrivateKey()
    return key, m.keycast.create_bunker(token, name, pub(key))


# ── writing ─────────────────────────────────────────────────────────────────────────────────
def test_a_bind_writes_list_1_then_2_with_prev(m, odoo, keycast):
    acct, _, token = member(m, odoo, keycast)
    phone, _ = device(m, token, "Belfry for iPhone")
    assert chain_of(m, pub(acct)) == [[pub(phone)]]
    laptop, _ = device(m, token)
    assert chain_of(m, pub(acct)) == [[pub(phone)], sorted([pub(phone), pub(laptop)])]


def test_a_revoke_writes_a_list_without_that_device(m, odoo, keycast):
    acct, _, token = member(m, odoo, keycast)
    phone, _ = device(m, token)
    laptop, made = device(m, token)
    m.keycast.revoke_session(token, made["bunker_pubkey"])
    assert chain_of(m, pub(acct))[-1] == [pub(phone)]
    assert devices.store(m).removal(pub(laptop))["account"] == pub(acct)


def test_nothing_is_written_when_nothing_changed(m, odoo, keycast):
    acct, _, token = member(m, odoo, keycast)
    device(m, token)
    m.keycast.create_bunker(token, "Notes")  # another app's session: not a device
    assert devices.refresh(m, pub(acct)) == "same"
    assert devices.check_all(m) == {"same": 1}
    assert len(lists(m, pub(acct))) == 1


def test_only_chat_sessions_with_a_client_key_are_devices(m, odoo, keycast):
    acct, _, token = member(m, odoo, keycast)
    phone, _ = device(m, token)
    device(m, token, "Notes · Firefox on Linux")
    m.keycast.create_bunker(token, "Chat · Safari on iPhone")  # not bound yet
    devices.check_all(m)
    assert chain_of(m, pub(acct)) == [[pub(phone)]]


def test_a_twenty_first_device_is_not_listed(m, odoo, keycast):
    acct, _, token = member(m, odoo, keycast)
    keys = [pub(device(m, token)[0]) for _ in range(21)]
    last = chain_of(m, pub(acct))[-1]
    assert len(last) == 20 and keys[20] not in last and set(last) == set(keys[:20])
    # one of the twenty leaves: the twenty-first comes in
    made = [s for s in m.keycast.list_sessions(token) if s["client_pubkey"] == keys[3]][0]
    m.keycast.revoke_session(token, made["bunker_pubkey"])
    assert set(chain_of(m, pub(acct))[-1]) == set(keys) - {keys[3]}


def test_an_own_key_account_is_its_own_device(m, odoo, keycast):
    acct, _, _ = member(m, odoo, keycast, "Agent", custody="self")
    assert devices.refresh(m, pub(acct)) == "published"
    [e] = lists(m, pub(acct))
    check_list(e, BELFRY_PUB, pub(acct), None)
    assert [t for t in e["tags"] if t[0] == "device"] == [["device", pub(acct), "self"]]
    assert devices.refresh(m, pub(acct)) == "same"


def test_the_service_keys_get_no_list(m, odoo):
    for key in (BELFRY, GROUPS):
        odoo.add("belfry.pass", partner_id=odoo.add("res.partner", name="svc"), tier="week",
                 state="active", keycast_pubkey=pub(key))  # fmt: skip
        assert devices.refresh(m, pub(key)) == "skipped"
    assert pub(BELFRY) not in devices.accounts(m) and pub(GROUPS) not in devices.accounts(m)
    devices.check_all(m)
    assert not [e for e in m.relay.events if e["kind"] == 8570]


def test_two_binds_at_once_give_n_and_n_plus_1(m, odoo, keycast, monkeypatch):
    acct, _, _ = member(m, odoo, keycast)
    a, b = pub(PrivateKey()), pub(PrivateKey())
    sets = {"one": [(a,)], "two": sorted([(a,), (b,)])}
    inside = threading.Barrier(2, timeout=5)

    def entries(svc, account):
        name = threading.current_thread().name
        time.sleep(0.05)
        return sets[name]

    monkeypatch.setattr(devices, "entries_for", entries)
    results: list[str] = []

    def run():
        inside.wait()
        results.append(devices.refresh(m, pub(acct)))

    threads = [threading.Thread(target=run, name=n) for n in ("one", "two")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == ["published", "published"]
    got = chain_of(m, pub(acct))
    assert len(got) == 2  # n = 1 and n = 2, chained: never two lists 1


def test_a_restart_continues_the_counter_from_the_table(m, odoo, keycast, monkeypatch):
    acct, _, token = member(m, odoo, keycast)
    device(m, token)
    device(m, token)
    devices._stores.clear()  # a new process: the table read from disk
    # the relay not answering reads: the table alone says where the chain is
    monkeypatch.setattr(devices, "relay_latest", lambda svc, a: (_ for _ in ()).throw(OSError()))
    device(m, token)
    assert len(chain_of(m, pub(acct))) == 3


def test_an_empty_table_continues_the_chain_found_on_the_relay(m, odoo, keycast, tmp_path):
    acct, _, token = member(m, odoo, keycast)
    device(m, token)
    device(m, token)
    # the table lost (a new register folder): the relay's list 2 is where it goes on from
    m.settings.register_path = str(tmp_path / "elsewhere" / "register.sqlite")
    devices._stores.clear()
    device(m, token)
    assert len(chain_of(m, pub(acct))) == 3
    # and a list equal to the relay's latest is not written again
    m.settings.register_path = str(tmp_path / "third" / "register.sqlite")
    devices._stores.clear()
    assert devices.refresh(m, pub(acct)) == "same"


def test_no_table_and_no_relay_writes_nothing(m, odoo, keycast, monkeypatch):
    acct, _, token = member(m, odoo, keycast)
    monkeypatch.setattr(devices, "relay_latest", lambda svc, a: (_ for _ in ()).throw(OSError()))
    device(m, token)
    assert lists(m, pub(acct)) == []
    assert devices.refresh(m, pub(acct)) == "unknown"


def test_the_switch_off_writes_nothing(m, odoo, keycast):
    m.settings.device_lists = False
    acct, _, token = member(m, odoo, keycast)
    device(m, token)
    assert devices.check_all(m) == {}
    assert devices.refresh(m, pub(acct)) == "off"
    assert not [e for e in m.relay.events if e["kind"] == 8570]


def test_an_unbound_session_is_looked_at_again(m, odoo, keycast, monkeypatch):
    acct, _, token = member(m, odoo, keycast)
    asked: list[tuple[str, float]] = []
    monkeypatch.setattr(devices, "schedule", lambda svc, a, delay=0.0: asked.append((a, delay)))
    m.keycast.create_bunker(token, "Chat · Safari on iPhone")
    assert asked == [(pub(acct), d) for d in devices.UNBOUND_RECHECKS]
    asked.clear()
    device(m, token)  # bound: at once, and looked at again too
    assert asked == [(pub(acct), 0.0)] + [(pub(acct), d) for d in devices.UNBOUND_RECHECKS]


def test_a_device_gone_without_a_revoke_is_recorded_removed(m, odoo, keycast):
    acct, _, token = member(m, odoo, keycast)
    phone, made = device(m, token)
    device(m, token)
    for b in keycast.bunkers:  # the signer ends it by itself (an expiry)
        if b.get("client_pubkey") == pub(phone):
            b["revoked"] = True
    devices.check_all(m)
    assert pub(phone) not in chain_of(m, pub(acct))[-1]
    assert devices.store(m).removal(pub(phone))["by_name"] is None


# ── GET /api/sessions/removed, POST /api/me/sessions/revoke with ``by`` ─────────────────────
@pytest.fixture
def web(m):
    with TestClient(create_app(m)) as c:
        yield c


def removed(web, key: PrivateKey):
    path = devices.REMOVED_PATH
    return web.get(path, headers={"Authorization": nip98(key, BASE + path)})


def test_a_device_asks_whether_it_was_removed(m, odoo, keycast, web):
    acct, _, token = member(m, odoo, keycast)
    phone, made = device(m, token, "Chat · Safari on iPhone")
    laptop, _ = device(m, token, "Chat · Firefox on Linux")
    r = removed(web, phone)
    assert r.status_code == 200 and r.json() == {"removed": False}
    # the laptop removes the phone: the account signs (through the laptop's session)
    r = post(
        web,
        acct,
        "/api/me/sessions/revoke",
        {"bunker_pubkey": made["bunker_pubkey"], "by": pub(laptop)},
    )
    assert r.status_code == 200, r.text
    time.sleep(1.1)  # a NIP-98 authorization is single use
    r = removed(web, phone)
    assert r.status_code == 200
    body = r.json()
    assert body["removed"] is True and body["by_name"] == "Chat · Firefox on Linux"
    assert abs(body["at"] - time.time()) < 10
    assert pub(phone) not in chain_of(m, pub(acct))[-1]
    # still a live device
    assert removed(web, laptop).json() == {"removed": False}


def test_removed_without_by_has_no_name(m, odoo, keycast, web):
    acct, _, token = member(m, odoo, keycast)
    phone, made = device(m, token)
    r = post(web, acct, "/api/me/sessions/revoke", {"bunker_pubkey": made["bunker_pubkey"]})
    assert r.status_code == 200
    body = removed(web, phone).json()
    assert body["removed"] is True and "by_name" not in body


def test_by_must_be_a_key(m, odoo, keycast, web):
    acct, _, token = member(m, odoo, keycast)
    _, made = device(m, token)
    r = post(
        web, acct, "/api/me/sessions/revoke", {"bunker_pubkey": made["bunker_pubkey"], "by": "x"}
    )
    assert r.status_code == 422


def test_unknown_keys_and_account_keys_are_not_devices(m, odoo, keycast, web):
    acct, _, token = member(m, odoo, keycast)
    device(m, token)
    assert removed(web, PrivateKey()).status_code == 404
    assert removed(web, acct).status_code == 404  # the account itself is no device


def test_a_removal_is_kept_thirty_days(m, odoo, keycast):
    acct, _, token = member(m, odoo, keycast)
    phone, made = device(m, token)
    m.keycast.revoke_session(token, made["bunker_pubkey"])
    st = devices.store(m)
    st.mark_removed(pub(phone), pub(acct), int(time.time()) - 31 * 86400 - 7200)
    assert devices.removed_answer(m, pub(phone))[0] == 404


def test_belfry_json_names_the_format(m, web):
    assert web.get("/.well-known/belfry.json").json()["encrypted"] == {"format": 1}
    m.settings.encrypted_format = 3
    assert web.get("/.well-known/belfry.json").json()["encrypted"] == {"format": 3}


def test_only_1_or_3(settings):
    from pydantic import ValidationError

    from belfry_onboarding.settings import Settings

    with pytest.raises(ValidationError, match="ENCRYPTED_FORMAT"):
        Settings(HOOK_TOKEN="x", ENCRYPTED_FORMAT=2, _env_file=None)
    assert Settings(HOOK_TOKEN="x", ENCRYPTED_FORMAT=3, _env_file=None).encrypted_format == 3


def test_looking_at_devices_never_makes_a_signer_account(m, odoo, keycast):
    """A pass holder the own signer has no account for: no list, and no account made."""
    from .conftest import TEST_SIGNER

    if TEST_SIGNER != "belfry":
        pytest.skip("the own signer can say whether an account exists; Keycast can't")
    pk = pub(PrivateKey())
    partner = odoo.add("res.partner", name="Nobody", email="n@example.org")
    odoo.add("belfry.pass", partner_id=partner, tier="week", state="active", keycast_pubkey=pk)
    before = dict(keycast.users)
    assert devices.refresh(m, pk) == "unknown"
    assert keycast.users == before and lists(m, pk) == []


def test_old_web_sessions_named_by_the_chat_address_are_devices():
    """Sessions opened before the chat had its own title were named by its address
    ("chat.<domain> · Safari on iPhone", earlier "apps.<domain>/chat"): they are chat devices."""
    from types import SimpleNamespace

    from belfry_onboarding import apps, discovery

    svc = SimpleNamespace(settings=SimpleNamespace(nip05_domain="belfry.test"))
    names = apps.chat_names(svc)
    for name in ("chat.belfry.test · Safari on iPhone", "apps.belfry.test/chat · Chrome on iPhone"):
        assert discovery.chat_session(svc, {"application_name": name, "redirect_origin": ""}, names)
    for name in ("other.example · Safari", "belfry.test/website · Firefox"):
        assert not discovery.chat_session(svc, {"application_name": name, "redirect_origin": ""}, names)
