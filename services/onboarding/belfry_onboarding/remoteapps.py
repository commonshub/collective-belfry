"""Remote apps: a member signs in to a third-party Nostr app with their Belfry account
(docs/remote-apps.md).

The app shows a ``nostrconnect://`` QR (NIP-46, started by the client: its own key, a few
public relays, a secret). The phone app scans it and posts it here; from then on this service
is the app's remote signer, on the app's relays, with the member's key held by the signer
(Keycast, ``POST /api/nostr`` with the member's token). The signer itself is not changed: it
listens on its own relay only and never sends the acknowledgement such an app waits for.

- ``POST /api/remote-apps/preview``, ``POST /api/remote-apps``, ``GET /api/remote-apps``,
  ``POST /api/remote-apps/<id>/disconnect`` (belfry.<domain>/api/v1/…), NIP-98 by the account's
  own key: a member or an admin, never a guest, an agent or a website session.
- ``Hub``: one websocket per relay in use, a subscription for kind 24133 addressed to the
  members who have a connection there, and the answers. ``allowed`` says what an app may ask.

The URI's secret is used once, for the acknowledgement, and never stored or logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import random
import re
import secrets
import time
import urllib.parse
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .blossom import event_id, tag_values, verify_signature
from .core import Services, m2o_id
from .holder_invites import ApiError, authenticate

log = logging.getLogger(__name__)

API = "/api/remote-apps"
KIND = 24133
IDLE_DAYS = 30
MAX_APPS = 10  # live connections per account
CONNECTS_PER_HOUR = 10
MAX_URI_RELAYS = 8
MAX_RELAYS = 30  # distinct relays over every connection
ACK_SECONDS = 8.0
REQUESTS_PER_MINUTE = 120
SEEN_MAX = 2000
MAX_AGE = 120  # a request older than this is not answered
TOKEN_SECONDS = 300
TOUCH_SECONDS = 60
REFRESH_SECONDS = 30.0
URI_MAX = 4096
NO_STORE = {"Cache-Control": "no-store"}

HEX = re.compile(r"^[0-9a-f]{64}$")
HOST = re.compile(
    r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$"
)
PATH = re.compile(r"^[A-Za-z0-9._~/%-]{0,120}$")
PERM = re.compile(r"^[a-z0-9_]{1,40}(:[A-Za-z0-9_.-]{1,40})?$")
PRIVATE_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".arpa")
OPEN_METHODS = ("connect", "ping", "get_public_key", "get_relays")
CRYPT_METHODS = ("nip44_encrypt", "nip44_decrypt", "nip04_encrypt", "nip04_decrypt")
DECRYPT_METHODS = ("nip44_decrypt", "nip04_decrypt")

Connect = Callable[[str], Any]


# ── the URI ─────────────────────────────────────────────────────────────────────────────────
class UriError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _public_host(host: str) -> bool:
    """A name on the public internet, as far as its spelling tells: no IP literal, no
    single-label name (the belfry's own services: ``relay``, ``keycast``…), no private suffix."""
    host = host.lower().rstrip(".")
    if not HOST.match(host) or host.endswith(PRIVATE_SUFFIXES):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


def _relay(raw: str) -> str:
    """The relay's address as kept (``wss://host[:port][/path]``), or UriError bad_relay."""
    if len(raw) > 200 or not raw.isascii():
        raise UriError("bad_relay")
    try:
        u = urllib.parse.urlsplit(raw.strip())
        port = u.port
    except ValueError as exc:
        raise UriError("bad_relay") from exc
    host = (u.hostname or "").lower()
    if (
        u.scheme.lower() != "wss"
        or u.username is not None
        or u.password is not None
        or u.query
        or u.fragment
        or "[" in u.netloc
        or not _public_host(host)
        or not PATH.match(u.path)
        or port == 0
    ):
        raise UriError("bad_relay")
    out = "wss://" + host.rstrip(".")
    if port and port != 443:
        out += f":{port}"
    return out + u.path.rstrip("/")


def _https(raw: str | None, limit: int) -> tuple[str, str] | None:
    """(the address, its host) for an https address of a public name; else None."""
    raw = (raw or "").strip()
    if not raw or len(raw) > limit or not raw.isascii() or any(c <= " " for c in raw):
        return None
    try:
        u = urllib.parse.urlsplit(raw)
        u.port  # noqa: B018 — raises for a port that is none
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if u.scheme.lower() != "https" or u.username is not None or u.password is not None:
        return None
    if "[" in u.netloc or not _public_host(host):
        return None
    return raw, host.rstrip(".")


def _name(raw: str | None) -> str | None:
    raw = (raw or "").strip()
    if not raw or len(raw) > 60 or any(ord(c) < 32 or ord(c) in (127, 0x2028, 0x2029) for c in raw):
        return None
    return raw


def parse_perms(raw: str | None) -> list[str] | None:
    """The URI's ``perms``: None when it names none (the app may ask anything the rules
    allow), else the entries that read as ``method`` or ``method:param`` (the rest dropped:
    what is not understood is not granted)."""
    if raw is None or not raw.strip():
        return None
    out: list[str] = []
    for part in raw.split(",")[:64]:
        part = part.strip()
        if PERM.match(part) and part not in out:
            out.append(part)
    return out


def _on_curve(key: str) -> bool:
    """Whether 64 hex characters are a public key (nothing can be encrypted to the rest)."""
    from coincurve import PublicKey

    try:
        PublicKey(b"\x02" + bytes.fromhex(key))
    except Exception:  # noqa: BLE001
        return False
    return True


def parse_uri(uri: Any) -> dict[str, Any]:
    """The parts of a ``nostrconnect://`` URI: client, relays, secret, perms, name, url, host,
    image. UriError(reason): bad_scheme, bad_key, no_relay, bad_relay, no_secret."""
    if not isinstance(uri, str) or not 0 < len(uri) <= URI_MAX:
        raise UriError("bad_scheme")
    uri = uri.strip()
    if uri[:15].lower() != "nostrconnect://":
        raise UriError("bad_scheme")
    rest = uri[15:]
    key, _, query = rest.partition("?")
    key = key.rstrip("/").lower()
    if not HEX.match(key) or not _on_curve(key):
        raise UriError("bad_key")
    try:
        q = urllib.parse.parse_qs(query, keep_blank_values=True, max_num_fields=64)
    except ValueError as exc:
        raise UriError("bad_scheme") from exc
    given = q.get("relay") or []
    if not given:
        raise UriError("no_relay")
    relays: list[str] = []
    for raw in given:
        r = _relay(raw)
        if r not in relays:
            relays.append(r)
    if len(relays) > MAX_URI_RELAYS:
        raise UriError("bad_relay")
    secret = (q.get("secret") or [""])[0]
    if not 1 <= len(secret) <= 128:
        raise UriError("no_secret")
    url = _https((q.get("url") or [None])[0], 200)
    image = _https((q.get("image") or [None])[0], 300)
    return {
        "client": key,
        "relays": relays,
        "secret": secret,
        "perms": parse_perms((q.get("perms") or [None])[0]),
        "name": _name((q.get("name") or [None])[0]),
        "url": url[0] if url else None,
        "host": url[1] if url else None,
        "image": image[0] if image else None,
    }


def asks(perms: list[str] | None) -> dict[str, bool]:
    reads = perms is None or any(p.split(":")[0] in DECRYPT_METHODS for p in perms)
    return {"sign": True, "decrypt": reads}


# ── what an app may ask ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Own:
    """The belfry's own names: what a remote app is never given a signature for."""

    domain: str = ""
    api: frozenset[str] = field(default_factory=frozenset)
    relays: frozenset[str] = field(default_factory=frozenset)
    blossom: frozenset[str] = field(default_factory=frozenset)


def _host(value: Any) -> str:
    """The host of an address or a bare host[:port], lower case; "" when there is none."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "://" not in text:
        text = "x://" + text
    try:
        return (urllib.parse.urlsplit(text).hostname or "").rstrip(".")
    except ValueError:
        return ""


def own_hosts(settings: Any) -> Own:
    domain = str(getattr(settings, "nip05_domain", "") or "").strip().lower().rstrip(".")
    g = lambda name: getattr(settings, name, "") or ""  # noqa: E731

    def hosts(*values: str) -> set[str]:
        out = set()
        for v in values:
            for part in re.split(r"[\s,]+", str(v)):
                if h := _host(part):
                    out.add(h)
        return out

    api = hosts(
        g("public_url"), g("site_url"), g("console_url"), g("apps_public_url"), g("chat_public_url")
    )
    relays = hosts(
        g("relay_url"),
        g("community_relay_public_url"),
        g("groups_relay_public_url"),
        g("groups_api_url"),
        g("bunker_relays"),
        g("session_relay_url"),
    )
    blossom = hosts(g("blossom_public_url"), g("blossom_hosts"), g("homepage_upload_url"))
    if domain:
        api |= {domain} | {f"{n}.{domain}" for n in ("belfry", "pass", "apps", "chat")}
        relays |= {f"{n}.{domain}" for n in ("relay", "groups-relay", "bunker")}
        blossom |= {f"blossom.{domain}"}
    return Own(domain, frozenset(api), frozenset(relays), frozenset(blossom))


def covered(perms: list[str] | None, method: str, kind: int | None = None) -> bool:
    """Whether the URI's perms cover a request (no perms: everything)."""
    if perms is None:
        return True
    for p in perms:
        name, _, param = p.partition(":")
        if name != method:
            continue
        if method != "sign_event" or not param:
            return True
        if kind is not None and param == str(kind):
            return True
    return False


def _tag(event: dict[str, Any], name: str) -> list[str]:
    return [
        str(t[1])
        for t in event.get("tags") or []
        if isinstance(t, list) and len(t) > 1 and t[0] == name
    ]


def _forbidden(event: dict[str, Any], perms: list[str] | None, own: Own) -> bool:
    """The events no remote app gets signed, whatever its perms say."""
    kind = event.get("kind")
    if kind == 27235:  # NIP-98: never for the belfry's own hosts (the API acts on this key)
        hosts = [_host(u) for u in _tag(event, "u")]
        if not hosts:
            return True
        for h in hosts:
            if not h or h in own.api:
                return True
            if own.domain and (h == own.domain or h.endswith("." + own.domain)):
                return True
        return False
    if kind == 22242:  # NIP-42: never for the belfry's own relays (members only)
        hosts = [_host(r) for r in _tag(event, "relay")]
        return not hosts or any(not h or h in own.relays for h in hosts)
    if kind == KIND:
        return True
    if isinstance(kind, int) and (9000 <= kind <= 9009 or kind in (9021, 9022)):
        return True  # NIP-29 moderation and joins
    if kind == 24242:  # Blossom: never for the belfry's own storage, nor for "any server"
        servers = [_host(s) for s in _tag(event, "server")]
        return not servers or any(not s or s in own.blossom for s in servers)
    if kind in (0, 3):  # the profile and the contact list: only when the app said so
        return perms is None or f"sign_event:{kind}" not in perms
    if kind == 30078:
        # app data under the belfry's own names is the belfry's protocol: the sealed
        # conversations' anchor (belfry:sealed), the read state, the channel order, a channel's
        # inbox and web settings… An outside app keeps its own data under its own names.
        return any(d.strip().lower().startswith("belfry:") for d in _tag(event, "d"))
    if kind == 1985:
        # labels in the belfry's namespaces are decisions here: a pin, a proposal's status, a
        # ticket checked in, a vouch, a button pressed (belfry/…)
        return any(_belfry_label(n) for n in _tag(event, "L")) or any(
            isinstance(t, list) and len(t) > 2 and t[0] == "l" and _belfry_label(t[2])
            for t in event.get("tags") or []
        )
    # kind 5 (a deletion) stays allowed: relays apply it to the author's own events only
    return False


def _belfry_label(namespace: Any) -> bool:
    n = str(namespace or "").strip().lower()
    return n == "belfry" or n.startswith("belfry/")


def allowed(
    perms: list[str] | None,
    decrypt: bool,
    method: str,
    event: dict[str, Any] | None,
    own: Own,
) -> bool:
    """Whether a connection (the URI's PERMS, the member's DECRYPT choice) may have METHOD
    done; EVENT is the template of a ``sign_event``."""
    if method in OPEN_METHODS:
        return True
    if method in CRYPT_METHODS:
        if method in DECRYPT_METHODS and not decrypt:
            return False
        return covered(perms, method)
    if method != "sign_event" or not isinstance(event, dict):
        return False
    kind = event.get("kind")
    if not isinstance(kind, int) or isinstance(kind, bool):
        return False
    if not covered(perms, "sign_event", kind):
        return False
    return not _forbidden(event, perms, own)


# ── the register's rows ─────────────────────────────────────────────────────────────────────
def own(svc: Services) -> bool:
    from . import invite

    return invite.own(svc) and hasattr(svc.odoo, "remote_put")


def _now() -> int:
    return int(time.time())


def expires(row: dict[str, Any]) -> int:
    return int(row.get("used") or row["created"]) + IDLE_DAYS * 86400


def live(row: dict[str, Any] | None, now: int | None = None) -> bool:
    return bool(row) and not row.get("revoked") and expires(row) > (_now() if now is None else now)


def public(row: dict[str, Any]) -> dict[str, Any]:
    """A connection as the phone app shows it: no keys, no relays."""
    return {
        "id": row["id"],
        "name": row.get("name"),
        "url": row.get("url"),
        "host": _host(row.get("url")) or None,
        "image": row.get("image"),
        "decrypt": bool(row.get("decrypt")),
        "created": int(row["created"]),
        "used": int(row["used"]) if row.get("used") else None,
        "expires": expires(row),
    }


def member(svc: Services, caller: str) -> int:
    """The contact whose account CALLER is the key of, when they are a member or an admin.
    403 ``no_pass`` (a key that is nobody's here: a website session too), ``guest``, ``agent``."""
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "=", caller]],
        ["partner_id", "tier"],
    )
    if not rows:
        raise ApiError(403, "no_pass", "This key holds no active pass.")
    full = [r for r in rows if r.get("tier") not in ("guest", "agent")]
    if not full:
        if any(r.get("tier") == "agent" for r in rows):
            raise ApiError(403, "agent", "An agent signs with its own key.")
        raise ApiError(403, "guest", "Guests can't connect other apps: this is for members.")
    partner = m2o_id(full[0].get("partner_id"))
    if not partner:
        raise ApiError(403, "no_pass", "This key holds no active pass.")
    return partner


