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
import json
import logging
import re
import sqlite3
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
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
    return website.parse_ref(message.get("content") or "", website.domain(svc), svc)


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
    try:  # the message's own ["path", …] tags: only those paths of the website change
        scope = website.parse_paths(website.path_tags(p.message))
    except website.PathsError:
        return Result.error("failed", f"Couldn't publish: {website.INVALID_PATHS}.")
    name = await run_in_threadpool(_name, svc, p.press["pubkey"])
    try:
        # the message's imeta screenshots and ["summary", …] go into the "Website updated" line
        await website.publish_from(svc, target[0], target[1], name, scope, source=p.message)
    except Exception as exc:  # noqa: BLE001 — said under the button
        log.warning("actions: website-publish failed: %r", exc)
        return Result.error("failed", f"Couldn't publish: {_short(exc)}.")
    hp = getattr(getattr(p.app, "state", None), "homepage", None)
    if hp is not None:
        hp._site = None  # serve the new version at once
    if scope:
        return Result("ok", f"Published by {name}: only {website.scope_words(scope)}.", closed=True)
    return Result("ok", f"Published by {name}.", closed=True)


# name (after "belfry:") → handler. New actions go into docs/actions.md §4 first.
async def offer_decide(p: Press) -> Result:
    from . import sales

    return await sales.offer_decide_press(p)


async def request_close(p: Press) -> Result:
    from . import sales

    return await sales.request_close_press(p)


