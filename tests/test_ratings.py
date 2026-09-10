"""``sortandpost.db.ratings.set_rating`` -- the UI's single rating write path."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from sortandpost.db import set_rating
from sortandpost.db.models import Change, ChangeOp, Photo, Rating, RatingSource

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
PH = "a" * 64


def _add_photo(session: Session, pixel_hash: str = PH) -> None:
    session.add(
        Photo(
            pixel_hash=pixel_hash,
            primary_path=f"/library/{pixel_hash}.jpg",
            width=10,
            height=10,
            format="JPEG",
            file_size=1,
            file_mtime_ns=1,
            first_indexed_at=NOW,
            last_indexed_at=NOW,
        )
    )
    session.commit()


def test_first_rating_creates_row_and_change(engine: Engine) -> None:
    with Session(engine) as s:
        _add_photo(s)
        changed = set_rating(s, pixel_hash=PH, stars=4, device_id="dev1", now=NOW)
        s.commit()
        assert changed is True

        row = s.get(Rating, PH)
        assert row is not None
        assert row.stars == 4
        assert row.source == RatingSource.app
        assert row.updated_by_device == "dev1"

        changes = s.exec(select(Change)).all()
        assert len(changes) == 1
        assert changes[0].op == ChangeOp.rate
        assert changes[0].field == "stars"
        assert changes[0].old_value is None
        assert changes[0].new_value == "4"


def test_re_rate_updates_row_and_logs_old_value(engine: Engine) -> None:
    with Session(engine) as s:
        _add_photo(s)
        set_rating(s, pixel_hash=PH, stars=2, device_id="dev1", now=NOW)
        set_rating(s, pixel_hash=PH, stars=5, device_id="dev2", now=NOW)
        s.commit()

        assert s.get(Rating, PH).stars == 5  # type: ignore[union-attr]
        changes = s.exec(select(Change).order_by(Change.id)).all()  # type: ignore[arg-type]
        assert [(c.old_value, c.new_value) for c in changes] == [(None, "2"), ("2", "5")]


def test_noop_re_rate_writes_nothing(engine: Engine) -> None:
    with Session(engine) as s:
        _add_photo(s)
        set_rating(s, pixel_hash=PH, stars=3, device_id="dev1", now=NOW)
        s.commit()
        changed = set_rating(s, pixel_hash=PH, stars=3, device_id="dev1", now=NOW)
        s.commit()

        assert changed is False
        assert len(s.exec(select(Change)).all()) == 1


def test_zero_clears_rating_and_logs_it(engine: Engine) -> None:
    with Session(engine) as s:
        _add_photo(s)
        set_rating(s, pixel_hash=PH, stars=4, device_id="dev1", now=NOW)
        changed = set_rating(s, pixel_hash=PH, stars=0, device_id="dev1", now=NOW)
        s.commit()

        assert changed is True
        assert s.get(Rating, PH).stars == 0  # type: ignore[union-attr]
        last = s.exec(select(Change).order_by(Change.id)).all()[-1]  # type: ignore[arg-type]
        assert (last.old_value, last.new_value) == ("4", "0")


@pytest.mark.parametrize("stars", [-1, 6, 99])
def test_out_of_range_rejected(engine: Engine, stars: int) -> None:
    with Session(engine) as s:
        _add_photo(s)
        with pytest.raises(ValueError):
            set_rating(s, pixel_hash=PH, stars=stars, device_id="dev1", now=NOW)
