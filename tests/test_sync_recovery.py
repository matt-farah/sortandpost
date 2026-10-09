"""Boot-time recovery of a broken working DB (sortandpost.sync.recovery).

The 2026-10-08 failure: a sync tool evicted ``sortandpost.db-journal`` and the
bytes were gone, so every open of the working DB failed with ``disk I/O error``
and the app couldn't boot. An unreadable file is simulated with ``chmod 000``.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from sortandpost.config import bootstrap
from sortandpost.db import make_engine
from sortandpost.paths import Paths
from sortandpost.sync.publish import quick_check, read_generation
from sortandpost.sync.recovery import (
    check_working_db,
    read_report,
    recover_working_db,
    salvage_dir,
)
from sortandpost.sync.snapshots import write_snapshot
from tests.factories import hash_for, make_db, rate, read_ratings, ts

needs_chmod = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 000 doesn't make a file unreadable here",
)


@pytest.fixture
def lib(tmp_path: Path) -> Paths:
    p = Paths(app_data=tmp_path / "appdata")
    bootstrap(p)
    return p


def _publish(p: Paths) -> None:
    shutil.copyfile(p.working_db, p.published_db)


def _unreadable(path: Path) -> None:
    path.write_bytes(b"\0" * 4096)
    path.chmod(0)


def _opens(db: Path) -> bool:
    engine = make_engine(db)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT count(*) FROM photos")).one()
        return True
    finally:
        engine.dispose()


def test_healthy_or_missing_db_is_left_alone(lib: Paths) -> None:
    assert recover_working_db(lib, device_id="dev") is None  # no DB yet
    make_db(lib.working_db, photos=["a"])
    assert check_working_db(lib.working_db) is None
    assert recover_working_db(lib, device_id="dev") is None
    assert not (lib.local / "recovered").exists()
    assert read_report(lib) is None


@needs_chmod
def test_unreadable_journal_rebuilds_from_published_and_keeps_unpublished_edits(
    lib: Paths,
) -> None:
    make_db(lib.working_db, photos=["a", "b"], generation=5)
    rate(lib.working_db, "a", 4, device="dev", when=ts(10))
    _publish(lib)
    # edits after the last publish -- only in the working copy
    rate(lib.working_db, "b", 3, device="dev", when=ts(20))
    rate(lib.working_db, "a", 5, device="dev", when=ts(30))
    journal = lib.working_db.with_name("sortandpost.db-journal")
    _unreadable(journal)
    assert "can't be read" in (check_working_db(lib.working_db) or "")

    report = recover_working_db(lib, device_id="dev")

    assert report is not None
    assert "journal" in report.reason
    assert report.base == "the published copy in the shared folder"
    assert report.salvaged  # the working copy's own unpublished edits were merged in
    assert read_ratings(lib.working_db) == {hash_for("a"): 5, hash_for("b"): 3}
    assert not journal.exists()  # never left beside the rebuilt DB
    assert check_working_db(lib.working_db) is None
    assert _opens(lib.working_db)
    assert read_generation(lib.working_db) > 5  # ahead of sync/, so it gets published
    # the damaged files are kept, not deleted
    assert report.set_aside is not None
    kept = Path(report.set_aside)
    assert (kept / "sortandpost.db").exists()
    assert (kept / "sortandpost.db-journal").exists()
    (kept / "sortandpost.db-journal").chmod(0o600)  # let tmp_path cleanup remove it
    assert not any(salvage_dir(lib).glob("*.db"))
    assert read_report(lib) == report


def test_garbage_db_restores_newest_snapshot_when_nothing_published(lib: Paths) -> None:
    make_db(lib.working_db, photos=["a"], generation=3)
    write_snapshot(lib.working_db, lib.snapshots_dir, now=ts(0))
    lib.working_db.write_bytes(b"this is not a database" * 400)

    report = recover_working_db(lib, device_id="dev")

    assert report is not None
    assert report.base.startswith("snapshot sortandpost-")
    assert report.salvaged == []  # garbage isn't salvageable
    assert quick_check(lib.working_db)
    assert read_generation(lib.working_db) == 3


def test_higher_generation_wins_between_published_and_snapshot(lib: Paths) -> None:
    make_db(lib.working_db, photos=["a"], generation=3)
    write_snapshot(lib.working_db, lib.snapshots_dir, now=ts(0))
    lib.working_db.unlink()
    make_db(lib.working_db, photos=["a", "b"], generation=9)
    _publish(lib)
    lib.working_db.write_bytes(b"junk" * 2000)

    report = recover_working_db(lib, device_id="dev")

    assert report is not None and "published" in report.base
    assert read_generation(lib.working_db) == 9


def test_nothing_intact_leaves_no_db_for_a_fresh_start(lib: Paths) -> None:
    lib.working_db.write_bytes(b"junk" * 2000)
    report = recover_working_db(lib, device_id="dev")
    assert report is not None
    assert report.base.startswith("nothing")
    assert not lib.working_db.exists()  # init_db then starts empty; the scan rebuilds


def test_salvage_left_by_relocation_is_merged_into_the_published_copy(lib: Paths) -> None:
    make_db(lib.working_db, photos=["a", "b"], generation=4)
    _publish(lib)
    rate(lib.working_db, "b", 2, device="dev", when=ts(5))
    salvage_dir(lib).mkdir()
    lib.working_db.replace(salvage_dir(lib) / "relocated-x.db")

    report = recover_working_db(lib, device_id="dev")

    assert report is not None
    assert report.reason == ""
    assert report.salvaged == ["relocated-x.db"]
    assert read_ratings(lib.working_db) == {hash_for("b"): 2}
    assert not any(salvage_dir(lib).glob("*.db"))
