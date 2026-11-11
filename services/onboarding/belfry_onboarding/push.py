"""Push notifications to the phone apps (docs/push.md §3–§5): devices, settings and the watcher.

**Devices.** ``POST /api/push/devices`` (belfry.<domain>/api/v1/push/devices), NIP-98 by anyone
holding an active pass (member, guest or agent):

- ``{"add": {"platform", "token", "env", "key", "app", "name"}}`` → ``{"id"}``. ``key`` is 32
  random bytes (base64) the device made; the belfry encrypts every notification to it. The same
  token again replaces the old row (a new key, maybe a new owner).
- ``{"remove": "<token>"}`` → ``{"ok": true}`` (only the caller's own device).
- ``{}`` → ``{"devices": [{"id", "app", "name", "since"}]}``.

A device goes away when its key holds no active pass any more (core._close_pass → ``closed``,
and the hourly prune), when the gateway answers 410, or after ``DEVICE_DAYS`` without an
``add`` from it (the apps register on every start). Devices live in the register's own table
(register.py ``push_*``): push needs the belfry's own register (REGISTER=belfry); with Odoo the
API answers 409 and nothing is watched.

**Settings.** ``POST /api/notifications``: ``{}`` → the caller's settings, ``{"set": {…}}``
merges and returns them: the switches ``dms``, ``mentions`` (mentions and replies),
``announcements``, ``pins`` (all on by default) and ``channels`` = {group id: all | default |
mentions | off} (``default`` removes the entry). Kept as register param ``belfry.push.<partner>``.

**Watcher.** Two live subscriptions, reconnecting with backoff: the groups relay as the groups
key (its owner: it reads every group, closed pods too) for kinds 9, 1111 and 1985 labels
``belfry/pin``; the community relay as the belfry key for gift wraps (1059) to people with a
device. Each event is handled once (ids kept in memory). Group events older than ``MAX_AGE`` on
arrival are skipped; events the relay sends before EOSE are history on the first connection
(a restart never replays) and caught up after a reconnect. Gift wraps carry a randomised
``created_at`` (NIP-59), so they count by arrival: only those that come in live.

Recipients of a group event: keys with a device and an active pass that are in the group's
39002 member list, or community admins (not guests) for any group, minus the author. The type,
first that applies: ``mention`` (a ``p`` tag or ``nostr:npub`` of them; on a 1111 the ``p``/``P``
naming the message it answers is the reply marker, not a mention) → ``reply`` (the ``e``/``E``/
``q`` it points at is theirs, or a 1111 in a thread they wrote in) → ``announcement`` (``t
announcement`` by a group admin, followers.py's rule) → ``message``. A pin (``l pinned`` by a
group or community admin) notifies ``pin`` with the pinned message's text. The channel's level
and the type's switch decide; at level ``all`` a type switched off still comes as ``message``.

**Sending.** The plaintext (``v``, ``domain``, ``type``, ``title``, ``body`` ≤ 180 characters,
``channel``, ``event``, ``from``, ``url``; at most 2 KB) is NIP-44 v2 encrypted with the device
key as conversation key, and POSTed to ``PUSH_GATEWAY_URL``/v1/send with a NIP-98 header signed
by the belfry key. Empty ``PUSH_GATEWAY_URL``: no push at all.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import random
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .core import Services, m2o_id
from .nip46check import nip44_encrypt, sign_event

log = logging.getLogger(__name__)

PATH_DEVICES = "/api/push/devices"
PATH_SETTINGS = "/api/notifications"
PLATFORMS = ("apns", "fcm")
ENVS = ("production", "sandbox")
APPS = ("belfry-ios", "belfry-android")
LEVELS = ("all", "default", "mentions", "off")
SWITCHES = ("dms", "mentions", "announcements", "pins")
DEFAULTS: dict[str, Any] = {s: True for s in SWITCHES} | {"channels": {}}
TOKEN = re.compile(r"^[A-Za-z0-9_:.\-]{8,4096}$")
CHANNEL = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_CHANNELS = 1000
DEVICE_DAYS = 60
MAX_AGE = 600  # seconds: an older group event on arrival is history
MAX_PLAIN = 2048  # bytes of plaintext before encryption
BODY_CHARS = 180
PIN_NS = "belfry/pin"
CACHE_SECONDS = 60
NAMES_SECONDS = 600
SEEN_MAX = 20_000
QUEUE_MAX = 2000
PRUNE_SECONDS = 3600
WRAP_WINDOW = 3 * 86400  # gift wraps are backdated up to two days (NIP-59)
NPUB = re.compile(r"nostr:(npub1[02-9ac-hj-np-z]+)")
URGENT = {"dm", "mention", "reply", "announcement"}


# ── who asks ────────────────────────────────────────────────────────────────────────────────
def own(svc: Services) -> bool:
    from .register import Register

    return isinstance(svc.odoo, Register)


def enabled(svc: Services) -> bool:
    """Push runs with the belfry's own register, a gateway and the belfry key."""
    from .homepage import belfry_pubkey

    return bool(own(svc) and svc.settings.push_gateway_url and belfry_pubkey(svc.settings))


