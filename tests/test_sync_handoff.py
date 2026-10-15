"""Editing checkout: request -> grace -> grant -> take (V3 P6d).

Two kinds of setup:

* a **shared** ``sync/`` (directory link), the instant-sync case (Synology on
  the same NAS, or two processes on one machine);
* **bridged** ``sync/`` dirs, where the test plays the sync tool and copies
  files across one at a time -- so the lock can arrive before the database,
  as Google Drive / OneDrive may deliver them.
"""

from __future__ import annotations

import dataclasses
import shutil
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session

from sortandpost.config import Settings
from sortandpost.db import make_engine
from sortandpost.db.models import ChangeOp
from sortandpost.sync.changelog import ReadOnlyError, record_change, set_write_guard
from sortandpost.sync.handoff import read_requests, request_file
from sortandpost.sync.lock import (
    LockInfo,
    LockStatus,
    handoff_lock,
    lock_status,
    new_lock,
    read_lock,
    write_lock,
)
from sortandpost.sync.manager import SyncManager, WriteMode
from sortandpost.sync.merge import find_conflict_copies
from sortandpost.sync.publish import read_generation
from tests.factories import hash_for, make_db, rate, read_ratings, ts

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _settings(monkeypatch: pytest.MonkeyPatch, app_data: Path) -> Settings:
    monkeypatch.setenv("APP_DATA", str(app_data))
    s = Settings.resolve()
    make_db(s.paths.working_db, photos=["a", "b"])
    return s


def _shared(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    dir_link: Callable[[Path, Path], None],
    names: tuple[str, ...] = ("a", "b"),
) -> list[Settings]:
    first = _settings(monkeypatch, tmp_path / names[0])
    out = [first]
    for name in names[1:]:
        data = tmp_path / name
        data.mkdir()
        dir_link(data / "sync", first.paths.sync)
        out.append(_settings(monkeypatch, data))
    return out


def _deliver(src: Settings, dst: Settings, *what: str) -> None:
    """Play the sync tool: copy named items from *src*'s ``sync/`` to *dst*'s."""
    for item in what:
        if item == "lock":
            shutil.copy2(src.paths.lock_file, dst.paths.lock_file)
        elif item == "db":
            shutil.copy2(src.paths.published_db, dst.paths.published_db)
        elif item == "requests":
            dst.paths.locks_dir.mkdir(parents=True, exist_ok=True)
            for p in src.paths.locks_dir.glob("request-*.json"):
                shutil.copy2(p, dst.paths.locks_dir / p.name)
            for p in dst.paths.locks_dir.glob("request-*.json"):
                if not (src.paths.locks_dir / p.name).exists():
                    p.unlink()
        else:
            raise AssertionError(item)


# --------------------------------------------------------------------------- #
# lock_status
# --------------------------------------------------------------------------- #
def _lock(device: str, *, age: float = 0, generation: int = 3) -> LockInfo:
    base = new_lock(device_id=device, hostname=f"{device}-host", db_generation=generation, now=NOW)
    return dataclasses.replace(base, heartbeat_at=(NOW - timedelta(seconds=age)).isoformat())


def _status(info: LockInfo | None, *, me: str = "me", published: int = 0) -> LockStatus:
    return lock_status(
        info, device_id=me, now=NOW, stale_after_sec=300, published_generation=published
    )


def test_lock_status_plain_locks() -> None:
    assert _status(None) is LockStatus.free
    assert _status(_lock("me")) is LockStatus.free
    assert _status(_lock("them")) is LockStatus.held
    assert _status(_lock("them", age=301)) is LockStatus.free


def test_lock_status_handoffs_wait_for_the_generation() -> None:
    grant = handoff_lock(_lock("them"), to="me", hostname="mine", generation=7, now=NOW)
    assert _status(grant, published=6) is LockStatus.waiting
    assert _status(grant, published=7) is LockStatus.free
    # granted to a third machine: held until it goes stale
    assert _status(grant, me="other", published=99) is LockStatus.held
    released = handoff_lock(_lock("them"), to="", hostname="", generation=7, now=NOW)
    assert _status(released, me="other", published=6) is LockStatus.waiting
    assert _status(released, me="other", published=7) is LockStatus.free
    # the database never arrived: the wait ends when the handoff goes stale
    old = dataclasses.replace(grant, heartbeat_at=(NOW - timedelta(seconds=301)).isoformat())
    assert _status(old, published=0) is LockStatus.free


def test_old_lock_files_without_handoff_fields_still_parse(tmp_path: Path) -> None:
    path = tmp_path / ".sortandpost.lock"
    path.write_text(
        '{"device_id": "x", "hostname": "h", "pid": 1, "acquired_at": "2026-10-14T12:00:00+00:00",'
        ' "heartbeat_at": "2026-10-14T12:00:00+00:00", "db_generation": 4}',
        encoding="utf-8",
    )
    info = read_lock(path)
    assert info is not None and not info.is_handoff and info.db_generation == 4


