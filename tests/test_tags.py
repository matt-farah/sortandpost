"""``sortandpost.db.tags`` -- manual tag add/remove + changes-log rows."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from sortandpost.db import add_manual_tag, remove_manual_tag
from sortandpost.db.models import Change, ChangeOp, Photo, PhotoTag, Tag, TagSource
from sortandpost.db.tags import embeddable_keywords, move_tags, photo_keywords

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


# A line break in a tag name would become extra exiftool arguments on its
# -stay_open stdin (-Directory= moves the file, -if runs Perl) -- see
# sortandpost.tag_names.
def test_add_collapses_line_breaks_in_the_name(engine: Engine) -> None:
    with Session(engine) as s:
        _photo(s)
        add_manual_tag(
            s, pixel_hash=PH, tag_name="sunset\n-Directory=/tmp/x", device_id="d1", now=NOW
        )
        assert [t.name for t in s.exec(select(Tag)).all()] == ["sunset -Directory=/tmp/x"]


def test_add_rejects_control_characters(engine: Engine) -> None:
    with Session(engine) as s:
        _photo(s)
        with pytest.raises(ValueError):
            add_manual_tag(s, pixel_hash=PH, tag_name="sun\x00set", device_id="d1", now=NOW)


def test_add_rejects_overlong_names(engine: Engine) -> None:
    with Session(engine) as s:
        _photo(s)
        add_manual_tag(s, pixel_hash=PH, tag_name="x" * 100, device_id="d1", now=NOW)
        with pytest.raises(ValueError):
            add_manual_tag(s, pixel_hash=PH, tag_name="x" * 101, device_id="d1", now=NOW)


def test_embeddable_keywords_keeps_an_older_long_tag() -> None:
    # the length cap is for new tags only -- a file keeps a long one it already had
    assert embeddable_keywords(["y" * 300]) == ["y" * 300]


def test_embeddable_keywords_drops_unsafe_names_from_older_or_merged_rows() -> None:
    assert embeddable_keywords(["beach", "x\n-if\n1", "to-edit", "a\u2028b"]) == ["beach"]


def test_move_tags_keeps_source_skips_reserved_and_logs(engine: Engine) -> None:
    dst = "b" * 64
    with Session(engine) as s:
        _photo(s)
        _photo(s, dst)
        add_manual_tag(s, pixel_hash=PH, tag_name="beach", device_id="d1", now=NOW)
        add_manual_tag(s, pixel_hash=PH, tag_name="to-edit", device_id="d1", now=NOW)
        add_manual_tag(s, pixel_hash=dst, tag_name="dog", device_id="d1", now=NOW)
        red = Tag(name="red")
        s.add(red)
        s.flush()
        assert red.id is not None
        s.add(
            PhotoTag(
                pixel_hash=PH,
                tag_id=red.id,
                source=TagSource.clip,
                added_at=NOW,
                added_by_device="x",
            )
        )
        s.commit()

        moved = move_tags(s, src=PH, dst=dst, device_id="d2", now=NOW)
        s.commit()

        assert sorted(moved) == ["beach", "red"]
        assert photo_keywords(s, PH) == ["to-edit"]  # the workflow marker stays
        assert photo_keywords(s, dst) == ["beach", "dog", "red"]
        red_link = s.exec(
            select(PhotoTag).where(PhotoTag.pixel_hash == dst, PhotoTag.tag_id == red.id)
        ).one()
        assert red_link.source == TagSource.clip
        ops = {
            (c.op, c.pixel_hash, c.field)
            for c in s.exec(select(Change)).all()
            if c.device_id == "d2"
        }
        assert ops == {
            (ChangeOp.tag_add, dst, "beach"),
            (ChangeOp.clip_approve, dst, "red"),
            (ChangeOp.tag_remove, PH, "beach"),
            (ChangeOp.tag_remove, PH, "red"),
        }
