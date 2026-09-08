"""Perceptual hashing + near-duplicate clustering."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from PIL import Image
from sqlmodel import Session

from sortandpost.db import init_db, make_engine
from sortandpost.db.models import Photo
from sortandpost.dedup import _popcount64, dhash, find_duplicate_groups, hamming

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _gradient(w: int, h: int, *, shift: int = 0) -> Image.Image:
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = ((x * 7 + shift) % 256, (y * 11) % 256, ((x + y) * 3) % 256)
    return img


def test_dhash_is_16_hex_and_deterministic() -> None:
    img = _gradient(64, 64)
    h1 = dhash(img)
    h2 = dhash(img.copy())
    assert h1 == h2
    assert len(h1) == 16 and int(h1, 16) >= 0


def test_dhash_survives_a_resize() -> None:
    img = _gradient(200, 160)
    resized = img.resize((90, 72)).resize((200, 160))  # lossy round-trip
    assert hamming(dhash(img), dhash(resized)) <= 6


def test_dhash_differs_for_unrelated_images() -> None:
    a = dhash(_gradient(64, 64))
    b = dhash(Image.new("RGB", (64, 64), (255, 255, 255)))
    assert hamming(a, b) >= 12


def test_hamming_matches_manual_popcount() -> None:
    assert hamming("0000000000000000", "000000000000000f") == 4
    assert hamming("ffffffffffffffff", "fffffffffffffff0") == 4
    assert hamming("abcdef0123456789", "abcdef0123456789") == 0


def test_popcount64_vectorised() -> None:
    import numpy as np

    xs = np.array([0, 1, 0xF, 0xFF, 2**63, 2**64 - 1], dtype=np.uint64)
    assert list(_popcount64(xs)) == [0, 1, 4, 8, 1, 64]


def _photo(s: Session, tag: str, phash: str | None, *, missing: bool = False) -> None:
    ph = (tag * 64)[:64]
    s.add(
        Photo(
            pixel_hash=ph,
            primary_path=f"/lib/{tag}.jpg",
            width=1,
            height=1,
            format="JPEG",
            file_size=1,
            file_mtime_ns=1,
            first_indexed_at=NOW,
            last_indexed_at=NOW,
            phash=phash,
            missing_since=NOW if missing else None,
        )
    )


def test_find_duplicate_groups_clusters_within_distance(tmp_path: Path) -> None:
    engine = make_engine(tmp_path / "db.sqlite")
    init_db(engine)
    with Session(engine) as s:
        _photo(s, "a", "00000000000000ff")  # a/b identical
        _photo(s, "b", "00000000000000ff")
        _photo(s, "c", "00000000000000fd")  # 1 bit from a/b
        _photo(s, "d", "ffffffffffffffff")  # far away
        _photo(s, "e", None)  # no phash -> ignored
        s.commit()

        groups = find_duplicate_groups(s, max_distance=2)
        assert groups == [[("a" * 64)[:64], ("b" * 64)[:64], ("c" * 64)[:64]]]

        # tighter threshold splits c off
        tight = find_duplicate_groups(s, max_distance=0)
        assert tight == [[("a" * 64)[:64], ("b" * 64)[:64]]]


def test_find_duplicate_groups_skips_missing(tmp_path: Path) -> None:
    engine = make_engine(tmp_path / "db.sqlite")
    init_db(engine)
    with Session(engine) as s:
        _photo(s, "a", "00000000000000ff")
        _photo(s, "b", "00000000000000ff", missing=True)
        s.commit()
        assert find_duplicate_groups(s, max_distance=0) == []
        with_missing = find_duplicate_groups(s, max_distance=0, include_missing=True)
        assert with_missing == [[("a" * 64)[:64], ("b" * 64)[:64]]]
