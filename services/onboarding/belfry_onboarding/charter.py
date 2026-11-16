"""The belfry's charter: a short list of agreements every person ticks for themselves (iOS SPEC
19.32, 2026-11-16; docs/charter.md).

It is the to-do list of the NIP-29 group ``charter`` on the groups relay (a fixed id, made by
the admin who writes the first agreement), so no new kind is needed:

- **an agreement** = kind 2100 with ``["t", LIST_ID]`` (the list id of ``charter``:
  sha256hex("osv-village:charter")[:16]), ``["h", "charter"]``, ``["each", "1"]``,
  ``["order", "<int>"]`` and an optional ``["about", "<one line>"]``; content = its title. Only a
  2100 by a **belfry admin** counts;
- **a change** = 2101 ``["e", id] ["action", "edit"]`` (+ the same tags) by a belfry admin:
  content = the full title, ``about`` (missing: emptied) and ``order`` (missing: kept). When the
  title or the explanation changed, that edit's ``created_at`` becomes the agreement's **text
  time**. **Removal** = 2101 ``action`` ``delete`` by a belfry admin;
- **a person's tick** = their own 2101 ``done`` / ``undone`` (latest per author wins); they
  agreed when the latest is ``done`` and not older than the text time (else it is *stale*);
- **order**: ``order`` ascending (none last), then the 2100's ``created_at``, then id.

The server reads it as the groups key (the relay's owner) for the people who can't read the
group themselves (guests, people applying from outside):

- ``GET /api/charter`` (no auth: the belfry's rules are public like its homepage) →
  ``{"agreements": [{"id", "title", "about", "order", "added", "text_at"}]}`` in order
  (``order`` is null when the agreement has none); 503 ``unavailable`` when the relay can't be
  read. Cached 60 seconds.
- belfry.json ``"charter": true|false`` (discovery.py, through ``known``): never waits long for
  the relay; left out while nothing is known.
- ``publish_ticks``: when someone who ticked agreements before they were in (a guest's request,
  the web form ``/apply``) becomes a member, the belfry signs one ``done`` per agreement for
  them, as them, through the signer (community.member_signer), so they don't tick twice.
"""

from __future__ import annotations

import hashlib
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

GROUP = "charter"
LIST_ID = hashlib.sha256(b"osv-village:" + GROUP.encode()).hexdigest()[:16]
TITLE_MAX, ABOUT_MAX = 300, 200  # UTF-16 units, as the apps count
CACHE_SECONDS = 60.0
WAIT_SECONDS = 1.5  # what belfry.json waits for a first answer at most
HEX64 = re.compile(r"^[0-9a-f]{64}$")
PATH = "/api/charter"


def _tag(e: dict[str, Any], name: str) -> str | None:
    for t in e.get("tags") or []:
        if isinstance(t, list) and len(t) > 1 and t[0] == name:
            return str(t[1])
    return None


def _units(text: str, n: int) -> str:
    """TEXT cut to N UTF-16 code units (a surrogate pair is never split)."""
    raw = text.encode("utf-16-le")
    if len(raw) <= 2 * n:
        return text
    return raw[: 2 * n].decode("utf-16-le", errors="ignore")


def _int(value: str | None) -> int | None:
    try:
        return int(str(value).strip()) if value is not None else None
    except ValueError:
        return None


