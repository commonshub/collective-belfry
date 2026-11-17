"""Buttons on messages (docs/actions.md): the belfry answers presses on buttons that name a
``belfry:`` action.

A message (kind 9 or 1111 in a group) carries up to four ``["button", id, label, style, action]``
tags. Tapping one publishes a press: a kind 1985 label ``belfry/action`` signed by whoever tapped.
This module keeps one live subscription on the groups relay, as the groups key (the owner: it
reads every group), for presses of the last ``CATCHUP`` seconds; after a reconnect it sees again
what it may have missed. For each press it fetches the message, finds the pressed button and,
when that button's action starts with ``belfry:``, runs the handler named in ``HANDLERS`` and
answers once with a kind 1985 label ``belfry/action-result`` signed by the **groups key** (the
key clients find as ``groups_key`` in belfry.json; ``nostr`` there is the belfry key, which
clients trust as well). Buttons without an action belong to the message's author: skipped.

Once per press: an in-memory set, and before answering the relay is asked whether an answer to
that press already exists (a restart, two services). A press older than ``MAX_AGE`` when it's
seen gets ``expired``; more than ``RATE`` presses an hour from one key get ``failed``.

Rights are checked on the person who pressed, never on the message's author: anyone may put a
``belfry:website-publish`` button on a message, only an admin of the belfry makes it publish.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from .core import Services

log = logging.getLogger(__name__)

PRESS_NS = "belfry/action"
RESULT_NS = "belfry/action-result"
MAX_BUTTONS = 4
BUTTON_ID = re.compile(r"[a-z0-9-]{1,32}")
STYLES = ("", "primary", "danger")
MESSAGE_KINDS = (9, 1111)
MAX_AGE = 600  # a press older than this when the belfry sees it: expired
CATCHUP = 1800  # presses asked for on every (re)connection
RATE = 20  # presses an hour per key
SEEN_MAX = 5000

FIXED = {
    "not_allowed": "You're not allowed to do this.",
    "unknown_action": "This belfry doesn't know this button yet.",
    "no_target": "This message doesn't say what to act on.",
    "gone": "This is no longer there.",
    "expired": "That took too long. Try again.",
    "failed": "That didn't work. Try again.",
}


# ── the buttons on a message ────────────────────────────────────────────────────────────────
def parse_buttons(tags: list[Any]) -> list[dict[str, str]]:
    """The buttons TAGS carry, in order (docs/actions.md §1): bad tags, repeated ids and every
    button after the fourth are left out; an unknown style reads as plain."""
    out: list[dict[str, str]] = []
    ids: set[str] = set()
    for t in tags or []:
        if not isinstance(t, list) or len(t) < 3 or t[0] != "button":
            continue
        if not all(isinstance(x, str) for x in t[1:5]):
            continue
        bid, label = t[1], t[2].strip()
        if not BUTTON_ID.fullmatch(bid) or not label or len(label) > 40 or bid in ids:
            continue
        style = t[3] if len(t) > 3 and t[3] in STYLES else ""
        action = t[4] if len(t) > 4 else ""
        ids.add(bid)
        out.append({"id": bid, "label": label, "style": style, "action": action})
        if len(out) == MAX_BUTTONS:
            break
    return out


def _tag(e: dict[str, Any], k: str) -> str:
    return next(
        (
            t[1]
            for t in e.get("tags", [])
            if isinstance(t, list) and len(t) > 1 and t[0] == k and isinstance(t[1], str)
        ),
        "",
    )


def press_of(e: dict[str, Any]) -> dict[str, Any] | None:
    """{id, pubkey, button, message, group, created_at} of a well-formed press, else None."""
    if not isinstance(e, dict) or e.get("kind") != 1985:
        return None
    tags = [t for t in e.get("tags", []) if isinstance(t, list)]
    if ["L", PRESS_NS] not in [t[:2] for t in tags]:
        return None
    button = next(
        (t[1] for t in tags if len(t) > 2 and t[0] == "l" and t[2] == PRESS_NS),
        "",
    )
    message, group = _tag(e, "e"), _tag(e, "h")
    if not BUTTON_ID.fullmatch(button or "") or not message or not group:
        return None
    if not isinstance(e.get("id"), str) or not isinstance(e.get("pubkey"), str):
        return None
    return {
        "id": e["id"],
        "pubkey": e["pubkey"],
        "button": button,
        "message": message,
        "group": group,
        "created_at": int(e.get("created_at") or 0),
    }


# ── results and handlers ────────────────────────────────────────────────────────────────────
@dataclass
class Result:
    status: str  # "ok" | "error"
    text: str = ""
    code: str = ""
    closed: bool = False

    @classmethod
    def error(cls, code: str, text: str = "") -> Result:
        return cls("error", text or FIXED.get(code, "That didn't work."), code)


@dataclass
class Press:
    """What a handler gets: the press, the message, the button."""

    svc: Services
    press: dict[str, Any]
    message: dict[str, Any]
    button: dict[str, str]
    app: Any = None


Handler = Callable[[Press], Awaitable[Result]]


def _name(svc: Services, pubkey: str) -> str:
    from . import community

    try:
        return (community.names_for(svc, [pubkey]) or {}).get(pubkey) or "an admin"
    except Exception:  # noqa: BLE001 — a name is a nicety
        return "an admin"


def website_target(svc: Services, message: dict[str, Any]) -> tuple[str, str] | None:
    """The site a message points to: its first ``a`` 35128:<pubkey>:<name>, else the first
    nsite / preview link in its text (website.parse_ref)."""
    from . import website

    for t in message.get("tags", []):
        if isinstance(t, list) and len(t) > 1 and t[0] == "a" and isinstance(t[1], str):
            parts = t[1].split(":", 2)
            if len(parts) == 3 and parts[0] == "35128" and re.fullmatch(r"[0-9a-f]{64}", parts[1]):
                if parts[2] == "" or re.fullmatch(website.NAME, parts[2]):
                    return parts[1], parts[2]
            break  # only the first `a` tag counts
    return website.parse_ref(message.get("content") or "", website.domain(svc))


def _short(exc: Exception) -> str:
    from . import homepage

    if isinstance(exc, homepage.HomepageError):
        text = str(exc).strip().rstrip(".")
        return (text[:1].lower() + text[1:])[:120] or "the files could not be copied"
    return "the files could not be copied"


async def website_publish(p: Press) -> Result:
    from . import apps, website

    svc = p.svc
    admins = await run_in_threadpool(apps.admin_pubkeys, svc)
    if p.press["pubkey"] not in admins:
        return Result.error("not_allowed", "Only admins of this belfry can publish the website.")
    target = website_target(svc, p.message)
    if target is None:
        return Result.error("no_target", "This message doesn't point to a version of the website.")
    name = await run_in_threadpool(_name, svc, p.press["pubkey"])
    try:
        await website.publish_from(svc, target[0], target[1], name)
    except Exception as exc:  # noqa: BLE001 — said under the button
        log.warning("actions: website-publish failed: %r", exc)
        return Result.error("failed", f"Couldn't publish: {_short(exc)}.")
    hp = getattr(getattr(p.app, "state", None), "homepage", None)
    if hp is not None:
        hp._site = None  # serve the new version at once
    return Result("ok", f"Published by {name}.", closed=True)


# name (after "belfry:") → handler. New actions go into docs/actions.md §4 first.
HANDLERS: dict[str, Handler] = {
    "website-publish": website_publish,
}


# ── answering ───────────────────────────────────────────────────────────────────────────────
def answer_event(press: dict[str, Any], result: Result) -> dict[str, Any]:
    tags = [
        ["L", RESULT_NS],
        ["l", result.status, RESULT_NS],
        ["e", press["message"]],
        ["press", press["id"]],
        ["p", press["pubkey"]],
        ["h", press["group"]],
    ]
    if result.status == "error":
        tags.append(["code", result.code or "failed"])
    elif result.closed:
        tags.append(["closed", "1"])
    return {"kind": 1985, "created_at": int(time.time()), "tags": tags, "content": result.text}


class Answerer:
    """Handles presses one at a time: fetch, check, run, answer once."""

    def __init__(
        self,
        svc: Services,
        relay: Any,
        sign: Callable[[dict[str, Any]], dict[str, Any]],
        handlers: dict[str, Handler] | None = None,
        app: Any = None,
        clock: Callable[[], float] = time.time,
        trusted: set[str] | None = None,
    ):
        self.svc = svc
        self.relay = relay
        self.sign = sign
        self.handlers = HANDLERS if handlers is None else handlers
        self.app = app
        self.clock = clock
        self.trusted = trusted or set()  # keys whose answers count (groups key, belfry key)
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.tries: dict[str, deque[float]] = {}

    def _mark(self, pid: str) -> bool:
        """True the first time PID is seen."""
        if pid in self.seen:
            return False
        self.seen[pid] = None
        while len(self.seen) > SEEN_MAX:
            self.seen.popitem(last=False)
        return True

    def _answered(self, press: dict[str, Any]) -> bool:
        found = self.relay.query(
            [{"kinds": [1985], "#L": [RESULT_NS], "#e": [press["message"]], "limit": 500}],
            self.sign,
        )
        return any(
            _tag(a, "press") == press["id"]
            and (not self.trusted or a.get("pubkey") in self.trusted)
            for a in found or []
        )

    def _too_many(self, pubkey: str) -> bool:
        now = self.clock()
        q = self.tries.setdefault(pubkey, deque())
        while q and now - q[0] > 3600:
            q.popleft()
        q.append(now)
        return len(q) > RATE

    async def handle(self, e: dict[str, Any]) -> Result | None:
        """The answer given to press E, or None when the belfry doesn't answer it."""
        press = press_of(e)
        if press is None or not self._mark(press["id"]):
            return None
        found = await run_in_threadpool(self.relay.query, [{"ids": [press["message"]]}], self.sign)
        message = next((m for m in found or [] if m.get("id") == press["message"]), None)
        if message is None or message.get("kind") not in MESSAGE_KINDS:
            return None  # nothing says the belfry handles it
        if _tag(message, "h") != press["group"]:
            return None
        button = next(
            (b for b in parse_buttons(message.get("tags", [])) if b["id"] == press["button"]), None
        )
        if button is None or not button["action"].startswith("belfry:"):
            return None  # the author's button (or no such button)
        if await run_in_threadpool(self._answered, press):
            return None
        if self.clock() - press["created_at"] > MAX_AGE:
            result = Result.error("expired")
        elif self._too_many(press["pubkey"]):
            result = Result.error("failed", "Too many tries. Wait a little.")
        else:
            handler = self.handlers.get(button["action"].removeprefix("belfry:"))
            if handler is None:
                result = Result.error("unknown_action")
            else:
                try:
                    result = await handler(Press(self.svc, press, message, button, self.app))
                except Exception as exc:  # noqa: BLE001 — every press gets an answer
                    log.warning("actions: %s failed: %r", button["action"], exc)
                    result = Result.error("failed")
        await run_in_threadpool(
            self.relay.publish, self.sign(answer_event(press, result)), self.sign
        )
        log.info(
            "actions: %s by %s… → %s %s",
            button["action"],
            press["pubkey"][:8],
            result.status,
            result.code,
        )
        return result


