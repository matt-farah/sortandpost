from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, select

from sortandpost.db import make_engine
from sortandpost.db.meta import get_generation
from sortandpost.db.models import Change, Photo, PhotoPath
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
# two-machine convergence (CLAUDE.md step-4 acceptance test)
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
