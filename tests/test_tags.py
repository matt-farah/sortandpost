"""``sortandpost.db.tags`` -- manual tag add/remove + changes-log rows."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from sortandpost.db import add_manual_tag, remove_manual_tag
from sortandpost.db.models import Change, ChangeOp, Photo, PhotoTag, Tag, TagSource

NOW = datetime(2026, 1, 2, tzinfo=UTC)
PH = "a" * 64


def _photo(session: Session, pixel_hash: str = PH) -> None:
    session.add(
        Photo(
            pixel_hash=pixel_hash,
            primary_path=f"/library/{pixel_hash}.jpg",
            width=1,
            height=1,
            format="JPEG",
            file_size=1,
            file_mtime_ns=1,
            first_indexed_at=NOW,
            last_indexed_at=NOW,
        )
    )
    session.commit()


def test_add_creates_tag_photo_tag_and_change(engine: Engine) -> None:
    with Session(engine) as s:
        _photo(s)
        added = add_manual_tag(s, pixel_hash=PH, tag_name="  Beach ", device_id="d1", now=NOW)
        s.commit()
        assert added is True

        tag = s.exec(select(Tag).where(Tag.name == "Beach")).one()
        link = s.exec(select(PhotoTag).where(PhotoTag.tag_id == tag.id)).one()
        assert link.source == TagSource.manual
        assert link.added_by_device == "d1"

        change = s.exec(select(Change)).one()
        assert change.op == ChangeOp.tag_add
        assert (change.field, change.new_value) == ("Beach", "Beach")


def test_add_duplicate_is_noop(engine: Engine) -> None:
    with Session(engine) as s:
        _photo(s)
        add_manual_tag(s, pixel_hash=PH, tag_name="beach", device_id="d1", now=NOW)
        s.commit()
        again = add_manual_tag(s, pixel_hash=PH, tag_name="beach", device_id="d1", now=NOW)
        s.commit()
        assert again is False
        assert len(s.exec(select(PhotoTag)).all()) == 1
        assert len(s.exec(select(Change)).all()) == 1


def test_remove_deletes_row_and_logs(engine: Engine) -> None:
    with Session(engine) as s:
        _photo(s)
        add_manual_tag(s, pixel_hash=PH, tag_name="beach", device_id="d1", now=NOW)
        removed = remove_manual_tag(s, pixel_hash=PH, tag_name="beach", device_id="d1", now=NOW)
        s.commit()
        assert removed is True
        assert s.exec(select(PhotoTag)).all() == []
        last = s.exec(select(Change).order_by(Change.id)).all()[-1]  # type: ignore[arg-type]
        assert last.op == ChangeOp.tag_remove
        assert last.old_value == "beach"


def test_remove_missing_is_noop(engine: Engine) -> None:
    with Session(engine) as s:
        _photo(s)
        got = remove_manual_tag(s, pixel_hash=PH, tag_name="nope", device_id="d1", now=NOW)
        assert got is False


def test_remove_leaves_clip_tags_alone(engine: Engine) -> None:
    with Session(engine) as s:
        _photo(s)
        tag = Tag(name="sunset")
        s.add(tag)
        s.flush()
        s.add(
            PhotoTag(
                pixel_hash=PH,
                tag_id=tag.id,
                source=TagSource.clip,
                added_at=NOW,
                added_by_device="d1",
            )
        )
        s.commit()
        got = remove_manual_tag(s, pixel_hash=PH, tag_name="sunset", device_id="d1", now=NOW)
        assert got is False
        assert len(s.exec(select(PhotoTag)).all()) == 1


def test_add_empty_name_rejected(engine: Engine) -> None:
    with Session(engine) as s:
        _photo(s)
        with pytest.raises(ValueError):
            add_manual_tag(s, pixel_hash=PH, tag_name="   ", device_id="d1", now=NOW)
