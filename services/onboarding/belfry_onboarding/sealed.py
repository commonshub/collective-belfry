"""Sealed conversations (docs/sealed.md): what the server needs of them, and the shared rules.

A sealed message is a NIP-59 gift wrap to a **letterbox key** whose secret only the person's
devices hold; the seal is signed by a **device key** (the NIP-46 client key of a sign-in), not by
the account. Which devices and which letterbox are a person's is a chain of roster events
(kind 8560) anchored by the account (kind 30078 ``d`` = ``belfry:sealed``).

The server never holds a letterbox secret. It needs three things, all here:

- read a member's chain (``chain_for``) to know their head letterbox and their devices;
- seal a mail to that letterbox as the belfry key (``seal_to_letterbox``; mail.py, §7 In);
- accept a member's outgoing mail whose seal a device signed (``chain`` + ``live_devices``;
  mail.py, §7 Out).

Everything above ``chain_for`` is pure: no clock unless asked, no network, no settings. The
functions the server does not call itself (``open_sealed``, ``accept``, key hand-over, link
proof, shared history) are the reference the vectors in ``docs/sealed-vectors.json`` are
generated from (tools/sealed_vectors.py); the apps implement the same rules.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from coincurve import PrivateKey

from .blossom import event_id, verify_signature
from .nip46check import conversation_key, nip44_decrypt, nip44_encrypt

log = logging.getLogger(__name__)

ROSTER, KEYS, REQUEST, HISTORY = 8560, 8561, 8562, 8564
ANCHOR, ANCHOR_D = 30078, "belfry:sealed"
SEAL, WRAP, MAIL = 13, 1059, 1301
MAX_DEVICES = 20
MAX_RUMOR = 40_000  # bytes of a sealed rumor's JSON (§8)
MAX_SHARED = 200  # rumors in one kind 8564
BACKDATE = 2 * 86400  # seals and wraps are back-dated up to two days (NIP-59), as mail.py does

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_N = re.compile(r"^[1-9][0-9]{0,8}$")


# ── events ──────────────────────────────────────────────────────────────────────────────────
def is_key(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX64.match(value))


def pubkey_of(secret: bytes) -> str:
    return PrivateKey(secret).public_key_xonly.format().hex()


def valid_event(event: Any) -> bool:
    """A well-formed signed event: its id is the hash of its fields and its signature is the
    pubkey's over that id. (``verify_signature`` alone trusts the id it is given.)"""
    if not isinstance(event, dict):
        return False
    try:
        if not (is_key(event["pubkey"]) and is_key(event["id"])):
            return False
        if not isinstance(event["kind"], int) or isinstance(event["kind"], bool):
            return False
        if not isinstance(event["created_at"], int) or isinstance(event["created_at"], bool):
            return False
        if not isinstance(event["content"], str) or not isinstance(event["tags"], list):
            return False
        if not all(
            isinstance(t, list) and all(isinstance(v, str) for v in t) for t in event["tags"]
        ):
            return False
        return event_id(event) == event["id"] and verify_signature(event)
    except (KeyError, TypeError, ValueError):
        return False


def sign(secret: bytes, event: dict[str, Any], aux: bytes | None = b"") -> dict[str, Any]:
    """EVENT signed by SECRET (pubkey and id set). AUX: BIP-340's auxiliary randomness; the
    default draws it, 32 fixed bytes make the signature reproducible (the vectors)."""
    key = PrivateKey(secret)
    event = {**event, "pubkey": key.public_key_xonly.format().hex()}
    event["id"] = event_id(event)
    event["sig"] = key.sign_schnorr(bytes.fromhex(event["id"]), aux).hex()
    return event


def _values(event: dict[str, Any], name: str) -> list[str]:
    return [t[1] for t in event.get("tags", []) if len(t) > 1 and t[0] == name]


def _one(event: dict[str, Any], name: str) -> str | None:
    """The value of the tag NAME when the event has exactly one."""
    found = _values(event, name)
    return found[0] if len(found) == 1 else None


# ── the roster chain (§3) ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Roster:
    id: str
    signer: str
    n: int
    prev: str | None
    lb: str
    devices: tuple[str, ...]
    ready: bool
    created_at: int