def token_of(svc: Services, partner: int, pubkey: str) -> str:
    """The signer's token for the member whose key is PUBKEY: 409 ``own_key`` when they hold
    their own key, 503 ``signer``."""
    from . import community

    try:
        found = community.member_token(svc, partner)
    except Exception as exc:  # noqa: BLE001
        raise ApiError(503, "signer", "The signer isn't answering: try again in a moment.") from exc
    if found is None or found[0] != pubkey:
        raise ApiError(409, "own_key", "You hold your own key: connect the app with it.")
    return found[1]


def closed(svc: Services, pass_row: dict[str, Any]) -> None:
    """A pass closed (core._close_pass): the contact's connections end unless they are still
    a member."""
    if not own(svc):
        return
    partner = m2o_id(pass_row.get("partner_id"))
    if not partner:
        return
    still = svc.odoo.search_read(
        "belfry.pass", [["partner_id", "=", partner], ["state", "=", "active"]], ["tier"]
    )
    if any(r.get("tier") not in ("guest", "agent") for r in still):
        return
    n = svc.odoo.remote_revoke(partner_id=partner)
    if n:
        log.info("remote apps: %d connection(s) ended with pass %s", n, pass_row.get("id"))


# ── the relays ──────────────────────────────────────────────────────────────────────────────
class _Guarded:
    """A websocket to a relay on the public internet: the name is looked up first, an address
    that is not a public one is refused, and the connection goes to the address checked."""

    def __init__(self, url: str):
        self.url = url
        self._cm: Any = None

    async def __aenter__(self) -> Any:
        from websockets.asyncio.client import connect

        u = urllib.parse.urlsplit(self.url)
        port = u.port or 443
        infos = await asyncio.get_running_loop().getaddrinfo(u.hostname, port, type=1)
        addresses = [i[4][0] for i in infos]
        if not addresses or not all(ipaddress.ip_address(a).is_global for a in addresses):
            raise RuntimeError("not a public address")
        self._cm = connect(
            self.url,
            host=addresses[0],
            port=port,
            open_timeout=10,
            close_timeout=3,
            max_size=2**18,
            proxy=None,
        )
        return await self._cm.__aenter__()

    async def __aexit__(self, *exc: Any) -> Any:
        return await self._cm.__aexit__(*exc)


