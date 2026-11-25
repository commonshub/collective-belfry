from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from belfry_onboarding import dbcheck

# the damage seen on the boxes: a TLS 1.3 alert record over bytes 19-42 of the header
ALERT = bytes.fromhex("1703030013") + bytes(range(1, 20))


def _db(path: Path) -> None:
    db = sqlite3.connect(path)
    db.execute("create table t(x)")
    db.execute("insert into t values (1)")
    db.commit()
    db.close()


def _hit(path: Path) -> bytes:
    data = bytearray(path.read_bytes())
    data[19 : 19 + len(ALERT)] = ALERT
    path.write_bytes(bytes(data))
    return bytes(data)


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setattr(dbcheck, "CONFIRM_SECONDS", 0)
    monkeypatch.setattr(dbcheck.confirmed, "__defaults__", (0,))
    dbcheck._damaged.clear()
    yield SimpleNamespace(
        settings=SimpleNamespace(
            register_path=str(tmp_path / "register.sqlite"), community_name="T"
        ),
        odoo=None,
    )
    dbcheck._damaged.clear()


def test_sound_files_say_nothing(svc, tmp_path):
    for name in ("register.sqlite", "events.sqlite", "sales.sqlite"):
        _db(tmp_path / name)
    (tmp_path / "empty.sqlite").write_bytes(b"")
    assert dbcheck.check(svc) == {}
    assert dbcheck.status_rows(svc) == []


def test_a_damaged_data_file_is_reported_never_changed_and_told_once(svc, tmp_path, monkeypatch):
    told: list[str] = []
    monkeypatch.setattr(dbcheck, "_tell", lambda s, name: told.append(name))
    _db(tmp_path / "events.sqlite")
    _db(tmp_path / "sales.sqlite")
    damaged = _hit(tmp_path / "sales.sqlite")

    found = dbcheck.check(svc)
    assert list(found) == ["sales.sqlite"]
    assert "header overwritten" in found["sales.sqlite"]
    assert (tmp_path / "sales.sqlite").read_bytes() == damaged  # nothing touched
    assert not list(tmp_path.glob("sales.sqlite.*"))
    rows = dbcheck.status_rows(svc)
    assert [r["name"] for r in rows] == ["Database sales.sqlite"] and rows[0]["up"] is False

    dbcheck.check(svc)
    assert told == ["sales.sqlite"]  # the admins hear once, not every hour

    # repaired by a person: the row goes away
    _db_bytes = bytearray(damaged)
    healthy = tmp_path / "healthy.sqlite"
    _db(healthy)
    _db_bytes[:100] = healthy.read_bytes()[:100]
    healthy.unlink()
    (tmp_path / "sales.sqlite").write_bytes(bytes(_db_bytes))
    assert dbcheck.check(svc) == {}
    assert dbcheck.status_rows(svc) == []


def test_the_devices_cache_is_rebuilt(svc, tmp_path, monkeypatch):
    told: list[str] = []
    monkeypatch.setattr(dbcheck, "_tell", lambda s, name: told.append(name))
    from belfry_onboarding import devices

    devices.store(svc)  # the store makes its tables
    devices._stores.clear()
    _hit(tmp_path / "devices.sqlite")

    assert "devices.sqlite" in dbcheck.check(svc)
    assert dbcheck.inspect(tmp_path / "devices.sqlite") is None  # a fresh one
    assert list(tmp_path.glob("devices.sqlite.unreadable-*"))  # the old one kept aside
    assert told == [] and dbcheck.status_rows(svc) == []
    devices._stores.clear()


def test_open_early_opens_the_stores(svc, tmp_path, monkeypatch):
    from belfry_onboarding import devices, events, sales

    for mod in (devices, events, sales):
        monkeypatch.setattr(mod, "_stores", {})
    dbcheck.open_early(svc)
    for name in ("events.sqlite", "sales.sqlite", "devices.sqlite"):
        assert (tmp_path / name).exists()


def test_a_wal_file_being_written_is_never_called_damaged(svc, tmp_path, monkeypatch):
    """2026-11-25: a read-only check read register.sqlite mid-checkpoint on four boxes and mailed
    a false alarm ("disk I/O error", "error code=522")."""
    told: list[str] = []
    monkeypatch.setattr(dbcheck, "_tell", lambda s, name: told.append(name))
    path = tmp_path / "register.sqlite"
    w = sqlite3.connect(path)
    w.execute("pragma journal_mode=wal")
    w.execute("create table t(x)")
    for i in range(2000):
        w.execute("insert into t values (?)", (i,))
    w.commit()
    assert dbcheck.check(svc) == {}  # writer still open, WAL not checkpointed
    w.close()
    assert told == []


@pytest.mark.parametrize(
    "err", ["disk I/O error", "database is locked", "unable to get the page. error code=522"]
)
def test_transient_read_errors_are_not_damage(svc, tmp_path, monkeypatch, err):
    _db(tmp_path / "register.sqlite")
    real = sqlite3.connect

    class Conn:
        def __init__(self, *a, **k):
            self.c = real(*a, **k)

        def execute(self, q):
            if "quick_check" in q:
                raise sqlite3.OperationalError(err)
            return self.c.execute(q)

        def close(self):
            self.c.close()

    monkeypatch.setattr(dbcheck.sqlite3, "connect", Conn)
    assert dbcheck.check(svc) == {}


def test_damage_must_be_seen_twice(svc, tmp_path, monkeypatch):
    _db(tmp_path / "events.sqlite")
    looks = iter(["header overwritten (x)", None])
    monkeypatch.setattr(dbcheck, "inspect", lambda p: next(looks))
    assert dbcheck.confirmed(tmp_path / "events.sqlite", 0) is None