HANDLERS: dict[str, Handler] = {
    "website-publish": website_publish,
    "offer-decide": offer_decide,  # an offer in kind's card (sales.py)
    "request-close": request_close,  # a plain request's card (sales.py)
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
        result = await self.decide(press, message, button)
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

    async def decide(
        self, press: dict[str, Any], message: dict[str, Any], button: dict[str, str]
    ) -> Result:
        """The answer to PRESS on BUTTON of MESSAGE: too old, too many, or the handler's."""
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
        return result

    # ── a private group, format 3 (docs/encrypted.md §24) ─────────────────────────────────
    def copy_of(self, rumor: dict[str, Any]) -> tuple[dict, dict, dict[str, str]] | None:
        """(press, message, button) of the copy of a press sent to the groups key, or None when
        it isn't one: kind 1985 ``belfry/action`` with ``h``, ``["e", press id, "", "press"]``,
        the message as JSON in the content (its id not recomputed: imeta keys are stripped)
        naming the same group, and the pressed ``belfry:`` button among its tags."""
        if rumor.get("kind") != 1985:
            return None
        tags = [t for t in rumor.get("tags", []) if isinstance(t, list)]
        if ["L", PRESS_NS] not in [t[:2] for t in tags]:
            return None
        bid = next((t[1] for t in tags if len(t) > 2 and t[0] == "l" and t[2] == PRESS_NS), "")
        group = _tag(rumor, "h")
        press_ids = [t[1] for t in tags if len(t) > 3 and t[0] == "e" and t[3] == "press"]
        targets = [
            t[1] for t in tags if len(t) > 1 and t[0] == "e" and (len(t) < 4 or t[3] != "press")
        ]
        if not BUTTON_ID.fullmatch(bid or "") or not group or len(press_ids) != 1:
            return None
        try:
            message = json.loads(rumor.get("content") or "")
        except ValueError:
            return None
        if (
            not isinstance(message, dict)
            or not isinstance(message.get("id"), str)
            or message.get("kind") not in MESSAGE_KINDS
            or not isinstance(message.get("tags"), list)
            or _tag(message, "h") != group
            or (targets and targets[0] != message["id"])
        ):
            return None
        button = next((b for b in parse_buttons(message["tags"]) if b["id"] == bid), None)
        if button is None or not button["action"].startswith("belfry:"):
            return None
        press = {
            "id": press_ids[0],
            "pubkey": rumor["pubkey"],
            "button": bid,
            "message": message["id"],
            "group": group,
            "created_at": int(rumor.get("created_at") or 0),
        }
        return press, message, button

    async def handle_copy(self, event: dict[str, Any]) -> Result | None:
        """An 8572 to the groups key on the community relay: the copy of a press in a private
        group. Opened as the groups key and judged as the apps judge it; the presser must be in
        the group's member list (39002); the action's own rules as in clear. The answer goes
        into the group as an 8572 by the groups key (enc.send_group), told to nobody."""
        from . import enc

        secret = enc.groups_secret(self.svc)
        if secret is None or not isinstance(event, dict) or not self._mark(str(event.get("id"))):
            return None
        try:
            rumor = await run_in_threadpool(enc.open_for, self.svc, secret, event)
        except enc.Later:
            self.seen.pop(str(event.get("id")), None)  # looked at again on the next catch-up
            return None
        except ValueError:
            return None
        found = self.copy_of(rumor)
        if found is None:
            return None
        press, message, button = found
        if not self._mark("press:" + press["id"]) or await run_in_threadpool(
            _answered_before, self.svc, press["id"]
        ):
            return None
        try:
            members = await run_in_threadpool(enc.group_members, self.svc, press["group"])
            private = await run_in_threadpool(enc.is_private, self.svc, press["group"])
        except Exception as exc:  # noqa: BLE001 — not known now: looked at again later
            log.info("actions: group of a copy not read: %r", exc)
            self.seen.pop(str(event.get("id")), None)
            self.seen.pop("press:" + press["id"], None)
            return None
        if not private:
            return None  # copies exist for private groups only
        if press["pubkey"] not in members:
            # not in the group: its press can't be in the group either, and an answer there
            # would reach only the members, never them. Nothing is done.
            log.info(
                "actions: a copy from %s…, not in %s: left", press["pubkey"][:8], press["group"]
            )
            return None
        result = await self.decide(press, message, button)
        answer = {**answer_event(press, result), "pubkey": enc.pubkey_of(secret)}
        try:
            await run_in_threadpool(
                lambda: enc.send_group(self.svc, secret, answer, press["group"], silent=True)
            )
        except Exception as exc:  # noqa: BLE001 — the press is answered when it can be
            log.warning("actions: an encrypted answer not sent: %r", exc)
            return None
        _answered_now(self.svc, press["id"])
        log.info(
            "actions: %s by %s… (private group) → %s %s",
            button["action"],
            press["pubkey"][:8],
            result.status,
            result.code,
        )
        return result


# presses answered in private groups: the answer is encrypted, so the relay can't be asked (as
# ``_answered`` does in clear); kept next to the register for a restart
_answered_lock = threading.Lock()
_answered_dbs: dict[str, sqlite3.Connection] = {}


def _answered_db(svc: Services) -> sqlite3.Connection:
    path = str(Path(svc.settings.register_path).with_name("presses.sqlite"))
    with _answered_lock:
        if path not in _answered_dbs:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            db.execute("CREATE TABLE IF NOT EXISTS answered (press TEXT PRIMARY KEY, at INTEGER)")
            _answered_dbs[path] = db
        return _answered_dbs[path]


def _answered_before(svc: Services, press_id: str) -> bool:
    db = _answered_db(svc)
    with _answered_lock:
        return (
            db.execute("SELECT 1 FROM answered WHERE press = ?", (press_id,)).fetchone() is not None
        )


def _answered_now(svc: Services, press_id: str) -> None:
    db = _answered_db(svc)
    with _answered_lock:
        db.execute(
            "INSERT OR IGNORE INTO answered (press, at) VALUES (?, ?)", (press_id, int(time.time()))
        )


def filters(_first: bool) -> list[dict[str, Any]]:
    return [{"kinds": [1985], "#L": [PRESS_NS], "since": int(time.time()) - CATCHUP, "limit": 200}]


def trusted_keys(svc: Services) -> set[str]:
    from .homepage import belfry_pubkey
    from .webthreads import _groups_pubkey

    return {k for k in (_groups_pubkey(svc), belfry_pubkey(svc.settings)) if k}


def copies_job(svc: Services, answerer: Answerer) -> Awaitable[None] | None:
    """Format 3 (docs/encrypted.md §24): the copies of presses in private groups, 8572s to the
    groups key on the community relay (which serves them only to that key, §18.1)."""
    from . import enc, push

    secret = enc.groups_secret(svc)
    if not enc.format3(svc) or secret is None or not svc.settings.relay_url:
        return None
    me = enc.pubkey_of(secret)

    def copy_filters(_first: bool) -> list[dict[str, Any]]:
        return [{"kinds": [enc.MSG], "#p": [me], "since": int(time.time()) - CATCHUP}]

    async def on_copy(e: dict[str, Any], _live: bool, _first: bool) -> None:
        try:
            await answerer.handle_copy(e)
        except Exception as exc:  # noqa: BLE001 — one copy never stops the rest
            log.warning("actions: a copy %s… failed: %r", str(e.get("id"))[:8], exc)

    return push.subscribe(
        svc.settings.relay_url, copy_filters, lambda t: enc.sign(secret, t), on_copy
    )


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

        jobs = [work(), push.subscribe(relay.url, filters, sign, on_event)]
        copies = copies_job(svc, answerer)
        if copies is not None:
            jobs.append(copies)

        async def both() -> None:
            await asyncio.gather(*jobs)

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
