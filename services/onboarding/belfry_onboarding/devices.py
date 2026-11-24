"""Device lists, kind 8570 (docs/encrypted.md §3, as amended by §16–§21).

For every account the belfry publishes, on the community relay and signed with the belfry key,
the list of the account's devices: the client keys of its live **chat** sessions in the signer
(``discovery.chat_session``), never more than ``MAX_DEVICES``. A regular event, one per change,
chained by a counter and the id of the list before:

    kind 8570, pubkey = belfry
    tags: ["p", account], ["n", "<n>"], ["prev", <id of n-1>] (not for n = 1),
          ["device", <pubkey>] … (sorted), content ""

- **Devices.** Sessions that are not revoked or expired; idle is not removal (§17.3). The
  oldest ``MAX_DEVICES`` by creation are listed, a later one is not (§17.4): nothing is evicted.
- **An account whose key the signer does not hold** (an own-key member, an agent with its own
  key) is its own device: ``["device", <account>, "self"]`` alone (§17.1, §18.5).
- **The belfry key and the groups key** get no list (§20).
- **When.** A session bound to a client key or revoked (the signer client's ``create_bunker``
  and ``revoke_session`` are wrapped by :func:`install`, so every place that makes or ends a
  session is covered, whoever calls), and a check every ``CHECK_SECONDS`` of every account
  (expiry, sessions bound by a first ``connect``, anything missed). A list equal to the latest
  one is never published.
- **The counter** survives restarts in ``devices.sqlite`` next to the register. Before a list
  is published the relay's latest list of the account is read and wins when it is further on
  (a fresh table, a restored backup): the chain never forks because of the belfry's memory. An
  account with no row and a relay that does not answer is left for the next check (never a
  second list 1).
- **One at a time per account** (a lock): two binds at once give n and n + 1.
- **Pace.** The community relay takes 60 events a minute from one address, and the service
  shares its address with everything else it publishes: at most one list every
  ``PACE_SECONDS``, binds first.
- **Switch.** ``DEVICE_LISTS`` (off by default): off, nothing is published and the check does
  nothing. What a revoke records for ``GET /api/sessions/removed`` (§16.6) is kept either way.

- **Readers** (§11): a member's own agent they let read (``POST /api/agents {pubkey, reads}``,
  agents.py) is listed as ``["device", <agent>, "reader"]`` after the devices, not counted in
  the 20.
"""

from __future__ import annotations

import heapq
import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

log = logging.getLogger(__name__)

KIND = 8570
MAX_DEVICES = 20
PACE_SECONDS = 2.0  # between two lists published (the relay: 60 events a minute per address)
CHECK_SECONDS = 600  # the check of every account
FIRST_CHECK_SECONDS = 60  # after a start
UNBOUND_RECHECKS = (20, 90, 300)  # a session claimed by its first connect: looked at again
REMOVED_DAYS = 30  # how long GET /api/sessions/removed remembers a removal (§16.6)
REMOVED_PATH = "/api/sessions/removed"

# tests set this: a bind or a revoke refreshes the list at once, in the caller's thread
INLINE = False

_HEX = re.compile(r"^[0-9a-f]{64}$")


def is_key(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX.match(value))


def enabled(svc: Any) -> bool:
    return bool(getattr(svc.settings, "device_lists", False))


def service_keys(svc: Any) -> set[str]:
    """The belfry key and the groups key: their own device, no list (§20)."""
    from .groups_members import api_pubkey
    from .homepage import belfry_pubkey

    return {
        k
        for k in (
            belfry_pubkey(svc.settings),
            api_pubkey(getattr(svc.settings, "groups_api_nsec", "") or ""),
        )
        if k
    }


