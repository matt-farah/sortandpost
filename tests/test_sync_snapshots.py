from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sortandpost.sync.publish import quick_check, read_generation, sha256_file
from sortandpost.sync.snapshots import (
    list_snapshots,
    newest_snapshot,
    prune_snapshots,
    restore_snapshot,
    write_snapshot,
)
from tests.factories import make_db


def _at(day: int) -> datetime:
    return datetime(2026, 9, day, 3, 0, 0, tzinfo=UTC)


def test_write_snapshot_is_compressed_and_restores_to_a_valid_db(tmp_path: Path) -> None:
    db = tmp_path / "local" / "sortandpost.db"
    make_db(db, photos=["a", "b"], generation=6)
    snaps_dir = tmp_path / "sync" / "snapshots"

    snap = write_snapshot(db, snaps_dir, now=_at(1))
    assert snap.name == "sortandpost-20260901-030000.db.zst"
    assert snap.stat().st_size < db.stat().st_size  # zstd actually shrank it

    restored = tmp_path / "restored.db"
    restore_snapshot(snap, restored)
    assert quick_check(restored)
    assert read_generation(restored) == 6
    assert sha256_file(restored) == sha256_file(db)


def test_same_second_snapshots_get_a_uniquifier(tmp_path: Path) -> None:
    db = tmp_path / "sortandpost.db"
    make_db(db, photos=["a"])
    snaps_dir = tmp_path / "snapshots"
    a = write_snapshot(db, snaps_dir, now=_at(2))
    b = write_snapshot(db, snaps_dir, now=_at(2))
    assert a != b
    assert len(list_snapshots(snaps_dir)) == 2


def test_prune_keeps_only_the_newest_n(tmp_path: Path) -> None:
    db = tmp_path / "sortandpost.db"
    make_db(db, photos=["a"])
    snaps_dir = tmp_path / "snapshots"
    made = [write_snapshot(db, snaps_dir, now=_at(day)) for day in range(1, 6)]

    removed = prune_snapshots(snaps_dir, keep=2)

    assert set(removed) == set(made[:3])
    assert list_snapshots(snaps_dir) == made[3:]
    assert newest_snapshot(snaps_dir) == made[-1]


def test_prune_noop_when_under_the_limit(tmp_path: Path) -> None:
    db = tmp_path / "sortandpost.db"
    make_db(db, photos=["a"])
    snaps_dir = tmp_path / "snapshots"
    write_snapshot(db, snaps_dir, now=_at(1))
    assert prune_snapshots(snaps_dir, keep=10) == []
