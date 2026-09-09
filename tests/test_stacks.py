"""``sortandpost.stacks`` -- read-time image stacking."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session

from sortandpost.config import StackingConfig
from sortandpost.db.models import Photo, PhotoPath
from sortandpost.stacks import build_stacks, normalize_stem

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("img", "img"),
        ("IMG", "img"),
        ("img copy", "img"),
        ("img Copy", "img"),
        ("img copy 2", "img"),
        ("img copy (3)", "img"),
        ("img (1)", "img"),
        ("Sunset (2)", "sunset"),
        ("copy img", "img"),
        ("copy of img", "img"),
        ("img - Copy", "img"),
        ("img - Copy (2)", "img"),
        ("img (1) copy", "img"),
        ("img copy copy", "img"),
        # bare trailing digits are NOT copy markers
        ("DSCF4460", "dscf4460"),
        ("DSCF4461", "dscf4461"),
        ("vacation-3", "vacation-3"),
        ("IMG_1234", "img_1234"),
        # degenerate: nothing left after stripping -> fall back to the stem
        ("copy", "copy"),
    ],
)
def test_normalize_stem(stem: str, expected: str) -> None:
    assert normalize_stem(stem) == expected


def _add(session: Session, pixel_hash: str, path: str, *, is_raw: bool, mtime_ns: int) -> None:
    session.add(
        Photo(
            pixel_hash=pixel_hash,
            primary_path=path,
            width=1,
            height=1,
            format="RAW" if is_raw else "JPEG",
            is_raw=is_raw,
            file_size=1,
            file_mtime_ns=mtime_ns,
            first_indexed_at=NOW,
            last_indexed_at=NOW,
        )
    )
    session.add(
        PhotoPath(pixel_hash=pixel_hash, path=path, size=1, mtime_ns=mtime_ns, present=True)
    )


ALL_ON = StackingConfig(enabled=True, raw_jpeg=True, similar_names=True)


def test_disabled_returns_empty(engine: Engine) -> None:
    with Session(engine) as s:
        _add(s, "a" * 64, "/lib/img.jpg", is_raw=False, mtime_ns=1)
        _add(s, "b" * 64, "/lib/img copy.jpg", is_raw=False, mtime_ns=2)
        s.commit()
        idx = build_stacks(s, StackingConfig(enabled=False))
        assert idx.rep_of == {}
        assert idx.size("a" * 64) == 1


def test_similar_names_group_and_representative_is_newest(engine: Engine) -> None:
    a, b, c = "a" * 64, "b" * 64, "c" * 64
    with Session(engine) as s:
        _add(s, a, "/lib/img.jpg", is_raw=False, mtime_ns=100)
        _add(s, b, "/lib/img copy.jpg", is_raw=False, mtime_ns=300)  # newest -> rep
        _add(s, c, "/lib/img (1).jpg", is_raw=False, mtime_ns=200)
        s.commit()
        idx = build_stacks(s, ALL_ON)

        assert idx.representative(a) == b
        assert idx.members(a) == [b, c, a]  # newest first
        assert idx.size(a) == 3
        assert idx.is_hidden_member(a) is True
        assert idx.is_hidden_member(b) is False


def test_different_bare_numbers_do_not_stack(engine: Engine) -> None:
    a, b = "a" * 64, "b" * 64
    with Session(engine) as s:
        _add(s, a, "/lib/DSCF4460.JPG", is_raw=False, mtime_ns=1)
        _add(s, b, "/lib/DSCF4461.JPG", is_raw=False, mtime_ns=2)
        s.commit()
        idx = build_stacks(s, ALL_ON)
        assert idx.rep_of == {}


def test_same_name_different_folder_do_not_stack(engine: Engine) -> None:
    a, b = "a" * 64, "b" * 64
    with Session(engine) as s:
        _add(s, a, "/lib/2023/img.jpg", is_raw=False, mtime_ns=1)
        _add(s, b, "/lib/2024/img.jpg", is_raw=False, mtime_ns=2)
        s.commit()
        idx = build_stacks(s, ALL_ON)
        assert idx.rep_of == {}


def test_raw_jpeg_pair(engine: Engine) -> None:
    raw, jpg = "a" * 64, "b" * 64
    with Session(engine) as s:
        _add(s, raw, "/lib/IMG_1.CR2", is_raw=True, mtime_ns=1)
        _add(s, jpg, "/lib/IMG_1.JPG", is_raw=False, mtime_ns=2)
        s.commit()

        idx = build_stacks(s, StackingConfig(enabled=True, raw_jpeg=True, similar_names=False))
        assert idx.members(raw) == [jpg, raw]

        # similar-only never stacks a RAW
        idx2 = build_stacks(s, StackingConfig(enabled=True, raw_jpeg=False, similar_names=True))
        assert idx2.rep_of == {}


def test_raw_plus_jpeg_plus_copy_all_one_stack_when_both_modes_on(engine: Engine) -> None:
    raw, jpg, cpy = "a" * 64, "b" * 64, "c" * 64
    with Session(engine) as s:
        _add(s, raw, "/lib/IMG_1.CR2", is_raw=True, mtime_ns=1)
        _add(s, jpg, "/lib/IMG_1.JPG", is_raw=False, mtime_ns=2)
        _add(s, cpy, "/lib/IMG_1 copy.JPG", is_raw=False, mtime_ns=3)
        s.commit()
        idx = build_stacks(s, ALL_ON)
        assert set(idx.members(raw)) == {raw, jpg, cpy}
        assert idx.representative(jpg) == cpy  # newest
