"""``sortandpost.db.flags`` -- the "marked for editing" queue."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from sortandpost.db.flags import (
    EDIT_FLAG,
    clear_all_edit_flags,
    edit_flag_count,
    edit_flagged_hashes,
    set_edit_flag,
)
from sortandpost.db.models import Change, ChangeOp, Photo, PhotoPath, PhotoTag, Tag
from sortandpost.db.tags import add_manual_tag, embed_keywords

NOW = datetime(2026, 1, 2, tzinfo=UTC)
PH = "a" * 64
PH2 = "b" * 64


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


def test_mark_adds_reserved_tag_and_change(engine: Engine) -> None:
    with Session(engine) as s:
        _photo(s)
        changed = set_edit_flag(s, pixel_hash=PH, marked=True, device_id="d1", now=NOW)
        s.commit()
        assert changed is True

        tag = s.exec(select(Tag).where(Tag.name == EDIT_FLAG)).one()
        assert s.exec(select(PhotoTag).where(PhotoTag.tag_id == tag.id)).one().pixel_hash == PH
        change = s.exec(select(Change)).one()
        assert change.op == ChangeOp.tag_add
        assert change.new_value == EDIT_FLAG


def test_mark_twice_is_noop(engine: Engine) -> None:
    with Session(engine) as s:
        _photo(s)
        set_edit_flag(s, pixel_hash=PH, marked=True, device_id="d1", now=NOW)
        s.commit()
        again = set_edit_flag(s, pixel_hash=PH, marked=True, device_id="d1", now=NOW)
        s.commit()
        assert again is False
        assert len(s.exec(select(PhotoTag)).all()) == 1
        assert len(s.exec(select(Change)).all()) == 1


def test_unmark_removes_and_logs(engine: Engine) -> None:
    with Session(engine) as s:
        _photo(s)
        set_edit_flag(s, pixel_hash=PH, marked=True, device_id="d1", now=NOW)
        removed = set_edit_flag(s, pixel_hash=PH, marked=False, device_id="d1", now=NOW)
        s.commit()
        assert removed is True
        assert s.exec(select(PhotoTag)).all() == []
        last = s.exec(select(Change).order_by(Change.id)).all()[-1]  # type: ignore[arg-type]
        assert last.op == ChangeOp.tag_remove


def test_unmark_when_absent_is_noop(engine: Engine) -> None:
    with Session(engine) as s:
        _photo(s)
        assert set_edit_flag(s, pixel_hash=PH, marked=False, device_id="d1", now=NOW) is False


def test_flagged_hashes_and_count(engine: Engine) -> None:
    with Session(engine) as s:
        _photo(s, PH)
        _photo(s, PH2)
        set_edit_flag(s, pixel_hash=PH, marked=True, device_id="d1", now=NOW)
        s.commit()
        assert edit_flagged_hashes(s) == {PH}
        assert edit_flagged_hashes(s, [PH2]) == set()
        assert edit_flagged_hashes(s, []) == set()
        assert edit_flag_count(s) == 1


def test_clear_all_removes_every_flag_and_logs(engine: Engine) -> None:
    with Session(engine) as s:
        _photo(s, PH)
        _photo(s, PH2)
        set_edit_flag(s, pixel_hash=PH, marked=True, device_id="d1", now=NOW)
        set_edit_flag(s, pixel_hash=PH2, marked=True, device_id="d1", now=NOW)
        s.commit()

        n = clear_all_edit_flags(s, device_id="d1", now=NOW)
        s.commit()
        assert n == 2
        assert edit_flag_count(s) == 0
        assert edit_flagged_hashes(s) == set()
        removes = s.exec(select(Change).where(Change.op == ChangeOp.tag_remove)).all()
        assert {c.pixel_hash for c in removes} == {PH, PH2}


def test_clear_all_when_empty_is_zero(engine: Engine) -> None:
    with Session(engine) as s:
        _photo(s)
        assert clear_all_edit_flags(s, device_id="d1", now=NOW) == 0


def test_flag_is_never_embedded_into_files(engine: Engine, tmp_path: Path) -> None:
    jpg = tmp_path / "x.jpg"
    jpg.write_bytes(b"stub")
    with Session(engine) as s:
        _photo(s)
        s.add(PhotoPath(pixel_hash=PH, path=str(jpg), size=4, mtime_ns=1, present=True))
        add_manual_tag(s, pixel_hash=PH, tag_name="beach", device_id="d1", now=NOW)
        set_edit_flag(s, pixel_hash=PH, marked=True, device_id="d1", now=NOW)
        s.commit()

        calls: list[list[str]] = []

        class _Exif:
            def write_keywords(self, path: Path, keywords: list[str]) -> None:
                calls.append(list(keywords))

        embed_keywords(s, PH, _Exif())  # type: ignore[arg-type]
        assert calls == [["beach"]]  # "to-edit" stripped
