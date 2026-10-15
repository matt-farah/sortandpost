from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlmodel import Session, select

from sortandpost.clip.tagging import approve, unapprove
from sortandpost.db import make_engine
from sortandpost.db.meta import get_generation
from sortandpost.db.models import (
    Change,
    ChangeOp,
    Photo,
    PhotoPath,
    PhotoTag,
    Prediction,
    PredictionState,
    Tombstone,
)
from sortandpost.sync.merge import find_conflict_copies, merge_all
from tests.factories import hash_for, make_db, rate, read_ratings, ts

NOW = datetime(2026, 9, 6, tzinfo=UTC)


def _merge(target_db: Path, source_db: Path, *, target_gen: int, source_gen: int) -> None:
    te = make_engine(target_db)
    se = make_engine(source_db)
    try:
        with Session(te) as target, Session(se) as source:
            merge_all(
                target,
                [(source_db.name, source)],
                now=NOW,
                base_generation=target_gen,
                source_generations=[source_gen],
            )
            target.commit()
    finally:
        se.dispose()
        te.dispose()


# --------------------------------------------------------------------------- #
# conflict detection
# --------------------------------------------------------------------------- #
def test_find_conflict_copies(tmp_path: Path) -> None:
    (tmp_path / "sortandpost.db").write_bytes(b"")
    (tmp_path / "sortandpost (1).db").write_bytes(b"")
    (tmp_path / "sortandpost-mac-2.db").write_bytes(b"")
    (tmp_path / "sortandpost.conflicted copy.db").write_bytes(b"")
    (tmp_path / ".sortandpost.db.tmp-devx").write_bytes(b"")
    (tmp_path / "notes.db").write_bytes(b"")

    found = {p.name for p in find_conflict_copies(tmp_path)}
    assert found == {
        "sortandpost (1).db",
        "sortandpost-mac-2.db",
        "sortandpost.conflicted copy.db",
    }


# --------------------------------------------------------------------------- #
# two-machine convergence (docs/architecture.md step-4 acceptance test)
# --------------------------------------------------------------------------- #
def test_two_machine_merge_converges(tmp_path: Path) -> None:
    base = tmp_path / "base.db"
    make_db(base, photos=["a", "b"], generation=1)

    a = tmp_path / "machine_a.db"
    b = tmp_path / "machine_b.db"
    shutil.copy(base, a)
    shutil.copy(base, b)

    # A rates photo a=5 early; B rates a=3 and b=4 later -> B's edits are newer.
    rate(a, "a", 5, device="A", when=ts(10))
    rate(b, "a", 3, device="B", when=ts(20))
    rate(b, "b", 4, device="B", when=ts(30))

    # each machine merges the *other's* pre-merge conflict copy
    a_copy = tmp_path / "a_conflict.db"
    b_copy = tmp_path / "b_conflict.db"
    shutil.copy(a, a_copy)
    shutil.copy(b, b_copy)

    _merge(a, b_copy, target_gen=1, source_gen=1)
    _merge(b, a_copy, target_gen=1, source_gen=1)

    want = {hash_for("a"): 3, hash_for("b"): 4}
    assert read_ratings(a) == want
    assert read_ratings(b) == want
    assert get_generation_of(a) == get_generation_of(b)

    # changes log is the union of both machines' rows, on both machines
    assert count(a, Change) == 3
    assert count(b, Change) == 3
    assert count(a, Photo) == count(b, Photo) == 2


def test_rating_merge_is_last_writer_wins_by_ts_then_device(tmp_path: Path) -> None:
    left = tmp_path / "l.db"
    right = tmp_path / "r.db"
    make_db(left, photos=["a"], generation=1)
    shutil.copy(left, right)

    # same timestamp, different device -> higher device_id wins the tie
    rate(left, "a", 2, device="A", when=ts(50))
    rate(right, "a", 9, device="Z", when=ts(50))

    _merge(left, right, target_gen=1, source_gen=1)
    assert read_ratings(left) == {hash_for("a"): 9}

    # now an older edit must not clobber the merged value
    stale = tmp_path / "stale.db"
    make_db(stale, photos=["a"], generation=1)
    rate(stale, "a", 1, device="A", when=ts(1))
    _merge(left, stale, target_gen=2, source_gen=1)
    assert read_ratings(left) == {hash_for("a"): 9}


def test_derived_union_adds_photos_only_in_the_source(tmp_path: Path) -> None:
    target = tmp_path / "t.db"
    source = tmp_path / "s.db"
    make_db(target, photos=["a"], generation=1)
    make_db(source, photos=["a", "b", "c"], generation=1)

    _merge(target, source, target_gen=1, source_gen=1)

    with Session(make_engine(target)) as s:
        assert {p.pixel_hash for p in s.exec(select(Photo)).all()} == {
            hash_for("a"),
            hash_for("b"),
            hash_for("c"),
        }
        assert len(s.exec(select(PhotoPath)).all()) == 3