# ── the store ───────────────────────────────────────────────────────────────────────────────
class Store:
    """``lists``: the latest list published per account. ``owners``: which account a device
    key was last seen on. ``removed``: devices whose session was ended, when and by which."""

    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS lists (
                    account TEXT PRIMARY KEY, n INTEGER NOT NULL, id TEXT NOT NULL,
                    devices TEXT NOT NULL, at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS owners (
                    device TEXT PRIMARY KEY, account TEXT NOT NULL, at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS removed (
                    device TEXT PRIMARY KEY, account TEXT NOT NULL, at INTEGER NOT NULL,
                    by_name TEXT
                );
                """
            )

    def latest(self, account: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT n, id, devices, at FROM lists WHERE account = ?", (account,)
            ).fetchone()
        if row is None:
            return None
        return {
            "n": int(row[0]),
            "id": row[1],
            "devices": [tuple(d) for d in json.loads(row[2])],
            "at": int(row[3]),
        }

    def accounts(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT account FROM lists")]

    def keep(self, account: str, n: int, event_id: str, devices: list[tuple], at: int) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO lists (account, n, id, devices, at) VALUES (?, ?, ?, ?, ?)",
                (account, n, event_id, json.dumps([list(d) for d in devices]), at),
            )

    def owner(self, device: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT account FROM owners WHERE device = ?", (device,))
            found = row.fetchone()
            if found is None:
                found = self._db.execute(
                    "SELECT account FROM removed WHERE device = ?", (device,)
                ).fetchone()
        return found[0] if found else None

    def seen(self, devices: Iterable[str], account: str) -> None:
        now = int(time.time())
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO owners (device, account, at) VALUES (?, ?, ?)",
                [(d, account, now) for d in devices],
            )

    def mark_removed(self, device: str, account: str, at: int, by_name: str | None = None) -> None:
        """A device's session ended. A second record of the same removal keeps the first time
        and adds the name of the device that did it when the first did not know it."""
        with self._lock:
            had = self._db.execute(
                "SELECT at, by_name FROM removed WHERE device = ?", (device,)
            ).fetchone()
            if had is not None and abs(at - int(had[0])) < 3600:
                at = int(had[0])
                by_name = by_name or had[1]
            self._db.execute(
                "INSERT OR REPLACE INTO removed (device, account, at, by_name) VALUES (?, ?, ?, ?)",
                (device, account, at, by_name),
            )

    def removal(self, device: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT account, at, by_name FROM removed WHERE device = ?", (device,)
            ).fetchone()
        return {"account": row[0], "at": int(row[1]), "by_name": row[2]} if row else None

    def unremove(self, device: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM removed WHERE device = ?", (device,))


_stores: dict[str, Store] = {}
_stores_lock = threading.Lock()


def store(svc: Any) -> Store:
    path = str(Path(svc.settings.register_path).with_name("devices.sqlite"))
    with _stores_lock:
        if path not in _stores:
            _stores[path] = Store(path)
        return _stores[path]


# ── an account's devices ────────────────────────────────────────────────────────────────────
class Unknown(RuntimeError):
    """The account's devices can't be told now (the signer or the register not answering, a key
    that is not the register's): nothing is published for it this time."""


def _partner_of(svc: Any, account: str) -> int | None:
    from .core import m2o_id

    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "=", account]],
        ["partner_id"],
        limit=1,
    )
    return m2o_id(rows[0].get("partner_id")) if rows else None


def _when(value: Any) -> float:
    from .sealed import _when as when

    found = when(value)
    return found if found is not None else float("inf")


def sessions_of(svc: Any, account: str) -> tuple[list[dict[str, Any]] | None, str | None]:
    """(the signer's sessions of ACCOUNT, its token); (None, None) when the signer does not hold
    the account's key (its own device). Raises :class:`Unknown`."""
    from . import community

    partner = _partner_of(svc, account)
    if not partner:
        raise Unknown("no active pass")
    try:
        _never_new(svc, partner)
        found = community.member_token(svc, partner)
    except Unknown:
        raise
    except Exception as exc:  # noqa: BLE001
        raise Unknown(f"signer: {exc!r}") from exc
    if found is None:
        return None, None
    if found[0] != account:
        raise Unknown("the signer's key is not the register's")
    try:
        return list(svc.keycast.list_sessions(found[1])), found[1]
    except Exception as exc:  # noqa: BLE001
        raise Unknown(f"signer sessions: {exc!r}") from exc


def _never_new(svc: Any, partner: int) -> None:
    """community.member_token makes the signer's account when it is missing. Looking at
    devices must never make one: with a signer that can say whether an account exists (the own
    signer's ``account``), a person whose key it should hold and doesn't is left alone."""
    from . import signerguard

    lookup = getattr(svc.keycast, "account", None)
    if not callable(lookup):
        return  # Keycast: no such question (its preload is idempotent on the account)
    rows = svc.odoo.read("res.partner", [partner], ["belfry_key_custody"])
    if not rows or rows[0].get("belfry_key_custody") == "self":
        return
    if signerguard.own_key_person(svc, partner):
        return
    if lookup(f"odoo-partner-{partner}") is None:
        raise Unknown("the signer has no account for this person")


