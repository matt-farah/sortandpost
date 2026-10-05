from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy.engine import Engine
from sqlmodel import Session

from sortandpost.db.models import Photo, PhotoPath
from sortandpost.post_export import (
    IgSpec,
    export_jpeg,
    export_key,
    fit_and_pad,
    is_instagram_ratio,
    parse_color,
    resolve_ig_spec,
    resolve_sources,
    upright_sizes,
)
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


# --- Instagram sizing --------------------------------------------------------
RED = (200, 30, 30)
WHITE = (255, 255, 255)


@pytest.mark.parametrize("size", [(2000, 800), (800, 2000), (1080, 1350), (100, 100)])
def test_fit_and_pad_exact_canvas(size: tuple[int, int]) -> None:
    out = fit_and_pad(Image.new("RGB", size, RED), 1080, 1350, 0, WHITE)
    assert out.size == (1080, 1350)


def test_fit_and_pad_centres_and_pads() -> None:
    out = fit_and_pad(Image.new("RGB", (2000, 1000), RED), 1080, 1350, 0, (0, 0, 255))
    assert out.getpixel((540, 675)) == RED  # centre is the photo
    assert out.getpixel((540, 5)) == (0, 0, 255)  # top is pad colour
    assert out.getpixel((540, 1345)) == (0, 0, 255)
    assert out.getpixel((2, 675)) == RED  # width-limited: touches the sides


def test_fit_and_pad_border_insets_content() -> None:
    out = fit_and_pad(Image.new("RGB", (1080, 1350), RED), 1080, 1350, 40, WHITE)
    assert out.getpixel((5, 5)) == WHITE
    assert out.getpixel((540, 675)) == RED


def test_is_instagram_ratio() -> None:
    assert is_instagram_ratio(1080, 1350)  # 4:5
    assert is_instagram_ratio(1000, 1000)
    assert is_instagram_ratio(1910, 1000)  # 1.91:1
    assert is_instagram_ratio(1079, 1349)  # rounding
    assert not is_instagram_ratio(1000, 1500)  # 2:3 is too tall
    assert not is_instagram_ratio(3000, 1000)  # 3:1 too wide
    assert not is_instagram_ratio(3000, 2000)  # 3:2 is between ratios: gets sized
    assert not is_instagram_ratio(1000, 900)
    assert is_instagram_ratio(1080, 1920, story=True)
    assert not is_instagram_ratio(1000, 1000, story=True)
    assert not is_instagram_ratio(0, 10)


def test_resolve_ig_spec() -> None:
    ok = [(1080, 1350), (1000, 1000)]
    assert resolve_ig_spec(ok, fmt="square", border=0, rgb=WHITE, story=False) is None
    mixed = [*ok, (3000, 1000)]
    assert resolve_ig_spec(mixed, fmt="square", border=10, rgb=WHITE, story=False) == IgSpec(
        1080, 1080, 10, WHITE
    )
    # Story format only for story posts
    assert resolve_ig_spec(mixed, fmt="story", border=0, rgb=WHITE, story=False) == IgSpec(
        1080, 1350, 0, WHITE
    )
    assert resolve_ig_spec(mixed, fmt="portrait", border=0, rgb=WHITE, story=True) == IgSpec(
        1080, 1920, 0, WHITE
    )
    bogus = resolve_ig_spec(mixed, fmt="bogus", border=0, rgb=WHITE, story=False)
    assert bogus is not None and bogus.height == 1350


def test_export_with_ig_spec_has_exact_size_and_no_exif(tmp_path: Path) -> None:
    src = tmp_path / "lib" / "a.jpg"
    src.parent.mkdir()
    _jpeg_with_gps(src, size=(600, 400))
    dest = tmp_path / "out" / "a.jpg"
    size = export_jpeg(str(src), str(dest), 2048, 90, IgSpec(1080, 1350, 20, (0, 0, 0)))
    assert size == (1080, 1350)
    with Image.open(dest) as im:
        assert im.size == (1080, 1350)
        assert not im.getexif()


def test_export_key_varies_with_ig_spec() -> None:
    base = export_key(2048, 90)
    a = export_key(2048, 90, IgSpec(1080, 1350, 0, WHITE))
    assert a != base
    assert a != export_key(2048, 90, IgSpec(1080, 1080, 0, WHITE))
    assert a != export_key(2048, 90, IgSpec(1080, 1350, 10, WHITE))
    assert a != export_key(2048, 90, IgSpec(1080, 1350, 0, (0, 0, 0)))


def test_parse_color() -> None:
    assert parse_color("#0a0b0c") == (10, 11, 12)
    assert parse_color("nope") == WHITE


def test_upright_sizes_swaps_for_rotated_orientation(engine: Engine) -> None:
    now = utcnow()
    with Session(engine) as s:
        for i, o in enumerate((1, 6)):
            s.add(
                Photo(
                    pixel_hash=f"{i + 1:064x}",
                    primary_path=f"/x{i}.jpg",
                    width=400,
                    height=200,
                    format="JPEG",
                    orientation=o,
                    file_size=1,
                    file_mtime_ns=1,
                    first_indexed_at=now,
                    last_indexed_at=now,
                )
            )
        s.commit()
    got = upright_sizes(engine, [f"{1:064x}", f"{2:064x}", "f" * 64])
    assert got == {f"{1:064x}": (400, 200), f"{2:064x}": (200, 400)}
