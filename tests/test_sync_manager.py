from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, select

from sortandpost.config import Settings
from sortandpost.db import make_engine
from sortandpost.db.models import Change, ChangeOp, Photo, Rating, RatingSource
from sortandpost.sync.changelog import record_change
from sortandpost.sync.lock import read_lock
from sortandpost.sync.manager import SyncManager, WriteMode
from sortandpost.sync.publish import read_generation
from tests.factories import hash_for, make_db, rate, read_ratings, ts


class FakeClock:
    def __init__(self, start: datetime) -> None:
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


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC))


def test_single_writer_then_read_only_second_machine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    dir_link: Callable[[Path, Path], None],
) -> None:
    a_data = tmp_path / "a"
    settings_a = _settings(monkeypatch, a_data)

    # machine B shares machine A's sync/ folder
    b_data = tmp_path / "b"
    b_data.mkdir()
    dir_link(b_data / "sync", settings_a.paths.sync)
    settings_b = _settings(monkeypatch, b_data)

    mgr_a = SyncManager(settings_a, clock=clock)
    assert mgr_a.start(background=False) is WriteMode.writable
    assert read_lock(settings_a.paths.lock_file).device_id == settings_a.device_id
    assert settings_a.paths.published_db.exists()

    mgr_b = SyncManager(settings_b, clock=clock)
    assert mgr_b.start(background=False) is WriteMode.read_only
    # B still catches up to A's published generation
    assert read_generation(settings_b.paths.working_db) == read_generation(
        settings_b.paths.published_db
    )

    mgr_a.stop()
    mgr_b.stop()


def test_pull_disposes_stale_engine_connections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    dir_link: Callable[[Path, Path], None],
) -> None:
    """A read-only machine that pulls a newer published DB on start must not keep
    serving the pre-pull file: ``os.replace`` unlinks the inode the engine's
    pooled SQLite connections still hold, and SQLite then refuses writes to it
    ("attempt to write a readonly database"). Regression for the crash when such
    an instance later promotes to writable and publishes.
    """
    settings_a = _settings(monkeypatch, tmp_path / "a")  # working DB has photos a, b

    b_data = tmp_path / "b"
    b_data.mkdir()
    dir_link(b_data / "sync", settings_a.paths.sync)
    monkeypatch.setenv("APP_DATA", str(b_data))
    settings_b = Settings.resolve()
    make_db(settings_b.paths.working_db, photos=[], generation=1)  # emptier + older

    mgr_a = SyncManager(settings_a, clock=clock)
    assert mgr_a.start(background=False) is WriteMode.writable  # publishes gen >= 2

    # Pin a pooled connection to machine B's pre-pull (empty) working DB.
    b_engine = make_engine(settings_b.paths.working_db)
    with Session(b_engine) as s:
        assert s.exec(select(Photo)).all() == []

    mgr_b = SyncManager(settings_b, engine=b_engine, clock=clock)
    assert mgr_b.start(background=False) is WriteMode.read_only

    # The shared engine must now see A's rows, not the stale empty inode.
    with Session(b_engine) as s:
        assert {p.pixel_hash for p in s.exec(select(Photo)).all()} == {hash_for("a"), hash_for("b")}

    # Promotion + publish must not raise OperationalError(readonly database).
    mgr_a.stop()  # releases the lock
    assert mgr_b.refresh() is WriteMode.writable
    assert mgr_b.publish(force=True) is True

    mgr_b.stop()


def test_read_only_machine_takes_over_a_stale_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    dir_link: Callable[[Path, Path], None],
) -> None:
    settings_a = _settings(monkeypatch, tmp_path / "a")
    b_data = tmp_path / "b"
    b_data.mkdir()
    dir_link(b_data / "sync", settings_a.paths.sync)
    settings_b = _settings(monkeypatch, b_data)

    mgr_a = SyncManager(settings_a, clock=clock)
    mgr_a.start(background=False)

    b_clock = FakeClock(clock.now)
    mgr_b = SyncManager(settings_b, clock=b_clock)
    assert mgr_b.start(background=False) is WriteMode.read_only

    # A goes silent; B's clock runs past lock_stale_after_sec
    b_clock.advance(settings_b.config.sync.lock_stale_after_sec + 60)
    assert mgr_b.refresh() is WriteMode.writable
    assert read_lock(settings_b.paths.lock_file).device_id == settings_b.device_id

    mgr_b.stop()