@dataclass(frozen=True)
class Chain:
    """An account's valid chain, root first."""

    account: str
    rosters: tuple[Roster, ...]
    # device → the first roster that contains it ("Added from": that roster's signer)
    added: dict[str, Roster] = field(default_factory=dict, compare=False)
    # device no longer in the head → the roster that dropped it (the latest, if it came back
    # and left again)
    dropped: dict[str, Roster] = field(default_factory=dict, compare=False)

    @property
    def root(self) -> Roster:
        return self.rosters[0]

    @property
    def head(self) -> Roster:
        return self.rosters[-1]

    @property
    def devices(self) -> tuple[str, ...]:
        return self.head.devices

    @property
    def lb(self) -> str:
        return self.head.lb

    @property
    def ready(self) -> bool:
        """Ready ever: once a roster of the chain had the tag, later ones don't unset it (§6)."""
        return any(r.ready for r in self.rosters)

    @property
    def valid_ids(self) -> list[str]:
        return [r.id for r in self.rosters]

    @property
    def all_devices(self) -> set[str]:
        """Every device of any roster of the chain."""
        return set(self.added)

    def has_device(self, device: str, at: int) -> bool:
        """§4: DEVICE may have sealed a rumor dated AT: it is in the head, or it was in an
        earlier roster and AT is not after the ``created_at`` of the roster that dropped it."""
        if device in self.head.devices:
            return True
        gone = self.dropped.get(device)
        return gone is not None and at <= gone.created_at


def parse_roster(event: Any, account: str) -> Roster | None:
    """EVENT as a roster of ACCOUNT, or None: kind 8560, signed, one ``p`` (the account), one
    ``n``, one ``lb``, at most one ``prev``, 1 to 20 devices, none listed twice. Says nothing
    about its place in a chain."""
    if not valid_event(event) or event["kind"] != ROSTER:
        return None
    if _values(event, "p") != [account]:
        return None
    n, lb, prevs = _one(event, "n"), _one(event, "lb"), _values(event, "prev")
    if n is None or not _N.match(n) or not is_key(lb) or len(prevs) > 1:
        return None
    if prevs and not is_key(prevs[0]):
        return None
    devices = tuple(_values(event, "device"))
    if not 1 <= len(devices) <= MAX_DEVICES or len(set(devices)) != len(devices):
        return None
    if not all(is_key(d) for d in devices):
        return None
    return Roster(
        id=event["id"],
        signer=event["pubkey"],
        n=int(n),
        prev=prevs[0] if prevs else None,
        lb=lb,  # type: ignore[arg-type]
        devices=devices,
        ready=any(t and t[0] == "ready" for t in event["tags"]),
        created_at=event["created_at"],
    )


def anchor_root(anchor: Any, account: str | None = None) -> str | None:
    """The root an anchor names (§3.2), or None when it is not a signed anchor (of ACCOUNT)."""
    if not valid_event(anchor) or anchor["kind"] != ANCHOR:
        return None
    if account is not None and anchor["pubkey"] != account:
        return None
    if _values(anchor, "d") != [ANCHOR_D]:
        return None
    root = _one(anchor, "root")
    return root if is_key(root) else None


def newest_anchor(events: list[dict[str, Any]], account: str) -> dict[str, Any] | None:
    """The account's current anchor among EVENTS: the newest, the lower id on a tie (NIP-01's
    rule for replaceable events)."""
    mine = [e for e in events if anchor_root(e, account)]
    return min(mine, key=lambda e: (-e["created_at"], e["id"])) if mine else None


def _follows(prev: Roster, earlier_lbs: set[str], r: Roster) -> bool:
    """§3.3 "Roster n", given that ``r.prev`` is ``prev``."""
    if r.n != prev.n + 1 or r.signer not in prev.devices:
        return False
    if any(d not in r.devices for d in prev.devices):
        return r.lb not in earlier_lbs  # a device left: a letterbox the chain never had
    return r.lb == prev.lb