def entries_for(svc: Any, account: str) -> list[tuple[str, ...]]:
    """The ``device`` entries ACCOUNT's list should hold now, in tag order. Raises
    :class:`Unknown`."""
    from . import apps, discovery

    rows, _ = sessions_of(svc, account)
    readers = [(k, "reader") for k in readers_of(svc, account)]
    if rows is None:
        return [(account, "self"), *readers]
    names = apps.chat_names(svc)
    chat = [
        (i, r)
        for i, r in enumerate(rows)
        if is_key(str(r.get("client_pubkey") or "").lower())
        and discovery.chat_session(svc, r, names)
    ]
    # the oldest first (the signer's order breaks ties); a key twice counts once
    chat.sort(key=lambda ir: (_when(ir[1].get("created_at")), ir[0]))
    keys: list[str] = []
    for _, r in chat:
        k = str(r["client_pubkey"]).lower()
        if k not in keys:
            keys.append(k)
    store(svc).seen(keys, account)
    return [*[(k,) for k in sorted(keys[:MAX_DEVICES])], *readers]


READS_PARAM = "belfry.agent_reads."  # + the agent's key: "1" while its member lets it read (§11)


def reads(svc: Any, agent: str) -> bool:
    param = getattr(svc.odoo, "param", None)
    return bool(callable(param) and param(READS_PARAM + agent) == "1")


def readers_of(svc: Any, account: str) -> list[str]:
    """§11: the account's own agents it lets read its encrypted conversations, sorted. Listed as
    ``["device", <agent>, "reader"]``, outside the 20 devices. Only active agents of this
    member count (an agent whose pass closed drops off at the next check)."""
    agents_of = getattr(svc.odoo, "agents_of", None)
    if not callable(agents_of):
        return []
    partner = _partner_of(svc, account)
    if not partner:
        return []
    found = {
        str(a.get("pubkey") or "").lower()
        for a in agents_of(partner)
        if is_key(str(a.get("pubkey") or "").lower())
    }
    return sorted(k for k in found if k != account and reads(svc, k))


def accounts(svc: Any) -> list[str]:
    """Every key holding an active pass, but the service keys."""
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "!=", False]],
        ["keycast_pubkey"],
    )
    skip = service_keys(svc)
    found = {str(r["keycast_pubkey"]).lower() for r in rows if r.get("keycast_pubkey")}
    return sorted(k for k in found if is_key(k) and k not in skip)


# ── the lists on the relay ──────────────────────────────────────────────────────────────────
def parse(event: Any, belfry: str, account: str | None = None) -> dict[str, Any] | None:
    """A device list as the apps read it (the web's vectors, ``lists.valid``), or None when
    invalid: kind 8570 by the belfry key, content "", one ``p``, one ``n`` (from 1), ``prev``
    exactly when n > 1, device keys 64 hex and never twice, at most ``MAX_DEVICES`` plain ones
    (readers apart), ``self`` only for the account itself and only with no other device. A
    ``device`` tag with a marker it does not know is skipped."""
    from .sealed import valid_event

    if not isinstance(event, dict) or event.get("kind") != KIND or event.get("pubkey") != belfry:
        return None
    if event.get("content") != "" or not valid_event(event):
        return None
    tags = event["tags"]
    ps = [t[1] for t in tags if len(t) > 1 and t[0] == "p"]
    ns = [t[1] for t in tags if len(t) > 1 and t[0] == "n"]
    prevs = [t[1] for t in tags if len(t) > 1 and t[0] == "prev"]
    if len(ps) != 1 or not is_key(ps[0]) or (account and ps[0] != account):
        return None
    if len(ns) != 1 or not re.fullmatch(r"[1-9][0-9]{0,8}", ns[0]):
        return None
    n = int(ns[0])
    if (n == 1 and prevs) or (n > 1 and (len(prevs) != 1 or not is_key(prevs[0]))):
        return None
    devices: list[str] = []
    readers: list[str] = []
    selfs: list[str] = []
    every: list[str] = []
    for t in tags:
        if not t or t[0] != "device":
            continue
        if len(t) < 2 or not is_key(t[1]):
            return None
        if t[1] in every:
            return None
        every.append(t[1])
        marker = t[2] if len(t) > 2 else ""
        if marker == "":
            devices.append(t[1])
        elif marker == "reader":
            readers.append(t[1])
        elif marker == "self":
            selfs.append(t[1])
    if len(devices) > MAX_DEVICES:
        return None
    if selfs and (len(selfs) > 1 or devices or selfs[0] != ps[0]):
        return None
    return {
        "id": event["id"],
        "account": ps[0],
        "n": n,
        "prev": prevs[0] if prevs else None,
        "devices": devices,
        "readers": readers,
        "self": bool(selfs),
        "created_at": event["created_at"],
        "entries": [tuple(t[1:3]) for t in tags if t and t[0] == "device"],
    }


