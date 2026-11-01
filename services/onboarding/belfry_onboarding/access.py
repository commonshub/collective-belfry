"""Access as Nostr events: admins, grants, invites and requests, all on the groups relay.

Who may use the village is decided by signed events, so every member can audit it; the only
HTTP surface is the read-only state in ``/.well-known/belfry.json`` (discovery.py).

Kinds (Belfry's own, regular events, readable by every member of the groups relay):

- **8534 grant**: ``["p", <member>]``, ``["role", <role>]``, optional ``["until", <unix>]``,
  optional ``["e", <id>, "", "request"|"invite"|"approval"]``. Signed by an admin or by
  Belfry's service key. The newest grant or revoke for a member wins. Role ``admin`` makes an
  admin; any other role is a label apps may show (``member`` by default).
- **8535 revoke**: ``["p", <member>]`` ends a grant; ``["e", <request>]`` declines a request,
  ``["e", <invite>]`` withdraws an invite.
- **8536 invite**: ``["code", sha256(<code>)]``, ``["role", <role>]``, optional ``["days", n]``
  (counted from the day it is used) or ``["until", <unix>]``, optional ``["max", n]`` uses,
  optional ``["expires", <unix>]``. Signed by an admin; the code itself never goes on a relay.
- **8537 request**: never published as such; the rumor of a NIP-17 gift wrap (kind 1059) to the
  service key, which the groups relay accepts from anyone. Content: a note for the admins.
  Tags: optional ``["claim", <invite code>]``, ``["role", "admin"]`` (only honoured while the
  Belfry has no admin: the first to ask becomes admin), ``["name", <display name>]``.
- **8538 receipt**: signed by the service, ``["p", <requester>]``, ``["e", <gift wrap>]``,
  optional ``["role", <asked>]``: a request is waiting. The note stays private (it reaches the
  admins in their DM).

Admins are the active ``admin`` grants plus the stewards' active passes (so a Belfry that has
stewards never offers itself to the first comer). An admin's grant, revoke or invite counts
only if they were an admin at the time they signed it; the service key's always count, which is
how an operator with shell access changes admins (``python -m belfry_onboarding admins ...``).

The service (``AccessService``) follows those kinds and the gift wraps to its key on the groups
relay. A request with a valid invite code or (no admin yet) for the admin role is granted at
once; any other is written to every admin as a NIP-17 DM, which they answer with ``approve``
(optionally a role and a number of days) or ``decline``, or in an app that signs the grant
itself. Members (active grants) join the active passes in the relay whitelists (core.py).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import socket
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from coincurve import PrivateKey
from websockets.sync.client import connect

from . import nip44
from .nostr import hex_to_npub, npub_to_hex

log = logging.getLogger(__name__)

GRANT, REVOKE, INVITE, REQUEST, RECEIPT = 8534, 8535, 8536, 8537, 8538
KINDS = {"grant": GRANT, "revoke": REVOKE, "invite": INVITE, "request": REQUEST, "receipt": RECEIPT}
DM = 14
AUTH_KIND = 22242
ADMIN = "admin"
MEMBER = "member"
DAY = 86400
_ROLE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _tag(event: dict[str, Any], name: str) -> str | None:
    for t in event.get("tags") or ():
        if len(t) > 1 and t[0] == name:
            return str(t[1])
    return None


def _etags(event: dict[str, Any]) -> list[tuple[str, str]]:
    """``(id, marker)`` of every e tag."""
    return [
        (str(t[1]), str(t[3]) if len(t) > 3 else "")
        for t in event.get("tags") or ()
        if len(t) > 1 and t[0] == "e"
    ]


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def code_hash(code: str) -> str:
    return hashlib.sha256(code.strip().encode()).hexdigest()


def parse_pubkey(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("npub1"):
        return npub_to_hex(value)
    if _HEX64.match(value):
        return value
    raise ValueError(f"not a pubkey: {value!r}")


# -- the state, a pure function of the events ----------------------------------------------------


@dataclass
class Grant:
    pubkey: str
    role: str
    until: int | None
    by: str
    id: str
    at: int

    def active(self, now: int) -> bool:
        return self.until is None or self.until > now


@dataclass
class Invite:
    id: str
    code_hash: str
    role: str
    days: int | None
    until: int | None
    max_uses: int | None
    expires: int | None
    by: str
    at: int
    uses: int = 0

    def usable(self, now: int) -> bool:
        return (self.expires is None or self.expires > now) and (
            self.max_uses is None or self.uses < self.max_uses
        )


@dataclass
class Request:
    wrap_id: str
    pubkey: str
    role: str | None
    at: int


@dataclass
class State:
    service: str
    stewards: frozenset[str] = frozenset()
    grants: dict[str, Grant] = field(default_factory=dict)  # newest grant per member
    revoked: dict[str, int] = field(default_factory=dict)  # newest revoke per member
    invites: dict[str, Invite] = field(default_factory=dict)
    requests: dict[str, Request] = field(default_factory=dict)  # by gift wrap id
    handled: set[str] = field(default_factory=set)  # wrap ids the service acted on
    ignored: list[str] = field(default_factory=list)  # events from non-admins

    def admins(self, now: int) -> set[str]:
        return {g.pubkey for g in self.grants.values() if g.role == ADMIN and g.active(now)} | set(
            self.stewards
        )

    def members(self, now: int) -> dict[str, Grant]:
        return {p: g for p, g in self.grants.items() if g.active(now)}

    def pending(self, now: int) -> list[Request]:
        members = self.members(now)
        out = []
        for r in self.requests.values():
            later = max(
                (self.grants[r.pubkey].at if r.pubkey in self.grants else 0),
                self.revoked.get(r.pubkey, 0),
            )
            if r.wrap_id in self.handled or r.pubkey in members or later >= r.at:
                continue
            out.append(r)
        return sorted(out, key=lambda r: r.at)


def compute(events: Iterable[dict[str, Any]], service: str, stewards: Iterable[str] = ()) -> State:
    """Replay the access events in order. An event counts if the service signed it or its
    author was an admin when it was signed."""
    state = State(service=service, stewards=frozenset(stewards))
    uses: list[str] = []
    for e in sorted(events, key=lambda e: (e["created_at"], e["id"])):
        kind, author, at = e["kind"], e["pubkey"], e["created_at"]
        if kind == RECEIPT:
            if author != service:
                continue
            for i, marker in _etags(e):
                if marker == "approval":  # an admin's answer that named no request
                    state.handled.add(i)
                elif p := _tag(e, "p"):
                    state.requests[i] = Request(i, p, _tag(e, "role"), at)
            continue
        if kind not in (GRANT, REVOKE, INVITE):
            continue
        if author != service and author not in state.admins(at):
            state.ignored.append(e["id"])
            continue
        if author == service:
            state.handled.update(i for i, m in _etags(e) if m in ("request", "approval"))
        if kind == GRANT:
            p, role = _tag(e, "p"), (_tag(e, "role") or MEMBER).lower()
            if not p or not _HEX64.match(p) or not _ROLE.match(role):
                continue
            state.grants[p] = Grant(p, role, _int(_tag(e, "until")), author, e["id"], at)
            state.handled.update(i for i, marker in _etags(e) if marker == "request")
            if author == service:
                uses.extend(i for i, marker in _etags(e) if marker == "invite")
        elif kind == REVOKE:
            if (p := _tag(e, "p")) and p in state.grants:
                del state.grants[p]
            if p:
                state.revoked[p] = at
            for i, _ in _etags(e):
                state.invites.pop(i, None)
                state.handled.add(i)
        elif kind == INVITE:
            h, role = _tag(e, "code"), (_tag(e, "role") or MEMBER).lower()
            if not h or not _ROLE.match(role):
                continue
            state.invites[e["id"]] = Invite(
                e["id"],
                h.lower(),
                role,
                _int(_tag(e, "days")),
                _int(_tag(e, "until")),
                _int(_tag(e, "max")),
                _int(_tag(e, "expires")),
                author,
                at,
            )
    for i in uses:
        if i in state.invites:
            state.invites[i].uses += 1
    return state


# -- admins' answers ---------------------------------------------------------------------------

_YES = {"approve", "approved", "yes", "ok", "accept", "grant", "y"}
_NO = {"decline", "declined", "no", "reject", "deny", "n"}


@dataclass
class Answer:
    approve: bool
    ref: str | None
    role: str
    days: int | None


def parse_answer(text: str) -> Answer | None:
    """``approve [ref] [role] [days]`` or ``decline [ref]``, in any order after the verb; a days
    count may end with ``d`` or ``days``."""
    words = re.findall(r"[a-z0-9_-]+", text.lower())
    if not words or words[0] not in _YES | _NO:
        return None
    ref, role, days = None, MEMBER, None
    rest = words[1:]
    i = 0
    while i < len(rest):
        w = rest[i]
        if re.fullmatch(r"\d+d?", w) and days is None and not re.fullmatch(r"[0-9a-f]{6,}", w):
            days = int(w.rstrip("d"))
            if i + 1 < len(rest) and rest[i + 1] in ("day", "days"):
                i += 1
        elif re.fullmatch(r"[0-9a-f]{6,64}", w) and ref is None:
            ref = w
        elif w in ("as", "for", "a", "an", "role", "day", "days"):
            pass
        elif _ROLE.match(w) and role == MEMBER:
            role = w
        i += 1
    return Answer(words[0] in _YES, ref, role, days)


# -- the relay session -----------------------------------------------------------------------


def open_socket(public_url: str, transport_url: str, timeout: float):
    """A websocket to the groups relay: zooid serves a relay only on its public Host, so inside
    Compose the public name goes over a socket to the internal address."""
    public = urllib.parse.urlsplit(public_url)
    target = urllib.parse.urlsplit(transport_url or public_url)
    if target.netloc and target.netloc != public.netloc:
        host, _, port = target.netloc.partition(":")
        sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        uri = urllib.parse.urlunsplit(public._replace(scheme="ws"))
        return connect(uri, sock=sock, open_timeout=timeout, max_size=2**22)
    return connect(public_url, open_timeout=timeout, max_size=2**22)


# -- the service ----------------------------------------------------------------------------------


class AccessService:
    """Follows the access events on the groups relay and acts on requests and answers.

    ``stewards`` returns the stewards' pubkeys (Odoo); ``on_members`` is called with the member
    pubkeys whenever they change (the relay whitelists follow). ``publish`` sends one signed event
    and raises on refusal; tests replace it."""

    def __init__(
        self,
        key: PrivateKey,
        relay_url: str,
        transport_url: str = "",
        community: str = "the village",
        app_url: str = "",
        stewards: Callable[[], Iterable[str]] = lambda: (),
        on_members: Callable[[list[str]], None] = lambda pubkeys: None,
        extra_relays: Iterable[Any] = (),
        timeout: float = 10.0,
    ):
        self.key = key
        self.pubkey = key.public_key_xonly.format().hex()
        self.relay_url = relay_url
        self.transport_url = transport_url
        self.community = community
        self.app_url = app_url
        self.stewards_source = stewards
        self.on_members = on_members
        self.extra_relays = list(extra_relays)  # RelayPublisher-like, for DMs (community relay)
        self.timeout = timeout
        self.events: dict[str, dict[str, Any]] = {}
        self.wraps: dict[str, dict[str, Any]] = {}
        self.stewards: frozenset[str] = frozenset()
        self.state = State(service=self.pubkey)
        self.synced = False
        self.lock = threading.RLock()
        self._members: list[str] | None = None
        self._stop = threading.Event()
        self._last_at = 0
        self._thread: threading.Thread | None = None

    # signing and publishing

    def sign(self, kind: int, tags: list[list[str]], content: str = "") -> dict[str, Any]:
        # strictly increasing: the replay orders by created_at, so two of our events in one
        # second (a grant, then its revoke) must not tie
        with self.lock:
            self._last_at = max(int(time.time()), self._last_at + 1)
            at = self._last_at
        return nip44.sign_event(
            self.key, {"kind": kind, "created_at": at, "tags": tags, "content": content}
        )

    def publish(self, event: dict[str, Any]) -> None:
        with open_socket(self.relay_url, self.transport_url, self.timeout) as ws:
            _publish(ws, event, self.relay_url, self.sign_auth, self.timeout)

    def sign_auth(self, template: dict[str, Any]) -> dict[str, Any]:
        return nip44.sign_event(self.key, template)

    def emit(self, event: dict[str, Any]) -> dict[str, Any]:
        """Publish an access event of ours and take it into the state at once."""
        self.publish(event)
        self.add([event])
        return event

    def dm(self, to: str, text: str, tags: list[list[str]] = ()) -> None:
        rumor = {
            "kind": DM,
            "created_at": int(time.time()),
            "tags": [["p", to], *tags],
            "content": text,
        }
        wrapped = nip44.wrap(self.key, rumor, to)
        try:
            self.publish(wrapped)
        except Exception as exc:
            log.warning("access: DM to %s not delivered on the groups relay: %s", to, exc)
        for relay in self.extra_relays:
            try:
                relay.publish(wrapped, self.sign_auth)
            except Exception as exc:
                log.info("access: DM to %s not delivered on %s: %s", to, relay.url, exc)

    # state

    def now(self) -> int:
        return int(time.time())

    def refresh_stewards(self) -> None:
        try:
            stewards = frozenset(p.lower() for p in self.stewards_source() if p)
        except Exception as exc:
            log.warning("access: stewards not refreshed: %s", exc)
            return
        with self.lock:
            if stewards != self.stewards:
                self.stewards = stewards
                self._recompute()

    def add(self, events: Iterable[dict[str, Any]]) -> None:
        with self.lock:
            changed = False
            for e in events:
                if e.get("kind") in (GRANT, REVOKE, INVITE, RECEIPT) and e["id"] not in self.events:
                    if not nip44.verify(e):
                        continue
                    self.events[e["id"]] = e
                    changed = True
            if changed:
                self._recompute()

    def _recompute(self) -> None:
        self.state = compute(self.events.values(), self.pubkey, self.stewards)
        self.check_members()

    def check_members(self) -> None:
        """Tell ``on_members`` when the set of active members changed (grants, expiry)."""
        with self.lock:
            if not self.synced:
                return
            members = sorted(self.state.members(self.now()))
            if members == self._members:
                return
            self._members = members
        try:
            self.on_members(members)
        except Exception as exc:
            log.warning("access: relay members not updated: %s", exc)

    def members(self) -> list[str]:
        with self.lock:
            return sorted(self.state.members(self.now()))

    def is_member(self, pubkey: str) -> bool:
        with self.lock:
            return pubkey in self.state.members(self.now())

    def admins(self) -> list[str]:
        with self.lock:
            return sorted(self.state.admins(self.now()))

    def public_state(self) -> dict[str, Any]:
        """The read-only state in ``/.well-known/belfry.json``."""
        with self.lock:
            admins = sorted(self.state.admins(self.now()))
            return {
                "relay": self.relay_url,
                "service": self.pubkey,
                "kinds": KINDS,
                "synced": self.synced,
                "admins": admins,
                "needs_admin": self.synced and not admins,
            }

    # acting on requests and answers

    def handle_wrap(self, wrapped: dict[str, Any]) -> None:
        wrap_id = wrapped.get("id", "")
        with self.lock:
            if wrap_id in self.state.handled or wrap_id in self.state.requests:
                return
        try:
            rumor = nip44.unwrap(self.key, wrapped)
        except Exception as exc:
            log.info("access: gift wrap %s not readable: %s", wrap_id, exc)
            return
        if rumor["kind"] == REQUEST:
            self.on_request(wrap_id, rumor)
        elif rumor["kind"] == DM:
            self.on_answer(wrap_id, rumor)

    def _grant(
        self,
        pubkey: str,
        role: str,
        until: int | None,
        refs: list[tuple[str, str]],
        content: str = "",
        extra: list[list[str]] = (),
    ) -> dict[str, Any]:
        tags = [["p", pubkey], ["role", role]]
        if until is not None:
            tags.append(["until", str(until)])
        tags += [["e", i, "", m] for i, m in refs]
        return self.emit(self.sign(GRANT, [*tags, *extra], content))

    def on_request(self, wrap_id: str, rumor: dict[str, Any]) -> None:
        who, now = rumor["pubkey"], self.now()
        note = rumor["content"].strip()[:2000]
        asked = (_tag(rumor, "role") or "").lower() or None
        claim = _tag(rumor, "claim")
        with self.lock:
            state = self.state
            admins = state.admins(now)
            member = state.members(now).get(who)
            invite = None
            if claim:
                h = code_hash(claim)
                invite = next(
                    (i for i in state.invites.values() if i.code_hash == h and i.usable(now)), None
                )
        if asked == ADMIN and not admins and self.synced:
            self._grant(who, ADMIN, None, [(wrap_id, "request")], "first admin of this Belfry")
            log.info("access: %s is the first admin", who)
            self.dm(who, f"You are now the admin of {self.community} on Belfry.")
            return
        if invite is not None:
            until = invite.until
            if invite.days:
                until = now + invite.days * DAY
            self._grant(who, invite.role, until, [(invite.id, "invite"), (wrap_id, "request")])
            log.info("access: %s joined with invite %s", who, invite.id)
            return
        if member is not None and (asked is None or asked == member.role):
            self.emit(self.sign(RECEIPT, [["p", who], ["e", wrap_id]], ""))
            return
        receipt = [["p", who], ["e", wrap_id]] + ([["role", asked]] if asked else [])
        self.emit(self.sign(RECEIPT, receipt, ""))
        name = (_tag(rumor, "name") or "").strip()[:80]
        ref = wrap_id[:8]
        lines = [
            f"Access request for {self.community}",
            f"{name + ' (' + hex_to_npub(who) + ')' if name else hex_to_npub(who)} asks to join"
            + (f" as {asked}." if asked else "."),
        ]
        if note:
            lines.append(f"“{note}”")
        if claim:
            lines.append("They gave an invite code that is not valid (any more).")
        lines += [
            "",
            f"Reply “approve {ref}” to let them in, optionally with a role and a number of days:"
            f" “approve {ref} volunteer 14”.",
            f"Reply “decline {ref}” to say no.",
        ]
        if self.app_url:
            lines.append(f"Or open {self.app_url}#/admin/requests/{wrap_id}")
        tags = [
            ["requester", who],
            ["request", wrap_id],
            ["t", "belfry-access-request"],
        ] + ([["note", note]] if note else [])
        for admin in sorted(admins):
            self.dm(admin, "\n".join(lines), tags)
        log.info("access: request %s from %s sent to %d admins", wrap_id, who, len(admins))

    def on_answer(self, wrap_id: str, rumor: dict[str, Any]) -> None:
        admin, now = rumor["pubkey"], self.now()
        answer = parse_answer(rumor["content"])
        with self.lock:
            if admin not in self.state.admins(now) or answer is None:
                return
            pending = self.state.pending(now)
        if answer.ref:
            matches = [r for r in pending if r.wrap_id.startswith(answer.ref)]
        else:
            # a reply in the thread of our DM names the request in its tags
            tagged = {_tag(rumor, "request")} | {i for i, _ in _etags(rumor)}
            matches = [r for r in pending if r.wrap_id in tagged] or (
                pending if len(pending) == 1 else []
            )
        if len(matches) != 1:
            self.dm(
                admin,
                "Which request? Reply with its reference, e.g. "
                + (f"“approve {pending[0].wrap_id[:8]}”." if pending else "none is waiting."),
            )
            # remember the answer as seen so it is not asked about again after a restart
            self.emit(self.sign(RECEIPT, [["p", admin], ["e", wrap_id, "", "approval"]], ""))
            return
        req = matches[0]
        refs = [(req.wrap_id, "request"), (wrap_id, "approval")]
        if answer.approve:
            until = now + answer.days * DAY if answer.days else None
            self._grant(req.pubkey, answer.role, until, refs, "", [["approved_by", admin]])
            self.dm(
                admin,
                f"Done: {hex_to_npub(req.pubkey)} is in as {answer.role}"
                + (f" for {answer.days} days." if answer.days else "."),
            )
            self.welcome(req.pubkey, answer.role, until)
        else:
            tags = [["p", req.pubkey], *[["e", i, "", m] for i, m in refs], ["declined_by", admin]]
            self.emit(self.sign(REVOKE, tags, "declined"))
            self.dm(admin, f"Declined: {hex_to_npub(req.pubkey)}.")

    def welcome(self, pubkey: str, role: str, until: int | None) -> None:
        when = time.strftime(" until %Y-%m-%d", time.gmtime(until)) if until else ""
        self.dm(pubkey, f"Welcome to {self.community}: you are in as {role}{when}.")

    # operator commands (shell access: python -m belfry_onboarding admins ...)

    def set_role(self, pubkey: str, role: str, days: int | None = None, note: str = "") -> None:
        if not _ROLE.match(role):
            raise ValueError(f"not a role: {role!r}")
        until = self.now() + days * DAY if days else None
        self._grant(pubkey, role, until, [], note or "set by the operator")

    def revoke(self, pubkey: str, note: str = "") -> None:
        self.emit(self.sign(REVOKE, [["p", pubkey]], note or "revoked by the operator"))

    # the subscription

    def load_once(self) -> None:
        """Read every access event and the gift wraps to us once (CLI and start)."""
        with open_socket(self.relay_url, self.transport_url, self.timeout) as ws:
            events = _query(ws, self._filters(), self.relay_url, self.sign_auth, self.timeout)
        self.refresh_stewards()
        self.add(e for e in events if e.get("kind") != nip44.GIFT_WRAP_KIND)
        with self.lock:
            self.synced = True
            for e in events:
                if e.get("kind") == nip44.GIFT_WRAP_KIND:
                    self.wraps[e["id"]] = e
        self.check_members()

    def _filters(self) -> list[dict[str, Any]]:
        return [
            {"kinds": [GRANT, REVOKE, INVITE, RECEIPT]},
            {"kinds": [nip44.GIFT_WRAP_KIND], "#p": [self.pubkey]},
        ]

    def process_waiting(self) -> None:
        """Act on gift wraps not handled yet, oldest first (also after a restart)."""
        with self.lock:
            waiting = sorted(self.wraps.values(), key=lambda e: e["created_at"])
        for w in waiting:
            try:
                self.handle_wrap(w)
            except Exception as exc:
                log.warning("access: gift wrap %s not handled: %s", w.get("id"), exc)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="access", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            try:
                self._follow()
                backoff = 2.0
            except Exception as exc:
                log.warning("access: groups relay session ended: %s", exc)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 120)

    def _follow(self) -> None:
        with open_socket(self.relay_url, self.transport_url, self.timeout) as ws:
            sub = "access-" + secrets.token_hex(3)
            req = json.dumps(["REQ", sub, *self._filters()])
            ws.send(req)
            auth_id, eose = None, False
            batch: list[dict[str, Any]] = []
            last_tick = 0.0
            while not self._stop.is_set():
                if time.monotonic() - last_tick > 60:
                    last_tick = time.monotonic()
                    if eose:
                        self.refresh_stewards()
                        self.check_members()
                try:
                    msg = json.loads(ws.recv(timeout=5))
                except TimeoutError:
                    continue
                if msg[0] == "AUTH" and auth_id is None:
                    auth = self.sign_auth(_auth_template(self.relay_url, msg[1]))
                    auth_id = auth["id"]
                    ws.send(json.dumps(["AUTH", auth]))
                elif msg[0] == "OK" and msg[1] == auth_id:
                    if not msg[2]:
                        raise RuntimeError(f"AUTH refused: {msg[3] if len(msg) > 3 else ''}")
                    batch.clear()
                    ws.send(req)
                elif msg[0] == "CLOSED" and msg[1] == sub:
                    if str(msg[2] if len(msg) > 2 else "").startswith("auth-required"):
                        continue
                    raise RuntimeError(f"subscription closed: {msg[2:]}")
                elif msg[0] == "EVENT" and msg[1] == sub:
                    event = msg[2]
                    if not eose:
                        batch.append(event)
                    elif event.get("kind") == nip44.GIFT_WRAP_KIND:
                        with self.lock:
                            self.wraps[event["id"]] = event
                        self.handle_wrap(event)
                    else:
                        self.add([event])
                elif msg[0] == "EOSE" and msg[1] == sub and not eose:
                    eose = True
                    self.refresh_stewards()
                    self.add(e for e in batch if e.get("kind") != nip44.GIFT_WRAP_KIND)
                    with self.lock:
                        self.synced = True
                        self.wraps.update(
                            (e["id"], e) for e in batch if e.get("kind") == nip44.GIFT_WRAP_KIND
                        )
                    batch.clear()
                    self.check_members()
                    self.process_waiting()
                    log.info(
                        "access: following %s (%d admins, %d members)",
                        self.relay_url,
                        len(self.admins()),
                        len(self.members()),
                    )


def _auth_template(relay: str, challenge: str) -> dict[str, Any]:
    return {
        "kind": AUTH_KIND,
        "created_at": int(time.time()),
        "tags": [["relay", relay], ["challenge", challenge]],
        "content": "",
    }


def _publish(ws: Any, event: dict[str, Any], relay: str, sign: Callable, timeout: float) -> None:
    ws.send(json.dumps(["EVENT", event]))
    auth_id, deadline = None, time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
        if msg[0] == "AUTH" and auth_id is None:
            auth = sign(_auth_template(relay, msg[1]))
            auth_id = auth["id"]
            ws.send(json.dumps(["AUTH", auth]))
        elif msg[0] == "OK" and msg[1] == auth_id:
            if not msg[2]:
                raise RuntimeError(f"AUTH refused: {msg[3] if len(msg) > 3 else ''}")
            ws.send(json.dumps(["EVENT", event]))
        elif msg[0] == "OK" and msg[1] == event["id"]:
            if msg[2]:
                return
            if str(msg[3] if len(msg) > 3 else "").startswith("auth-required"):
                continue
            raise RuntimeError(f"kind {event['kind']} refused: {msg[3] if len(msg) > 3 else ''}")
    raise RuntimeError("no OK from the relay")


def _query(ws: Any, filters: list[dict[str, Any]], relay: str, sign: Callable, timeout: float):
    sub = "access"
    req = json.dumps(["REQ", sub, *filters])
    ws.send(req)
    events: list[dict[str, Any]] = []
    auth_id, deadline = None, time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
        if msg[0] == "AUTH" and auth_id is None:
            auth = sign(_auth_template(relay, msg[1]))
            auth_id = auth["id"]
            ws.send(json.dumps(["AUTH", auth]))
        elif msg[0] == "OK" and msg[1] == auth_id:
            if not msg[2]:
                raise RuntimeError(f"AUTH refused: {msg[3] if len(msg) > 3 else ''}")
            events.clear()
            ws.send(req)
        elif msg[0] == "EVENT" and msg[1] == sub:
            events.append(msg[2])
        elif msg[0] == "EOSE" and msg[1] == sub:
            return events
        elif msg[0] == "CLOSED" and msg[1] == sub:
            if str(msg[2] if len(msg) > 2 else "").startswith("auth-required"):
                continue
            raise RuntimeError(f"query closed: {msg[2:]}")
    raise RuntimeError("no EOSE from the relay")


def from_settings(settings: Any, stewards: Callable, on_members: Callable, extra_relays=()):
    """The service, or None without the groups relay and its key (access then stays with the
    passes alone)."""
    from .groups_members import GroupsMembersError, api_key

    try:
        key = api_key(settings.groups_api_nsec)
    except GroupsMembersError:
        return None
    if key is None or not settings.groups_relay_public_url or not settings.access_enabled:
        return None
    return AccessService(
        key,
        settings.groups_relay_public_url,
        transport_url=re.sub(r"^http", "ws", settings.groups_api_url or ""),
        community=settings.community_name,
        app_url=settings.access_app_url,
        stewards=stewards,
        on_members=on_members,
        extra_relays=extra_relays,
    )
