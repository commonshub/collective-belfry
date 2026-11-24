"""Encrypted conversations, format 3 (docs/encrypted.md, §16–§26): the rules, and the belfry's
own keys as senders and receivers.

**The rules** (the first half) are a port of the web chat's ``src/core/enc/core.ts`` (branch
enc-v3): device lists 8570 read as an app reads them, conversation keys 8571 (key rumors,
people envelopes, group envelopes), messages 8572 (seals, people copies, group messages) and
the checks a receiver makes. ``tests/test_enc.py`` runs the shared vectors
(``tests/data/encrypted.json``, the web's ``vectors/encrypted.json``) against them: what this
module writes is byte for byte what the web writes for the same keys, nonces and dates.

**The service keys.** The belfry key and the groups key are their own device (§2, §20): their
entry in a people envelope is the key itself, a seal they sign is valid for themselves. They
never keep a conversation secret ``k``: as senders they make a key, publish its envelopes and the
message, and forget ``k`` (the public half ``K`` is kept for reuse while the lists are the same,
§10: sending needs only ``K``). As receivers (mail out to the belfry key, button presses to the
groups key) they open what is addressed to them with their own secret.

Never log a secret or a rumor's text; errors say what failed only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets as _secrets
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from .nip46check import conversation_key, nip44_decrypt, nip44_encrypt
from .sealed import is_key, pubkey_of, sign, valid_event, with_id

log = logging.getLogger(__name__)

LIST, KEY, MSG, PACK, ASK, ANSWER = 8570, 8571, 8572, 8573, 8574, 8575
SEAL = 13
MAX_LIST = 20
MAX_PEOPLE_DEVICES = 100
MAX_GROUP_DEVICES = 1000
KEY_AGE = 30 * 86400
GRACE = 600
MAX_ENVELOPE = 60_000
MAX_RUMOR = 40_000
FUTURE = 900
SKEW = 300
SERVICE = "service"
SILENT = ["silent", "1"]

_N = re.compile(r"^[1-9][0-9]{0,8}$")


# ── small helpers ───────────────────────────────────────────────────────────────────────────
def _values(e: dict[str, Any], name: str) -> list[str]:
    return [
        t[1]
        for t in e.get("tags", [])
        if isinstance(t, list) and len(t) > 1 and t[0] == name and isinstance(t[1], str)
    ]


def _one(e: dict[str, Any], name: str) -> str | None:
    v = _values(e, name)
    return v[0] if len(v) == 1 else None


def _conv(secret: bytes, pub: str) -> bytes:
    return conversation_key(secret, bytes.fromhex(pub))


def _json(value: Any) -> str:
    """JSON as JavaScript's JSON.stringify writes it (compact, UTF-8 kept)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def rumor_json(r: dict[str, Any]) -> str:
    """The rumor as it goes into a seal: the six fields only, in the order id, pubkey,
    created_at, kind, tags, content (the web's rumorJson)."""
    return _json({k: r[k] for k in ("id", "pubkey", "created_at", "kind", "tags", "content")})


def _now() -> int:
    return int(time.time())


@dataclass
class Fixed:
    """What would otherwise be random (the vectors fix it)."""

    at: int | None = None
    seal_at: int | None = None
    one_time: bytes | None = None
    nonce: Callable[[int], bytes] | None = None
    aux: bytes | None = None


def _nonce(f: Fixed | None, i: int) -> bytes | None:
    return f.nonce(i) if f is not None and f.nonce is not None else None


def _aux(f: Fixed | None) -> bytes | None:
    # sealed.sign: b"" draws the auxiliary randomness; 32 fixed bytes make it reproducible
    return f.aux if f is not None and f.aux is not None else b""


def _at(f: Fixed | None) -> int:
    return f.at if f is not None and f.at is not None else _now()


def _signed(secret: bytes, kind: int, at: int, tags: list, content: str, f: Fixed | None):
    return sign(secret, {"kind": kind, "created_at": at, "tags": tags, "content": content}, _aux(f))


# ── §3 device lists ─────────────────────────────────────────────────────────────────────────
@dataclass
class DeviceList:
    id: str
    account: str
    n: int
    prev: str | None
    devices: list[str]
    readers: list[str]
    created_at: int


