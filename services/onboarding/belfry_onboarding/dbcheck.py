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
import subprocess
import sys
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
FIRST_CHECK_SECONDS = 120  # after the start's own reads and writes settle
CONFIRM_SECONDS = 20  # a second look before anything is said
CACHES = {"devices.sqlite"}  # rebuilt from the relay when damaged; nothing else is ever touched

_lock = threading.Lock()
_damaged: dict[str, dict[str, Any]] = {}  # path → {"why", "since", "told"}


def folder(svc: Any) -> Path | None:
    path = getattr(svc.settings, "register_path", "") or ""
    return Path(path).parent if path else None


TRANSIENT = (
    "disk i/o error",
    "database is locked",
    "database table is locked",
    "unable to open",
    "busy",
    "short read",
    "error code=522",
    "error code=10",
)  # what a reader sees while the service writes or checkpoints the same file: not damage


def _header_damage(head: bytes) -> str | None:
    """Damage in the header's fixed bytes (what the stray TLS write of 2026-11-24/25 hit)."""
    if not head.startswith(MAGIC) or len(head) < 100:
        return f"no SQLite header (bytes 0-23: {head[:24].hex(' ')})"
    page = int.from_bytes(head[16:18], "big")
    if not (page == 1 or (512 <= page <= 32768 and page & (page - 1) == 0)):
        return f"bad page size in the header (bytes 16-23: {head[16:24].hex(' ')})"
    if head[18] not in (1, 2) or head[19] not in (1, 2) or head[21:24] != b"\x40\x20\x20":
        return f"header overwritten (bytes 16-31: {head[16:32].hex(' ')})"
    return None


HEADER_READER = "import sys; sys.stdout.buffer.write(open(sys.argv[1], 'rb').read(100))"


def read_header(path: Path) -> bytes:
    """The first 100 bytes of PATH, read by a child process. Never with open()/close() here: POSIX
    locks belong to the process, and closing any file descriptor of a database drops every lock
    this process holds on it (sqlite.org/howtocorrupt.html §2.2). The service's own connections
    then look gone to other processes, and the next one that opens the register and closes it
    (``belfry apply``'s ``exec … has-people``, a read by hand) checkpoints and deletes the WAL
    file the service is still writing to: those writes are lost at the next restart. That
    happened on every box from 2026-11-25 to 2026-11-27."""
    out = subprocess.run(
        [sys.executable, "-I", "-c", HEADER_READER, str(path)],
        capture_output=True,
        timeout=20,
        check=False,
    )
    if out.returncode != 0:
        raise OSError(f"header not read (exit {out.returncode})")
    return out.stdout


def inspect(path: Path) -> str | None:
    """None when PATH is a sound database (or can't be judged just now); else why not (fixed
    words + the header's bytes, never content).

    Only two things count as damage: the header's fixed bytes are wrong, or ``quick_check`` names
    a corruption. A read error while the service is writing (WAL checkpoint, short read, locked)
    is not damage: on 2026-11-25 a read-only check read register.sqlite mid-checkpoint on all
    four boxes and mailed a false alarm. The check now opens the file normally (it joins the WAL
    like any reader; ``query_only`` keeps it from writing) and lets transient errors pass."""
    try:
        head = read_header(path)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("dbcheck: %s not readable just now (%s)", path.name, type(exc).__name__)
        return None
    if not head:
        return None  # an empty file is a database SQLite has not written yet
    bad = _header_damage(head)
    if bad:
        return bad
    try:
        db = sqlite3.connect(str(path), timeout=10)
        try:
            db.execute("PRAGMA query_only = 1")
            rows = [r[0] for r in db.execute("PRAGMA quick_check").fetchall()]
        finally:
            db.close()
    except sqlite3.DatabaseError as exc:
        text = str(exc).lower()
        if any(t in text for t in TRANSIENT):
            log.warning("dbcheck: %s could not be checked just now (%s)", path.name, exc)
            return None
        return f"{exc} (header bytes 16-31: {head[16:32].hex(' ')})"
    if rows == ["ok"]:
        return None
    joined = " / ".join(str(r) for r in rows)
    if any(t in joined.lower() for t in TRANSIENT):
        log.warning("dbcheck: %s could not be checked just now (%s)", path.name, joined[:200])
        return None
    return f"quick_check: {joined[:200]}"


def confirmed(path: Path, wait: float = CONFIRM_SECONDS) -> str | None:
    """Damage seen twice, WAIT seconds apart; None as soon as one look finds it sound."""
    why = inspect(path)
    if why is None:
        return None
    time.sleep(wait)
    return inspect(path)


def check(svc: Any, now: float | None = None) -> dict[str, str]:
    """Check every *.sqlite of the register's folder. Returns {file name: why} for the damaged."""
    where = folder(svc)
    if where is None or not where.is_dir():
        return {}
    now = time.time() if now is None else now
    found: dict[str, str] = {}
    for path in sorted(where.glob("*.sqlite")):
        why = confirmed(path)
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
            "dbcheck: %s is DAMAGED (%s), seen twice. It holds real data: nothing is changed; "
            "restore it from the backup",
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
                f"The hourly check found the belfry's file {name} damaged, twice in a row. "
                "The service has not changed it.",
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
