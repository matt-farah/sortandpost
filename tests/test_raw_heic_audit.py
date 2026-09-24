"""P6: RAW/HEIC no-write audit — verify constraint #2.

RAW files are read-only (ratings/tags live in DB only, never in the file).
HEIC files support rating round-trips and pixel_hash stability.
"""

from __future__ import annotations

import hashlib
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image
from sqlmodel import Session

from sortandpost.clip.tagging import approve
from sortandpost.db import init_db, make_engine
from sortandpost.db.models import Photo, PhotoPath, Prediction, Rating, Tag
from sortandpost.db.ratings import set_rating
from sortandpost.hashing import pixel_hash
from sortandpost.metadata import ExiftoolClient, is_raw

pytestmark = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not on PATH")

# Paths to real fixtures from ~/Downloads/sampleImages
SAMPLE_CR2 = Path.home() / "Downloads" / "sampleImages" / "RAWs" / "IMG_4909.CR2"
SAMPLE_HEIC = Path.home() / "Downloads" / "sampleImages" / "HEIC" / "IMG_8709.HEIC"


def file_hash_and_mtime(path: Path) -> tuple[str, int]:
    """Return (SHA256 of file bytes, mtime_ns)."""
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    h = hashlib.sha256(path.read_bytes()).hexdigest()
    mtime_ns = int(path.stat().st_mtime_ns)
    return h, mtime_ns


def assert_file_unchanged(path: Path, baseline_hash: str, baseline_mtime: int) -> None:
    """Assert file bytes and mtime haven't changed."""
    current_hash, current_mtime = file_hash_and_mtime(path)
    assert current_hash == baseline_hash, f"File bytes changed: {path}"
    assert current_mtime == baseline_mtime, f"File mtime changed: {path}"


def assert_no_sidecars(path: Path) -> None:
    """Assert no sidecar files (*.xmp, etc.) were created beside the file."""
    parent = path.parent
    stem = path.stem
    sidecars = list(parent.glob(f"{stem}.*")) + list(parent.glob(f"{stem}-*"))
    # Filter out the original file
    sidecars = [p for p in sidecars if p != path]
    assert not sidecars, f"Sidecar files were created: {sidecars}"


@pytest.mark.skipif(
    not SAMPLE_CR2.is_file(),
    reason=f"Sample RAW fixture not found at {SAMPLE_CR2}",
)
def test_raw_cr2_not_modified_by_rating(tmp_path: Path) -> None:
    """Verify constraint #2: CR2 files are never written to when rated."""
    # Copy the fixture into a temporary library
    lib = tmp_path / "library"
    lib.mkdir()
    test_cr2 = lib / SAMPLE_CR2.name
    shutil.copy2(SAMPLE_CR2, test_cr2)

    # Baseline: record the file's bytes and mtime
    baseline_hash, baseline_mtime = file_hash_and_mtime(test_cr2)

    # Create a DB and index this file
    engine = make_engine(tmp_path / "db.sqlite")
    init_db(engine)
    now = datetime.now(UTC)

    # Compute pixel hash
    ph = pixel_hash(test_cr2)

    # Get image dimensions
    with Image.open(test_cr2) as img:
        width, height = img.size

    # Insert the photo into the DB
    with Session(engine) as session:
        session.add(
            Photo(
                pixel_hash=ph,
                primary_path=str(test_cr2),
                width=width,
                height=height,
                format="CR2",
                file_size=test_cr2.stat().st_size,
                file_mtime_ns=int(test_cr2.stat().st_mtime_ns),
                first_indexed_at=now,
                last_indexed_at=now,
            )
        )
        session.add(
            PhotoPath(
                pixel_hash=ph,
                path=str(test_cr2),
                size=test_cr2.stat().st_size,
                mtime_ns=int(test_cr2.stat().st_mtime_ns),
                present=True,
            )
        )
        session.commit()

    # Rate the photo through the DB layer
    with Session(engine) as session, ExiftoolClient() as exiftool:
        set_rating(
            session,
            pixel_hash=ph,
            stars=4,
            device_id="test-device",
            now=now,
            exiftool=exiftool,  # this is the key: exiftool is used, but RAW should be skipped
        )
        session.commit()

    # Verify the file is unchanged
    assert_file_unchanged(test_cr2, baseline_hash, baseline_mtime)

    # Verify no sidecars were created
    assert_no_sidecars(test_cr2)

    # Verify the rating was recorded in the DB
    with Session(engine) as session:
        row = session.get(Rating, ph)
        assert row is not None and row.stars == 4