# --------------------------------------------------------------------------- #
# the whole handover on a shared sync/
# --------------------------------------------------------------------------- #
def test_request_grace_grant_take(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dir_link: Callable[[Path, Path], None]
) -> None:
    sa, sb = _shared(monkeypatch, tmp_path, dir_link)
    clock = FakeClock()
    a = SyncManager(sa, clock=clock)
    b = SyncManager(sb, clock=clock)
    assert a.start(background=False) is WriteMode.writable
    assert b.start(background=False) is WriteMode.read_only

    rate(sa.paths.working_db, "a", 5, device=sa.device_id, when=ts(10))  # unpublished edit
    b.request_edit()
    assert request_file(sb.paths.locks_dir, sb.device_id).exists()
    assert b.refresh() is WriteMode.read_only  # still A's

    got = a.incoming_request()
    assert got is not None and got[0].device_id == sb.device_id
    assert got[1] == sa.config.sync.handoff_grace_sec
    prepared: list[bool] = []
    assert a.maybe_hand_over(lambda: prepared.append(a.writes_open)) is False
    assert prepared == []

    clock.advance(sa.config.sync.handoff_grace_sec)
    assert a.maybe_hand_over(lambda: prepared.append(a.writes_open)) is True
    assert prepared == [False]  # edits were closed before the indexer stopped
    assert a.mode is WriteMode.read_only and a.handed_to
    grant = read_lock(sa.paths.lock_file)
    assert grant is not None and grant.handoff_to == sb.device_id
    assert grant.handoff_generation == read_generation(sa.paths.published_db)

    assert b.refresh() is WriteMode.writable
    assert not request_file(sb.paths.locks_dir, sb.device_id).exists()
    assert read_ratings(sb.paths.working_db) == {hash_for("a"): 5}  # A's edit came along
    assert a.refresh() is WriteMode.read_only  # A doesn't snatch it back

    # B quits: released, and A (nobody else asking) reclaims it
    rate(sb.paths.working_db, "b", 2, device=sb.device_id, when=ts(20))
    b.stop()
    assert a.refresh() is WriteMode.writable
    assert read_ratings(sa.paths.working_db) == {hash_for("a"): 5, hash_for("b"): 2}
    a.stop()


def test_keep_editing_refuses_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dir_link: Callable[[Path, Path], None]
) -> None:
    sa, sb = _shared(monkeypatch, tmp_path, dir_link)
    clock = FakeClock()
    a = SyncManager(sa, clock=clock)
    b = SyncManager(sb, clock=clock)
    a.start(background=False)
    b.start(background=False)

    b.request_edit()
    got = a.incoming_request()
    assert got is not None
    a.deny(got[0])
    clock.advance(600)
    a.heartbeat()  # the denial survives heartbeats
    assert a.incoming_request() is None
    assert a.maybe_hand_over() is False

    assert b.refresh() is WriteMode.read_only
    assert b.denied_by and b.my_request is None
    assert not request_file(sb.paths.locks_dir, sb.device_id).exists()

    # a fresh request is a new question
    b.request_edit()
    assert a.incoming_request() is not None
    a.stop()
    b.stop()


def test_a_request_from_a_machine_that_went_away_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dir_link: Callable[[Path, Path], None]
) -> None:
    sa, sb = _shared(monkeypatch, tmp_path, dir_link)
    clock = FakeClock()
    a = SyncManager(sa, clock=clock)
    b = SyncManager(sb, clock=clock)
    a.start(background=False)
    b.start(background=False)
    b.request_edit()  # ...and B is never heard from again
    clock.advance(sa.config.sync.lock_stale_after_sec + 1)
    assert a.incoming_request() is None
    assert a.maybe_hand_over() is False
    a.stop()


def test_requester_that_never_takes_the_grant_loses_it_when_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dir_link: Callable[[Path, Path], None]
) -> None:
    sa, sb = _shared(monkeypatch, tmp_path, dir_link)
    clock = FakeClock()
    a = SyncManager(sa, clock=clock)
    b = SyncManager(sb, clock=clock)
    a.start(background=False)
    b.start(background=False)
    b.request_edit()
    a.incoming_request()
    clock.advance(sa.config.sync.handoff_grace_sec)
    assert a.maybe_hand_over() is True
    assert a.refresh() is WriteMode.read_only  # granted to B

    # B crashed before taking it; its request and the grant both go stale
    clock.advance(sa.config.sync.lock_stale_after_sec + 1)
    assert a.refresh() is WriteMode.writable
    a.stop()


