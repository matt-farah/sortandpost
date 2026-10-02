from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image

from sortandpost.metadata import (
    EmbeddedMetadata,
    ExiftoolClient,
    MetadataError,
    can_embed_metadata,
    is_raw,
    stars_to_rating_percent,
)

pytestmark = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not on PATH")


@pytest.fixture
def jpg(tmp_path: Path) -> Path:
    path = tmp_path / "IMG_0001.jpg"
    img = Image.new("RGB", (48, 32), (90, 140, 200))
    img.save(path, "JPEG", quality=90)
    return path


@pytest.fixture
def et() -> Iterator[ExiftoolClient]:
    with ExiftoolClient(write_windows_compat_tags=True) as client:
        yield client


def _raw_read(path: Path, *tags: str) -> dict[str, object]:
    out = subprocess.run(
        ["exiftool", "-G0", "-json", "-n", *tags, str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out)[0]  # type: ignore[no-any-return]


def test_fresh_image_has_no_rating_or_keywords(et: ExiftoolClient, jpg: Path) -> None:
    md = et.read(jpg)
    assert isinstance(md, EmbeddedMetadata)
    assert md.rating is None
    assert md.keywords == ()


def test_rating_round_trip(et: ExiftoolClient, jpg: Path) -> None:
    et.write_rating(jpg, 4)
    assert et.read(jpg).rating == 4
    et.write_rating(jpg, 2)
    assert et.read(jpg).rating == 2


def test_rating_zero_clears(et: ExiftoolClient, jpg: Path) -> None:
    et.write_rating(jpg, 5)
    assert et.read(jpg).rating == 5
    et.write_rating(jpg, 0)
    assert et.read(jpg).rating is None


def test_windows_compat_tags_mirror_rating(et: ExiftoolClient, jpg: Path) -> None:
    et.write_rating(jpg, 3)
    # -G0 collapses the Microsoft XMP schema to the "XMP" family-0 group.
    row = _raw_read(
        jpg,
        "-EXIF:Rating",
        "-EXIF:RatingPercent",
        "-XMP-microsoft:RatingPercent",
    )
    assert row["EXIF:Rating"] == 3
    assert row["EXIF:RatingPercent"] == 50
    assert row["XMP:RatingPercent"] == 50


def test_compat_tags_off(jpg: Path) -> None:
    with ExiftoolClient(write_windows_compat_tags=False) as client:
        client.write_rating(jpg, 3)
        assert client.read(jpg).rating == 3
    row = _raw_read(jpg, "-EXIF:Rating")
    assert "EXIF:Rating" not in row


def test_keywords_round_trip_and_replace(et: ExiftoolClient, jpg: Path) -> None:
    et.write_keywords(jpg, ["beach", "sunset", "beach"])  # duplicate dropped
    assert set(et.read(jpg).keywords) == {"beach", "sunset"}

    et.write_keywords(jpg, ["mountain"])  # replace, not append
    assert et.read(jpg).keywords == ("mountain",)

    et.write_keywords(jpg, [])  # clear
    assert et.read(jpg).keywords == ()


def test_keywords_written_to_both_xmp_and_iptc(et: ExiftoolClient, jpg: Path) -> None:
    et.write_keywords(jpg, ["forest"])
    row = _raw_read(jpg, "-XMP:Subject", "-IPTC:Keywords")
    assert row["XMP:Subject"] == "forest"
    assert row["IPTC:Keywords"] == "forest"


def test_read_reports_dimensions_free_fields(et: ExiftoolClient, jpg: Path) -> None:
    # exiftool writes these onto the file; make sure the reader surfaces them.
    et.write_rating(jpg, 1)
    subprocess.run(
        [
            "exiftool",
            "-overwrite_original_in_place",
            "-EXIF:Make=Canon",
            "-EXIF:Model=EOS R5",
            "-EXIF:DateTimeOriginal=2021:07:04 09:30:00",
            "-EXIF:Orientation#=6",
            "-GPSLatitude=37.5",
            "-GPSLatitudeRef=N",
            "-GPSLongitude=122.3",
            "-GPSLongitudeRef=W",
            str(jpg),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    md = et.read(jpg)
    assert md.camera_make == "Canon"
    assert md.camera_model == "EOS R5"
    assert md.orientation == 6
    assert md.taken_at is not None and md.taken_at.year == 2021
    assert md.gps_lat == pytest.approx(37.5)
    assert md.gps_lon == pytest.approx(-122.3)


def test_invalid_rating_rejected(et: ExiftoolClient, jpg: Path) -> None:
    with pytest.raises(MetadataError):
        et.write_rating(jpg, 7)
    with pytest.raises(MetadataError):
        et.write_rating(jpg, -1)


def test_raw_write_refused(et: ExiftoolClient, tmp_path: Path) -> None:
    raw = tmp_path / "DSC1234.NEF"
    raw.write_bytes(b"\x00 not really a raw file")
    with pytest.raises(MetadataError, match="RAW"):
        et.write_rating(raw, 3)
    with pytest.raises(MetadataError, match="RAW"):
        et.write_keywords(raw, ["x"])


def test_unsupported_format_refused(et: ExiftoolClient, tmp_path: Path) -> None:
    gif = tmp_path / "anim.gif"
    gif.write_bytes(b"GIF89a")
    with pytest.raises(MetadataError):
        et.write_rating(gif, 3)


def test_missing_file_refused(et: ExiftoolClient, tmp_path: Path) -> None:
    with pytest.raises(MetadataError):
        et.write_rating(tmp_path / "nope.jpg", 3)


def test_client_must_be_started(jpg: Path) -> None:
    client = ExiftoolClient()
    with pytest.raises(MetadataError):
        client.read(jpg)


def test_read_batch(et: ExiftoolClient, tmp_path: Path) -> None:
    paths = []
    for i in range(3):
        p = tmp_path / f"p{i}.jpg"
        Image.new("RGB", (16, 16), (i * 40, i * 40, i * 40)).save(p, "JPEG")
        paths.append(p)
    et.write_rating(paths[0], 5)
    et.write_rating(paths[2], 2)

    out = et.read_batch(paths)
    assert out[paths[0]].rating == 5
    assert out[paths[1]].rating is None
    assert out[paths[2]].rating == 2


def test_one_process_serves_many_calls(et: ExiftoolClient, jpg: Path) -> None:
    for stars in (1, 2, 3, 4, 5, 4, 3):
        et.write_rating(jpg, stars)
        assert et.read(jpg).rating == stars


def test_stars_to_rating_percent() -> None:
    assert [stars_to_rating_percent(s) for s in range(6)] == [0, 1, 25, 50, 75, 99]
    with pytest.raises(MetadataError):
        stars_to_rating_percent(6)


def test_suffix_helpers() -> None:
    assert is_raw(Path("a.CR2"))
    assert is_raw(Path("b.nef"))
    assert not is_raw(Path("c.jpg"))
    assert can_embed_metadata(Path("d.JPEG"))
    assert can_embed_metadata(Path("e.heic"))
    assert not can_embed_metadata(Path("f.gif"))


@pytest.mark.parametrize("breaker", ["\n", "\r", "\x00"])
def test_a_line_break_in_a_keyword_cannot_inject_exiftool_options(
    et: ExiftoolClient, jpg: Path, tmp_path: Path, breaker: str
) -> None:
    """-stay_open reads one argument per line; a smuggled ``-Directory=`` would
    move the photo out of the library (and ``-if`` would run Perl)."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with pytest.raises(MetadataError):
        et.write_keywords(jpg, [f"sunset{breaker}-Directory={elsewhere}"])
    assert jpg.exists()
    assert list(elsewhere.iterdir()) == []
    et.write_keywords(jpg, ["sunset"])  # the client is still usable afterwards
    assert et.read(jpg).keywords == ("sunset",)