def _belfry(svc: Any):
    from .homepage import belfry_key

    key = belfry_key(svc.settings)
    if key is None or svc.relay is None:
        raise Unknown("no belfry key or no community relay")
    return key


def _as_belfry(key: Any):
    from .sealed import sign

    return lambda event: sign(key.secret, event)


def relay_latest(svc: Any, account: str) -> dict[str, Any] | None:
    """The account's furthest valid list on the relay (highest n; the lower id on a tie, as the
    apps choose), or None when there is none. Raises when the relay does not answer."""
    from .relay import query_all

    key = _belfry(svc)
    belfry = key.public_key_xonly.format().hex()
    found = query_all(
        svc.relay, {"kinds": [KIND], "authors": [belfry], "#p": [account]}, _as_belfry(key)
    )
    lists = [p for p in (parse(e, belfry, account) for e in found) if p]
    if not lists:
        return None
    best = min(lists, key=lambda p: (-p["n"], p["id"]))
    return {
        "n": best["n"],
        "id": best["id"],
        "devices": [tuple(e) for e in best["entries"]],
        "at": best["created_at"],
    }


# ── publishing ──────────────────────────────────────────────────────────────────────────────
_account_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()
_pace_lock = threading.Lock()
_last_publish = 0.0


def _lock_for(account: str) -> threading.Lock:
    with _locks_lock:
        return _account_locks.setdefault(account, threading.Lock())


def _pace() -> None:
    global _last_publish
    with _pace_lock:
        wait = _last_publish + PACE_SECONDS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_publish = time.monotonic()


def event_for(
    account: str, n: int, prev: str | None, entries: list[tuple[str, ...]], at: int
) -> dict[str, Any]:
    tags: list[list[str]] = [["p", account], ["n", str(n)]]
    if prev:
        tags.append(["prev", prev])
    tags += [["device", *e] for e in entries]
    return {"kind": KIND, "created_at": at, "tags": tags, "content": ""}


def refresh(svc: Any, account: str, *, force: bool = False) -> str:
    """Bring ACCOUNT's list up to date: ``published``, ``same``, ``off`` (the switch),
    ``skipped`` (a service key) or ``unknown`` (the signer, the register or the relay could not
    say; tried again at the next check). FORCE: publish even with the switch off (tests)."""
    if not (enabled(svc) or force):
        return "off"
    if not is_key(account) or account in service_keys(svc):
        return "skipped"
    with _lock_for(account):
        try:
            entries = entries_for(svc, account)
        except Unknown as exc:
            log.info("devices: %s…: not now (%s)", account[:8], exc)
            return "unknown"
        st = store(svc)
        latest = st.latest(account)
        if latest is not None and latest["devices"] == entries:
            return "same"
        try:
            on_relay = relay_latest(svc, account)
        except Exception as exc:  # noqa: BLE001
            if latest is None:  # never a second list 1
                log.info("devices: %s…: the relay can't be read: %r", account[:8], exc)
                return "unknown"
            on_relay = None
        if on_relay is not None and (latest is None or on_relay["n"] > latest["n"]):
            latest = on_relay
            st.keep(account, latest["n"], latest["id"], latest["devices"], latest["at"])
        if latest is not None and latest["devices"] == entries:
            return "same"
        _note_dropped(svc, account, latest, entries)
        key = _belfry(svc)
        _pace()
        n = latest["n"] + 1 if latest else 1
        at = max(int(time.time()), latest["at"] if latest else 0)
        event = _as_belfry(key)(
            event_for(account, n, latest["id"] if latest else None, entries, at)
        )
        try:
            svc.relay.publish(event, _as_belfry(key))
        except Exception as exc:  # noqa: BLE001
            log.warning("devices: list %d of %s… not published: %r", n, account[:8], exc)
            return "unknown"
        st.keep(account, n, event["id"], entries, at)
        log.info("devices: list %d of %s… (%d entries)", n, account[:8], len(entries))
        return "published"