def filters(_first: bool) -> list[dict[str, Any]]:
    return [{"kinds": [1985], "#L": [PRESS_NS], "since": int(time.time()) - CATCHUP, "limit": 200}]


def trusted_keys(svc: Services) -> set[str]:
    from .homepage import belfry_pubkey
    from .webthreads import _groups_pubkey

    return {k for k in (_groups_pubkey(svc), belfry_pubkey(svc.settings)) if k}


@contextlib.asynccontextmanager
async def running(app: FastAPI):
    from . import followers, push
    from .relay import RelayPublisher

    svc = app.state.services
    task: asyncio.Task[None] | None = None
    try:
        got = followers._relay(svc)
    except Exception as exc:  # noqa: BLE001 — the service runs without buttons
        log.warning("actions: not started: %r", exc)
        got = None
    # only a real groups relay (tests hand in fakes, which must not see a live REQ)
    if got is not None and isinstance(got[0], RelayPublisher) and got[0].url:
        relay, sign = got
        answerer = Answerer(svc, relay, sign, app=app, trusted=trusted_keys(svc))
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(500)

        async def on_event(e: dict[str, Any], _live: bool, _first: bool) -> None:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(e)

        async def work() -> None:
            while True:
                e = await queue.get()
                try:
                    await answerer.handle(e)
                except Exception as exc:  # noqa: BLE001 — one press never stops the rest
                    log.warning("actions: press %s… failed: %r", str(e.get("id"))[:8], exc)
                    with contextlib.suppress(Exception):
                        answerer.seen.pop(str(e.get("id")), None)  # tried again on reconnect

        async def both() -> None:
            await asyncio.gather(work(), push.subscribe(relay.url, filters, sign, on_event))

        async def start() -> None:
            from . import website

            try:  # an old default description of #website gets the new one (website.py)
                await run_in_threadpool(website.refresh_about, svc, relay, sign)
            except Exception as exc:  # noqa: BLE001
                log.warning("actions: #website description not checked: %r", exc)
            await both()

        task = asyncio.create_task(start())
        log.info("actions: watching presses on %s", relay.url)
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