def person(svc: Services, caller: str) -> int:
    """The partner behind an active pass of CALLER (any tier), or ApiError 403."""
    from .holder_invites import ApiError

    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "=", caller]],
        ["partner_id"],
        limit=1,
    )
    partner = m2o_id(rows[0].get("partner_id")) if rows else None
    if not partner:
        raise ApiError(403, "no_pass", "This key holds no active pass.")
    return int(partner)


# ── devices ─────────────────────────────────────────────────────────────────────────────────
def _device(data: Any) -> dict[str, Any]:
    from .holder_invites import ApiError

    def bad(what: str) -> ApiError:
        return ApiError(400, "invalid", f"add: {what}")

    if not isinstance(data, dict):
        raise bad("send an object")
    platform, env, app = data.get("platform"), data.get("env"), data.get("app")
    token, key, name = data.get("token"), data.get("key"), data.get("name", "")
    if platform not in PLATFORMS:
        raise bad("platform is apns or fcm")
    if env not in ENVS:
        raise bad("env is production or sandbox")
    if app not in APPS:
        raise bad("app is belfry-ios or belfry-android")
    if not isinstance(token, str) or not TOKEN.match(token):
        raise bad("token")
    try:
        raw = base64.b64decode(key or "", validate=True) if isinstance(key, str) else b""
    except ValueError:
        raw = b""
    if len(raw) != 32:
        raise bad("key is 32 bytes in base64")
    if not isinstance(name, str):
        raise bad("name is text")
    return {
        "platform": platform,
        "env": env,
        "app": app,
        "token": token,
        "key": base64.b64encode(raw).decode(),
        "name": " ".join(name.split())[:80],
    }


def devices(svc: Services, caller: str, data: dict[str, Any]) -> dict[str, Any]:
    from .holder_invites import ApiError

    partner = person(svc, caller)
    reg = svc.odoo
    if "add" in data:
        dev = _device(data["add"]) | {"pubkey": caller, "partner_id": partner}
        return {"id": reg.push_add(dev, int(time.time()))}
    if "remove" in data:
        token = data["remove"]
        if not isinstance(token, str) or not token:
            raise ApiError(400, "invalid", 'Send {"remove": "<token>"}.')
        reg.push_remove(token, caller)
        return {"ok": True}
    return {
        "devices": [
            {"id": d["id"], "app": d["app"], "name": d["name"], "since": d["since"]}
            for d in reg.push_devices([caller])
        ]
    }


def closed(svc: Services, pass_row: dict[str, Any]) -> None:
    """A pass closed (core._close_pass): its key's devices go unless another pass is active."""
    if not own(svc):
        return
    key = pass_row.get("keycast_pubkey") or ""
    if not key:
        return
    if svc.odoo.search_read(
        "belfry.pass", [["state", "=", "active"], ["keycast_pubkey", "=", key]], ["id"], limit=1
    ):
        return
    n = svc.odoo.push_remove_key(key)
    if n:
        log.info("push: %d device(s) removed with pass %s", n, pass_row.get("id"))


def prune(svc: Services, now: float | None = None) -> int:
    """Devices not registered again in DEVICE_DAYS, or whose key holds no active pass."""
    from .whois import kinds

    if not own(svc):
        return 0
    now = time.time() if now is None else now
    n = svc.odoo.push_prune(int(now) - DEVICE_DAYS * 86400, set(kinds(svc)))
    if n:
        log.info("push: %d stale device(s) removed", n)
    return n