def parse_list(e: Any, belfry: str) -> DeviceList | None:
    """E as a device list of the belfry BELFRY (the web's parseList), or None."""
    if not valid_event(e) or e["kind"] != LIST or e["pubkey"] != belfry or e["content"] != "":
        return None
    account, n, prevs = _one(e, "p"), _one(e, "n"), _values(e, "prev")
    if not is_key(account) or not n or not _N.match(n):
        return None
    num = int(n)
    if (num == 1 and prevs) or (num != 1 and (len(prevs) != 1 or not is_key(prevs[0]))):
        return None
    devices: list[str] = []
    readers: list[str] = []
    has_self = False
    for t in e["tags"]:
        if not t or t[0] != "device":
            continue
        if len(t) < 2 or not is_key(t[1]) or len(t) > 3:
            return None
        if len(t) == 3 and t[2] == "self" and t[1] != account:
            return None
        if len(t) == 3 and t[2] not in ("reader", "self"):
            continue  # a marker this reader doesn't know: that entry is skipped
        if t[1] in devices or t[1] in readers:
            return None
        if len(t) == 3 and t[2] == "self":
            has_self = True
        (readers if len(t) == 3 and t[2] == "reader" else devices).append(t[1])
    if len(devices) > MAX_LIST:
        return None
    if has_self and len(devices) != 1:
        return None
    return DeviceList(
        e["id"], account, num, prevs[0] if num > 1 else None, devices, readers, e["created_at"]
    )


@dataclass
class Chain:
    account: str
    lists: list[DeviceList]
    gaps: bool = False
    fork: bool = False
    invalid_above: bool = False


def chain_of(events: Iterable[Any], belfry: str, account: str) -> Chain | None:
    """ACCOUNT's lists among EVENTS (the web's chainOf); None when there is none."""
    by_n: dict[int, list[DeviceList]] = {}
    bad_top = 0
    for e in events:
        lst = parse_list(e, belfry)
        if lst is None:
            if (
                valid_event(e)
                and e["kind"] == LIST
                and e["pubkey"] == belfry
                and any(len(t) > 1 and t[0] == "p" and t[1] == account for t in e["tags"])
            ):
                n = next((t[1] for t in e["tags"] if len(t) > 1 and t[0] == "n"), "")
                if re.fullmatch(r"[0-9]{1,12}", n or "") and int(n) > bad_top:
                    bad_top = int(n)
            continue
        if lst.account != account:
            continue
        at = by_n.setdefault(lst.n, [])
        if not any(x.id == lst.id for x in at):
            at.append(lst)
    if not by_n:
        return None
    fork = False
    lists: list[DeviceList] = []
    for n in sorted(by_n):
        c = sorted(by_n[n], key=lambda x: x.id)
        if len(c) > 1:
            fork = True
        lists.append(c[0])
    for i in range(1, len(lists)):
        if lists[i - 1].n == lists[i].n - 1 and lists[i].prev != lists[i - 1].id:
            fork = True
    top = lists[-1].n
    return Chain(account, lists, len(lists) < top, fork, bad_top > top)


def current(c: Chain) -> DeviceList:
    return c.lists[-1]


def list_by_id(c: Chain, list_id: str) -> DeviceList | None:
    return next((x for x in c.lists if x.id == list_id), None)


def receivers(lst: DeviceList) -> list[str]:
    """Every key that receives copies for the account now: devices, then readers."""
    return [*lst.devices, *lst.readers]


def listed_at(c: Chain, device: str, at: int) -> bool | None:
    """§16.2 + §21 (the web's listedAt): DEVICE on the account's list current at AT."""
    ls = c.lists
    for i, x in enumerate(ls):
        before = ls[i - 1] if i > 0 else None
        if (
            device in x.devices
            and not (before is not None and device in before.devices)
            and at < x.created_at <= at + SKEW
        ):
            at = x.created_at
            break
    idx = -1
    for j, x in enumerate(ls):
        if x.created_at <= at:
            idx = j
    if idx < 0:
        return False if ls[0].n == 1 else None
    if idx + 1 < len(ls) and ls[idx + 1].n != ls[idx].n + 1:
        return None
    if device in ls[idx].devices:
        return True
    prev = ls[idx - 1] if idx > 0 else None
    if prev is None or prev.n != ls[idx].n - 1:
        return False if ls[idx].n == 1 else None
    return device in prev.devices and at <= ls[idx].created_at + GRACE


