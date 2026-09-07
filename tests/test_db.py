from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from sortandpost.db.models import Change, ChangeOp, Photo, Rating, RatingSource


def _now() -> datetime:
    return datetime.now(UTC)


def test_pragmas_applied(engine: Engine) -> None:
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "delete"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def _photo(pixel_hash: str = "a" * 64) -> Photo:
    now = _now()
    return Photo(
        pixel_hash=pixel_hash,
        primary_path="/library/IMG_0001.jpg",
        width=6000,
        height=4000,
        format="JPEG",
        file_size=123456,
        file_mtime_ns=1_000_000_000,
        first_indexed_at=now,
        last_indexed_at=now,
    )


def test_insert_photo_and_rating(engine: Engine) -> None:
    with Session(engine) as s:
        s.add(_photo())
        s.add(
            Rating(
                pixel_hash="a" * 64,
                stars=4,
                updated_at=_now(),
                updated_by_device="dev1",
                source=RatingSource.app,
            )
        )
        s.commit()

    with Session(engine) as s:
        rating = s.exec(select(Rating)).one()
        assert rating.stars == 4
        assert rating.source == RatingSource.app


def test_rating_requires_existing_photo(engine: Engine) -> None:
    with Session(engine) as s, pytest.raises(IntegrityError):
        s.add(
            Rating(
                pixel_hash="missing" * 8,
                stars=3,
                updated_at=_now(),
                updated_by_device="dev1",
            )
        )
        s.commit()


def test_changes_log_is_append_only_friendly(engine: Engine) -> None:
    with Session(engine) as s:
        s.add(_photo())
        for stars in (2, 5):
            s.add(
                Change(
                    ts=_now(),
                    device_id="dev1",
                    op=ChangeOp.rate,
                    pixel_hash="a" * 64,
                    field="stars",
                    new_value=str(stars),
                )
            )
        s.commit()

    with Session(engine) as s:
        rows = s.exec(select(Change).order_by(Change.id)).all()
        assert [r.new_value for r in rows] == ["2", "5"]
        assert all(r.op == ChangeOp.rate for r in rows)
