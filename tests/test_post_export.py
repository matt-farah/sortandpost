from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy.engine import Engine
from sqlmodel import Session

from sortandpost.db.models import Photo, PhotoPath
from sortandpost.post_export import export_jpeg, export_key, resolve_sources
from sortandpost.sync.clock import utcnow

SAMPLE_CR2 = Path.home() / "Downloads" / "sampleImages" / "RAWs" / "IMG_4909.CR2"


def _jpeg_with_gps(path: Path, size: tuple[int, int] = (400, 200), orientation: int = 1) -> None:
    img = Image.new("RGB", size, (200, 30, 30))
    exif = Image.Exif()
    exif[0x0112] = orientation  # Orientation
    exif[0x010F] = "CameraMaker"
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (37.0, 46.0, 30.0)
    img.save(path, "JPEG", exif=exif.tobytes(), quality=95)


def _fingerprint(p: Path) -> tuple[str, int]:
    return hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns


def test_export_strips_metadata_and_scales(tmp_path: Path) -> None:
    src = tmp_path / "lib" / "a.jpg"
    src.parent.mkdir()
    _jpeg_with_gps(src, size=(4000, 2000))
    before = _fingerprint(src)
    out = tmp_path / "out" / "00_a.jpg"
    assert export_jpeg(str(src), str(out), 2048, 90) == (2048, 1024)
    assert _fingerprint(src) == before  # source untouched
    with Image.open(out) as im:
        assert im.format == "JPEG" and im.size == (2048, 1024)
        exif = im.getexif()
        assert len(exif) == 0
        assert "icc_profile" not in im.info
    assert b"CameraMaker" not in out.read_bytes()
    assert not list(out.parent.glob("*.tmp-*"))


def test_export_applies_orientation(tmp_path: Path) -> None:
    src = tmp_path / "rot.jpg"
    _jpeg_with_gps(src, size=(400, 200), orientation=6)  # rotate 90 CW on display
    out = tmp_path / "rot_out.jpg"
    assert export_jpeg(str(src), str(out), 0, 90) == (200, 400)


def test_export_full_resolution_and_flattens_alpha(tmp_path: Path) -> None:
    src = tmp_path / "a.png"
    Image.new("RGBA", (300, 100), (0, 0, 0, 0)).save(src)
    out = tmp_path / "a_out.jpg"
    assert export_jpeg(str(src), str(out), 0, 80) == (300, 100)
    with Image.open(out) as im:
        assert im.mode == "RGB"
        assert im.getpixel((5, 5)) == (255, 255, 255)


@pytest.mark.skipif(not SAMPLE_CR2.is_file(), reason="sample CR2 not present")
def test_export_raw_leaves_source_untouched(tmp_path: Path) -> None:
    cr2 = tmp_path / SAMPLE_CR2.name
    shutil.copy2(SAMPLE_CR2, cr2)
    before = _fingerprint(cr2)
    listing = sorted(p.name for p in tmp_path.iterdir())
    out = tmp_path.parent / f"{tmp_path.name}-out" / "raw.jpg"
    w, h = export_jpeg(str(cr2), str(out), 1024, 85)
    assert max(w, h) == 1024
    assert _fingerprint(cr2) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == listing  # no sidecars


def test_export_key() -> None:
    assert export_key(2048, 90) == "2048-90"


def test_resolve_sources_prefers_primary_and_skips_missing(tmp_path: Path, engine: Engine) -> None:
    a1, a2, b = tmp_path / "a1.jpg", tmp_path / "a2.jpg", tmp_path / "b.jpg"
    for f in (a1, a2):
        f.write_bytes(b"x")
    now = utcnow()
    ha, hb, hc = "a" * 64, "b" * 64, "c" * 64
    with Session(engine) as s:
        for ph, primary in ((ha, a2), (hb, b)):
            s.add(
                Photo(
                    pixel_hash=ph,
                    primary_path=str(primary),
                    width=1,
                    height=1,
                    format="JPEG",
                    file_size=1,
                    file_mtime_ns=1,
                    first_indexed_at=now,
                    last_indexed_at=now,
                )
            )
        s.add(PhotoPath(pixel_hash=ha, path=str(a1), size=1, mtime_ns=1, present=True))
        s.add(PhotoPath(pixel_hash=ha, path=str(a2), size=1, mtime_ns=1, present=True))
        # b's row says present but the file is gone (offline drive): unavailable
        s.add(PhotoPath(pixel_hash=hb, path=str(b), size=1, mtime_ns=1, present=True))
        s.commit()
    assert resolve_sources(engine, [ha, hb, hc]) == {ha: str(a2), hb: None, hc: None}