def parse(events: list[dict[str, Any]], admins: set[str]) -> list[dict[str, Any]]:
    """The agreements from EVENTS (2100 / 2101 of the charter's list), in order. Each:
    ``{"id", "title", "about", "order" (None = last), "author", "added", "text_at",
    "agreed": {pubkey: when}, "stale": [pubkey]}``."""
    evs = sorted(
        (
            e
            for e in events
            if e.get("kind") in (2100, 2101) and _tag(e, "t") == LIST_ID and _tag(e, "h") == GROUP
        ),
        key=lambda e: (int(e.get("created_at") or 0), str(e.get("id") or "")),
    )
    rules: dict[str, dict[str, Any]] = {}
    ticks: dict[str, dict[str, tuple[bool, int]]] = {}
    for e in evs:
        at = int(e.get("created_at") or 0)
        if e["kind"] == 2100:
            if _tag(e, "each") != "1" or e.get("pubkey") not in admins or e["id"] in rules:
                continue
            title = _units((e.get("content") or "").strip(), TITLE_MAX)
            if not title:
                continue
            rules[e["id"]] = {
                "id": e["id"],
                "title": title,
                "about": _units((_tag(e, "about") or "").strip(), ABOUT_MAX),
                "order": _int(_tag(e, "order")),
                "author": e["pubkey"],
                "added": at,
                "text_at": at,
            }
            continue
        rid = _tag(e, "e")
        r = rules.get(rid or "")
        if r is None:
            continue
        action = _tag(e, "action") or ""
        if action == "edit":
            if e.get("pubkey") not in admins:
                continue
            title = _units((e.get("content") or "").strip(), TITLE_MAX)
            about = _units((_tag(e, "about") or "").strip(), ABOUT_MAX)
            if title and (title != r["title"] or about != r["about"]):
                r.update(title=title, about=about, text_at=at)
            order = _int(_tag(e, "order"))
            if order is not None:
                r["order"] = order
        elif action == "delete":
            if e.get("pubkey") in admins:
                rules.pop(r["id"], None)
                ticks.pop(r["id"], None)
        elif action in ("done", "undone"):
            ticks.setdefault(r["id"], {})[e["pubkey"]] = (action == "done", at)
    out = []
    for r in rules.values():
        agreed: dict[str, int] = {}
        stale: list[str] = []
        for pk, (on, at) in (ticks.get(r["id"]) or {}).items():
            if on:
                if at >= r["text_at"]:
                    agreed[pk] = at
                else:
                    stale.append(pk)
        out.append({**r, "agreed": agreed, "stale": sorted(stale)})
    out.sort(key=lambda r: (r["order"] if r["order"] is not None else 2**63, r["added"], r["id"]))
    return out


def public(r: dict[str, Any]) -> dict[str, Any]:
    return {k: r[k] for k in ("id", "title", "about", "order", "added", "text_at")}


# ── reading it from the groups relay ────────────────────────────────────────────────────────
_lock = threading.Lock()
_cache: dict[str, Any] = {"at": float("-inf"), "agreements": None, "failed": float("-inf")}
_refreshing = threading.Lock()


def forget() -> None:
    with _lock:
        _cache.update(at=float("-inf"), agreements=None, failed=float("-inf"))


def fetch(svc: Services) -> list[dict[str, Any]]:
    """The agreements as the groups relay has them now (raises when it can't be read). Members'
    own ticks are not needed here: only the agreements and the admins' changes."""
    from . import apps, followers

    got = followers._relay(svc)
    if got is None:
        raise RuntimeError("no groups relay")
    relay, sign = got
    admins = set(apps.admin_pubkeys(svc))
    if not admins:
        return []
    found = relay.query(
        [
            {"kinds": [2100], "#h": [GROUP], "authors": sorted(admins), "limit": 500},
            {"kinds": [2101], "#h": [GROUP], "authors": sorted(admins), "limit": 2000},
        ],
        sign,
    )
    return parse(found, admins)


def agreements(svc: Services) -> list[dict[str, Any]]:
    """The agreements, cached CACHE_SECONDS (raises when the relay can't be read and nothing is
    cached)."""
    with _lock:
        if _cache["agreements"] is not None and time.monotonic() - _cache["at"] < CACHE_SECONDS:
            return _cache["agreements"]
    got = fetch(svc)
    with _lock:
        _cache.update(at=time.monotonic(), agreements=got)
    return got


