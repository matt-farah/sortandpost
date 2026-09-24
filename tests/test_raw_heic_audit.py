"""P6: RAW/HEIC no-write audit -- verify constraints #1, #2 and #3 end to end.

RAW files are read-only (ratings/tags live in the DB only, never in or beside
the file) on every user write path: single rating, stacked rating (a RAW paired
with its same-named JPEG), bulk rating, manual tags, bulk tags, and CLIP
approval. Each RAW test snapshots the whole library folder (every file's bytes +
mtime, hidden files included) before and after, so a stray sidecar, exiftool
temp file or ``._`` AppleDouble would fail the audit too. HEIC ratings
round-trip and the pixel_hash is stable across our own EXIF writes.

Uses real fixtures from ``~/Downloads/sampleImages``; skipped when they're absent.
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy.engine import Engine
from sqlmodel import Session

from sortandpost.app import _apply_bulk_rating, _apply_bulk_tag, _apply_rating
from sortandpost.clip.tagging import approve
from sortandpost.config import Settings
from sortandpost.db import init_db, make_engine
from sortandpost.db.models import Photo, PhotoPath, Prediction, Rating, Tag
from sortandpost.db.ratings import set_rating
from sortandpost.db.tags import add_manual_tag, photo_keywords
from sortandpost.hashing import pixel_hash
from sortandpost.metadata import ExiftoolClient, is_raw
from sortandpost.metadata.shared import EXIFTOOL

pytestmark = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not on PATH")

SAMPLE_CR2 = Path.home() / "Downloads" / "sampleImages" / "RAWs" / "IMG_4909.CR2"
SAMPLE_HEIC = Path.home() / "Downloads" / "sampleImages" / "HEIC" / "IMG_8709.HEIC"

needs_cr2 = pytest.mark.skipif(
    not SAMPLE_CR2.is_file(), reason=f"Sample RAW fixture not found at {SAMPLE_CR2}"
)
needs_heic = pytest.mark.skipif(
    not SAMPLE_HEIC.is_file(), reason=f"Sample HEIC fixture not found at {SAMPLE_HEIC}"
)

DEVICE = "test-device"
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)

FolderSnapshot = dict[str, tuple[str, int]]


def snapshot_folder(root: Path) -> FolderSnapshot:
    """Every file under *root* (hidden ones included) -> (sha256, mtime_ns)."""
    return {
        str(p.relative_to(root)): (
            hashlib.sha256(p.read_bytes()).hexdigest(),
            p.stat().st_mtime_ns,
        )
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def assert_only_changed(before: FolderSnapshot, after: FolderSnapshot, *changed: str) -> None:
    """No file appeared or vanished, and only *changed* differ in bytes/mtime."""
    assert sorted(after) == sorted(before), "library folder listing changed"
    differs = sorted(name for name in before if before[name] != after[name])
    assert differs == sorted(changed)


def index_file(session: Session, path: Path) -> str:
    """Insert a ``photos`` + ``photo_paths`` row for *path*; return its pixel_hash."""
    ph = pixel_hash(path)
    with Image.open(path) as img:
        width, height = img.size
    st = path.stat()
    session.add(
        Photo(
            pixel_hash=ph,
            primary_path=str(path),
            width=width,
            height=height,
            format=path.suffix.lstrip(".").upper(),
            is_raw=is_raw(path),
            file_size=st.st_size,
            file_mtime_ns=st.st_mtime_ns,
            first_indexed_at=NOW,
            last_indexed_at=NOW,
        )
    )
    session.add(PhotoPath(pixel_hash=ph, path=str(path), size=st.st_size, mtime_ns=st.st_mtime_ns))
    session.commit()
    return ph


@pytest.fixture
def library(tmp_path: Path) -> Path:
    lib = tmp_path / "library"
    lib.mkdir()
    return lib


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Engine]:
    engine = make_engine(tmp_path / "db.sqlite")
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def raw_jpeg_pair(library: Path, db: Engine) -> tuple[str, str]:
    """``IMG_4909.CR2`` + a same-named ``IMG_4909.JPG`` (a RAW+JPEG stack);
    returns their (raw, jpeg) pixel_hashes."""
    cr2 = library / SAMPLE_CR2.name
    shutil.copy2(SAMPLE_CR2, cr2)
    jpg = library / f"{SAMPLE_CR2.stem}.JPG"
    Image.new("RGB", (64, 48), "red").save(jpg, "JPEG")
    with Session(db) as session:
        return index_file(session, cr2), index_file(session, jpg)


@pytest.fixture
def stacked_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """App settings with stacking on (RAW+JPEG pairing), as the UI would run."""
    monkeypatch.setenv("APP_DATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("PORT", raising=False)
    settings = Settings.resolve()
    settings.config.stacking.enabled = True
    settings.config.stacking.raw_jpeg = True
    yield settings
    EXIFTOOL.close()


# -- RAW: every user write path leaves the file alone ---------------------- #


@needs_cr2
def test_raw_cr2_not_modified_by_rating(library: Path, db: Engine) -> None:
    cr2 = library / SAMPLE_CR2.name
    shutil.copy2(SAMPLE_CR2, cr2)
    with Session(db) as session:
        ph = index_file(session, cr2)
    before = snapshot_folder(library)

    with Session(db) as session, ExiftoolClient() as exiftool:
        set_rating(session, pixel_hash=ph, stars=4, device_id=DEVICE, now=NOW, exiftool=exiftool)
        session.commit()

    assert_only_changed(before, snapshot_folder(library))
    with Session(db) as session:
        row = session.get(Rating, ph)
        assert row is not None and row.stars == 4


@needs_cr2
def test_stacked_rating_writes_jpeg_but_not_raw(
    library: Path, db: Engine, raw_jpeg_pair: tuple[str, str], stacked_settings: Settings
) -> None:
    """The UI path: rating the JPEG of a RAW+JPEG stack fans out to the RAW
    (DB only) and embeds into the JPEG only."""
    raw_ph, jpg_ph = raw_jpeg_pair
    before = snapshot_folder(library)

    assert _apply_rating(stacked_settings, db, jpg_ph, 4)

    jpg_name = f"{SAMPLE_CR2.stem}.JPG"
    assert_only_changed(before, snapshot_folder(library), jpg_name)
    with Session(db) as session:
        raw_row = session.get(Rating, raw_ph)
        assert raw_row is not None and raw_row.stars == 4, "stack fan-out didn't reach the RAW"
    with ExiftoolClient() as exiftool:
        assert exiftool.read(library / jpg_name).rating == 4


@needs_cr2
def test_bulk_rating_and_bulk_tag_skip_raw(
    library: Path, db: Engine, raw_jpeg_pair: tuple[str, str], stacked_settings: Settings
) -> None:
    raw_ph, _jpg_ph = raw_jpeg_pair
    before = snapshot_folder(library)

    _apply_bulk_rating(stacked_settings, db, [raw_ph], 3)
    _apply_bulk_tag(stacked_settings, db, [raw_ph], "sunset")

    jpg_name = f"{SAMPLE_CR2.stem}.JPG"
    assert_only_changed(before, snapshot_folder(library), jpg_name)
    with Session(db) as session:
        raw_row = session.get(Rating, raw_ph)
        assert raw_row is not None and raw_row.stars == 3
        assert photo_keywords(session, raw_ph) == ["sunset"]
    with ExiftoolClient() as exiftool:
        md = exiftool.read(library / jpg_name)
    assert md.rating == 3
    assert "sunset" in md.keywords


@needs_cr2
def test_raw_cr2_not_modified_by_manual_tag(library: Path, db: Engine) -> None:
    cr2 = library / SAMPLE_CR2.name
    shutil.copy2(SAMPLE_CR2, cr2)
    with Session(db) as session:
        ph = index_file(session, cr2)
    before = snapshot_folder(library)

    with Session(db) as session, ExiftoolClient() as exiftool:
        assert add_manual_tag(
            session, pixel_hash=ph, tag_name="beach", device_id=DEVICE, now=NOW, exiftool=exiftool
        )
        session.commit()

    assert_only_changed(before, snapshot_folder(library))
    with Session(db) as session:
        assert photo_keywords(session, ph) == ["beach"]


@needs_cr2
def test_raw_cr2_not_modified_by_clip_approval(library: Path, db: Engine) -> None:
    cr2 = library / SAMPLE_CR2.name
    shutil.copy2(SAMPLE_CR2, cr2)
    with Session(db) as session:
        ph = index_file(session, cr2)
        session.add(Tag(name="test-tag", facet="test"))
        session.add(
            Prediction(
                pixel_hash=ph,
                tag_name="test-tag",
                score=0.95,
                model="ViT-B-32",
                run_at=NOW,
                state="pending",
            )
        )
        session.commit()
    before = snapshot_folder(library)

    with Session(db) as session, ExiftoolClient() as exiftool:
        approve(
            session,
            device_id=DEVICE,
            model="ViT-B-32",
            pixel_hash=ph,
            tag_name="test-tag",
            facet="test",
            now=NOW,
            exiftool=exiftool,
        )
        session.commit()

    assert_only_changed(before, snapshot_folder(library))


@needs_cr2
def test_raw_files_are_correctly_identified() -> None:
    """Sanity check: the fixture really is treated as RAW."""
    assert is_raw(SAMPLE_CR2)


# -- HEIC: ratings round-trip, pixel_hash is stable ------------------------ #


@needs_heic
def test_heic_rating_round_trip(library: Path, db: Engine) -> None:
    heic = library / SAMPLE_HEIC.name
    shutil.copy2(SAMPLE_HEIC, heic)
    with Session(db) as session:
        ph = index_file(session, heic)
    before = snapshot_folder(library)

    with Session(db) as session, ExiftoolClient() as exiftool:
        set_rating(session, pixel_hash=ph, stars=5, device_id=DEVICE, now=NOW, exiftool=exiftool)
        session.commit()

    # Written in place: same listing, only the HEIC itself changed.
    assert_only_changed(before, snapshot_folder(library), heic.name)
    with ExiftoolClient() as exiftool:
        assert exiftool.read(heic).rating == 5, "HEIC rating did not round-trip"
    assert pixel_hash(heic) == ph, "pixel_hash changed after EXIF write (constraint #3)"


@needs_heic
def test_heic_pixel_hash_stable_across_rating_writes(tmp_path: Path) -> None:
    heic = tmp_path / SAMPLE_HEIC.name
    shutil.copy2(SAMPLE_HEIC, heic)
    ph_before = pixel_hash(heic)

    with ExiftoolClient() as exiftool:
        for stars in (1, 2, 3, 4, 5, 0):
            exiftool.write_rating(heic, stars)
            assert pixel_hash(heic) == ph_before, f"pixel_hash changed after rating {stars}"