def _note_dropped(
    svc: Any, account: str, latest: dict[str, Any] | None, entries: list[tuple[str, ...]]
) -> None:
    """Devices that leave the list without a recorded revoke (an expiry, a revoke inside the
    signer): GET /api/sessions/removed answers for them too."""
    if latest is None:
        return
    now_keys = {e[0] for e in entries}
    st = store(svc)
    for e in latest["devices"]:
        if len(e) == 1 and e[0] not in now_keys and st.removal(e[0]) is None:
            st.mark_removed(e[0], account, int(time.time()))


def check_all(svc: Any) -> dict[str, int]:
    """Every account (and every account the table has a list for): its list brought up to
    date. Counts per result."""
    counts: dict[str, int] = {}
    if not enabled(svc):
        return counts
    try:
        everyone = accounts(svc)
    except Exception as exc:  # noqa: BLE001
        log.warning("devices: the register can't be read: %r", exc)
        return counts
    for account in everyone:
        try:
            result = refresh(svc, account)
        except Exception as exc:  # noqa: BLE001 — one account never stops the others
            log.warning("devices: %s…: %r", account[:8], exc)
            result = "unknown"
        counts[result] = counts.get(result, 0) + 1
    return counts


# ── the hooks: every session made or ended ──────────────────────────────────────────────────
_queue: list[tuple[float, str]] = []
_queue_cv = threading.Condition()
_worker: threading.Thread | None = None
_tokens: dict[str, str] = {}  # signer token → account key (from preload_user)
_tokens_lock = threading.Lock()


def schedule(svc: Any, account: str, delay: float = 0.0) -> None:
    """Refresh ACCOUNT's list soon, in the background (at once with ``INLINE``)."""
    if not enabled(svc) or not is_key(account):
        return
    if INLINE:  # tests: at once, and the later looks are left out
        if delay == 0:
            refresh(svc, account)
        return
    global _worker
    with _queue_cv:
        heapq.heappush(_queue, (time.monotonic() + delay, account))
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, args=(svc,), name="device-lists", daemon=True)
            _worker.start()
        _queue_cv.notify()


def _work(svc: Any) -> None:
    while True:
        with _queue_cv:
            while not _queue or _queue[0][0] > time.monotonic():
                _queue_cv.wait(timeout=(_queue[0][0] - time.monotonic()) if _queue else 60)
            _, account = heapq.heappop(_queue)
            # the same account due again by now: one refresh does it
            now = time.monotonic()
            _queue[:] = [(t, a) for t, a in _queue if a != account or t > now]
            heapq.heapify(_queue)
        try:
            refresh(svc, account)
        except Exception as exc:  # noqa: BLE001
            log.warning("devices: %s…: %r", account[:8], exc)


def account_of_token(svc: Any, token: str) -> str | None:
    """The account a signer token is for: what preload_user answered with it, or (the own
    signer's tokens name the account) the register's key of that partner."""
    with _tokens_lock:
        found = _tokens.get(token)
    if found:
        return found
    m = re.fullmatch(r"acct:odoo-partner-(\d+)", token or "")
    if m is None:
        return None
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["partner_id", "=", int(m.group(1))], ["state", "=", "active"]],
        ["keycast_pubkey"],
    )
    keys = sorted({str(r["keycast_pubkey"]).lower() for r in rows if r.get("keycast_pubkey")})
    return keys[0] if len(keys) == 1 and is_key(keys[0]) else None