def test_publish_is_skipped_when_nothing_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    settings = _settings(monkeypatch, tmp_path / "a")
    mgr = SyncManager(settings, clock=clock)
    mgr.start(background=False)  # performs the first publish

    clock.advance(settings.config.sync.publish_interval_sec + 1)
    assert mgr.publish() is False  # DB untouched since the startup publish

    gen_before = read_generation(settings.paths.working_db)
    assert mgr.publish(force=True) is True
    assert read_generation(settings.paths.working_db) == gen_before + 1

    mgr.stop()


def test_snapshot_cadence_and_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    settings = _settings(monkeypatch, tmp_path / "a")
    settings.config.sync.snapshot_every_publishes = 2
    settings.config.sync.snapshot_keep = 1

    mgr = SyncManager(settings, clock=clock)
    mgr.start(background=False)

    for _ in range(5):
        clock.advance(1)
        mgr.publish(force=True)

    snaps = list((settings.paths.snapshots_dir).glob("sortandpost-*.db*"))
    assert len(snaps) == settings.config.sync.snapshot_keep

    mgr.stop()


def test_daily_snapshot_even_with_few_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    settings = _settings(monkeypatch, tmp_path / "a")
    settings.config.sync.snapshot_every_publishes = 1000
    settings.config.sync.snapshot_every_hours = 24
    snaps_dir = settings.paths.snapshots_dir

    mgr = SyncManager(settings, clock=clock)
    mgr.start(background=False)  # the startup publish: no backup yet -> one now
    first = sorted(snaps_dir.glob("sortandpost-*.db*"))
    assert len(first) == 1

    clock.advance(3600)
    mgr.publish(force=True)
    assert len(list(snaps_dir.glob("sortandpost-*.db*"))) == 1  # not due yet

    clock.advance(24 * 3600)
    mgr.publish(force=True)
    assert len(list(snaps_dir.glob("sortandpost-*.db*"))) == 2
    last = mgr.last_snapshot()
    assert last is not None and last[1] == clock.now.replace(microsecond=0)

    settings.config.sync.snapshot_every_hours = 0  # off: back to publish counting
    clock.advance(48 * 3600)
    mgr.publish(force=True)
    assert len(list(snaps_dir.glob("sortandpost-*.db*"))) == 2
    mgr.stop()


def test_back_up_now_writes_a_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    settings = _settings(monkeypatch, tmp_path / "a")
    mgr = SyncManager(settings, clock=clock)
    mgr.start(background=False)
    clock.advance(5)
    snap = mgr.snapshot()
    assert snap.exists() and mgr.last_snapshot() == (snap, clock.now.replace(microsecond=0))
    mgr.stop()


def test_stop_releases_the_lock_and_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    settings = _settings(monkeypatch, tmp_path / "a")
    mgr = SyncManager(settings, clock=clock)
    mgr.start(background=False)
    assert settings.paths.lock_file.exists()

    mgr.stop()
    assert not settings.paths.lock_file.exists()
    assert settings.paths.published_db.exists()


def test_conflict_copy_is_merged_and_archived_on_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    settings = _settings(monkeypatch, tmp_path / "a")

    # seed the working DB, publish it, then fabricate a conflict copy that
    # carries an extra rating + its changes row.
    boot = SyncManager(settings, clock=clock)
    boot.start(background=False)
    boot.stop()

    conflict = settings.paths.sync / "sortandpost (1).db"
    conflict.write_bytes(settings.paths.published_db.read_bytes())
    ce = make_engine(conflict)
    with Session(ce) as s:
        ph = hash_for("a")
        s.add(
            Rating(
                pixel_hash=ph,
                stars=5,
                updated_at=clock.now,
                updated_by_device="other",
                source=RatingSource.app,
            )
        )
        record_change(
            s,
            device_id="other",
            op=ChangeOp.rate,
            pixel_hash=ph,
            ts=clock.now,
            field="stars",
            new_value="5",
        )
        s.commit()
    ce.dispose()

    mgr = SyncManager(settings, clock=clock)
    assert mgr.start(background=False) is WriteMode.writable

    with Session(make_engine(settings.paths.working_db)) as s:
        merged = {r.pixel_hash: r.stars for r in s.exec(select(Rating)).all()}
    assert merged.get(hash_for("a")) == 5
    assert not conflict.exists()
    assert (settings.paths.merged_snapshots_dir / "sortandpost (1).db").exists()

    mgr.stop()


