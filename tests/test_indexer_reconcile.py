"""Pipeline stage 6: file-vs-DB rating reconciliation + additive keyword import."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlmodel import Session, select

from sortandpost.db import init_db, make_engine
from sortandpost.db.models import (
    Change,
    ChangeOp,
    Photo,
    PhotoTag,
    Rating,
    RatingSource,
    Tag,
    TagSource,
)
from sortandpost.indexer.reconcile import reconcile_metadata
from sortandpost.metadata import EmbeddedMetadata

OLD = datetime(2020, 1, 1, tzinfo=UTC)
NEW = datetime(2026, 1, 1, tzinfo=UTC)
NOW = datetime(2026, 6, 1, tzinfo=UTC)
DEVICE = "dev-1"


def _ns(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000_000_000)


class MockExiftool:
    def __init__(self) -> None:
        self.rating_calls: list[tuple[str, int]] = []

    def write_rating(self, path: Path, stars: int) -> None:
        self.rating_calls.append((path.name, stars))


@pytest.fixture
def session(tmp_path: Path):
    engine = make_engine(tmp_path / "db.sqlite")
    init_db(engine)
    with Session(engine) as s:
        yield s


def _photo(s: Session, pixel_hash: str, path: str) -> None:
    s.add(
        Photo(
            pixel_hash=pixel_hash,
            primary_path=path,
            width=1,
            height=1,
            format=path.rsplit(".", 1)[-1].upper(),
            file_size=1,
            file_mtime_ns=1,
            first_indexed_at=NOW,
            last_indexed_at=NOW,
        )
    )
    s.commit()


def _md(*, rating: int | None = None, keywords: tuple[str, ...] = ()) -> EmbeddedMetadata:
    return EmbeddedMetadata(rating=rating, keywords=keywords)


# --------------------------------------------------------------------------- #
# rating
# --------------------------------------------------------------------------- #
def test_import_rating_when_db_has_none(session: Session) -> None:
    ph = "a" * 64
    _photo(session, ph, "/lib/a.jpg")
    et = MockExiftool()

    out = reconcile_metadata(
        session,
        pixel_hash=ph,
        path=Path("/lib/a.jpg"),
        md=_md(rating=4),
        exiftool=et,
        device_id=DEVICE,
        file_mtime_ns=_ns(NEW),
        now=NOW,
    )

    assert out.rating_imported and not out.rating_written
    row = session.get(Rating, ph)
    assert row is not None and row.stars == 4 and row.source == RatingSource.file
    change = session.exec(select(Change).where(Change.op == ChangeOp.rate)).one()
    assert (change.old_value, change.new_value, change.field) == (None, "4", "stars")
    assert et.rating_calls == []


def test_file_wins_when_mtime_newer(session: Session) -> None:
    ph = "b" * 64
    _photo(session, ph, "/lib/b.jpg")
    session.add(Rating(pixel_hash=ph, stars=2, updated_at=OLD, updated_by_device="other"))
    session.commit()

    out = reconcile_metadata(
        session,
        pixel_hash=ph,
        path=Path("/lib/b.jpg"),
        md=_md(rating=5),
        exiftool=MockExiftool(),
        device_id=DEVICE,
        file_mtime_ns=_ns(NEW),
        now=NOW,
    )

    assert out.rating_imported
    row = session.get(Rating, ph)
    assert row is not None and row.stars == 5 and row.updated_by_device == DEVICE
    assert session.exec(select(Change).where(Change.op == ChangeOp.rate)).one().new_value == "5"


def test_db_wins_when_mtime_older(session: Session) -> None:
    ph = "c" * 64
    _photo(session, ph, "/lib/c.jpg")
    session.add(Rating(pixel_hash=ph, stars=3, updated_at=NEW, updated_by_device="other"))
    session.commit()
    et = MockExiftool()

    out = reconcile_metadata(
        session,
        pixel_hash=ph,
        path=Path("/lib/c.jpg"),
        md=_md(rating=1),
        exiftool=et,
        device_id=DEVICE,
        file_mtime_ns=_ns(OLD),
        now=NOW,
    )

    assert out.rating_written and not out.rating_imported
    assert et.rating_calls == [("c.jpg", 3)]
    assert session.get(Rating, ph).stars == 3  # DB untouched
    assert session.exec(select(Change)).all() == []


def test_db_rating_written_out_when_file_has_none(session: Session) -> None:
    ph = "d" * 64
    _photo(session, ph, "/lib/d.jpg")
    session.add(Rating(pixel_hash=ph, stars=4, updated_at=OLD, updated_by_device="x"))
    session.commit()
    et = MockExiftool()

    out = reconcile_metadata(
        session,
        pixel_hash=ph,
        path=Path("/lib/d.jpg"),
        md=_md(rating=None),
        exiftool=et,
        device_id=DEVICE,
        file_mtime_ns=_ns(NEW),
        now=NOW,
    )
    assert out.rating_written
    assert et.rating_calls == [("d.jpg", 4)]


def test_equal_rating_is_noop(session: Session) -> None:
    ph = "e" * 64
    _photo(session, ph, "/lib/e.jpg")
    session.add(Rating(pixel_hash=ph, stars=3, updated_at=OLD, updated_by_device="x"))
    session.commit()
    et = MockExiftool()

    out = reconcile_metadata(
        session,
        pixel_hash=ph,
        path=Path("/lib/e.jpg"),
        md=_md(rating=3),
        exiftool=et,
        device_id=DEVICE,
        file_mtime_ns=_ns(NEW),
        now=NOW,
    )
    assert not out.touched
    assert et.rating_calls == []


def test_raw_path_is_untouched(session: Session) -> None:
    ph = "f" * 64
    _photo(session, ph, "/lib/f.nef")
    session.add(Rating(pixel_hash=ph, stars=2, updated_at=OLD, updated_by_device="x"))
    session.commit()
    et = MockExiftool()

    out = reconcile_metadata(
        session,
        pixel_hash=ph,
        path=Path("/lib/f.nef"),
        md=_md(rating=5, keywords=("dog",)),
        exiftool=et,
        device_id=DEVICE,
        file_mtime_ns=_ns(NEW),
        now=NOW,
    )
    assert not out.touched
    assert et.rating_calls == []
    assert session.get(Rating, ph).stars == 2
    assert session.exec(select(PhotoTag)).all() == []


def test_db_wins_without_exiftool_skips_write(session: Session) -> None:
    ph = "g" * 64
    _photo(session, ph, "/lib/g.jpg")
    session.add(Rating(pixel_hash=ph, stars=3, updated_at=NEW, updated_by_device="x"))
    session.commit()

    out = reconcile_metadata(
        session,
        pixel_hash=ph,
        path=Path("/lib/g.jpg"),
        md=_md(rating=1),
        exiftool=None,
        device_id=DEVICE,
        file_mtime_ns=_ns(OLD),
        now=NOW,
    )
    assert not out.rating_written
    assert session.get(Rating, ph).stars == 3


# --------------------------------------------------------------------------- #
# keywords
# --------------------------------------------------------------------------- #
def test_keyword_import_is_additive(session: Session) -> None:
    ph = "h" * 64
    _photo(session, ph, "/lib/h.jpg")
    existing = Tag(name="sunset")
    session.add(existing)
    session.flush()
    session.add(
        PhotoTag(
            pixel_hash=ph,
            tag_id=existing.id,
            source=TagSource.manual,
            added_at=OLD,
            added_by_device="x",
        )
    )
    session.commit()

    out = reconcile_metadata(
        session,
        pixel_hash=ph,
        path=Path("/lib/h.jpg"),
        md=_md(keywords=("dog", "sunset")),
        exiftool=MockExiftool(),
        device_id=DEVICE,
        file_mtime_ns=_ns(NEW),
        now=NOW,
    )
    assert out.keywords_imported == 1

    names = sorted(
        session.exec(
            select(Tag.name)
            .join(PhotoTag, PhotoTag.tag_id == Tag.id)
            .where(PhotoTag.pixel_hash == ph)
        ).all()
    )
    assert names == ["dog", "sunset"]
    dog_tag = session.exec(select(PhotoTag).join(Tag).where(Tag.name == "dog")).one()
    assert dog_tag.source == TagSource.manual and dog_tag.written_to_file is True
    change = session.exec(select(Change).where(Change.op == ChangeOp.tag_add)).one()
    assert change.field == "dog"
