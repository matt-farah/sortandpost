"""Delete on the photo page returns to the photo you came from (V3.1 #2)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Session, delete

from sortandpost.app import _delete_return_target, _parse_trail, _photo_url
from sortandpost.db import init_db, make_engine
from sortandpost.db.models import Photo, PhotoPath

from .factories import hash_for, make_db

A, B, C = hash_for("a"), hash_for("b"), hash_for("c")


def test_photo_url_carries_the_trail_after_the_view() -> None:
    assert _photo_url(A) == f"/photo/{A}"
    assert _photo_url(A, "group=flat", [B, C]) == f"/photo/{A}?group=flat&from={B},{C}"
    assert _photo_url(A, "", [B]) == f"/photo/{A}?from={B}"


def test_trail_is_capped_and_junk_is_dropped() -> None:
    long = [hash_for(c) for c in "abcdefghijklmnop"]
    assert _parse_trail(",".join(long)) == long[-10:]
    assert _parse_trail(f"{A},../etc,,{'x' * 65},{B}") == [A, B]
    assert _parse_trail(None) == []


def _engine(tmp_path: Path) -> Engine:
    db = tmp_path / "t.db"
    make_db(db, photos=["a", "b", "c"])
    engine = make_engine(db)
    init_db(engine)
    return engine


def test_delete_goes_to_the_last_photo_you_came_from(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    assert _delete_return_target(engine, [A, B], {C}) == (B, [A])
    assert _delete_return_target(engine, [A], {C}) == (A, [])


def test_delete_with_no_trail_goes_to_the_library(tmp_path: Path) -> None:
    assert _delete_return_target(_engine(tmp_path), [], {C}) is None


def test_delete_skips_photos_that_went_with_it_or_are_gone(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    # B went too (whole-stack delete): fall back to A
    assert _delete_return_target(engine, [A, B], {B, C}) == (A, [])
    with Session(engine) as s:
        s.exec(delete(PhotoPath).where(PhotoPath.pixel_hash == A))  # type: ignore[call-overload]
        s.exec(delete(Photo).where(Photo.pixel_hash == A))  # type: ignore[call-overload]
        s.commit()
    assert _delete_return_target(engine, [A, B], {B, C}) is None
