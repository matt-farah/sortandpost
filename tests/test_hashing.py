from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest
from PIL import Image

from sortandpost.hashing import pixel_hash

_HAS_EXIFTOOL = shutil.which("exiftool") is not None


def _write_jpeg(path: Path, size: tuple[int, int] = (80, 60)) -> None:
    """A deterministic gradient so the decoded pixel buffer is reproducible."""
    w, h = size
    img = Image.new("RGB", size)
    px = img.load()
    assert px is not None
    for y in range(h):
        for x in range(w):
            px[x, y] = (x * 3 % 256, y * 5 % 256, (x + y) % 256)
    img.save(path, "JPEG", quality=92)


def test_pixel_hash_is_deterministic(tmp_path: Path) -> None:
    a, b = tmp_path / "a.jpg", tmp_path / "b.jpg"
    _write_jpeg(a)
    _write_jpeg(b)
    assert pixel_hash(a) == pixel_hash(b)
    assert len(pixel_hash(a)) == 64


def test_pixel_hash_changes_with_pixels(tmp_path: Path) -> None:
    a, b = tmp_path / "a.jpg", tmp_path / "b.jpg"
    _write_jpeg(a, (80, 60))
    _write_jpeg(b, (81, 60))
    assert pixel_hash(a) != pixel_hash(b)


@pytest.mark.skipif(not _HAS_EXIFTOOL, reason="exiftool not on PATH")
def test_pixel_hash_stable_across_exif_write(tmp_path: Path) -> None:
    from sortandpost.metadata import ExiftoolClient

    photo = tmp_path / "IMG_0001.jpg"
    _write_jpeg(photo)

    before_hash = pixel_hash(photo)
    before_ino = os.stat(photo).st_ino
    before_mtime = os.stat(photo).st_mtime_ns
    time.sleep(0.01)

    with ExiftoolClient() as et:
        et.write_rating(photo, 5)
        et.write_keywords(photo, ["alpha", "beta"])

    # The pixels are untouched...
    assert pixel_hash(photo) == before_hash
    # ...the file is edited in place (no new inode, hard constraint #1)...
    assert os.stat(photo).st_ino == before_ino
    # ...but the mtime moves so sync tools notice.
    assert os.stat(photo).st_mtime_ns != before_mtime

    with ExiftoolClient() as et:
        md = et.read(photo)
    assert md.rating == 5
    assert set(md.keywords) == {"alpha", "beta"}