# ── settings ────────────────────────────────────────────────────────────────────────────────
def _param(partner: int) -> str:
    return f"belfry.push.{int(partner)}"


def get_settings(svc: Services, partner: int) -> dict[str, Any]:
    try:
        stored = json.loads(svc.odoo.param(_param(partner), "") or "{}")
    except ValueError:
        stored = {}
    out = {s: bool(stored.get(s, True)) for s in SWITCHES}
    chans = stored.get("channels") if isinstance(stored.get("channels"), dict) else {}
    out["channels"] = {c: lv for c, lv in chans.items() if lv in LEVELS and lv != "default"}
    return out


def set_settings(svc: Services, partner: int, change: Any) -> dict[str, Any]:
    from .holder_invites import ApiError

    if not isinstance(change, dict):
        raise ApiError(400, "invalid", 'Send {"set": {…}}.')
    out = get_settings(svc, partner)
    for k, v in change.items():
        if k in SWITCHES:
            if not isinstance(v, bool):
                raise ApiError(400, "invalid", f"{k} is true or false")
            out[k] = v
        elif k == "channels":
            if not isinstance(v, dict):
                raise ApiError(400, "invalid", "channels: {group id: level}")
            for c, lv in v.items():
                if not isinstance(c, str) or not CHANNEL.match(c) or lv not in LEVELS:
                    raise ApiError(400, "invalid", f"channels: a group id and {'/'.join(LEVELS)}")
                if lv == "default":
                    out["channels"].pop(c, None)
                else:
                    out["channels"][c] = lv
            if len(out["channels"]) > MAX_CHANNELS:
                raise ApiError(400, "invalid", f"at most {MAX_CHANNELS} channels")
        else:
            raise ApiError(400, "invalid", f"unknown setting {k}")
    svc.odoo.set_param(_param(partner), json.dumps(out, sort_keys=True))
    return out


def allowed(prefs: dict[str, Any], channel: str, kind: str) -> str | None:
    """The type to send for KIND in CHANNEL under PREFS, or None (docs/push.md §3)."""
    if kind == "dm":
        return "dm" if prefs["dms"] else None
    level = prefs["channels"].get(channel, "default")
    if level == "off":
        return None
    ok = {
        "mention": prefs["mentions"],
        "reply": prefs["mentions"],
        "announcement": prefs["announcements"] and level != "mentions",
        "pin": prefs["pins"] and level != "mentions",
        "message": level == "all",
    }.get(kind, False)
    if ok:
        return kind
    if level == "all" and kind in ("mention", "reply", "announcement"):
        return "message"  # every message at "all", even of a type switched off
    return None