def test_a_free_lock_is_left_for_the_machine_asking_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dir_link: Callable[[Path, Path], None]
) -> None:
    sa, sb, sc = _shared(monkeypatch, tmp_path, dir_link, ("a", "b", "c"))
    clock = FakeClock()
    a = SyncManager(sa, clock=clock)  # the laptop editing now
    server = SyncManager(sb, clock=clock)  # the always-on server, not asking
    c = SyncManager(sc, clock=clock)  # a second laptop, asking
    a.start(background=False)
    server.start(background=False)
    c.start(background=False)
    c.request_edit()

    a.stop()  # quits before its grace period ran out
    assert server.refresh() is WriteMode.read_only  # defers to C's request
    assert c.refresh() is WriteMode.writable
    c.stop()
    assert server.refresh() is WriteMode.writable  # nobody asking any more
    server.stop()


# --------------------------------------------------------------------------- #
# a slow sync tool: the grant arrives before the database
# --------------------------------------------------------------------------- #
def test_taker_waits_until_the_database_has_synced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sa = _settings(monkeypatch, tmp_path / "a")
    sb = _settings(monkeypatch, tmp_path / "b")
    clock = FakeClock()
    a = SyncManager(sa, clock=clock)
    assert a.start(background=False) is WriteMode.writable
    _deliver(sa, sb, "lock", "db")
    b = SyncManager(sb, clock=clock)
    assert b.start(background=False) is WriteMode.read_only

    rate(sa.paths.working_db, "a", 4, device=sa.device_id, when=ts(10))
    b.request_edit()
    _deliver(sb, sa, "requests")
    a.incoming_request()
    clock.advance(sa.config.sync.handoff_grace_sec)
    assert a.maybe_hand_over() is True

    _deliver(sa, sb, "lock")  # the grant got here first
    assert b.refresh() is WriteMode.read_only
    assert b.waiting_for is not None
    have, want = b.waiting_for
    assert have < want == read_generation(sa.paths.published_db)

    _deliver(sa, sb, "db")
    assert b.refresh() is WriteMode.writable
    assert b.waiting_for is None
    assert read_ratings(sb.paths.working_db) == {hash_for("a"): 4}
    b.stop()


# --------------------------------------------------------------------------- #
# both took a free lock before seeing each other's
# --------------------------------------------------------------------------- #
def test_losing_a_lock_race_steps_down_and_keeps_the_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dir_link: Callable[[Path, Path], None]
) -> None:
    sa, sb = _shared(monkeypatch, tmp_path, dir_link)
    clock = FakeClock()
    a = SyncManager(sa, clock=clock)
    a.start(background=False)
    rate(sa.paths.working_db, "a", 3, device=sa.device_id, when=ts(10))

    # the sync tool delivers B's lock over A's
    write_lock(
        sa.paths.lock_file,
        new_lock(device_id=sb.device_id, hostname="laptop-b", db_generation=1, now=clock.now),
    )
    a.heartbeat()
    assert a.mode is WriteMode.read_only and a.lost_lock_to == "laptop-b"
    assert not a.writes_open
    copies = find_conflict_copies(sa.paths.sync)
    assert len(copies) == 1
    assert read_ratings(copies[0]) == {hash_for("a"): 3}
    assert read_lock(sa.paths.lock_file).device_id == sb.device_id  # A didn't overwrite it

    # B's next promotion folds A's edit in
    b = SyncManager(sb, clock=clock)
    clock.advance(sa.config.sync.lock_stale_after_sec + 1)
    assert b.start(background=False) is WriteMode.writable
    assert read_ratings(sb.paths.working_db) == {hash_for("a"): 3}
    b.stop()


# --------------------------------------------------------------------------- #
# write guard
# --------------------------------------------------------------------------- #
def test_record_change_refuses_while_not_the_writer(tmp_path: Path) -> None:
    db = tmp_path / "w.db"
    make_db(db, photos=["a"])
    engine = make_engine(db)
    try:
        set_write_guard(lambda: False)
        with Session(engine) as s, pytest.raises(ReadOnlyError):
            record_change(s, device_id="d", op=ChangeOp.rate, pixel_hash=hash_for("a"), ts=ts(1))
        set_write_guard(lambda: True)
        with Session(engine) as s:
            record_change(s, device_id="d", op=ChangeOp.rate, pixel_hash=hash_for("a"), ts=ts(1))
    finally:
        set_write_guard(None)
        engine.dispose()


def test_requests_are_one_file_per_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dir_link: Callable[[Path, Path], None]
) -> None:
    sa, sb, sc = _shared(monkeypatch, tmp_path, dir_link, ("a", "b", "c"))
    clock = FakeClock()
    a = SyncManager(sa, clock=clock)
    a.start(background=False)
    b = SyncManager(sb, clock=clock)
    c = SyncManager(sc, clock=clock)
    b.start(background=False)
    c.start(background=False)
    b.request_edit()
    clock.advance(5)
    c.request_edit()
    reqs = read_requests(sa.paths.locks_dir)
    assert [r.device_id for r in reqs] == [sb.device_id, sc.device_id]
    got = a.incoming_request()
    assert got is not None and got[0].device_id == sb.device_id  # oldest first
    c.cancel_request()
    assert len(read_requests(sa.paths.locks_dir)) == 1
    a.stop()