def chain(anchor_event: Any, roster_events: list[Any]) -> Chain | None:
    """The valid chain of the anchor's account (§3.3), or None when there is none (no valid
    anchor, or no valid root for it). Anything invalid is ignored silently."""
    root_id = anchor_root(anchor_event)
    if root_id is None:
        return None
    account = anchor_event["pubkey"]
    parsed: dict[str, Roster] = {}
    for e in roster_events or []:
        r = parse_roster(e, account)
        if r is not None:
            parsed[r.id] = r
    root = parsed.get(root_id)
    if root is None or root.n != 1 or root.prev is not None or root.signer not in root.devices:
        return None
    children: dict[str, list[Roster]] = {}
    for r in parsed.values():
        if r.prev is not None:
            children.setdefault(r.prev, []).append(r)
    rosters = [root]
    lbs = {root.lb}
    added = {d: root for d in root.devices}
    dropped: dict[str, Roster] = {}
    while True:
        head = rosters[-1]
        valid = [r for r in children.get(head.id, []) if _follows(head, lbs, r)]
        if not valid:
            break
        nxt = min(valid, key=lambda r: r.id)  # a fork: the lower id wins
        for d in head.devices:
            if d not in nxt.devices:
                dropped[d] = nxt
        for d in nxt.devices:
            added.setdefault(d, nxt)
            dropped.pop(d, None)
        rosters.append(nxt)
        lbs.add(nxt.lb)
    return Chain(account=account, rosters=tuple(rosters), added=added, dropped=dropped)


# ── wraps (§4) ──────────────────────────────────────────────────────────────────────────────
def with_id(rumor: dict[str, Any]) -> dict[str, Any]:
    """RUMOR with the id computed from its fields (§4: the opener always computes it; an ``id``
    or ``sig`` it came with is ignored). ValueError when it is not an event."""
    if not isinstance(rumor, dict):
        raise ValueError("the rumor is not an event")
    if not is_key(rumor.get("pubkey")):
        raise ValueError("the rumor has no author")
    for name, kind in (("created_at", int), ("kind", int), ("tags", list), ("content", str)):
        if not isinstance(rumor.get(name), kind) or isinstance(rumor.get(name), bool):
            raise ValueError("the rumor is not an event")
    if not all(isinstance(t, list) and all(isinstance(v, str) for v in t) for t in rumor["tags"]):
        raise ValueError("the rumor is not an event")
    kept = {k: v for k, v in rumor.items() if k not in ("id", "sig")}
    return {**kept, "id": event_id(kept)}


def rumor_json(rumor: dict[str, Any]) -> str:
    """The rumor as it goes into a seal: compact JSON, UTF-8 as it is."""
    return json.dumps(rumor, separators=(",", ":"), ensure_ascii=False)


def fits(rumor: dict[str, Any]) -> bool:
    """§8: a sealed rumor's JSON is at most 40 000 bytes (NIP-44 carries 65 535, and the wrap
    holds the seal's base64 of the padded rumor)."""
    return len(rumor_json(rumor).encode()) <= MAX_RUMOR


def _wrap(
    signer_secret: bytes,
    rumor: dict[str, Any],
    to: str,
    tags: list[list[str]],
    *,
    now: int | None = None,
    seal_at: int | None = None,
    wrap_at: int | None = None,
    one_time: bytes | None = None,
    seal_nonce: bytes | None = None,
    wrap_nonce: bytes | None = None,
    aux: bytes | None = b"",
) -> dict[str, Any]:
    """RUMOR sealed by SIGNER_SECRET and gift-wrapped, both encrypted to the key TO. The
    keyword arguments fix what is otherwise drawn (the vectors)."""
    now = int(time.time()) if now is None else now
    rumor = with_id(rumor)
    if not fits(rumor):
        raise ValueError("the rumor is over 40 000 bytes: too big to seal")
    receiver = bytes.fromhex(to)
    seal = sign(
        signer_secret,
        {
            "kind": SEAL,
            "created_at": now - secrets.randbelow(BACKDATE) if seal_at is None else seal_at,
            "tags": [],
            "content": nip44_encrypt(
                conversation_key(signer_secret, receiver),
                rumor_json(rumor),
                seal_nonce,
            ),
        },
        aux,
    )
    once = one_time or PrivateKey().secret
    return sign(
        once,
        {
            "kind": WRAP,
            "created_at": now - secrets.randbelow(BACKDATE) if wrap_at is None else wrap_at,
            "tags": tags,
            "content": nip44_encrypt(
                conversation_key(once, receiver),
                json.dumps(seal, separators=(",", ":"), ensure_ascii=False),
                wrap_nonce,
            ),
        },
        aux,
    )


def seal_to_letterbox(
    signer_secret: bytes, rumor: dict[str, Any], account: str, lb: str, **fixed: Any
) -> dict[str, Any]:
    """§4: RUMOR sealed by SIGNER_SECRET (a device key; the belfry's key for a mail, §7) to the
    letterbox LB of ACCOUNT: a kind 1059 with ``["p", account]`` and ``["lb", lb]``."""
    if not (is_key(account) and is_key(lb)):
        raise ValueError("account and letterbox are 64 hex characters")
    return _wrap(signer_secret, rumor, lb, [["p", account], ["lb", lb]], **fixed)