@pytest.mark.skipif(
    not SAMPLE_CR2.is_file(),
    reason=f"Sample RAW fixture not found at {SAMPLE_CR2}",
)
def test_raw_cr2_not_modified_by_clip_approval(tmp_path: Path) -> None:
    """Verify constraint #2: CR2 files are never written to when CLIP tags are approved."""

    # Copy the fixture
    lib = tmp_path / "library"
    lib.mkdir()
    test_cr2 = lib / SAMPLE_CR2.name
    shutil.copy2(SAMPLE_CR2, test_cr2)

    # Baseline
    baseline_hash, baseline_mtime = file_hash_and_mtime(test_cr2)

    # Create DB and index
    engine = make_engine(tmp_path / "db.sqlite")
    init_db(engine)
    now = datetime.now(UTC)

    # Compute pixel hash and get dimensions
    ph = pixel_hash(test_cr2)
    with Image.open(test_cr2) as img:
        width, height = img.size

    with Session(engine) as session:
        # Add photo
        session.add(
            Photo(
                pixel_hash=ph,
                primary_path=str(test_cr2),
                width=width,
                height=height,
                format="CR2",
                file_size=test_cr2.stat().st_size,
                file_mtime_ns=int(test_cr2.stat().st_mtime_ns),
                first_indexed_at=now,
                last_indexed_at=now,
            )
        )
        session.add(
            PhotoPath(
                pixel_hash=ph,
                path=str(test_cr2),
                size=test_cr2.stat().st_size,
                mtime_ns=int(test_cr2.stat().st_mtime_ns),
                present=True,
            )
        )

        # Add a CLIP prediction to approve
        tag = Tag(name="test-tag", facet="test")
        session.add(tag)
        session.flush()

        session.add(
            Prediction(
                pixel_hash=ph,
                tag_name="test-tag",
                score=0.95,
                model="ViT-B-32",
                run_at=now,
                state="pending",
            )
        )
        session.commit()

    # Call the real approve() function with exiftool active
    with Session(engine) as session, ExiftoolClient() as exiftool:
        approve(
            session,
            device_id="test-device",
            model="ViT-B-32",
            pixel_hash=ph,
            tag_name="test-tag",
            facet="test",
            now=now,
            exiftool=exiftool,
        )
        session.commit()

    # Verify the file is unchanged
    assert_file_unchanged(test_cr2, baseline_hash, baseline_mtime)
    assert_no_sidecars(test_cr2)


@pytest.mark.skipif(
    not SAMPLE_HEIC.is_file(),
    reason=f"Sample HEIC fixture not found at {SAMPLE_HEIC}",
)
def test_heic_rating_round_trip(tmp_path: Path) -> None:
    """Verify HEIC files support rating round-trips."""
    # Copy the fixture
    lib = tmp_path / "library"
    lib.mkdir()
    test_heic = lib / SAMPLE_HEIC.name
    shutil.copy2(SAMPLE_HEIC, test_heic)

    # Create DB and index
    engine = make_engine(tmp_path / "db.sqlite")
    init_db(engine)
    now = datetime.now(UTC)

    # Compute pixel hash and get dimensions
    ph = pixel_hash(test_heic)
    with Image.open(test_heic) as img:
        width, height = img.size

    with Session(engine) as session:
        session.add(
            Photo(
                pixel_hash=ph,
                primary_path=str(test_heic),
                width=width,
                height=height,
                format="HEIC",
                file_size=test_heic.stat().st_size,
                file_mtime_ns=int(test_heic.stat().st_mtime_ns),
                first_indexed_at=now,
                last_indexed_at=now,
            )
        )
        session.add(
            PhotoPath(
                pixel_hash=ph,
                path=str(test_heic),
                size=test_heic.stat().st_size,
                mtime_ns=int(test_heic.stat().st_mtime_ns),
                present=True,
            )
        )
        session.commit()

    # Rate the HEIC file
    with Session(engine) as session, ExiftoolClient() as exiftool:
        set_rating(
            session,
            pixel_hash=ph,
            stars=5,
            device_id="test-device",
            now=now,
            exiftool=exiftool,
        )
        session.commit()

    # Verify the rating was written to the file and can be read back
    with ExiftoolClient() as exiftool:
        md = exiftool.read(test_heic)
        assert md.rating == 5, "HEIC rating did not round-trip"

    # Verify pixel_hash is stable (hasn't changed after EXIF write)
    ph_after = pixel_hash(test_heic)
    assert ph == ph_after, "pixel_hash changed after EXIF write (constraint #3)"


@pytest.mark.skipif(
    not SAMPLE_HEIC.is_file(),
    reason=f"Sample HEIC fixture not found at {SAMPLE_HEIC}",
)
def test_heic_pixel_hash_stable_across_rating_writes(tmp_path: Path) -> None:
    """Verify constraint #3: pixel_hash is immune to our own EXIF writes."""
    # Copy the fixture
    test_heic = tmp_path / SAMPLE_HEIC.name
    shutil.copy2(SAMPLE_HEIC, test_heic)

    # Compute baseline pixel hash
    ph_before = pixel_hash(test_heic)

    # Write a rating through exiftool multiple times
    with ExiftoolClient() as exiftool:
        for stars in (1, 2, 3, 4, 5):
            exiftool.write_rating(test_heic, stars)
            ph_after = pixel_hash(test_heic)
            assert ph_before == ph_after, f"pixel_hash changed after writing rating {stars}"


@pytest.mark.skipif(
    not SAMPLE_CR2.is_file(),
    reason=f"Sample RAW fixture not found at {SAMPLE_CR2}",
)
def test_raw_files_are_correctly_identified(tmp_path: Path) -> None:
    """Sanity check: verify the test fixture is correctly identified as RAW."""
    lib = tmp_path / "library"
    lib.mkdir()
    test_cr2 = lib / SAMPLE_CR2.name
    shutil.copy2(SAMPLE_CR2, test_cr2)

    assert is_raw(test_cr2), f"CR2 file not identified as RAW: {test_cr2}"
    assert test_cr2.suffix.upper() == ".CR2"
