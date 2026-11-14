"""Requests to join a group, accepted by vouches (Xavier, 2026-11-14).

A group (a closed NIP-29 group, ids ``pod-…``) takes requests: a 9021 by the applicant with
their answer to the group's question. Its admins accept (9000) or decline (9001), as before. A
group's admins may also say that N people vouching is enough (1–5): the tag ``["vouches", "N"]``
on the group's metadata edit (kind 9002, next to ``closed`` and ``prompt``; absent or ``0``:
admins accept).

A vouch is a NIP-32 label in the group, by a member of it::

    {"kind": 1985, "tags": [["h", <group>], ["L", "belfry/vouch"],
                            ["l", "vouch" | "withdrawn", "belfry/vouch"],
                            ["e", <the 9021's id>], ["p", <applicant>]], "content": ""}

The latest label per (voucher, request) counts. A vouch counts when its author is in the group
now (its 39002 list), is a member of the belfry (not a guest, not an agent: whois.kinds) and is
not the applicant; a vouch for an older request of the same person doesn't carry over.

When a waiting request has N such vouches, this module admits the applicant as the groups key
(the relay's owner, like guests.admitted)::

    {"kind": 9000, "tags": [["h", <group>], ["p", <applicant>, "member"], ["by", "vouches"],
                            ["vouches", "<n>"], ["e", <the 9021's id>]]}

``watch`` runs in the service's round (followers.tick, every few minutes) over the requests of
the last 30 days; ``POST /api/group-requests/check`` ``{"channel"}`` (NIP-98, anyone with an
active pass) runs it at once for one group, after a vouch, → ``{"admitted": [hex]}``. Both are
idempotent: a request that has a 9000 / 9001 / 9022 after it, or whose author is in the group,
is not waiting any more.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .core import Services

log = logging.getLogger(__name__)

NS = "belfry/vouch"
VOUCHES_MAX = 5
WINDOW = 30 * 86400  # requests older than this are left alone
CHANNEL = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_lock = threading.Lock()
_done: set[str] = set()  # request ids admitted by this process (the relay may lag)


def _tag(e: dict[str, Any], name: str) -> str | None:
    return next((t[1] for t in e.get("tags", []) if len(t) > 1 and t[0] == name), None)


def needed(events: list[dict[str, Any]]) -> int:
    """The group's rule from its metadata events (9002 by its admins, 39000 by the relay): the
    newest one that says anything about access. 0: admins accept."""
    best: tuple[int, int] | None = None
    for e in events:
        tags = e.get("tags", [])
        if not any(t[:1] in (["vouches"], ["closed"], ["open"]) for t in tags):
            continue
        n = 0
        raw = _tag(e, "vouches")
        if raw is not None and raw.isdigit():
            n = min(int(raw), VOUCHES_MAX)
        if any(t[:1] == ["open"] for t in tags):
            n = 0
        if best is None or e["created_at"] >= best[0]:
            best = (e["created_at"], n)
    return best[1] if best else 0


def waiting(
    requests: list[dict[str, Any]], decisions: list[dict[str, Any]], members: set[str]
) -> dict[str, dict[str, Any]]:
    """applicant → their latest 9021 that nothing answered yet."""
    latest: dict[str, dict[str, Any]] = {}
    for e in requests:
        if e.get("kind") != 9021:
            continue
        p = e["pubkey"]
        if p not in latest or e["created_at"] > latest[p]["created_at"]:
            latest[p] = e
    answered: dict[str, int] = {}
    for e in decisions:
        who = e["pubkey"] if e.get("kind") == 9022 else _tag(e, "p")
        if who:
            answered[who] = max(answered.get(who, 0), e["created_at"])
    return {
        p: e
        for p, e in latest.items()
        if p not in members and answered.get(p, -1) < e["created_at"] and e["id"] not in _done
    }


def count(labels: list[dict[str, Any]], request: dict[str, Any], voters: set[str]) -> list[str]:
    """Who vouches for REQUEST now: the latest label per author, ``vouch``, from VOTERS."""
    last: dict[str, tuple[int, str]] = {}
    for e in labels:
        if e.get("kind") != 1985 or _tag(e, "L") != NS or _tag(e, "e") != request["id"]:
            continue
        value = _tag(e, "l") or ""
        a = e["pubkey"]
        if a not in last or e["created_at"] >= last[a][0]:
            last[a] = (e["created_at"], value)
    return sorted(
        a for a, (_, v) in last.items() if v == "vouch" and a in voters and a != request["pubkey"]
    )


def check(svc: Services, channel: str, kinds: dict[str, str] | None = None) -> list[str]:
    """Admit the applicants of CHANNEL who have enough vouches; their keys."""
    from . import followers, whois

    got = followers._relay(svc)
    if got is None:
        return []
    relay, sign = got
    since = int(time.time()) - WINDOW
    meta = relay.query(
        [
            {"kinds": [9002], "#h": [channel], "limit": 50},
            {"kinds": [39000], "#d": [channel], "limit": 1},
        ],
        sign,
    )
    n = needed(meta)
    if n <= 0:
        return []
    lists = relay.query([{"kinds": [39002], "#d": [channel], "limit": 1}], sign)
    members = {t[1] for e in lists[:1] for t in e.get("tags", []) if t[:1] == ["p"] and len(t) > 1}
    found = relay.query(
        [
            {"kinds": [9021], "#h": [channel], "since": since, "limit": 200},
            {"kinds": [9000, 9001, 9022], "#h": [channel], "since": since, "limit": 500},
        ],
        sign,
    )
    open_ = waiting(
        [e for e in found if e["kind"] == 9021],
        [e for e in found if e["kind"] != 9021],
        members,
    )
    if not open_:
        return []
    labels = relay.query([{"kinds": [1985], "#L": [NS], "#h": [channel], "limit": 500}], sign)
    kinds = kinds if kinds is not None else whois.kinds(svc)
    voters = {p for p in members if kinds.get(p) == "member"}
    admitted: list[str] = []
    for applicant, req in open_.items():
        who = count(labels, req, voters)
        if len(who) < n:
            continue
        with _lock:
            if req["id"] in _done:
                continue
            tags = [
                ["h", channel],
                ["p", applicant, "member"],
                ["by", "vouches"],
                ["vouches", str(len(who))],
                ["e", req["id"]],
            ]
            try:
                relay.publish(
                    sign(
                        {"kind": 9000, "created_at": int(time.time()), "tags": tags, "content": ""}
                    ),
                    sign,
                )
            except Exception as exc:  # noqa: BLE001 — tried again on the next round
                log.warning("vouches: %s… not admitted to %s: %s", applicant[:8], channel, exc)
                continue
            _done.add(req["id"])
        log.info(
            "vouches: %s… admitted to %s by %d vouches (%s)",
            applicant[:8],
            channel,
            len(who),
            ", ".join(v[:8] for v in who),
        )
        admitted.append(applicant)
    return admitted


def watch(svc: Services) -> list[str]:
    """Every group with a recent request (the service's round)."""
    from . import followers, whois

    got = followers._relay(svc)
    if got is None:
        return []
    relay, sign = got
    recent = relay.query(
        [{"kinds": [9021], "since": int(time.time()) - WINDOW, "limit": 500}], sign
    )
    channels = sorted({c for e in recent if (c := _tag(e, "h")) and CHANNEL.match(c)})
    if not channels:
        return []
    kinds = whois.kinds(svc)
    out: list[str] = []
    for ch in channels:
        try:
            out += check(svc, ch, kinds)
        except Exception as exc:  # noqa: BLE001 — the next group, and the next round
            log.warning("vouches: %s: %s", ch, exc)
    return out


def register(app: FastAPI) -> None:
    from . import whois
    from .holder_invites import ApiError, authenticate

    @app.post("/api/group-requests/check")
    async def group_requests_check(request: Request) -> JSONResponse:
        s = request.app.state.services
        body = await request.body()
        caller = authenticate(
            s,
            request.app.state.api_seen,
            request.headers.get("authorization"),
            "POST",
            "/api/group-requests/check",
            body,
        )
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = None
        channel = data.get("channel") if isinstance(data, dict) else None
        if not isinstance(channel, str) or not CHANNEL.match(channel):
            raise ApiError(422, "invalid", 'Send {"channel": "<group id>"}.')

        def run() -> dict[str, Any]:
            if caller not in whois.kinds(s):
                raise ApiError(403, "not_here", "Only people of this belfry can ask.")
            return {"admitted": check(s, channel)}

        return JSONResponse(await run_in_threadpool(run), headers={"Cache-Control": "no-store"})
