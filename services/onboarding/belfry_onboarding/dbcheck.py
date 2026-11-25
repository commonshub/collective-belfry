"""The SQLite files next to the register: opened early, checked at start and every hour.

On 2026-11-24/25 devices.sqlite, sales.sqlite and events.sqlite stopped opening as databases on
every box ("file is not a database"): a TLS alert record written over bytes 19-42 of the header
by a WebSocket closed from another thread (wsync.py has the cause and the fix). This module is the
defence that holds whatever the cause:

* :func:`open_early` opens the stores at start, before the background threads. A file opened
  then keeps a low fd number for the life of the process; a stray write aims at numbers that were
  just freed, which is what a store opened lazily later picks up (register.sqlite, opened first,
  was never hit).
* :func:`check` reads every ``*.sqlite`` in the register's folder read-only (the header's magic,
  then ``PRAGMA quick_check``). A damaged file is logged as an ERROR, shown on the console's status
  page and mailed once to the belfry's admins. devices.sqlite is a cache of the relay and is
  rebuilt (devices.reset_store, the old file kept aside); every other file holds real data
  (sales, events, the register) and is never moved or changed: a person repairs it from the
  backup (the damage seen so far was the first 100 bytes only: the header).

WAL would not have prevented it: the header lives in the main file in WAL mode too, and the stray
write went to whatever file had the fd number.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

log = logging.getLogger(__name__)

MAGIC = b"SQLite format 3\x00"
CHECK_SECONDS = 3600
FIRST_CHECK_SECONDS = 30  # after the start's own reads
CACHES = {"devices.sqlite"}  # rebuilt from the relay when damaged; nothing else is ever touched

_lock = threading.Lock()
_damaged: dict[str, dict[str, Any]] = {}  # path → {"why", "since", "told"}


def folder(svc: Any) -> Path | None:
    path = getattr(svc.settings, "register_path", "") or ""
    return Path(path).parent if path else None


def inspect(path: Path) -> str | None:
    """None when PATH opens as a sound database; else why not (fixed words + the header's bytes,
    never content)."""
    try:
        with path.open("rb") as fh:
            head = fh.read(100)
    except OSError as exc:
        return f"not readable ({type(exc).__name__})"
    if not head:
        return None  # an empty file is a database SQLite has not written yet
    if not head.startswith(MAGIC) or len(head) < 100:
        return f"no SQLite header (bytes 0-23: {head[:24].hex(' ')})"
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            row = db.execute("PRAGMA quick_check").fetchone()
        finally:
            db.close()
    except sqlite3.DatabaseError as exc:
        return f"{exc} (header bytes 16-43: {head[16:44].hex(' ')})"
    if row is None or row[0] != "ok":
        return f"quick_check: {str(row[0] if row else '')[:200]}"
    return None


def check(svc: Any, now: float | None = None) -> dict[str, str]:
    """Check every *.sqlite of the register's folder. Returns {file name: why} for the damaged."""
    where = folder(svc)
    if where is None or not where.is_dir():
        return {}
    now = time.time() if now is None else now
    found: dict[str, str] = {}
    for path in sorted(where.glob("*.sqlite")):
        why = inspect(path)
        key = str(path)
        if why is None:
            with _lock:
                if _damaged.pop(key, None) is not None:
                    log.warning("dbcheck: %s opens as a database again", path.name)
            continue
        found[path.name] = why
        if path.name in CACHES:
            log.error("dbcheck: %s is damaged (%s); it is a cache: rebuilding it", path.name, why)
            try:
                _rebuild(svc, path.name)
            except Exception as exc:  # noqa: BLE001
                log.error("dbcheck: rebuilding %s failed: %r", path.name, exc)
            if inspect(path) is None:
                continue
        log.error(
            "dbcheck: %s is DAMAGED (%s). It holds real data: nothing is changed; "
            "restore it from the backup (only the header has been hit so far)",
            path.name,
            why,
        )
        with _lock:
            rec = _damaged.setdefault(key, {"since": now, "told": False})
            rec["why"] = why
            tell_now = not rec["told"]
            rec["told"] = True
        if tell_now:
            _tell(svc, path.name)
    return found


def _rebuild(svc: Any, name: str) -> None:
    if name == "devices.sqlite":
        from . import devices

        devices.reset_store(svc)


def open_early(svc: Any) -> None:
    """Open the stores now, before the background threads start (see the module's text)."""
    from . import devices, events, sales

    for name, opener in (
        ("events", events.store),
        ("sales", sales.store),
        ("devices", devices.store),
    ):
        try:
            opener(svc)
        except Exception as exc:  # noqa: BLE001 — the check says what is wrong
            log.error("dbcheck: the %s store did not open at start: %r", name, exc)


def status_rows(svc: Any) -> list[dict[str, Any]]:
    """Rows for the console's status page: one per damaged file, none when all are sound."""
    with _lock:
        items = sorted(_damaged.items())
    return [
        {
            "name": f"Database {Path(path).name}",
            "up": False,
            "ms": 0,
            "detail": "damaged since "
            + time.strftime("%d %b %H:%M UTC", time.gmtime(rec["since"]))
            + ": restore it from the backup",
        }
        for path, rec in items
    ]


def _tell(svc: Any, name: str) -> None:
    """One e-mail to the admins per damaged file and process (fixed texts)."""
    from . import agentrequests, mailer

    try:
        people = agentrequests.admins(svc)
    except Exception as exc:  # noqa: BLE001
        log.warning("dbcheck: admins not readable: %s", exc)
        return
    if not people:
        return
    community = getattr(svc.settings, "community_name", "") or "your belfry"
    try:
        m = getattr(svc.odoo, "mailer", None) or mailer.Mailer(svc.settings)
        m.send(
            [a["email"] for a in people if a.get("email")],
            f"{community}: the database file {name} is damaged",
            [
                f"The belfry's file {name} no longer opens as a database. Nothing was changed "
                "in it; what it holds is not readable until it is repaired.",
                "To repair: on the server, compare it with last night's backup "
                "(infra/scripts/backup.sh) and restore the file, or its first 100 bytes when "
                "only the header was hit. The service logs (docker logs belfry-onboarding-1) "
                "have a line starting with 'dbcheck:' that says what is wrong.",
            ],
            footer="You get this because you are an admin of " + community + ".",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("dbcheck: the e-mail to the admins failed: %s", type(exc).__name__)


@asynccontextmanager
async def running(app: FastAPI) -> AsyncIterator[None]:
    import asyncio

    async def loop() -> None:
        await asyncio.sleep(FIRST_CHECK_SECONDS)
        while True:
            try:
                await run_in_threadpool(check, app.state.services)
            except Exception as exc:  # noqa: BLE001
                log.warning("dbcheck: check failed: %r", exc)
            await asyncio.sleep(CHECK_SECONDS)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()