def test_unindex_replay_writes_a_tombstone_and_marks_missing(tmp_path: Path) -> None:
    target = tmp_path / "t.db"
    source = tmp_path / "s.db"
    make_db(target, photos=["a", "b"], generation=1)
    make_db(source, photos=["a", "b"], generation=2)

    # machine that owns `source` deleted photo "a"
    se = make_engine(source)
    with Session(se) as s:
        s.add(
            Change(
                ts=NOW,
                device_id="dev-src",
                op=ChangeOp.unindex,
                pixel_hash=hash_for("a"),
                field="delete",
                new_value="1",
            )
        )
        s.commit()
    se.dispose()

    _merge(target, source, target_gen=1, source_gen=2)

    with Session(make_engine(target)) as s:
        tomb = s.get(Tombstone, hash_for("a"))
        assert tomb is not None and tomb.applied is False
        assert tomb.deleted_by_device == "dev-src"
        assert s.get(Photo, hash_for("a")).missing_since is not None
        assert s.get(Photo, hash_for("b")).missing_since is None
        assert s.get(Tombstone, hash_for("b")) is None


def _log(db: Path, *changes: tuple[datetime, str, ChangeOp, str]) -> None:
    engine = make_engine(db)
    with Session(engine) as s:
        for ts_, dev, op, tag in changes:
            s.add(
                Change(
                    ts=ts_,
                    device_id=dev,
                    op=op,
                    pixel_hash=hash_for(tag),
                    field="delete",
                    new_value="1",
                )
            )
        s.commit()
    engine.dispose()


def test_restore_after_unindex_lifts_the_tombstone(tmp_path: Path) -> None:
    target = tmp_path / "t.db"
    source = tmp_path / "s.db"
    make_db(target, photos=["a"], generation=1)
    make_db(source, photos=["a"], generation=2)
    later = NOW + timedelta(minutes=1)
    _log(source, (NOW, "dev-src", ChangeOp.unindex, "a"), (later, "dev-src", ChangeOp.restore, "a"))

    _merge(target, source, target_gen=1, source_gen=2)

    with Session(make_engine(target)) as s:
        assert s.get(Tombstone, hash_for("a")) is None
        assert s.get(Photo, hash_for("a")).missing_since is None


def test_older_unindex_cannot_rebury_a_put_back_photo(tmp_path: Path) -> None:
    # base deleted then put back "a"; a conflict copy only saw the delete
    target = tmp_path / "t.db"
    source = tmp_path / "s.db"
    make_db(target, photos=["a"], generation=3)
    make_db(source, photos=["a"], generation=2)
    later = NOW + timedelta(minutes=1)
    _log(target, (NOW, "dev-t", ChangeOp.unindex, "a"), (later, "dev-t", ChangeOp.restore, "a"))
    _log(source, (NOW, "dev-t", ChangeOp.unindex, "a"))

    _merge(target, source, target_gen=3, source_gen=2)

    with Session(make_engine(target)) as s:
        assert s.get(Tombstone, hash_for("a")) is None


def test_newer_unindex_beats_an_older_restore(tmp_path: Path) -> None:
    target = tmp_path / "t.db"
    source = tmp_path / "s.db"
    make_db(target, photos=["a"], generation=1)
    make_db(source, photos=["a"], generation=2)
    t1, t2 = NOW + timedelta(minutes=1), NOW + timedelta(minutes=2)
    _log(target, (NOW, "dev-t", ChangeOp.unindex, "a"), (t1, "dev-t", ChangeOp.restore, "a"))
    _log(source, (t2, "dev-src", ChangeOp.unindex, "a"))

    _merge(target, source, target_gen=1, source_gen=2)

    with Session(make_engine(target)) as s:
        tomb = s.get(Tombstone, hash_for("a"))
        assert tomb is not None and tomb.deleted_by_device == "dev-src"


def test_merge_bumps_generation_past_every_input(tmp_path: Path) -> None:
    target = tmp_path / "t.db"
    source = tmp_path / "s.db"
    make_db(target, photos=["a"], generation=3)
    make_db(source, photos=["a"], generation=8)

    _merge(target, source, target_gen=3, source_gen=8)
    assert get_generation_of(target) == 9


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def get_generation_of(db: Path) -> int:
    engine = make_engine(db)
    try:
        with Session(engine) as s:
            return get_generation(s)
    finally:
        engine.dispose()


def count(db: Path, model: type) -> int:
    engine = make_engine(db)
    try:
        with Session(engine) as s:
            return len(s.exec(select(model)).all())
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- #
# unapprove (photo page ✕ on an approved CLIP tag) replays on the other machine
# --------------------------------------------------------------------------- #
def test_unapprove_on_one_machine_merges_into_the_other(tmp_path: Path) -> None:
    a = tmp_path / "machine_a.db"
    b = tmp_path / "machine_b.db"
    make_db(a, photos=["a"], generation=1)
    with Session(make_engine(a)) as s:
        s.add(
            Prediction(
                pixel_hash=hash_for("a"),
                tag_name="red",
                score=0.5,
                model="m",
                run_at=NOW,
                state=PredictionState.pending,
            )
        )
        approve(
            s,
            device_id="A",
            model="m",
            pixel_hash=hash_for("a"),
            tag_name="red",
            facet=None,
            now=ts(10),
            exiftool=None,
        )
        s.commit()
    shutil.copy(a, b)

    with Session(make_engine(b)) as s:
        assert unapprove(
            s,
            device_id="B",
            model="m",
            pixel_hash=hash_for("a"),
            tag_name="red",
            now=ts(20),
            exiftool=None,
        )
        s.commit()

    _merge(a, b, target_gen=1, source_gen=1)

    with Session(make_engine(a)) as s:
        assert s.exec(select(PhotoTag)).all() == []
        assert s.exec(select(Prediction)).one().state == PredictionState.rejected