def signs_at(c: Chain, device: str, at: int) -> bool | None:
    """§6 + §20 (the web's signsAt)."""
    v = listed_at(c, device, at)
    if v is not False:
        return v
    known = any(device in x.devices or device in x.readers for x in c.lists)
    if not known and at >= current(c).created_at:
        return None
    return False


# ── §5 names ────────────────────────────────────────────────────────────────────────────────
def alias(K: str, account: str) -> str:
    return hashlib.sha256(f"belfry:key:{K}:{account}".encode()).hexdigest()


def lists_hash(lists: Iterable[tuple[str, str]]) -> str:
    """§16.1: sha256 of the sorted lines "account:list id" joined by newlines."""
    lines = sorted(f"{a}:{i}" for a, i in lists)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def device_set(devices: Iterable[str]) -> str:
    """§18.4: sha256 of the sorted device keys joined by newlines."""
    return hashlib.sha256("\n".join(sorted(set(devices))).encode()).hexdigest()


def people_id(accounts: Iterable[str]) -> str:
    return ",".join(sorted(set(accounts)))


def people_of(r: dict[str, Any]) -> str:
    return people_id([r["pubkey"], *[p for p in _values(r, "p") if is_key(p)]])


def people_lists(lists: Iterable[tuple[str, str]]) -> str:
    return "\n".join(sorted(f"{a}:{i}" for a, i in lists))


# ── §5 conversation keys ────────────────────────────────────────────────────────────────────
def new_key(secret: bytes | None = None) -> tuple[str, str]:
    """(k, K) of a new conversation key."""
    s = secret or _secrets.token_bytes(32)
    return s.hex(), pubkey_of(s)


def key_rumor(maker: str, k: str, people: list[tuple[str, str | None]], at: int) -> dict:
    """The key rumor of a people conversation; PEOPLE = (account, its list id or None)."""
    K = pubkey_of(bytes.fromhex(k))
    tags = [["K", K], *[["p", a] for a, _ in people], *[["list", a, x] for a, x in people if x]]
    return with_id({"kind": KEY, "pubkey": maker, "created_at": at, "content": k, "tags": tags})


Devices = Callable[[str], "Chain | str | None"]


def accept_key_rumor(r: dict, receiver: str, devices: Devices, now: int | None = None) -> str:
    """'ok' | 'wait' | 'no' (the web's acceptKeyRumor)."""
    now = _now() if now is None else now
    if r.get("kind") != KEY or not is_key(r.get("content")) or r["created_at"] > now + FUTURE:
        return "no"
    K = _one(r, "K")
    try:
        pub = pubkey_of(bytes.fromhex(r["content"]))
    except ValueError:
        return "no"
    if not K or K != pub:
        return "no"
    people = _values(r, "p")
    if receiver not in people or any(not is_key(p) for p in people):
        return "no"
    wait = False
    for a in people:
        c = devices(a)
        if c == SERVICE:
            if r["pubkey"] == a:
                return "ok"
            continue
        named = next((t[2] for t in r["tags"] if len(t) > 2 and t[0] == "list" and t[1] == a), None)
        if not named:
            continue
        if c is None:
            wait = True
            continue
        lst = list_by_id(c, named)  # type: ignore[arg-type]
        if lst is None:
            wait = True
            continue
        if r["pubkey"] in lst.devices:
            return "ok"
    return "wait" if wait else "no"