# ── the gateway ─────────────────────────────────────────────────────────────────────────────
class Gateway:
    """POST /v1/send at PUSH_GATEWAY_URL, NIP-98 by the belfry key. ``send`` answers ok, gone
    (the token is dead: the device is removed), rate or error."""

    def __init__(self, svc: Services, http: httpx.Client | None = None):
        from .homepage import belfry_key

        self.url = svc.settings.push_gateway_url.rstrip("/") + "/v1/send"
        key = belfry_key(svc.settings)
        if key is None:
            raise RuntimeError("no belfry key (secret belfry_nsec)")
        self.secret = key.secret
        self.http = http or httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0))

    def header(self, body: bytes) -> str:
        event = sign_event(
            self.secret,
            {
                "kind": 27235,
                "created_at": int(time.time()),
                "tags": [
                    ["u", self.url],
                    ["method", "POST"],
                    ["payload", hashlib.sha256(body).hexdigest()],
                ],
                "content": "",
            },
        )
        return "Nostr " + base64.b64encode(json.dumps(event).encode()).decode()

    def send(self, device: dict[str, Any], payload: str, collapse: str, urgent: bool) -> str:
        body = json.dumps(
            {
                "platform": device["platform"],
                "token": device["token"],
                "env": device["env"],
                "payload": payload,
                "collapse": collapse[:64],
                "urgent": urgent,
            }
        ).encode()
        try:
            r = self.http.post(
                self.url,
                content=body,
                headers={"Authorization": self.header(body), "Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            log.warning("push: gateway unreachable: %s", exc)
            return "error"
        if r.status_code == 200:
            return "ok"
        if r.status_code == 410:
            return "gone"
        if r.status_code == 429:
            return "rate"
        log.warning("push: gateway answered %s %s", r.status_code, r.text[:200])
        return "error"


def encrypt(device_key_b64: str, plain: dict[str, Any]) -> str:
    """NIP-44 v2 with the device's 32-byte key as the conversation key (docs/push.md §4)."""
    return nip44_encrypt(base64.b64decode(device_key_b64), json.dumps(plain, ensure_ascii=False))


def fit(plain: dict[str, Any]) -> dict[str, Any]:
    """Cut ``body`` (then ``title``) until the JSON is at most MAX_PLAIN bytes."""
    plain = dict(plain)
    plain["body"] = _cut(plain.get("body", ""), BODY_CHARS)
    for field in ("body", "title"):
        while len(json.dumps(plain, ensure_ascii=False).encode()) > MAX_PLAIN and plain[field]:
            n = len(plain[field]) // 2
            plain[field] = _cut(plain[field], n) if n > 1 else ""
    return plain


def _cut(text: str, n: int) -> str:
    return text if len(text) <= n else text[: max(0, n - 1)] + "…"


# ── the watcher ─────────────────────────────────────────────────────────────────────────────
def _tag(e: dict[str, Any], k: str) -> str:
    return next((t[1] for t in e.get("tags", []) if t and t[0] == k and len(t) > 1), "")


def _tags(e: dict[str, Any], *ks: str) -> list[list[str]]:
    return [t for t in e.get("tags", []) if t and t[0] in ks and len(t) > 1]


class Watcher:
    """Decides who gets a notification for an event, builds it and sends it (sync: runs in a
    worker thread). ``handle`` returns the (pubkey, type) pairs sent, for tests and logs."""

    def __init__(self, svc: Services, gateway: Any = None, clock: Callable[[], float] = time.time):
        self.svc = svc
        self.gateway = gateway
        self.clock = clock
        self._lock = threading.Lock()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._events: OrderedDict[str, dict[str, Any]] = OrderedDict()  # recent 9/1111 by id
        self._groups: dict[str, tuple[float, set[str], set[str]]] = {}  # h → (at, admins, members)
        self._people: tuple[float, dict[str, str]] = (0.0, {})
        self._names: dict[str, tuple[float, str]] = {}

    # the relays ──────────────────────────────────────────────────────────────────────────────
    def groups_query(self, filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from . import followers

        got = followers._relay(self.svc)
        if got is None:
            return []
        relay, sign = got
        return relay.query(filters, sign)

    # what is known ───────────────────────────────────────────────────────────────────────────
    def first_time(self, event_id: str) -> bool:
        with self._lock:
            if event_id in self._seen:
                return False
            self._seen[event_id] = None
            while len(self._seen) > SEEN_MAX:
                self._seen.popitem(last=False)
            return True

    def remember(self, e: dict[str, Any]) -> None:
        if e.get("kind") in (9, 1111):
            with self._lock:
                self._events[e["id"]] = e
                while len(self._events) > SEEN_MAX // 4:
                    self._events.popitem(last=False)

    def people(self) -> dict[str, str]:
        """Every key with an active pass → member / agent / guest (whois.kinds), cached."""
        from .whois import kinds

        at, known = self._people
        if self.clock() - at > CACHE_SECONDS:
            known = kinds(self.svc)
            self._people = (self.clock(), known)
        return known

    def group(self, h: str) -> tuple[set[str], set[str]]:
        """(admins, members) of group H: 39001 and the creator (9007), 39002."""
        held = self._groups.get(h)
        if held and self.clock() - held[0] < CACHE_SECONDS:
            return held[1], held[2]
        found = self.groups_query(
            [{"kinds": [39001, 39002], "#d": [h]}, {"kinds": [9007], "#h": [h], "limit": 5}]
        )
        latest: dict[int, dict[str, Any]] = {}
        admins: set[str] = set()
        for e in found:
            if e["kind"] == 9007:
                admins.add(e["pubkey"])
            elif e["created_at"] >= latest.get(e["kind"], {"created_at": -1})["created_at"]:
                latest[e["kind"]] = e
        admins |= {t[1] for t in _tags(latest.get(39001, {}), "p")}
        members = {t[1] for t in _tags(latest.get(39002, {}), "p")}
        self._groups[h] = (self.clock(), admins, members)
        return admins, members

    def event(self, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            held = self._events.get(event_id)
        if held:
            return held
        found = self.groups_query([{"ids": [event_id], "limit": 1}])
        if found:
            self.remember(found[0])
            return found[0]
        return None

    def names(self, keys: set[str]) -> dict[str, str]:
        from . import mentions

        now = self.clock()
        out = {k: v[1] for k, v in self._names.items() if k in keys and now - v[0] < NAMES_SECONDS}
        missing = keys - set(out)
        if missing:
            try:
                got = mentions._names(self.svc, missing)
            except Exception as exc:  # noqa: BLE001 — a notification without a name is fine
                log.warning("push: names: %s", exc)
                got = {}
            for k in missing:
                self._names[k] = (now, got.get(k, ""))
                out[k] = got.get(k, "")
        return out

    # deciding ───────────────────────────────────────────────────────────────────────────────
    def handle(self, e: dict[str, Any], source: str, live: bool = True) -> list[tuple[str, str]]:
        if not isinstance(e, dict) or not isinstance(e.get("id"), str):
            return []
        if not self.first_time(e["id"]):
            return []
        self.remember(e)
        if source == "community":
            if not live or e.get("kind") != 1059:
                return []
            return self.dm(e)
        if e.get("created_at", 0) < self.clock() - MAX_AGE:
            return []
        if e.get("kind") in (9, 1111):
            return self.message(e)
        if e.get("kind") == 1985:
            return self.pin(e)
        return []

    def _recipients(self, h: str, author: str) -> dict[str, list[dict[str, Any]]]:
        """Devices of the people who should hear about an event in group H (docs/push.md §5)."""
        from . import apps

        devs = self.svc.odoo.push_devices()
        if not devs:
            return {}
        people = self.people()
        admins_here: set[str] = set()
        with contextlib.suppress(Exception):
            admins_here = {k for k in apps.admin_pubkeys(self.svc) if people.get(k) != "guest"}
        _admins, members = self.group(h)
        out: dict[str, list[dict[str, Any]]] = {}
        for d in devs:
            k = d["pubkey"]
            if k == author or k not in people or (k not in members and k not in admins_here):
                continue
            out.setdefault(k, []).append(d)
        return out

    def message(self, e: dict[str, Any]) -> list[tuple[str, str]]:
        h = _tag(e, "h")
        if not h:
            return []
        rcpt = self._recipients(h, e["pubkey"])
        if not rcpt:
            return []
        from .nostr import to_hex_pubkey

        answered = {t[3] for t in _tags(e, "e", "E") if len(t) > 3 and t[3]}
        answered |= {t[1] for t in _tags(e, "P")} if e["kind"] == 1111 else set()
        tagged = {t[1] for t in _tags(e, "p")}
        if e["kind"] == 1111:
            tagged -= answered
        for m in NPUB.finditer(e.get("content") or ""):
            with contextlib.suppress(Exception):
                tagged.add(to_hex_pubkey(m.group(1)))
        # whose message it answers: the author hints in e/E, else the events themselves
        targets = {t[1] for t in _tags(e, "e", "E", "q") if re.fullmatch(r"[0-9a-f]{64}", t[1])}
        owners = set(answered)
        for tid in targets:
            if not any(len(t) > 3 and t[1] == tid and t[3] for t in _tags(e, "e", "E")):
                got = self.event(tid)
                if got:
                    owners.add(got["pubkey"])
        thread: set[str] = set()
        root = _tag(e, "E") if e["kind"] == 1111 else ""
        if root:
            thread = {
                r["pubkey"]
                for r in self.groups_query([{"kinds": [1111], "#E": [root], "limit": 500}])
                if r["id"] != e["id"]
            }
        announce = False
        if e["kind"] == 9 and ["t", "announcement"] in [t[:2] for t in e.get("tags", [])]:
            admins, _ = self.group(h)
            announce = e["pubkey"] in admins
        kinds: dict[str, str] = {}
        for k in rcpt:
            if k in tagged:
                kinds[k] = "mention"
            elif k in owners or k in thread:
                kinds[k] = "reply"
            elif announce:
                kinds[k] = "announcement"
            else:
                kinds[k] = "message"
        return self._send_group(e, h, rcpt, kinds, e)

    def pin(self, e: dict[str, Any]) -> list[tuple[str, str]]:
        from . import apps

        if _tag(e, "L") != PIN_NS or ["l", "pinned", PIN_NS] not in [t[:3] for t in e["tags"]]:
            return []
        h, target = _tag(e, "h"), _tag(e, "e")
        if not h or not target:
            return []
        admins, _ = self.group(h)
        community_admins: set[str] = set()
        with contextlib.suppress(Exception):
            community_admins = set(apps.admin_pubkeys(self.svc))
        if e["pubkey"] not in admins | community_admins:
            return []
        msg = self.event(target)
        if msg is None or _tag(msg, "h") != h:
            return []
        rcpt = self._recipients(h, e["pubkey"])
        return self._send_group(e, h, rcpt, {k: "pin" for k in rcpt}, msg)

    def dm(self, e: dict[str, Any]) -> list[tuple[str, str]]:
        from . import followers

        keys = {t[1] for t in _tags(e, "p")}
        people = self.people()
        devs = [d for d in self.svc.odoo.push_devices(sorted(keys)) if d["pubkey"] in people]
        domain = self.svc.settings.nip05_domain
        plain = {
            "v": 1,
            "domain": domain,
            "type": "dm",
            "title": followers.name(self.svc),
            "body": "New message",
            "url": f"https://chat.{domain}/#/dm",
        }
        sent: list[tuple[str, str]] = []
        prefs: dict[int, dict[str, Any]] = {}
        for d in devs:
            p = prefs.setdefault(d["partner_id"], get_settings(self.svc, d["partner_id"]))
            if allowed(p, "", "dm") and self._deliver(d, plain, "dm", True):
                sent.append((d["pubkey"], "dm"))
        return sent

    # sending ────────────────────────────────────────────────────────────────────────────────
    def _send_group(
        self,
        e: dict[str, Any],
        h: str,
        rcpt: dict[str, list[dict[str, Any]]],
        kinds: dict[str, str],
        shown: dict[str, Any],
    ) -> list[tuple[str, str]]:
        """Send one notification per device; SHOWN is the message whose text is shown (the
        event itself, or the pinned message)."""
        from . import followers, mentions
        from .nostr import to_hex_pubkey

        want: dict[str, str] = {}
        for k, devs in rcpt.items():
            t = allowed(get_settings(self.svc, devs[0]["partner_id"]), h, kinds[k])
            if t:
                want[k] = t
        if not want:
            return []
        keys = {shown["pubkey"]}
        for m in NPUB.finditer(shown.get("content") or ""):
            with contextlib.suppress(Exception):
                keys.add(to_hex_pubkey(m.group(1)))
        names = self.names(keys)
        who = names.get(shown["pubkey"]) or "Someone"
        text = mentions._text(self.svc, shown, names)
        if not text:
            text = "📷 Photo" if _tags(shown, "imeta") else "…"
        label = followers.channel_label(self.svc, h)
        domain = self.svc.settings.nip05_domain
        sent: list[tuple[str, str]] = []
        for k, t in want.items():
            title = {"announcement": "📣 " + label, "pin": "📌 " + label}.get(t, label)
            plain = {
                "v": 1,
                "domain": domain,
                "type": t,
                "title": title,
                "body": f"{who}: {text}",
                "channel": h,
                "event": shown["id"],
                "from": shown["pubkey"],
                "url": self.link(shown),
            }
            ok = [self._deliver(d, plain, h, t in URGENT) for d in rcpt[k]]
            if any(ok):
                sent.append((k, t))
        return sent

    def link(self, e: dict[str, Any]) -> str:
        from . import mentions

        return mentions.link(self.svc, e)

    def _deliver(self, device: dict[str, Any], plain: dict[str, Any], collapse: str, urgent: bool):
        if self.gateway is None:
            return False
        result = self.gateway.send(device, encrypt(device["key"], fit(plain)), collapse, urgent)
        if result == "gone":
            self.svc.odoo.push_remove(device["token"])
            log.info("push: a dead device of %s… removed", device["pubkey"][:8])
        return result == "ok"


# ── the subscriptions ───────────────────────────────────────────────────────────────────────
Connect = Callable[[str], Any]  # url → async context manager with send() / recv()


def _default_connect(url: str) -> Any:
    from websockets.asyncio.client import connect

    return connect(url, open_timeout=15, close_timeout=5, max_size=2**22)


async def subscribe(
    url: str,
    filters: Callable[[bool], list[dict[str, Any]]],
    sign: Callable[[dict[str, Any]], dict[str, Any]],
    on_event: Callable[[dict[str, Any], bool, bool], Awaitable[None]],
    connect: Connect = _default_connect,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    stop: asyncio.Event | None = None,
    refresh: float = 30.0,
) -> None:
    """Keep one REQ open on URL, forever: answer AUTH (NIP-42) with SIGN, call ON_EVENT(event,
    live, first) for each event (``live`` after EOSE; ``first`` on the first connection), and
    reconnect with exponential backoff (1 s to 5 min, with jitter). FILTERS(first) is asked again
    every REFRESH seconds; a change replaces the subscription."""
    sub = "belfry-push"
    backoff = 1.0
    first = True
    stop = stop or asyncio.Event()
    while not stop.is_set():
        try:
            async with connect(url) as ws:
                current = filters(first)
                live = False
                auth_id = ""
                await ws.send(json.dumps(["REQ", sub, *current]))
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=refresh)
                    except TimeoutError:
                        wanted = filters(first)
                        if wanted != current:
                            current, live = wanted, False
                            await ws.send(json.dumps(["REQ", sub, *current]))
                        continue
                    msg = json.loads(raw)
                    if not isinstance(msg, list) or not msg:
                        continue
                    if msg[0] == "AUTH" and len(msg) > 1 and not auth_id:
                        auth = sign(
                            {
                                "kind": 22242,
                                "created_at": int(time.time()),
                                "tags": [["relay", url], ["challenge", str(msg[1])]],
                                "content": "",
                            }
                        )
                        auth_id = auth["id"]
                        await ws.send(json.dumps(["AUTH", auth]))
                    elif msg[0] == "OK" and len(msg) > 2 and msg[1] == auth_id:
                        if not msg[2]:
                            raise RuntimeError(f"AUTH refused: {msg[3:] or ''}")
                        await ws.send(json.dumps(["REQ", sub, *current]))
                    elif msg[0] == "EVENT" and len(msg) > 2 and msg[1] == sub:
                        await on_event(msg[2], live, first)
                    elif msg[0] == "EOSE" and len(msg) > 1 and msg[1] == sub:
                        live = True
                        backoff = 1.0
                    elif msg[0] == "CLOSED" and len(msg) > 1 and msg[1] == sub:
                        reason = str(msg[2]) if len(msg) > 2 else ""
                        if reason.startswith("auth-required"):
                            continue  # the AUTH answer brings a new REQ
                        raise RuntimeError(f"subscription closed: {reason}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — reconnect
            log.warning("push: %s: %s; again in %.0f s", url, exc, backoff)
        else:
            if stop.is_set():
                return
        first = False
        if stop.is_set():
            return
        await sleep(backoff * (0.8 + 0.4 * random.random()))
        backoff = min(backoff * 2, 300.0)


class Runner:
    """The watcher's moving parts: one queue, one worker, the two subscriptions, the prune."""

    def __init__(self, svc: Services, watcher: Watcher, connect: Connect = _default_connect):
        self.svc = svc
        self.watcher = watcher
        self.connect = connect
        self.queue: asyncio.Queue[tuple[dict[str, Any], str, bool]] = asyncio.Queue(QUEUE_MAX)

    def _enqueue(self, source: str) -> Callable[[dict[str, Any], bool, bool], Awaitable[None]]:
        async def on_event(e: dict[str, Any], live: bool, first: bool) -> None:
            if not live and first:  # history on the first connection: never replayed
                if isinstance(e, dict) and isinstance(e.get("id"), str):
                    self.watcher.first_time(e["id"])
                return
            try:
                self.queue.put_nowait((e, source, live))
            except asyncio.QueueFull:
                log.warning("push: queue full, an event from %s dropped", source)

        return on_event

    def groups_filters(self, _first: bool) -> list[dict[str, Any]]:
        since = int(time.time()) - MAX_AGE
        return [
            {"kinds": [9, 1111], "since": since, "limit": 200},
            {"kinds": [1985], "#L": [PIN_NS], "since": since, "limit": 50},
        ]

    def community_filters(self, _first: bool) -> list[dict[str, Any]]:
        keys = sorted({d["pubkey"] for d in self.svc.odoo.push_devices()})
        # nobody has a device yet: a filter that matches nothing, kept open for the next refresh
        keys = keys or ["0" * 64]
        # gift wraps are backdated: a wide `since`, and only the newest one before EOSE
        return [{"kinds": [1059], "#p": keys, "since": int(time.time()) - WRAP_WINDOW, "limit": 1}]

    async def work(self) -> None:
        while True:
            e, source, live = await self.queue.get()
            try:
                sent = await run_in_threadpool(self.watcher.handle, e, source, live)
                if sent:
                    log.info("push: %s %s… → %d", source, str(e.get("id"))[:8], len(sent))
            except Exception as exc:  # noqa: BLE001 — one event never stops the watcher
                log.warning("push: event %s… failed: %s", str(e.get("id"))[:8], exc)

    async def pruning(self) -> None:
        while True:
            try:
                await run_in_threadpool(prune, self.svc)
            except Exception as exc:  # noqa: BLE001 — retried next hour
                log.warning("push: prune failed: %s", exc)
            await asyncio.sleep(PRUNE_SECONDS)

    def tasks(self) -> list[Awaitable[None]]:
        from . import community, followers

        jobs: list[Awaitable[None]] = [self.work(), self.pruning()]
        got = followers._relay(self.svc)
        if got is not None:
            relay, sign = got
            jobs.append(
                subscribe(
                    relay.url, self.groups_filters, sign, self._enqueue("groups"), self.connect
                )
            )
        if self.svc.settings.relay_url:
            jobs.append(
                subscribe(
                    self.svc.settings.relay_url,
                    self.community_filters,
                    community.belfry_signer(self.svc),
                    self._enqueue("community"),
                    self.connect,
                )
            )
        return jobs


@contextlib.asynccontextmanager
async def running(app: FastAPI):
    svc = app.state.services
    tasks: list[asyncio.Task[None]] = []
    if enabled(svc):
        try:
            runner = Runner(svc, Watcher(svc, Gateway(svc)))
            tasks = [asyncio.create_task(job) for job in runner.tasks()]  # type: ignore[arg-type]
            log.info("push: watching, gateway %s", svc.settings.push_gateway_url)
        except Exception as exc:  # noqa: BLE001 — the service runs without push
            log.warning("push: not started: %s", exc)
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t


# ── the API ─────────────────────────────────────────────────────────────────────────────────
def register(app: FastAPI) -> None:
    from .holder_invites import ApiError, authenticate

    async def _read(request: Request, path: str) -> tuple[Services, str, dict[str, Any]]:
        s = request.app.state.services
        body = await request.body()
        caller = authenticate(
            s, request.app.state.api_seen, request.headers.get("authorization"), "POST", path, body
        )
        if not own(s):
            raise ApiError(409, "unsupported", "Push needs the belfry's own register.")
        try:
            data: Any = json.loads(body or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_json", "Send a JSON object.")
        return s, caller, data

    @app.post(PATH_DEVICES)
    async def push_devices(request: Request) -> JSONResponse:
        s, caller, data = await _read(request, PATH_DEVICES)
        out = await run_in_threadpool(devices, s, caller, data)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    @app.post(PATH_SETTINGS)
    async def notifications(request: Request) -> JSONResponse:
        s, caller, data = await _read(request, PATH_SETTINGS)

        def run() -> dict[str, Any]:
            partner = person(s, caller)
            if "set" in data:
                return set_settings(s, partner, data["set"])
            return get_settings(s, partner)

        out = await run_in_threadpool(run)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})
