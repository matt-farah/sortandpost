from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sortandpost.sync.lock import (
    LockInfo,
    clear_lock,
    held_by_other,
    is_stale,
    new_lock,
    read_lock,
    write_lock,
)

NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)


def _lock(device: str, *, heartbeat: datetime) -> LockInfo:
    base = new_lock(device_id=device, hostname=f"{device}-host", db_generation=3, now=NOW)
    return dataclasses.replace(base, heartbeat_at=heartbeat.astimezone(UTC).isoformat())


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    path = tmp_path / ".sortandpost.lock"
    lock = new_lock(device_id="dev-a", hostname="mac-a", db_generation=5, now=NOW)
    write_lock(path, lock)
    got = read_lock(path)
    assert got == lock
    assert not list(tmp_path.glob("*.tmp*"))  # atomic write left no temp behind


def test_read_missing_or_corrupt_returns_none(tmp_path: Path) -> None:
    assert read_lock(tmp_path / "nope.lock") is None
    bad = tmp_path / "bad.lock"
    bad.write_text("{ not json", encoding="utf-8")
    assert read_lock(bad) is None


def test_is_stale_by_heartbeat_age() -> None:
    fresh = _lock("dev-b", heartbeat=NOW - timedelta(seconds=30))
    old = _lock("dev-b", heartbeat=NOW - timedelta(seconds=600))
    assert not is_stale(fresh, now=NOW, stale_after_sec=300)
    assert is_stale(old, now=NOW, stale_after_sec=300)


def test_held_by_other_only_for_a_live_foreign_device() -> None:
    mine = _lock("me", heartbeat=NOW)
    theirs_live = _lock("them", heartbeat=NOW - timedelta(seconds=10))
    theirs_dead = _lock("them", heartbeat=NOW - timedelta(seconds=9999))

    assert not held_by_other(None, device_id="me", now=NOW, stale_after_sec=300)
    assert not held_by_other(mine, device_id="me", now=NOW, stale_after_sec=300)
    assert held_by_other(theirs_live, device_id="me", now=NOW, stale_after_sec=300)
    assert not held_by_other(theirs_dead, device_id="me", now=NOW, stale_after_sec=300)


def test_clear_lock_only_removes_our_own(tmp_path: Path) -> None:
    path = tmp_path / ".sortandpost.lock"
    write_lock(path, new_lock(device_id="them", hostname="h", db_generation=1, now=NOW))
    assert clear_lock(path, device_id="me") is False
    assert path.exists()
    assert clear_lock(path, device_id="them") is True
    assert not path.exists()