def install(svc: Any) -> None:
    """Wrap the signer client so that every session made bound to a client key, and every
    session ended, updates the account's list. Idempotent."""
    client = svc.keycast
    if getattr(client, "_device_lists", False):
        return
    preload, create, revoke = client.preload_user, client.create_bunker, client.revoke_session

    def preload_user(*args: Any, **kwargs: Any) -> dict[str, Any]:
        out = preload(*args, **kwargs)
        try:
            if out.get("token") and is_key(str(out.get("pubkey") or "")):
                with _tokens_lock:
                    if len(_tokens) > 10_000:
                        _tokens.clear()
                    _tokens[str(out["token"])] = str(out["pubkey"]).lower()
        except Exception:  # noqa: BLE001 — never in the way of the call
            pass
        return out

    def create_bunker(token: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        out = create(token, *args, **kwargs)
        try:
            bound(svc, token, out)
        except Exception as exc:  # noqa: BLE001
            log.warning("devices: a new session not taken into its list: %r", exc)
        return out

    def revoke_session(token: str, bunker_pubkey: str) -> None:
        before = _client_of(svc, token, bunker_pubkey)
        revoke(token, bunker_pubkey)
        try:
            ended(svc, token, before)
        except Exception as exc:  # noqa: BLE001
            log.warning("devices: an ended session not taken out of its list: %r", exc)

    client.preload_user = preload_user
    client.create_bunker = create_bunker
    client.revoke_session = revoke_session
    client._device_lists = True


def bound(svc: Any, token: str, made: dict[str, Any]) -> None:
    """A session was made (``create_bunker``'s answer MADE). Bound to a client key: the list at
    once; not bound (its first connect binds it, inside the signer): looked at again later."""
    account = account_of_token(svc, token)
    if account is None:
        return
    client = str(made.get("client_pubkey") or "").lower()
    if made.get("bound") and is_key(client):
        store(svc).seen([client], account)
        store(svc).unremove(client)
        schedule(svc, account)
    # and again a little later: a session claimed by its first connect, or one made before
    # the pass it belongs to is active (a guest joining): the list follows within minutes
    for delay in UNBOUND_RECHECKS:
        schedule(svc, account, delay)


def _client_of(svc: Any, token: str, bunker_pubkey: str) -> str | None:
    """The client key of the session BUNKER_PUBKEY, before it is ended (best effort)."""
    try:
        for s in svc.keycast.list_sessions(token):
            if s.get("bunker_pubkey") == bunker_pubkey:
                client = str(s.get("client_pubkey") or "").lower()
                return client if is_key(client) else None
    except Exception as exc:  # noqa: BLE001
        log.info("devices: sessions not read before a revoke: %r", exc)
    return None


def ended(
    svc: Any,
    token: str,
    client: str | None,
    by_name: str | None = None,
    account: str | None = None,
) -> None:
    """A session was ended: its device recorded as removed (GET /api/sessions/removed), the
    account's list refreshed."""
    account = account or account_of_token(svc, token)
    if account is None:
        return
    if client:
        store(svc).mark_removed(client, account, int(time.time()), by_name)
    schedule(svc, account)


# ── the service ─────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def running(app: FastAPI):
    import asyncio

    async def loop() -> None:
        await asyncio.sleep(FIRST_CHECK_SECONDS)
        while True:
            svc = app.state.services
            try:
                if enabled(svc):
                    counts = await run_in_threadpool(check_all, svc)
                    if counts.get("published"):
                        log.info("devices: check: %s", counts)
            except Exception as exc:  # noqa: BLE001
                log.warning("devices: check failed: %r", exc)
            await asyncio.sleep(CHECK_SECONDS)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()


READS = None  # the limiter, made at register (events.Limiter)


def removed_answer(svc: Any, device: str) -> tuple[int, dict[str, Any]]:
    """GET /api/sessions/removed for the device key DEVICE (§16.6)."""
    st = store(svc)
    account = st.owner(device)
    if account is None:
        return 404, {"error": "unknown", "message": "This key is no session here."}
    try:
        rows, _ = sessions_of(svc, account)
    except Unknown:
        rows = []  # no pass any more, or the signer's word missing: the record decides
        if st.removal(device) is None:
            return 503, {"error": "signer", "message": "Try again in a moment."}
    if rows and any(str(s.get("client_pubkey") or "").lower() == device for s in rows):
        return 200, {"removed": False}
    gone = st.removal(device)
    if gone and gone["at"] >= time.time() - REMOVED_DAYS * 86400:
        out: dict[str, Any] = {"removed": True, "at": gone["at"]}
        if gone.get("by_name"):
            out["by_name"] = gone["by_name"]
        return 200, out
    return 404, {"error": "unknown", "message": "This key is no session here."}


def register(app: FastAPI) -> None:
    global READS
    from .events import Limiter
    from .holder_invites import ApiError, authenticate
    from .sales import client_ip

    READS = Limiter(30, 60)

    @app.get(REMOVED_PATH)
    async def sessions_removed(request: Request) -> JSONResponse:
        """NIP-98 by the device key itself: was this device's session ended?"""
        s = request.app.state.services
        if not READS.allow("ip:" + client_ip(request)):
            raise ApiError(429, "slow_down", "Too many questions; try again in a minute.")
        device = authenticate(
            s,
            request.app.state.api_seen,
            request.headers.get("authorization"),
            "GET",
            REMOVED_PATH,
            b"",
        )
        code, body = await run_in_threadpool(removed_answer, s, device)
        return JSONResponse(body, status_code=code, headers={"Cache-Control": "no-store"})
