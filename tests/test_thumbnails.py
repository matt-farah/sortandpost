from __future__ import annotations

from pathlib import Path

from PIL import Image

from sortandpost.indexer.thumbnails import write_thumbnails


def _thumb_path_factory(root: Path):  # type: ignore[no-untyped-def]
    def thumb_path(pixel_hash: str, size: int, fmt: str) -> Path:
        return root / pixel_hash[:2] / f"{pixel_hash}_{size}.{fmt}"

    return thumb_path


def test_writes_requested_sizes_and_caps_long_edge(tmp_path: Path) -> None:
    img = Image.new("RGB", (2000, 1000), (200, 100, 50))
    thumb_path = _thumb_path_factory(tmp_path)

    written = write_thumbnails(img, "a" * 64, sizes=[320, 1280], thumb_path=thumb_path)
    assert written == (320, 1280)

    small = Image.open(thumb_path("a" * 64, 320, "webp"))
    assert max(small.size) == 320
    large = Image.open(thumb_path("a" * 64, 1280, "webp"))
    assert max(large.size) == 1280


def test_is_write_once(tmp_path: Path) -> None:
    img = Image.new("RGB", (100, 100), (0, 0, 0))
    thumb_path = _thumb_path_factory(tmp_path)

    assert write_thumbnails(img, "b" * 64, sizes=[64], thumb_path=thumb_path) == (64,)
    dest = thumb_path("b" * 64, 64, "webp")
    mtime = dest.stat().st_mtime_ns

    # second call finds the file already there and does nothing
    assert write_thumbnails(img, "b" * 64, sizes=[64], thumb_path=thumb_path) == ()
    assert dest.stat().st_mtime_ns == mtime


def test_bakes_in_exif_orientation(tmp_path: Path) -> None:
    # a portrait sensor read out landscape, tagged "rotate 90 CW" (orientation 6)
    landscape = Image.new("RGB", (120, 60), (255, 255, 255))
    src = tmp_path / "rot.jpg"
    exif = landscape.getexif()
    exif[0x0112] = 6
    landscape.save(src, "JPEG", exif=exif)

    with Image.open(src) as loaded:
        write_thumbnails(loaded, "c" * 64, sizes=[240], thumb_path=_thumb_path_factory(tmp_path))

    out = Image.open(tmp_path / "cc" / f"{'c' * 64}_240.webp")
    assert out.height > out.width  # rotated upright
