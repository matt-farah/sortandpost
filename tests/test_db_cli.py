from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlmodel import Session, select

from sortandpost.config import Settings
from sortandpost.db import make_engine
from sortandpost.db.__main__ import main as db_main
from sortandpost.db.models import ChangeOp, Rating, RatingSource
from sortandpost.sync.changelog import record_change
from sortandpost.sync.snapshots import list_snapshots
from tests.factories import hash_for, make_db, ts


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.setenv("APP_DATA", str(tmp_path / "appdata"))
    resolved = Settings.resolve()
    make_db(resolved.paths.working_db, photos=["a", "b"], generation=2)
    return resolved


def test_snapshot_command_writes_and_prunes(
    settings: Settings, capsys: pytest.CaptureFixture
) -> None:
    settings_keep = settings.config.sync.snapshot_keep
    assert db_main(["snapshot"]) == 0
    out = capsys.readouterr().out
    assert "wrote" in out
    assert len(list_snapshots(settings.paths.snapshots_dir)) == 1
    assert settings_keep >= 1


def test_restore_command_round_trips(settings: Settings) -> None:
    assert db_main(["snapshot"]) == 0
    snap = list_snapshots(settings.paths.snapshots_dir)[-1]

    # clobber the working DB, then restore it from the snapshot
    settings.paths.working_db.write_bytes(b"corrupted")
    assert db_main(["restore", str(snap)]) == 0

    with Session(make_engine(settings.paths.working_db)) as s:
        assert len(s.exec(select(Rating)).all()) == 0  # snapshot had no ratings


def test_restore_missing_snapshot_is_an_error(settings: Settings) -> None:
    assert db_main(["restore", str(settings.paths.snapshots_dir / "nope.db.zst")]) == 2


def test_merge_command_folds_in_a_conflict_copy(settings: Settings) -> None:
    incoming = settings.paths.sync / "sortandpost (1).db"
    shutil.copy(settings.paths.working_db, incoming)
    ie = make_engine(incoming)
    with Session(ie) as s:
        ph = hash_for("a")
        s.add(
            Rating(
                pixel_hash=ph,
                stars=4,
                updated_at=ts(100),
                updated_by_device="other",
                source=RatingSource.app,
            )
        )
        record_change(
            s, device_id="other", op=ChangeOp.rate, pixel_hash=ph, ts=ts(100), new_value="4"
        )
        s.commit()
    ie.dispose()

    assert db_main(["merge", str(incoming)]) == 0

    with Session(make_engine(settings.paths.working_db)) as s:
        ratings = {r.pixel_hash: r.stars for r in s.exec(select(Rating)).all()}
    assert ratings.get(hash_for("a")) == 4
    assert not incoming.exists()
    assert (settings.paths.merged_snapshots_dir / "sortandpost (1).db").exists()