def known(svc: Services, wait: float = WAIT_SECONDS) -> bool | None:
    """Whether the charter has at least one agreement, for belfry.json: the cached answer, else
    a refresh waited for at most WAIT seconds; None while nothing is known (relay down)."""
    with _lock:
        fresh = time.monotonic() - _cache["at"] < CACHE_SECONDS
        failed = time.monotonic() - _cache["failed"] < CACHE_SECONDS
        cached = _cache["agreements"]
    if cached is not None and fresh:
        return bool(cached)
    if failed:  # tried less than a minute ago: the last answer (or nothing), no waiting
        return None if cached is None else bool(cached)

    def refresh() -> None:
        try:
            agreements(svc)
        except Exception as exc:  # noqa: BLE001 — belfry.json leaves the key out
            log.info("charter: not readable: %s", exc)
            with _lock:
                _cache["failed"] = time.monotonic()
        finally:
            _refreshing.release()

    if _refreshing.acquire(blocking=False):
        t = threading.Thread(target=refresh, name="charter-refresh", daemon=True)
        t.start()
        t.join(wait)
    with _lock:
        cached = _cache["agreements"]
    return None if cached is None else bool(cached)


def clean_ids(value: Any) -> list[str] | None:
    """The agreement ids a request carries (hex, at most 100, no repeats); None when malformed."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 100:
        return None
    out: list[str] = []
    for v in value:
        if not isinstance(v, str) or not HEX64.match(v):
            return None
        if v not in out:
            out.append(v)
    return out


# ── their ticks, once they are members ──────────────────────────────────────────────────────
def tick_tags(agreement: str) -> list[list[str]]:
    return [
        ["e", agreement],
        ["action", "done"],
        ["t", LIST_ID],
        ["h", GROUP],
        ["each", "1"],
    ]


def publish_ticks(svc: Services, partner_id: int, agreed: list[str], asked_at: int) -> list[str]:
    """One ``done`` per agreement in AGREED that still exists and whose text is not newer than
    ASKED_AT (when the person ticked them), signed as the member PARTNER_ID through the signer
    and published on the groups relay. Returns the ids published; [] when the member's key is
    their own (the signer can't sign for them), the relay is down or nothing is left to tick."""
    from . import community, followers

    if not agreed:
        return []
    try:
        current = {r["id"]: r for r in fetch(svc)}
    except Exception as exc:  # noqa: BLE001
        log.warning("charter: ticks for partner %s not published: %s", partner_id, exc)
        return []
    wanted = [a for a in agreed if a in current and current[a]["text_at"] <= asked_at]
    if not wanted:
        return []
    found = community.member_signer(svc, partner_id)
    if found is None:
        log.info("charter: partner %s holds their own key; ticks left to them", partner_id)
        return []
    pubkey, sign = found
    got = followers._relay(svc)
    if got is None:
        return []
    relay, _ = got
    done: list[str] = []
    now = int(time.time())
    for a in wanted:
        if pubkey in current[a]["agreed"]:
            continue
        try:
            event = sign({"kind": 2101, "created_at": now, "tags": tick_tags(a), "content": ""})
            relay.publish(event, sign)
            done.append(a)
        except Exception as exc:  # noqa: BLE001 — they can tick it themselves
            log.warning(
                "charter: tick %s… for partner %s not published: %s", a[:8], partner_id, exc
            )
    if done:
        forget()
    log.info("charter: %d tick(s) published for partner %s", len(done), partner_id)
    return done


def register(app: FastAPI) -> None:
    @app.get(PATH)
    async def charter_get(request: Request) -> JSONResponse:
        s = request.app.state.services
        try:
            found = await run_in_threadpool(agreements, s)
        except Exception as exc:  # noqa: BLE001
            log.warning("charter: %s", exc)
            return JSONResponse(
                {"error": "unavailable", "message": "The charter can't be read just now."},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            {"agreements": [public(r) for r in found]},
            headers={"Cache-Control": "public, max-age=60"},
        )
