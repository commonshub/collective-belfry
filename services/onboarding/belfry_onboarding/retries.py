"""Two rules for the website's calls (docs/website-accounts.md "Retries"), each written once.

**A relay that can't be read is a 503, never an empty answer.** ``RelayDown`` is
``503 {"error": "relay"}`` with ``Retry-After: 5``. An endpoint runs its work through
``strictly`` (or ``run``): a relay's failure that reaches it becomes ``RelayDown``, and while
it runs the readers that otherwise degrade (an ``except`` that answers ``[]``, ``{}`` or
"Somebody") call ``soft(exc)`` first, which raises ``RelayDown`` for a relay's failure instead
of letting the answer look complete. Outside ``strictly`` (the HTML pages, the console, the
hourly jobs) ``soft`` does nothing: they degrade as before. What a relay's failure is:
``is_relay``. A signer or the register failing is not one: those degrade or answer their own
error, as before.

**``client_id``: a write repeated is answered, not done twice.** ``once`` remembers the first
2xx answer of (the account that signed, the call's method and path, the client's id) for 24
hours and gives it again with ``X-Belfry-Replayed: 1``; a repeat while the first still runs is
409 ``in_progress``; an error is never remembered. Kept in the register (table ``retries``:
it survives a restart); in memory with REGISTER=odoo.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import re
import threading
import time
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .holder_invites import ApiError
from .relay import RelayError

log = logging.getLogger(__name__)

RETRY_AFTER = "5"
REPLAYED = "X-Belfry-Replayed"
KEEP_SECONDS = 24 * 3600
CLIENT_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


# ── a relay that can't be read ──────────────────────────────────────────────────────────────
class RelayDown(ApiError):
    """503 ``relay``: the relay behind the answer could not be read (or written)."""

    headers = {"Retry-After": RETRY_AFTER}

    def __init__(self) -> None:
        super().__init__(503, "relay", "The relay isn't answering: try again in a moment.")


_strict: contextvars.ContextVar[bool] = contextvars.ContextVar("belfry_strict_reads", default=False)


def is_relay(exc: BaseException) -> bool:
    """Whether EXC is a relay not answering: refused, unreachable, timed out, closed, or its
    own refusal of the question (``RelayError``: a CLOSED, no EOSE, no OK). The signer's and
    the register's failures are other types (httpx's, sqlite's, Odoo's)."""
    if isinstance(exc, RelayDown | RelayError | TimeoutError | OSError):
        return True
    try:
        from websockets.exceptions import WebSocketException
    except ImportError:  # pragma: no cover
        return False
    return isinstance(exc, WebSocketException)


def check(exc: BaseException) -> None:
    """In an ``except`` that turns a failure into another answer: a relay's failure is
    ``RelayDown`` instead, always."""
    if isinstance(exc, RelayDown):
        raise exc
    if is_relay(exc):
        raise RelayDown from exc


def soft(exc: BaseException) -> None:
    """In an ``except`` that degrades (an empty list, "Somebody"): inside ``strictly`` a
    relay's failure is ``RelayDown`` instead; anywhere else, nothing."""
    if isinstance(exc, RelayDown):
        raise exc
    if _strict.get() and is_relay(exc):
        raise RelayDown from exc


def strict() -> bool:
    return _strict.get()


def strictly(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """FN, with every relay failure a ``RelayDown`` (see the module's text)."""
    token = _strict.set(True)
    try:
        return fn(*args, **kwargs)
    except ApiError:
        raise
    except Exception as exc:
        if is_relay(exc):
            log.warning("relay not answering in %s: %r", getattr(fn, "__name__", "?"), exc)
            raise RelayDown from exc
        raise
    finally:
        _strict.reset(token)


async def run(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """``strictly`` in a thread."""
    return await run_in_threadpool(strictly, fn, *args, **kwargs)


# ── client_id ───────────────────────────────────────────────────────────────────────────────
_lock = threading.Lock()
_running: set[str] = set()
_memory: dict[str, tuple[int, int, str]] = {}  # without the own register: key → (at, status, body)


def client_id(data: dict[str, Any]) -> str | None:
    """The body's ``client_id``, taken OUT of DATA (it goes into no event and no record):
    None when there is none, 422 ``invalid`` when it is not 8–64 of A–Z a–z 0–9 and -."""
    if "client_id" not in data:
        return None
    cid = data.pop("client_id")
    if cid is None:
        return None
    if not isinstance(cid, str) or not CLIENT_ID.match(cid):
        raise ApiError(422, "invalid", "client_id: 8 to 64 of A-Z, a-z, 0-9 and -.")
    return cid


def _key(who: str, route: str, cid: str) -> str:
    return hashlib.sha256(f"{who}\n{route}\n{cid}".encode()).hexdigest()


def _get(svc: Any, key: str, now: int) -> tuple[int, str] | None:
    reg = svc.odoo
    if hasattr(reg, "retry_get"):
        return reg.retry_get(key, now - KEEP_SECONDS)
    with _lock:
        hit = _memory.get(key)
    return (hit[1], hit[2]) if hit and hit[0] > now - KEEP_SECONDS else None


def _put(svc: Any, key: str, now: int, status: int, body: str) -> None:
    reg = svc.odoo
    if hasattr(reg, "retry_put"):
        reg.retry_put(key, now, status, body, now - KEEP_SECONDS)
        return
    with _lock:
        for k in [k for k, v in _memory.items() if v[0] <= now - KEEP_SECONDS]:
            del _memory[k]
        _memory[key] = (now, status, body)


def forget() -> None:
    """Tests: what is kept in memory."""
    with _lock:
        _memory.clear()
        _running.clear()


async def once(request: Request, who: str, cid: str | None, make: Any) -> Response:
    """The answer of ``await make()`` (a JSONResponse); with a client id CID, at most once per
    (WHO, the call's method and path, CID): see the module's text. WHO is the account the call
    acts as (its key), proven before this is called."""
    if cid is None:
        return await make()
    svc = request.app.state.services
    key = _key(who, f"{request.method} {request.url.path}", cid)
    with _lock:
        if key in _running:
            raise ApiError(409, "in_progress", "The first try is still running: ask again.")
        _running.add(key)
    try:
        hit = await run_in_threadpool(_get, svc, key, int(time.time()))
        if hit is not None:
            return Response(
                hit[1],
                status_code=hit[0],
                media_type="application/json",
                headers={"Cache-Control": "no-store", REPLAYED: "1"},
            )
        response = await make()
        if 200 <= response.status_code < 300 and isinstance(response, JSONResponse):
            body = bytes(response.body).decode()
            try:
                at, status = int(time.time()), response.status_code
                await run_in_threadpool(_put, svc, key, at, status, body)
            except Exception as exc:  # noqa: BLE001 — done all the same; a retry runs again
                log.warning("retries: an answer was not kept: %s", type(exc).__name__)
        return response
    finally:
        with _lock:
            _running.discard(key)


def json_body(raw: bytes) -> dict[str, Any]:
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_json", "Send a JSON object.")
    return data


__all__ = [
    "RelayDown",
    "REPLAYED",
    "is_relay",
    "check",
    "soft",
    "strictly",
    "run",
    "client_id",
    "once",
]