def _default_connect(url: str) -> Any:
    return _Guarded(url)


CONNECT: Connect = _default_connect  # tests put a network of fake relays here


class Link:
    """One relay: its socket while it is open, who is listened for, what waits for an OK."""

    def __init__(self, url: str):
        self.url = url
        self.keys: tuple[str, ...] = ()
        self.ws: Any = None
        self.ready = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        self.waiting: dict[str, tuple[asyncio.Future[bool], dict[str, Any]]] = {}
        self.challenge = ""
        self.authed = False
        self.logged = 0.0


class SignerDown(Exception):
    pass


class Hub:
    """The remote signer: the relays in use, the requests, the answers."""

    def __init__(
        self,
        svc: Services,
        connect: Connect | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ):
        self.svc = svc
        self._connect = connect
        self.clock = clock
        self.sleep = sleep
        self.own = own_hosts(svc.settings)
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}  # (member, client) → row
        self.pending: dict[tuple[str, str], dict[str, Any]] = {}  # being acknowledged
        self.links: dict[str, Link] = {}
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.rates: dict[str, deque[float]] = {}
        self.touched: dict[str, float] = {}
        self.tokens: dict[int, tuple[str, str, float]] = {}
        self.queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(500)
        self.handled = 0  # requests answered or refused (tests wait on it)
        self._lock = asyncio.Lock()

    def connect(self, url: str) -> Any:
        return (self._connect or CONNECT)(url)

    # the register → who is listened for, where
    async def refresh(self) -> None:
        async with self._lock:
            rows = await run_in_threadpool(self.svc.odoo.remote_list)
            now = int(self.clock())
            found: dict[tuple[str, str], dict[str, Any]] = {}
            for r in rows:  # the newest first
                if live(r, now):
                    found.setdefault((r["pubkey"], r["client"]), r)
            self.rows = found
            ids = {r["id"] for r in found.values()}
            for gone in [i for i in self.rates if i not in ids]:
                self.rates.pop(gone, None)
                self.touched.pop(gone, None)
            await self._apply()

    def _wanted(self) -> dict[str, tuple[str, ...]]:
        want: dict[str, set[str]] = {}
        for r in [*self.rows.values(), *self.pending.values()]:
            for url in r["relays"]:
                want.setdefault(url, set()).add(r["pubkey"])
        return {u: tuple(sorted(k)) for u, k in want.items()}

    async def _apply(self) -> None:
        want = self._wanted()
        for url in [u for u in self.links if u not in want]:
            link = self.links.pop(url)
            if link.task is not None:
                link.task.cancel()
        for url, keys in want.items():
            link = self.links.get(url)
            if link is None:
                link = self.links[url] = Link(url)
                link.keys = keys
                link.task = asyncio.create_task(self._run(link))
            elif link.keys != keys:
                link.keys = keys
                if link.ws is not None and link.ready.is_set():
                    with contextlib.suppress(Exception):
                        await self._req(link)

    def relays_for(self, relays: list[str]) -> list[str]:
        """Those of RELAYS a new connection uses: the ones in use already, and new ones while
        there is room (MAX_RELAYS over all)."""
        used = set(self._wanted())
        room = max(0, MAX_RELAYS - len(used))
        keep = [r for r in relays if r in used]
        keep += [r for r in relays if r not in used][:room]
        return [r for r in relays if r in keep]

    # one relay
    async def _req(self, link: Link) -> None:
        flt = {"kinds": [KIND], "#p": list(link.keys), "since": int(self.clock()) - 10}
        await link.ws.send(json.dumps(["REQ", "belfry-apps", flt]))

    def _complain(self, link: Link, what: str) -> None:
        now = self.clock()
        if now - link.logged >= 3600:
            link.logged = now
            log.warning("remote apps: relay %s: %s", _host(link.url), what)

    async def _auth(self, link: Link) -> bool:
        """Answer the relay's NIP-42 challenge as the members listened for here (a relay that
        asks for it before it serves or accepts their events). Once per connection."""
        if link.authed or not link.challenge:
            return False
        link.authed = True
        done = False
        for pubkey in link.keys[:5]:
            row = next(
                (r for r in [*self.rows.values(), *self.pending.values()] if r["pubkey"] == pubkey),
                None,
            )
            if row is None:
                continue
            try:
                token = await self._token(row)
                event = await run_in_threadpool(
                    self.svc.keycast.sign_event,
                    token,
                    {
                        "pubkey": pubkey,
                        "kind": 22242,
                        "created_at": int(self.clock()),
                        "tags": [["relay", link.url], ["challenge", link.challenge]],
                        "content": "",
                    },
                )
                await link.ws.send(json.dumps(["AUTH", event]))
                done = True
            except Exception as exc:  # noqa: BLE001 — the relay then keeps refusing
                self._complain(link, f"no AUTH ({type(exc).__name__})")
        return done

    async def _run(self, link: Link) -> None:
        backoff = 1.0
        while True:
            try:
                async with self.connect(link.url) as ws:
                    link.ws, link.challenge, link.authed = ws, "", False
                    await self._req(link)
                    link.ready.set()
                    while True:
                        msg = json.loads(await ws.recv())
                        if not isinstance(msg, list) or not msg:
                            continue
                        if msg[0] == "EVENT" and len(msg) > 2 and isinstance(msg[2], dict):
                            with contextlib.suppress(asyncio.QueueFull):
                                self.queue.put_nowait((link.url, msg[2]))
                        elif msg[0] == "EOSE":
                            backoff = 1.0
                        elif msg[0] == "AUTH" and len(msg) > 1:
                            link.challenge = str(msg[1])
                        elif msg[0] == "OK" and len(msg) > 2:
                            await self._ok(link, msg)
                        elif msg[0] == "CLOSED":
                            reason = str(msg[2]) if len(msg) > 2 else ""
                            if reason.startswith("auth-required") and await self._auth(link):
                                await self._req(link)
                                continue
                            raise RuntimeError("subscription closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a relay that is down or refuses: again later
                self._complain(link, f"{type(exc).__name__}; trying again")
            finally:
                link.ws = None
                link.ready.clear()
                for fut, _e in link.waiting.values():
                    if not fut.done():
                        fut.set_result(False)
                link.waiting.clear()
            await self.sleep(backoff * (0.8 + 0.4 * random.random()))
            backoff = min(backoff * 2, 300.0)

    async def _ok(self, link: Link, msg: list[Any]) -> None:
        got = link.waiting.get(str(msg[1]))
        if got is None:
            return
        fut, event = got
        if msg[2]:
            if not fut.done():
                fut.set_result(True)
            return
        reason = str(msg[3]) if len(msg) > 3 else ""
        if reason.startswith("auth-required") and await self._auth(link):
            await link.ws.send(json.dumps(["EVENT", event]))  # once more, known now
            return
        if not fut.done():
            fut.set_result(False)

    async def publish(self, url: str, event: dict[str, Any], timeout: float) -> bool:
        """Send EVENT on the relay's socket and wait for its OK; False when it isn't
        reachable, refuses, or says nothing in time."""
        link = self.links.get(url)
        if link is None:
            return False
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        fut: asyncio.Future[bool] = loop.create_future()
        try:
            await asyncio.wait_for(link.ready.wait(), timeout)
            link.waiting[event["id"]] = (fut, event)
            await link.ws.send(json.dumps(["EVENT", event]))
            return await asyncio.wait_for(fut, max(0.05, end - loop.time()))
        except (TimeoutError, Exception):  # noqa: BLE001
            return False
        finally:
            link.waiting.pop(event["id"], None)

    async def send(self, url: str, event: dict[str, Any]) -> bool:
        """An answer, to the relay its request came from; nothing waits for the OK."""
        link = self.links.get(url)
        if link is None or link.ws is None:
            return False
        try:
            await link.ws.send(json.dumps(["EVENT", event]))
            return True
        except Exception:  # noqa: BLE001 — the app asks again
            return False

    # the signer
    async def _token(self, row: dict[str, Any]) -> str:
        partner = int(row["partner_id"])
        got = self.tokens.get(partner)
        if got and got[0] == row["pubkey"] and self.clock() - got[2] < TOKEN_SECONDS:
            return got[1]
        from . import community

        try:
            found = await run_in_threadpool(community.member_token, self.svc, partner)
        except Exception as exc:  # noqa: BLE001
            raise SignerDown(type(exc).__name__) from exc
        if found is None or found[0] != row["pubkey"]:
            raise SignerDown("not this member's key")
        self.tokens[partner] = (found[0], found[1], self.clock())
        return found[1]

    async def _signer(self, row: dict[str, Any], method: str, *args: Any) -> Any:
        token = await self._token(row)
        try:
            return await run_in_threadpool(getattr(self.svc.keycast, method), token, *args)
        except Exception as exc:  # noqa: BLE001 — named by its type only: no text of the signer's
            self.tokens.pop(int(row["partner_id"]), None)
            raise SignerDown(f"{method}: {type(exc).__name__}") from exc

    async def signed(
        self, row: dict[str, Any], body: dict[str, Any], scheme: str = "nip44"
    ) -> dict[str, Any]:
        """BODY as a kind 24133 event for the app: encrypted to its key, signed as the member."""
        text = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        content = await self._signer(row, f"{scheme}_encrypt", row["client"], text)
        event = await self._signer(
            row,
            "sign_event",
            {
                "pubkey": row["pubkey"],
                "kind": KIND,
                "created_at": int(self.clock()),
                "tags": [["p", row["client"]]],
                "content": content,
            },
        )
        if not isinstance(event, dict) or event.get("pubkey") != row["pubkey"]:
            raise SignerDown("sign_event: not this member's event")
        return event

    # a new connection
    async def acknowledge(self, row: dict[str, Any], secret: str) -> bool:
        """Listen for the app on its relays, then tell it the connection is made (NIP-46:
        the secret of its URI, back to it). True when a relay took the message: the row is
        then listened for until the caller, having stored it, calls ``forget``."""
        key = (row["pubkey"], row["client"])
        self.pending[key] = row
        ok = False
        try:
            async with self._lock:
                await self._apply()
            event = await self.signed(row, {"id": secrets.token_hex(8), "result": secret})
            jobs = [asyncio.create_task(self.publish(u, event, ACK_SECONDS)) for u in row["relays"]]
            try:
                for job in asyncio.as_completed(jobs, timeout=ACK_SECONDS + 0.5):
                    if await job:
                        ok = True
                        break
            except TimeoutError:
                ok = False
            if not ok:
                for job in jobs:
                    job.cancel()
            return ok
        finally:
            if not ok:
                self.forget(row)

    def forget(self, row: dict[str, Any]) -> None:
        """ROW is no longer waited for as a new connection (stored by now, or given up)."""
        key = (row["pubkey"], row["client"])
        if self.pending.get(key) is row:
            del self.pending[key]

    # requests
    def _mark(self, eid: str) -> bool:
        if eid in self.seen:
            return False
        self.seen[eid] = None
        while len(self.seen) > SEEN_MAX:
            self.seen.popitem(last=False)
        return True

    def _rate(self, row_id: str) -> int:
        """How many requests this connection made in the last minute, this one included."""
        now = self.clock()
        q = self.rates.setdefault(row_id, deque())
        while q and now - q[0] > 60:
            q.popleft()
        q.append(now)
        return len(q)

    async def _row(self, member_key: str, client: str) -> dict[str, Any] | None:
        key = (member_key, client)
        row = self.pending.get(key)
        if row is not None:
            return row
        row = self.rows.get(key)
        if row is None:
            return None
        fresh = await run_in_threadpool(self.svc.odoo.remote_get, row["id"])
        if not live(fresh, int(self.clock())):
            self.rows.pop(key, None)  # disconnected, or the pass closed: at once
            return None
        return fresh

    async def handle(self, url: str, e: dict[str, Any]) -> str | None:
        """One event from a relay. What was done with it: None (not a request of a connected
        app, or one seen before), "ok", "refused", "error", "limited", "dropped"."""
        if not isinstance(e, dict) or e.get("kind") != KIND:
            return None
        client, eid, content = e.get("pubkey"), e.get("id"), e.get("content")
        if not (isinstance(client, str) and isinstance(eid, str) and isinstance(content, str)):
            return None
        targets = tag_values(e, "p")
        if len(targets) != 1 or not isinstance(e.get("created_at"), int):
            return None
        if abs(int(self.clock()) - e["created_at"]) > MAX_AGE or len(content) > 2**17:
            return None
        row = await self._row(targets[0], client)
        if row is None or url not in row["relays"]:
            return None
        try:
            genuine = event_id(e) == eid and verify_signature(e)
        except Exception:  # noqa: BLE001 — not an event
            genuine = False
        if not genuine or not self._mark(eid):
            return None
        n = self._rate(row["id"])
        if n > 2 * REQUESTS_PER_MINUTE:
            return "dropped"  # not even read: the signer is asked nothing for it
        try:
            return await self._serve(url, row, content, limited=n > REQUESTS_PER_MINUTE)
        except SignerDown as exc:
            log.warning("remote apps: signer: %s", exc)
            return "error"
        finally:
            self.handled += 1

    async def _serve(self, url: str, row: dict[str, Any], content: str, limited: bool) -> str:
        scheme = "nip04" if "?iv=" in content else "nip44"
        try:
            plain = await self._signer(row, f"{scheme}_decrypt", row["client"], content)
            req = json.loads(plain)
        except ValueError:
            return "error"
        if not isinstance(req, dict):
            return "error"
        rid, method, params = req.get("id"), req.get("method"), req.get("params")
        if not isinstance(rid, str) or len(rid) > 128 or not isinstance(method, str):
            return "error"
        if params is None:
            params = []
        ok = isinstance(params, list) and all(isinstance(p, str) for p in params)

        async def answer(result: str | None = None, error: str | None = None) -> None:
            body = (
                {"id": rid, "error": error} if error is not None else {"id": rid, "result": result}
            )
            await self.send(url, await self.signed(row, body, scheme))

        if limited:
            await answer(error="Too many requests")
            return "limited"
        if not ok:
            await answer(error="Invalid params")
            return "error"
        now = self.clock()
        if now - self.touched.get(row["id"], 0.0) >= TOUCH_SECONDS and row.get("id"):
            self.touched[row["id"]] = now
            with contextlib.suppress(Exception):
                await run_in_threadpool(self.svc.odoo.remote_touch, row["id"], int(now))
        try:
            state, result = await self._do(row, method, params)
        except SignerDown as exc:
            log.warning("remote apps: signer: %s", exc)
            with contextlib.suppress(SignerDown):
                await answer(error="The signer isn't answering")
            return "error"
        if state == "ok":
            await answer(result=result)
        else:
            await answer(error=result)
        return state

    async def _do(self, row: dict[str, Any], method: str, params: list[str]) -> tuple[str, str]:
        """(state, the result or the error's text) of one request."""
        perms, decrypt = row.get("perms"), bool(row.get("decrypt"))
        if method == "connect":
            return "ok", params[1] if len(params) > 1 and params[1] else "ack"
        if method == "ping":
            return "ok", "pong"
        if method == "get_public_key":
            return "ok", row["pubkey"]
        if method == "get_relays":
            return "ok", "{}"
        if method in CRYPT_METHODS:
            if not allowed(perms, decrypt, method, None, self.own):
                return "refused", "Not allowed"
            if len(params) != 2 or not HEX.match(params[0]) or not _on_curve(params[0]):
                return "error", "Invalid params"
            try:
                return "ok", await self._signer(row, method, params[0], params[1])
            except SignerDown as exc:
                if method in DECRYPT_METHODS:  # most often: not a message for this key
                    log.info("remote apps: %s failed (%s)", method, type(exc.__cause__).__name__)
                    return "error", "Could not decrypt"
                raise
        if method == "sign_event":
            try:
                template = json.loads(params[0]) if len(params) == 1 else None
            except ValueError:
                template = None
            event = _template(template, row["pubkey"], int(self.clock()))
            if event is None:
                return "error", "Invalid params"
            if not allowed(perms, decrypt, method, event, self.own):
                return "refused", "Not allowed"
            done = await self._signer(row, "sign_event", event)
            if (
                not isinstance(done, dict)
                or done.get("pubkey") != row["pubkey"]
                or done.get("kind") != event["kind"]
            ):
                raise SignerDown("sign_event: not the event asked for")
            return "ok", json.dumps(done, separators=(",", ":"), ensure_ascii=False)
        return "error", "Unsupported method"

    # the loop
    async def work(self) -> None:
        while True:
            url, e = await self.queue.get()
            try:
                await self.handle(url, e)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — one request never stops the rest
                log.warning("remote apps: a request failed: %s", type(exc).__name__)

    async def refreshing(self) -> None:
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — again in a moment
                log.warning("remote apps: connections not read: %s", type(exc).__name__)
            await self.sleep(REFRESH_SECONDS)

    def tasks(self) -> list[Any]:
        return [self.refreshing(), *(self.work() for _ in range(4))]

    async def stop(self) -> None:
        for link in self.links.values():
            if link.task is not None:
                link.task.cancel()
        for link in list(self.links.values()):
            if link.task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await link.task
        self.links.clear()


def _template(raw: Any, pubkey: str, now: int) -> dict[str, Any] | None:
    """The unsigned event an app asked to have signed, as the member's; None when it is none."""
    if not isinstance(raw, dict):
        return None
    kind, content, tags = raw.get("kind"), raw.get("content", ""), raw.get("tags", [])
    at = raw.get("created_at", now)
    if not isinstance(kind, int) or isinstance(kind, bool) or not 0 <= kind <= 65535:
        return None
    if not isinstance(at, int) or isinstance(at, bool) or at < 0:
        return None
    if not isinstance(content, str) or not isinstance(tags, list) or len(tags) > 2500:
        return None
    if not all(isinstance(t, list) and all(isinstance(x, str) for x in t) for t in tags):
        return None
    if raw.get("pubkey") not in (None, pubkey):
        return None
    return {"pubkey": pubkey, "kind": kind, "created_at": at, "tags": tags, "content": content}


@contextlib.asynccontextmanager
async def running(app: FastAPI):
    svc = app.state.services
    hub: Hub | None = None
    tasks: list[asyncio.Task[None]] = []
    app.state.remote_apps = None
    if own(svc):
        try:
            hub = Hub(svc)
            tasks = [asyncio.create_task(job) for job in hub.tasks()]
            app.state.remote_apps = hub
        except Exception as exc:  # noqa: BLE001 — the service runs without remote apps
            log.warning("remote apps: not started: %s", type(exc).__name__)
            hub = None
    try:
        yield
    finally:
        app.state.remote_apps = None
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        if hub is not None:
            await hub.stop()


# ── the API ─────────────────────────────────────────────────────────────────────────────────
class _PerHour:
    def __init__(self, limit: int):
        self.limit = limit
        self.seen: dict[int, deque[float]] = {}

    def over(self, who: int) -> bool:
        now = time.time()
        q = self.seen.setdefault(who, deque())
        while q and now - q[0] > 3600:
            q.popleft()
        return len(q) >= self.limit

    def count(self, who: int) -> None:
        self.seen.setdefault(who, deque()).append(time.time())

    def reset(self) -> None:
        self.seen.clear()


CONNECTS = _PerHour(CONNECTS_PER_HOUR)


def _parse(body: dict[str, Any]) -> dict[str, Any]:
    try:
        return parse_uri(body.get("uri"))
    except UriError as exc:
        err = ApiError(422, "invalid_uri", "This is not a code an app shows to connect.")
        err.extra = {"reason": exc.reason}
        raise err from exc


def register(app: FastAPI) -> None:
    async def caller(request: Request, method: str, path: str) -> tuple[Services, str, int, dict]:
        """(services, the account's key, its contact, the JSON body) for a member or admin."""
        s: Services = request.app.state.services
        raw = await request.body() if method == "POST" else b""
        key = authenticate(
            s, request.app.state.api_seen, request.headers.get("authorization"), method, path, raw
        )
        if not own(s):
            raise ApiError(409, "unsupported", "Remote apps need the belfry's own register.")
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_json", "Send a JSON object.")
        partner = await run_in_threadpool(member, s, key)
        return s, key, partner, body

    def mine(s: Services, partner: int) -> list[dict[str, Any]]:
        return [r for r in s.odoo.remote_list(partner_id=partner) if live(r)]

    @app.post(API + "/preview")
    async def remote_preview(request: Request) -> JSONResponse:
        s, key, partner, body = await caller(request, "POST", API + "/preview")
        uri = _parse(body)
        await run_in_threadpool(token_of, s, partner, key)
        rows = await run_in_threadpool(mine, s, partner)
        out = {
            "name": uri["name"],
            "url": uri["url"],
            "host": uri["host"],
            "image": uri["image"],
            "asks": asks(uri["perms"]),
            "known": any(r["client"] == uri["client"] for r in rows),
        }
        return JSONResponse(out, headers=NO_STORE)

    @app.post(API)
    async def remote_connect(request: Request) -> JSONResponse:
        s, key, partner, body = await caller(request, "POST", API)
        uri = _parse(body)
        want = body.get("decrypt", False)
        if not isinstance(want, bool):
            raise ApiError(422, "invalid", "decrypt: true or false")
        await run_in_threadpool(token_of, s, partner, key)
        same = await run_in_threadpool(lambda: s.odoo.remote_list(client=uri["client"]))
        if any(int(r["partner_id"]) != partner for r in same):
            raise ApiError(409, "key_taken", "This app is connected to another account.")
        rows = await run_in_threadpool(mine, s, partner)
        if len([r for r in rows if r["client"] != uri["client"]]) >= MAX_APPS:
            raise ApiError(409, "limit", f"{MAX_APPS} apps at most: disconnect one first.")
        if CONNECTS.over(partner):
            raise ApiError(429, "too_many", "Too many tries. Wait a little.")
        hub: Hub | None = getattr(request.app.state, "remote_apps", None)
        if hub is None:
            raise ApiError(503, "signer", "The signer isn't answering: try again in a moment.")
        relays = hub.relays_for(uri["relays"])
        if not relays:
            raise ApiError(503, "relays_full", "Too many relays are in use: try again later.")
        CONNECTS.count(partner)
        now = _now()
        row = {
            "id": secrets.token_urlsafe(12),
            "partner_id": partner,
            "pubkey": key,
            "client": uri["client"],
            "relays": relays,
            "perms": uri["perms"],
            "decrypt": bool(want) and asks(uri["perms"])["decrypt"],
            "name": uri["name"],
            "url": uri["url"],
            "image": uri["image"],
            "created": now,
            "used": None,
            "revoked": False,
        }
        try:
            reached = await hub.acknowledge(row, uri["secret"])
        except SignerDown as exc:
            log.warning("remote apps: signer: %s", exc)
            await hub.refresh()
            raise ApiError(
                503, "signer", "The signer isn't answering: try again in a moment."
            ) from exc
        if not reached:
            await hub.refresh()
            raise ApiError(502, "unreachable", "The app could not be reached: show its code again.")
        try:
            await run_in_threadpool(s.odoo.remote_put, row)
            await hub.refresh()
        finally:
            hub.forget(row)  # listened for without a gap until it is in the register
        log.info("remote apps: a connection made (%d relay(s))", len(relays))
        return JSONResponse({"app": public(row)}, status_code=201, headers=NO_STORE)

    @app.get(API)
    async def remote_list(request: Request) -> JSONResponse:
        s, _key, partner, _ = await caller(request, "GET", API)
        rows = await run_in_threadpool(mine, s, partner)
        return JSONResponse({"apps": [public(r) for r in rows]}, headers=NO_STORE)

    @app.post(API + "/{app_id}/disconnect")
    async def remote_disconnect(request: Request, app_id: str) -> JSONResponse:
        s, _key, partner, _ = await caller(request, "POST", f"{API}/{app_id}/disconnect")
        row = await run_in_threadpool(s.odoo.remote_get, app_id)
        if row is None or int(row["partner_id"]) != partner:
            raise ApiError(404, "not_found", "No such app.")
        await run_in_threadpool(lambda: s.odoo.remote_revoke(app_id=app_id))
        hub: Hub | None = getattr(request.app.state, "remote_apps", None)
        if hub is not None:
            await hub.refresh()
        return JSONResponse({"ok": True}, headers=NO_STORE)


__all__ = ["register", "running", "closed", "allowed", "parse_uri", "Hub"]