# --------------------------------------------------------------------------- #
# End-to-end two-machine convergence (build-order step 4 acceptance test):
# two independent APP_DATA trees, each with its own local/ + sync/, a fake
# sync tool that ferries files (and drops a conflicted copy) between the two
# sync/ folders, and a restart on each side. Both must land on identical
# user state.
# --------------------------------------------------------------------------- #
def _count(db: Path, model: type) -> int:
    engine = make_engine(db)
    try:
        with Session(engine) as s:
            return len(s.exec(select(model)).all())
    finally:
        engine.dispose()


def _drop_conflict_copy(src_sync: Path, dst_sync: Path, name: str) -> Path:
    """Simulate a third-party sync tool copying the canonical DB from *src_sync*
    into *dst_sync* under a conflicted-copy name (dst already has its own)."""
    dst = dst_sync / name
    shutil.copy(src_sync / "sortandpost.db", dst)
    return dst


def test_two_machines_with_separate_sync_dirs_converge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_a = _settings(monkeypatch, tmp_path / "a")
    settings_b = _settings(monkeypatch, tmp_path / "b")
    assert settings_a.device_id != settings_b.device_id

    # -- each machine edits offline against its own sync/ ------------------- #
    clock_a = FakeClock(datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC))
    mgr_a = SyncManager(settings_a, clock=clock_a)
    assert mgr_a.start(background=False) is WriteMode.writable
    rate(settings_a.paths.working_db, "a", 5, device=settings_a.device_id, when=ts(10))
    mgr_a.stop()  # publishes a/sync/sortandpost.db

    clock_b = FakeClock(datetime(2026, 9, 6, 12, 5, 0, tzinfo=UTC))
    mgr_b = SyncManager(settings_b, clock=clock_b)
    assert mgr_b.start(background=False) is WriteMode.writable
    rate(settings_b.paths.working_db, "a", 3, device=settings_b.device_id, when=ts(20))
    rate(settings_b.paths.working_db, "b", 4, device=settings_b.device_id, when=ts(30))
    mgr_b.stop()  # publishes b/sync/sortandpost.db

    # -- the sync tool cross-delivers each side's DB as a conflicted copy -- #
    _drop_conflict_copy(settings_b.paths.sync, settings_a.paths.sync, "sortandpost (1).db")
    _drop_conflict_copy(settings_a.paths.sync, settings_b.paths.sync, "sortandpost (1).db")

    # -- both machines restart and merge --------------------------------- #
    mgr_a2 = SyncManager(settings_a, clock=FakeClock(clock_a.now))
    assert mgr_a2.start(background=False) is WriteMode.writable
    mgr_a2.stop()

    mgr_b2 = SyncManager(settings_b, clock=FakeClock(clock_b.now))
    assert mgr_b2.start(background=False) is WriteMode.writable
    mgr_b2.stop()

    # -- identical user state on both machines --------------------------- #
    want = {hash_for("a"): 3, hash_for("b"): 4}  # B's edits are newer
    assert read_ratings(settings_a.paths.working_db) == want
    assert read_ratings(settings_b.paths.working_db) == want
    assert _count(settings_a.paths.working_db, Change) == 3
    assert _count(settings_b.paths.working_db, Change) == 3
    assert _count(settings_a.paths.working_db, Photo) == _count(settings_b.paths.working_db, Photo)
    assert read_generation(settings_a.paths.working_db) == read_generation(
        settings_b.paths.working_db
    )

    # conflict copies were consumed and archived, not left to re-merge
    assert not (settings_a.paths.sync / "sortandpost (1).db").exists()
    assert not (settings_b.paths.sync / "sortandpost (1).db").exists()
    assert (settings_a.paths.merged_snapshots_dir / "sortandpost (1).db").exists()

    # -- a second sync round must be a no-op (no double-applied changes) - #
    gen_b_before = read_generation(settings_b.paths.working_db)
    _drop_conflict_copy(settings_a.paths.sync, settings_b.paths.sync, "sortandpost (2).db")
    mgr_b3 = SyncManager(settings_b, clock=FakeClock(clock_b.now))
    mgr_b3.start(background=False)
    mgr_b3.stop()

    assert read_ratings(settings_b.paths.working_db) == want
    assert _count(settings_b.paths.working_db, Change) == 3  # deduped on change_key
    assert read_generation(settings_b.paths.working_db) >= gen_b_before
