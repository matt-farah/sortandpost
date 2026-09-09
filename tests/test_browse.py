from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.engine import Engine
from sqlmodel import Session

from sortandpost.db.browse import (
    get_paths,
    get_photo,
    get_rating,
    list_photos,
    photo_count,
)
from sortandpost.db.models import Photo, PhotoPath, Rating

_BASE = datetime(2020, 1, 1, tzinfo=UTC)


def _photo(session: Session, tag: str, *, taken_at: datetime | None, missing: bool = False) -> str:
    pixel_hash = tag * 64
    now = datetime.now(UTC)
    session.add(
        Photo(
            pixel_hash=pixel_hash,
            primary_path=f"/library/{tag}.jpg",
            width=100,
            height=100,
            format="JPEG",
            file_size=1000,
            file_mtime_ns=1,
            first_indexed_at=now,
            last_indexed_at=now,
            taken_at=taken_at,
            missing_since=now if missing else None,
        )
    )
    return pixel_hash


def test_list_photos_orders_newest_first_nulls_last(engine: Engine) -> None:
    with Session(engine) as s:
        _photo(s, "a", taken_at=_BASE)
        _photo(s, "b", taken_at=_BASE + timedelta(days=10))
        _photo(s, "c", taken_at=None)
        s.commit()

    with Session(engine) as s:
        order = [p.primary_path for p in list_photos(s)]
    assert order == ["/library/b.jpg", "/library/a.jpg", "/library/c.jpg"]


def test_count_and_list_exclude_missing_by_default(engine: Engine) -> None:
    with Session(engine) as s:
        _photo(s, "a", taken_at=_BASE)
        _photo(s, "b", taken_at=_BASE, missing=True)
        s.commit()

    with Session(engine) as s:
        assert photo_count(s) == 1
        assert photo_count(s, include_missing=True) == 2
        assert [p.primary_path for p in list_photos(s)] == ["/library/a.jpg"]
        assert len(list_photos(s, include_missing=True)) == 2


def test_pagination_offset_and_limit(engine: Engine) -> None:
    with Session(engine) as s:
        for i in range(5):
            _photo(s, chr(ord("a") + i), taken_at=_BASE + timedelta(days=i))
        s.commit()

    with Session(engine) as s:
        page0 = list_photos(s, limit=2, offset=0)
        page1 = list_photos(s, limit=2, offset=2)
    assert [p.primary_path for p in page0] == ["/library/e.jpg", "/library/d.jpg"]
    assert [p.primary_path for p in page1] == ["/library/c.jpg", "/library/b.jpg"]


def test_get_photo_paths_and_rating(engine: Engine) -> None:
    with Session(engine) as s:
        h = _photo(s, "a", taken_at=_BASE)
        s.add(PhotoPath(pixel_hash=h, path="/library/z.jpg", size=1, mtime_ns=1, present=True))
        s.add(PhotoPath(pixel_hash=h, path="/library/a.jpg", size=1, mtime_ns=1, present=False))
        s.add(
            Rating(
                pixel_hash=h,
                stars=4,
                updated_at=datetime.now(UTC),
                updated_by_device="dev",
            )
        )
        s.commit()

    with Session(engine) as s:
        assert get_photo(s, h) is not None
        assert get_photo(s, "missing" * 8) is None
        assert [p.path for p in get_paths(s, h)] == ["/library/a.jpg", "/library/z.jpg"]
        assert get_rating(s, h) == 4
        assert get_rating(s, "b" * 64) is None