def _open(secret: bytes, wrap: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    seal, rumor = open_wrap(secret, wrap)
    return seal, with_id(rumor)


def open_wrap(secret: bytes, wrap: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """(seal, rumor as it was sent) of a gift wrap to SECRET's key: wrap and seal are signed
    events with their own ids; nothing is said yet about the rumor. ValueError otherwise."""
    if not valid_event(wrap) or wrap["kind"] != WRAP:
        raise ValueError("not a valid gift wrap")
    try:
        seal = json.loads(
            nip44_decrypt(conversation_key(secret, bytes.fromhex(wrap["pubkey"])), wrap["content"])
        )
    except Exception as exc:  # noqa: BLE001 — another key's wrap, or not NIP-44 at all
        raise ValueError("the wrap does not open with this key") from exc
    if not valid_event(seal) or seal["kind"] != SEAL:
        raise ValueError("the seal is not signed")
    try:
        rumor = json.loads(
            nip44_decrypt(conversation_key(secret, bytes.fromhex(seal["pubkey"])), seal["content"])
        )
    except Exception as exc:  # noqa: BLE001
        raise ValueError("the seal does not open with this key") from exc
    if not isinstance(rumor, dict):
        raise ValueError("the rumor is not an event")
    return seal, rumor


def open_sealed(lb_secret: bytes, wrap: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(seal, rumor) of a sealed wrap opened with the letterbox secret its ``lb`` tag names.
    ValueError when it is not one or does not open. Who signed the seal is the caller's
    question (``accept``)."""
    lb = _one(wrap, "lb") if isinstance(wrap, dict) else None
    if lb is None:
        raise ValueError("not a sealed wrap (no lb tag)")
    if lb != pubkey_of(lb_secret):
        raise ValueError("another letterbox")
    return _open(lb_secret, wrap)


def accept(
    author_chain: Chain | None, seal: dict[str, Any], rumor: dict[str, Any], belfry: str = ""
) -> bool:
    """§4: whether the rumor of an opened sealed wrap is its author's. AUTHOR_CHAIN: the valid
    chain of ``rumor.pubkey`` (None: none, or not loaded: keep the wrap and try again). BELFRY:
    the belfry's key, whose own seal on its own mail (kind 1301, §7) is taken."""
    author, signer = rumor.get("pubkey"), seal.get("pubkey")
    if belfry and signer == belfry:
        return author == belfry and rumor.get("kind") == MAIL
    if author_chain is None or author_chain.account != author:
        return False
    at = rumor.get("created_at")
    if not isinstance(at, int) or isinstance(at, bool):
        return False
    return author_chain.has_device(signer, at)  # type: ignore[arg-type]


# ── handing keys over (§5.5), the link proof (§5.2), shared history (§8) ────────────────────
def keys_rumor(giver: str, account: str, keys: list[dict[str, str]], at: int) -> dict[str, Any]:
    return with_id(
        {
            "kind": KEYS,
            "pubkey": giver,
            "created_at": at,
            "tags": [["p", account]],
            "content": json.dumps(
                {"v": 1, "account": account, "keys": keys}, separators=(",", ":")
            ),
        }
    )


def hand_keys(
    giver_secret: bytes, account: str, keys: list[dict[str, str]], to_device: str, **fixed: Any
) -> dict[str, Any]:
    """§5.5: the giver's key chain gift-wrapped to a device: rumor, seal and wrap all by or to
    device keys, ``["p", <receiving device>]`` and no ``lb``."""
    at = fixed.pop("rumor_at", None)
    rumor = keys_rumor(
        pubkey_of(giver_secret), account, keys, int(time.time()) if at is None else at
    )
    return _wrap(giver_secret, rumor, to_device, [["p", to_device]], **fixed)


def open_keys(device_secret: bytes, wrap: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(seal, rumor) of a hand-over to this device. ValueError when it is not one."""
    if not isinstance(wrap, dict) or _values(wrap, "p") != [pubkey_of(device_secret)]:
        raise ValueError("not for this device")
    seal, rumor = _open(device_secret, wrap)
    if rumor.get("kind") != KEYS or rumor.get("pubkey") != seal["pubkey"]:
        raise ValueError("not a hand-over by its giver")
    return seal, rumor


def accept_keys(
    account: str, chains: list[Chain], seal: dict[str, Any], rumor: dict[str, Any]
) -> list[dict[str, str]]:
    """§5.5: the letterbox keys of a hand-over this device takes, [] when it takes none. The
    giver must be a device of any roster of CHAINS (the account's current chain and, after a
    fresh start, the one pinned before); each secret must be its ``lb``'s."""
    giver = seal.get("pubkey")
    if not any(c.account == account and giver in c.all_devices for c in chains):
        return []
    if rumor.get("kind") != KEYS or rumor.get("pubkey") != giver:
        return []
    if _values(rumor, "p") != [account]:
        return []
    try:
        body = json.loads(rumor.get("content") or "")
    except ValueError:
        return []
    if not isinstance(body, dict) or body.get("v") != 1 or body.get("account") != account:
        return []
    out: list[dict[str, str]] = []
    for k in body.get("keys") if isinstance(body.get("keys"), list) else []:
        if not isinstance(k, dict) or not is_key(k.get("lb")) or not is_key(k.get("secret")):
            continue
        try:
            good = pubkey_of(bytes.fromhex(k["secret"])) == k["lb"]
        except ValueError:  # not a secret key at all (zero, or over the curve's order)
            good = False
        if good and not any(o["lb"] == k["lb"] for o in out):
            out.append({"lb": k["lb"], "secret": k["secret"]})
    return out


def link_proof(link_secret: str, device: str) -> str:
    """§5.2: hex HMAC-SHA256 keyed with the link secret (the 32 bytes its 64 hex characters
    stand for) over the device's 32 public-key bytes."""
    if not (is_key(link_secret) and is_key(device)):
        raise ValueError("64 hex characters each")
    return hmac.new(bytes.fromhex(link_secret), bytes.fromhex(device), hashlib.sha256).hexdigest()


def link_ok(link_secret: str, request: dict[str, Any]) -> bool:
    """Whether a device request (kind 8562) proves it came from the link with LINK_SECRET."""
    if not valid_event(request) or request["kind"] != REQUEST:
        return False
    given = _one(request, "link")
    return given is not None and hmac.compare_digest(
        given, link_proof(link_secret, request["pubkey"])
    )


def history_rumor(sharer: str, person: str, rumors: list[dict[str, Any]], at: int) -> dict:
    return with_id(
        {
            "kind": HISTORY,
            "pubkey": sharer,
            "created_at": at,
            "tags": [["p", person]],
            "content": json.dumps(
                {"v": 1, "rumors": rumors}, separators=(",", ":"), ensure_ascii=False
            ),
        }
    )


def pack_history(
    sharer: str, person: str, rumors: list[dict[str, Any]], at: int
) -> list[dict[str, Any]]:
    """§8: RUMORS (oldest first) as kind 8564 rumors, each filled until one more would take it
    past 200 rumors or its own JSON (the shared rumors escaped inside its content) past 40 000
    bytes. A rumor that does not fit alone is left out."""
    out: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []
    for r in rumors:
        if len(held) < MAX_SHARED and fits(history_rumor(sharer, person, [*held, r], at)):
            held.append(r)
            continue
        if not fits(history_rumor(sharer, person, [r], at)):
            continue  # too big to share at all
        out.append(history_rumor(sharer, person, held, at))
        held = [r]
    if held:
        out.append(history_rumor(sharer, person, held, at))
    return out


def participants(rumor: dict[str, Any]) -> set[str]:
    """Who a rumor's conversation is between: its author and its ``p`` tags."""
    return {rumor.get("pubkey", ""), *_values(rumor, "p")} - {""}


def shared_rumors(history: dict[str, Any]) -> list[dict[str, Any]]:
    """§8: the rumors of a (already accepted, §4) kind 8564 the receiver may add: those whose
    conversation the sharer is part of and whose id is their own. The others are dropped."""
    if history.get("kind") != HISTORY:
        return []
    try:
        body = json.loads(history.get("content") or "")
    except ValueError:
        return []
    if not isinstance(body, dict) or body.get("v") != 1 or not isinstance(body.get("rumors"), list):
        return []
    sharer = history.get("pubkey")
    out = []
    for r in body["rumors"]:
        try:
            r = with_id(r)
        except (ValueError, TypeError):
            continue
        if sharer in participants(r):
            out.append(r)
    return out


# ── when a roster may say ready (§6) ────────────────────────────────────────────────────────
COUNTED_DAYS = 30


def _when(value: Any) -> float | None:
    """A session's time as seconds: an ISO 8601 time (the signer's) or a number."""
    from datetime import UTC, datetime

    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=UTC)).timestamp()


def counted(session: dict[str, Any], now: float) -> bool:
    """§6: a session of ``/api/me/sessions`` that counts: one of the belfry's own chat clients
    (``"chat": true``), last active less than 30 × 86 400 s ago (``last_activity``, or
    ``created_at`` when the session was never active)."""
    if session.get("chat") is not True:
        return False
    at = _when(session.get("last_activity"))
    if at is None:
        at = _when(session.get("created_at"))
    return at is not None and now - at < COUNTED_DAYS * 86400


def may_be_ready(sessions: list[dict[str, Any]], devices: list[str] | tuple[str, ...], now: float):
    """§6: whether a roster with DEVICES may carry ``["ready"]``: every counted session's
    ``client_pubkey`` is one of them."""
    have = set(devices)
    return all(
        str(s.get("client_pubkey") or "").lower() in have for s in sessions if counted(s, now)
    )


# ── reading a member's chain, and their live devices (the only I/O here) ────────────────────
CACHE_SECONDS = 60.0
FRESH_AFTER = 5.0  # ``fresh=True`` still takes an answer this young
_lock = threading.Lock()
_cache: dict[str, tuple[float, Chain | None]] = {}


def read_chain(svc: Any, account: str) -> Chain | None:
    """ACCOUNT's valid chain from the community relay, read as the belfry key, always without
    ``since`` (§3.3). Raises what the relay raises."""
    from . import homepage
    from .relay import query_all

    key = homepage.belfry_key(svc.settings)
    if svc.relay is None or key is None:
        raise RuntimeError("no community relay or no belfry key")

    def as_belfry(event: dict[str, Any]) -> dict[str, Any]:
        return sign(key.secret, event)

    anchors = svc.relay.query(
        [{"kinds": [ANCHOR], "authors": [account], "#d": [ANCHOR_D]}], as_belfry
    )
    anchor = newest_anchor(anchors or [], account)
    if anchor is None:
        return None
    rosters = query_all(svc.relay, {"kinds": [ROSTER], "#p": [account]}, as_belfry)
    return chain(anchor, rosters)


def chain_for(svc: Any, account: str, *, fresh: bool = False, strict: bool = False) -> Chain | None:
    """ACCOUNT's valid chain, cached a minute; None: no chain. Never raises (a relay that does
    not answer reads as "no chain": mail delivery goes on as before sealing) unless STRICT.
    FRESH: don't take an answer older than a few seconds."""
    if not is_key(account):
        return None
    now = time.monotonic()
    with _lock:
        held = _cache.get(account)
    if held and now - held[0] < (FRESH_AFTER if fresh else CACHE_SECONDS):
        return held[1]
    try:
        found = read_chain(svc, account)
    except Exception as exc:  # noqa: BLE001
        if strict:
            raise
        log.info("sealed: no chain read for %s…: %r", account[:8], exc)
        return None
    with _lock:
        _cache[account] = (now, found)
        for k in [k for k, v in _cache.items() if now - v[0] > 10 * CACHE_SECONDS]:
            del _cache[k]
    return found


def live_devices(svc: Any, account: str) -> set[str]:
    """The ``client_pubkey`` of each live signer session of the member ACCOUNT in one of the
    belfry's own chat clients: the ``"chat": true`` rows of ``GET /api/me/sessions``, asked for
    them (a token of their own from the signer, community.member_token). Empty for an account
    whose key the signer does not hold or that has no active pass. Raises when the signer does
    not answer."""
    from . import community, discovery
    from .core import m2o_id

    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "=", account]],
        ["partner_id"],
        limit=1,
    )
    partner = m2o_id(rows[0].get("partner_id")) if rows else None
    if not partner:
        return set()
    found = community.member_token(svc, partner)
    if found is None or found[0] != account:
        return set()
    from . import apps

    names = apps.chat_names(svc)
    return {
        str(s.get("client_pubkey")).lower()
        for s in svc.keycast.list_sessions(found[1])
        if is_key(str(s.get("client_pubkey") or "").lower())
        and discovery.chat_session(svc, s, names)
    }