def _split(devices: list[str], entry: Callable[[int, str], str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = [{}]
    size = 2
    for d in devices:
        v = entry(len(out) - 1, d)
        add = len(d) + len(v) + 6
        if size + add > MAX_ENVELOPE and out[-1]:
            out.append({})
            size = 2
            v = entry(len(out) - 1, d)
        out[-1][d] = v
        size += len(d) + len(v) + 6
    return out


def people_envelopes(
    maker: bytes, r: dict, people: list[tuple[str, list[str]]], f: Fixed | None = None
) -> list[dict[str, Any]]:
    """§5 people envelopes (the web's peopleEnvelopes): per person (account, devices), kind
    8571 by a one-time key, ["p", account], ["d", alias]; content {device: nip44(one-time →
    device, seal by MAKER of the key rumor)}."""
    K = _one(r, "K")
    assert K
    now = _at(f)
    seal_at = f.seal_at if f is not None and f.seal_at is not None else now
    out: list[dict[str, Any]] = []
    counter = [0]

    def nxt() -> bytes | None:
        v = _nonce(f, counter[0])
        counter[0] += 1
        return v

    for account, devs in people:
        seals = {}
        for d in devs:
            content = nip44_encrypt(_conv(maker, d), rumor_json(r), nxt())
            seals[d] = _json(_signed(maker, SEAL, seal_at, [], content, f))
        onces: dict[int, bytes] = {}

        def once_of(i: int, onces: dict[int, bytes] = onces) -> bytes:
            if i not in onces:
                onces[i] = (f.one_time if f and f.one_time else None) or _secrets.token_bytes(32)
            return onces[i]

        chunks = _split(
            devs,
            lambda i, d, seals=seals, once_of=once_of: nip44_encrypt(
                _conv(once_of(i), d), seals[d], nxt()
            ),
        )
        for i, content in enumerate(chunks):
            tags = [["p", account], ["d", alias(K, account)]]
            out.append(_signed(once_of(i), KEY, now, tags, _json(content), f))
    return out


def open_people_envelope(device: bytes, env: Any) -> tuple[dict, dict] | None:
    """(seal, key rumor with its id) of this device's entry, None when there is none. Raises
    ValueError on a bad entry."""
    if not valid_event(env) or env["kind"] != KEY:
        raise ValueError("not an envelope")
    me = pubkey_of(device)
    try:
        entries = json.loads(env["content"])
    except ValueError as exc:
        raise ValueError("not an envelope") from exc
    entry = entries.get(me) if isinstance(entries, dict) else None
    if not isinstance(entry, str):
        return None
    try:
        seal = json.loads(nip44_decrypt(_conv(device, env["pubkey"]), entry))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("the entry does not open") from exc
    if not valid_event(seal) or seal["kind"] != SEAL:
        raise ValueError("the seal is not signed")
    try:
        rumor = with_id(json.loads(nip44_decrypt(_conv(device, seal["pubkey"]), seal["content"])))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("the seal does not open") from exc
    if rumor["pubkey"] != seal["pubkey"]:
        raise ValueError("the key rumor is not the seal signer's")
    return seal, rumor


def group_envelopes(
    maker: bytes, k: str, group: str, devices: list[str], lists: str, f: Fixed | None = None
) -> list[dict[str, Any]]:
    """§16.1 + §18.4 group envelopes, unsigned (the web's groupEnvelopes)."""
    K = pubkey_of(bytes.fromhex(k))
    body = _json({"k": k, "K": K, "h": group, "set": device_set(devices)})
    counter = [0]

    def entry(_i: int, d: str) -> str:
        v = nip44_encrypt(_conv(maker, d), body, _nonce(f, counter[0]))
        counter[0] += 1
        return v

    tags = [["h", group], ["d", K], ["maker", pubkey_of(maker)], ["lists", lists]]
    return [
        {"kind": KEY, "created_at": _at(f), "tags": tags, "content": _json(c)}
        for c in _split(devices, entry)
    ]


def entry_keys(envs: Iterable[Any]) -> list[str]:
    out: set[str] = set()
    for e in envs:
        if not valid_event(e) or e["kind"] != KEY:
            continue
        try:
            m = json.loads(e["content"])
        except ValueError:
            continue
        if isinstance(m, dict):
            out |= {k for k in m if is_key(k)}
    return sorted(out)


@dataclass
class GroupKey:
    k: str
    K: str
    group: str
    maker: str
    account: str
    at: int
    set: str


def open_group_envelope(device: bytes, env: Any, group: str) -> GroupKey | None:
    if not valid_event(env) or env["kind"] != KEY:
        raise ValueError("not an envelope")
    h, d, maker = _one(env, "h"), _one(env, "d"), _one(env, "maker")
    if h != group or not is_key(d) or not is_key(maker):
        raise ValueError("not this group's envelope")
    try:
        entries = json.loads(env["content"])
    except ValueError as exc:
        raise ValueError("not an envelope") from exc
    entry = entries.get(pubkey_of(device)) if isinstance(entries, dict) else None
    if not isinstance(entry, str):
        return None
    try:
        x = json.loads(nip44_decrypt(_conv(device, maker), entry))  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        raise ValueError("the entry does not open") from exc
    if (
        not isinstance(x, dict)
        or not is_key(x.get("k"))
        or x.get("K") != d
        or x.get("h") != group
        or not is_key(x.get("set"))
        or pubkey_of(bytes.fromhex(x["k"])) != d
    ):
        raise ValueError("the entry is not this key")
    return GroupKey(x["k"], d, group, maker, env["pubkey"], env["created_at"], x["set"])  # type: ignore[arg-type]


def accept_group_key(key: GroupKey, devices: Chain | str | None, now: int | None = None) -> str:
    now = _now() if now is None else now
    if key.at > now + FUTURE:
        return "no"
    if devices == SERVICE:
        return "ok" if key.maker == key.account else "no"
    if devices is None:
        return "wait"
    v = listed_at(devices, key.maker, key.at)  # type: ignore[arg-type]
    return "wait" if v is None else "ok" if v else "no"


# ── §6 messages ─────────────────────────────────────────────────────────────────────────────
def _seal_for(device: bytes, r: dict, K: str, f: Fixed | None, i: int = 0) -> dict[str, Any]:
    body = rumor_json(r)
    if len(body.encode()) > MAX_RUMOR:
        raise ValueError("the message is too long to encrypt")
    at = f.seal_at if f and f.seal_at is not None else _at(f)
    return _signed(device, SEAL, at, [], nip44_encrypt(_conv(device, K), body, _nonce(f, i)), f)


def people_message(
    device: bytes, rumor: dict, K: str, people: list[str], me: str, f: Fixed | None = None
) -> list[dict[str, Any]]:
    """§6 people: one 8572 per person (the sender's own copy silent)."""
    r = with_id(rumor)
    seal = _json(_seal_for(device, r, K, f, 0))
    out = []
    for i, a in enumerate(people):
        once = (f.one_time if f and f.one_time else None) or _secrets.token_bytes(32)
        tags = [["p", a], ["d", alias(K, a)], *([list(SILENT)] if a == me else [])]
        content = nip44_encrypt(_conv(once, K), seal, _nonce(f, 100 + i))
        out.append(_signed(once, MSG, _at(f), tags, content, f))
    return out


def group_message(
    device: bytes,
    rumor: dict,
    K: str,
    group: str,
    f: Fixed | None = None,
    mentions: Iterable[str] = (),
) -> dict[str, Any]:
    """§6 group: one unsigned 8572 (h, d = K, p per mention)."""
    r = with_id(rumor)
    ps: list[str] = []
    for p in mentions:
        if is_key(p) and p not in ps:
            ps.append(p)
    tags = [["h", group], ["d", K], *[["p", p] for p in ps]]
    return {
        "kind": MSG,
        "created_at": _at(f),
        "tags": tags,
        "content": _json(_seal_for(device, r, K, f, 0)),
    }


def _open_seal(k: str, seal_json: str) -> tuple[dict, dict]:
    try:
        seal = json.loads(seal_json)
    except ValueError as exc:
        raise ValueError("the seal is not an event") from exc
    if not valid_event(seal) or seal["kind"] != SEAL:
        raise ValueError("the seal is not signed")
    try:
        rumor = with_id(
            json.loads(nip44_decrypt(_conv(bytes.fromhex(k), seal["pubkey"]), seal["content"]))
        )
    except Exception as exc:  # noqa: BLE001
        raise ValueError("the seal does not open with this key") from exc
    return seal, rumor


def open_people_message(k: str, msg: Any) -> tuple[dict, dict]:
    if not valid_event(msg) or msg["kind"] != MSG:
        raise ValueError("not a message")
    try:
        seal_json = nip44_decrypt(_conv(bytes.fromhex(k), msg["pubkey"]), msg["content"])
    except Exception as exc:  # noqa: BLE001
        raise ValueError("the message does not open with this key") from exc
    return _open_seal(k, seal_json)


def open_group_message(k: str, msg: Any) -> tuple[dict, dict]:
    if not valid_event(msg) or msg["kind"] != MSG:
        raise ValueError("not a message")
    return _open_seal(k, msg["content"])


def _signer(outer: dict, seal: dict, rumor: dict, chain: Chain | str | None) -> str:
    if rumor["created_at"] > outer["created_at"] + SKEW:
        return "no"
    if chain == SERVICE:
        return "ok" if seal["pubkey"] == rumor["pubkey"] else "no"
    if chain is None:
        return "wait"
    assert isinstance(chain, Chain)
    if seal["pubkey"] == rumor["pubkey"] and not any(
        seal["pubkey"] in x.devices for x in chain.lists
    ):
        return "no"
    v = signs_at(chain, seal["pubkey"], outer["created_at"])
    return "wait" if v is None else "ok" if v else "no"


def accept_people_message(
    outer: dict, seal: dict, rumor: dict, key_people: str, chain: Chain | str | None
) -> str:
    if people_of(rumor) != key_people:
        return "no"
    return _signer(outer, seal, rumor, chain)


def accept_group_message(
    outer: dict, seal: dict, rumor: dict, group: str, chain: Chain | str | None
) -> str:
    if outer["pubkey"] != rumor["pubkey"] or _one(rumor, "h") != group or _one(outer, "h") != group:
        return "no"
    return _signer(outer, seal, rumor, chain)


# ═══ the belfry's side ══════════════════════════════════════════════════════════════════════
class Held(RuntimeError):
    """What the belfry would send can't be encrypted now (lists not readable, a person with no
    device, a fork): never sent in clear; the caller holds or refuses."""


def format3(svc: Any) -> bool:
    return getattr(svc.settings, "encrypted_format", 1) == 3


def belfry_secret(svc: Any) -> bytes | None:
    from .homepage import belfry_key

    key = belfry_key(svc.settings)
    return key.secret if key is not None else None


def groups_secret(svc: Any) -> bytes | None:
    from .groups_members import GroupsMembersError, api_key

    raw = getattr(svc.settings, "groups_api_nsec", "") or ""
    if not raw:
        return None
    try:
        return api_key(raw).secret
    except GroupsMembersError:
        return None


def service_keys(svc: Any) -> set[str]:
    out = set()
    for s in (belfry_secret(svc), groups_secret(svc)):
        if s:
            out.add(pubkey_of(s))
    return out


def _as(secret: bytes) -> Callable[[dict[str, Any]], dict[str, Any]]:
    return lambda event: sign(secret, event)


def read_chains(svc: Any, accounts: Iterable[str]) -> dict[str, Chain | None]:
    """Every list of ACCOUNTS on the community relay, read as the belfry: account → its chain
    (None: no list). Raises when the relay doesn't answer."""
    from .relay import query_all

    wanted = sorted({a for a in accounts if is_key(a)})
    secret = belfry_secret(svc)
    if not wanted:
        return {}
    if secret is None or svc.relay is None:
        raise Held("no belfry key or no community relay")
    belfry = pubkey_of(secret)
    found = query_all(svc.relay, {"kinds": [LIST], "authors": [belfry], "#p": wanted}, _as(secret))
    return {a: chain_of(found, belfry, a) for a in wanted}


def devices_for(svc: Any, accounts: Iterable[str]) -> Devices:
    """The three answers (§20) for ACCOUNTS: SERVICE for the belfry and the groups key, a chain,
    or None (no list)."""
    services = service_keys(svc)
    chains = read_chains(svc, [a for a in accounts if a not in services])
    return lambda a: SERVICE if a in services else chains.get(a)


# K kept per (sender, conversation, lists) while they are the same and younger than 30 days;
# never k (§10). In memory: a restart makes a new key, which costs one envelope.
_reuse: dict[tuple[str, str, str], tuple[str, int]] = {}
_reuse_lock = threading.Lock()


def _reused(sender: str, conv: str, lists: str) -> str | None:
    with _reuse_lock:
        got = _reuse.get((sender, conv, lists))
    if got and _now() - got[1] < KEY_AGE:
        return got[0]
    return None


def _keep(sender: str, conv: str, lists: str, K: str) -> None:
    with _reuse_lock:
        if len(_reuse) > 5000:
            _reuse.clear()
        _reuse[(sender, conv, lists)] = (K, _now())


@dataclass
class Sent:
    envelopes: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    K: str = ""
    conv: str = ""
    lists: str = ""


def people_events(
    svc: Any, sender: bytes, rumor: dict, people: list[str], devices: Devices | None = None
) -> Sent:
    """The events that carry RUMOR from the service key SENDER to PEOPLE (accounts, the sender
    not among them): a key's envelopes (when a new one is made) and one copy per person. The
    sender has no envelope and no copy of its own (it keeps nothing). Raises Held when a person
    has no list, a fork, or no device at all (§17.1, §17.5), or over 100 devices."""
    me = pubkey_of(sender)
    others = sorted({p for p in people if p != me})
    if not others:
        raise Held("nobody to send to")
    devices = devices or devices_for(svc, others)
    per: list[tuple[str, str | None, list[str]]] = []
    for a in others:
        c = devices(a)
        if c is None:
            raise Held("no list")
        if c == SERVICE:
            per.append((a, None, [a]))
            continue
        assert isinstance(c, Chain)
        if c.fork or c.invalid_above:
            raise Held("fork")
        per.append((a, current(c).id, receivers(current(c))))
    if any(not devs for _, _, devs in per):
        raise Held("no device")
    if sum(len(d) for _, _, d in per) + 1 > MAX_PEOPLE_DEVICES:
        raise Held("too many devices")
    conv = people_id([me, *others])
    lists = people_lists([(a, x) for a, x, _ in per if x])
    out = Sent()
    K = _reused(me, conv, lists)
    if K is None:
        k, K = new_key()
        named = sorted([(me, None), *[(a, x) for a, x, _ in per]], key=lambda p: p[0])
        r = key_rumor(me, k, named, _now())
        out.envelopes = people_envelopes(sender, r, [(a, devs) for a, _, devs in per])
        del k
    r = {**rumor, "pubkey": me}
    out.messages = people_message(sender, r, K, others, me)
    out.K, out.conv, out.lists = K, conv, lists
    return out


def send_people(svc: Any, sender: bytes, rumor: dict, people: list[str]) -> Sent:
    """people_events, published on the community relay: envelopes first, then the copies. The
    key's public half is kept for the next message only once everything is out."""
    sent = people_events(svc, sender, rumor, people)
    publish = _as(sender)
    for e in [*sent.envelopes, *sent.messages]:
        svc.relay.publish(e, publish)
    _keep(pubkey_of(sender), sent.conv, sent.lists, sent.K)
    return sent


def group_members(svc: Any, group: str) -> set[str]:
    """The group's members (latest 39002) on the groups relay. Raises when it can't be read."""
    from . import followers

    got = followers._relay(svc)
    if got is None:
        raise Held("no groups relay")
    relay, sign_ = got
    found = [
        e
        for e in relay.query([{"kinds": [39002], "#d": [group]}], sign_) or []
        if any(t[:2] == ["d", group] for t in e.get("tags", []))
    ]
    if not found:
        raise Held("no member list")
    last = max(found, key=lambda e: e.get("created_at", 0))
    return {t[1] for t in last.get("tags", []) if len(t) > 1 and t[0] == "p" and is_key(t[1])}


def is_private(svc: Any, group: str) -> bool:
    """A private group (docs/one-shape.md): its metadata says ``private``. Raises when the
    metadata can't be read (never decided by a failed read)."""
    from . import followers

    tags = followers._meta_tags(svc, group)
    return bool(tags) and "private" in tags


def _guests(svc: Any) -> set[str]:
    from .whois import kinds

    try:
        return {k for k, v in kinds(svc).items() if v == "guest"}
    except Exception:  # noqa: BLE001 — a register that can't say: nobody is left out
        return set()


def group_events(
    svc: Any, sender: bytes, rumor: dict, group: str, mentions: Iterable[str] = ()
) -> tuple[list[dict[str, Any]], dict[str, Any], str, str]:
    """(envelopes, message, K, device set) carrying RUMOR into the private group GROUP from the
    service key SENDER, all signed by it (§10, §24). Members: the 39002 list; each member's
    current list (a guest gets no entry, §25; a member with no list after a second read is
    skipped for this message, §23; an empty list has no devices). Raises Held."""
    me = pubkey_of(sender)
    members = sorted(group_members(svc, group) - {me})
    guests = _guests(svc)
    services = service_keys(svc)
    chains = read_chains(svc, [a for a in members if a not in services])
    missing = [a for a in members if a not in services and chains.get(a) is None]
    if missing:
        chains.update(read_chains(svc, missing))
    devices: list[str] = []
    lines: list[tuple[str, str]] = []
    for a in members:
        if a in services:
            devices.append(a)
            continue
        c = chains.get(a)
        if c is None or a in guests:
            continue
        if c.fork or c.invalid_above:
            raise Held("fork")
        devices += receivers(current(c))
        lines.append((a, current(c).id))
    if len(devices) > MAX_GROUP_DEVICES:
        raise Held("too many devices")
    dset = device_set(devices)
    K = _reused(me, "g:" + group, dset)
    envs: list[dict[str, Any]] = []
    if K is None:
        k, K = new_key()
        envs = [
            sign(sender, t) for t in group_envelopes(sender, k, group, devices, lists_hash(lines))
        ]
        del k
    r = {**rumor, "pubkey": me}
    if _one(r, "h") != group:
        raise ValueError("the rumor is not for this group")
    msg = sign(sender, group_message(sender, r, K, group, mentions=mentions))
    return envs, msg, K, dset


def send_group(
    svc: Any, sender: bytes, rumor: dict, group: str, mentions: Iterable[str] = ()
) -> dict[str, Any]:
    """group_events published on the groups relay (envelopes first); the message."""
    from . import followers

    got = followers._relay(svc)
    if got is None:
        raise Held("no groups relay")
    relay, _ = got
    envs, msg, K, dset = group_events(svc, sender, rumor, group, mentions)
    publish = _as(sender)
    for e in [*envs, msg]:
        relay.publish(e, publish)
    _keep(pubkey_of(sender), "g:" + group, dset, K)
    return msg


# ── receiving: a service key opens what is addressed to it ──────────────────────────────────
class Later(RuntimeError):
    """The key, the lists or the relay aren't there yet: try again; never dropped on that."""


_keys: dict[tuple[str, str], tuple[str, str]] = {}  # (me, alias) → (k, key people)
_keys_lock = threading.Lock()


def open_for(svc: Any, secret: bytes, msg: dict[str, Any], relay: Any = None) -> dict[str, Any]:
    """The accepted rumor of a people 8572 addressed to the service key SECRET (["p", me]): its
    key found by its alias on RELAY (the community relay), the key rumor and the message judged
    as an app judges them (§5, §6, §21). ValueError: not for us, or refused (drop). Later: the
    envelope or a list isn't there yet, or the relay didn't answer (retry)."""
    me = pubkey_of(secret)
    relay = relay or svc.relay
    if not valid_event(msg) or msg["kind"] != MSG or _one(msg, "p") != me:
        raise ValueError("not a message for this key")
    d = _one(msg, "d")
    if not is_key(d):
        raise ValueError("no key named")
    with _keys_lock:
        held = _keys.get((me, d))  # type: ignore[arg-type]
    if held is None:
        try:
            envs = relay.query([{"kinds": [KEY], "#p": [me], "#d": [d]}], _as(secret)) or []
        except Exception as exc:  # noqa: BLE001
            raise Later(f"envelopes not read: {exc!r}") from exc
        waiting = False
        for env in envs:
            if _one(env, "d") != d or _one(env, "p") != me:
                continue
            try:
                got = open_people_envelope(secret, env)
            except ValueError:
                continue  # a bad envelope never blocks the search (§17.12)
            if got is None:
                continue
            _, r = got
            if alias(_one(r, "K") or "", me) != d:
                continue
            people = _values(r, "p")
            try:
                verdict = accept_key_rumor(r, me, devices_for(svc, people))
            except Exception as exc:  # noqa: BLE001
                raise Later(f"lists not read: {exc!r}") from exc
            if verdict == "wait":
                waiting = True
                continue
            if verdict != "ok":
                continue
            held = (r["content"], people_id(people))
            with _keys_lock:
                if len(_keys) > 5000:
                    _keys.clear()
                _keys.setdefault((me, d), held)  # §17.12: the first declaration stays
                held = _keys[(me, d)]  # type: ignore[index]
            break
        if held is None:
            raise Later("the key's envelope isn't here yet" if not waiting else "lists pending")
    k, key_people = held
    seal, rumor = open_people_message(k, msg)
    try:
        chain = devices_for(svc, [rumor["pubkey"]])(rumor["pubkey"])
    except Exception as exc:  # noqa: BLE001
        raise Later(f"lists not read: {exc!r}") from exc
    verdict = accept_people_message(msg, seal, rumor, key_people, chain)
    if verdict == "wait":
        raise Later("the author's lists aren't here yet")
    if verdict != "ok":
        raise ValueError("refused")
    return rumor


def forget_keys() -> None:
    """Tests: the opened keys and the reused public halves forgotten."""
    with _keys_lock:
        _keys.clear()
    with _reuse_lock:
        _reuse.clear()
