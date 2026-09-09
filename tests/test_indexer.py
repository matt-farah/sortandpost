from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image
from sqlmodel import Session, select

from sortandpost.config import Settings
from sortandpost.db import make_engine
from sortandpost.db.models import Photo, PhotoPath, ScanState
from sortandpost.indexer import scan_once
from sortandpost.indexer.pipeline import Stage

_HAS_EXIFTOOL = shutil.which("exiftool") is not None


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setenv("APP_DATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("PORT", raising=False)
    resolved = Settings.resolve()
    resolved.config.library_roots = [library]
    return resolved


def _lib(settings: Settings) -> Path:
    return Path(settings.config.library_roots[0])


def _jpeg(path: Path, color: tuple[int, int, int] = (120, 90, 200)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), color).save(path, "JPEG", quality=90)


def _session(settings: Settings) -> Session:
    return Session(make_engine(settings.paths.working_db))


def test_scan_once_populates_photos_and_thumbnails(settings: Settings) -> None:
    lib = _lib(settings)
    _jpeg(lib / "one.jpg", (10, 20, 30))
    _jpeg(lib / "sub" / "two.jpg", (200, 50, 50))

    stats = scan_once(settings)
    assert stats.seen == 2
    assert stats.indexed == 2
    assert stats.new_photos == 2

    with _session(settings) as s:
        photos = s.exec(select(Photo)).all()
        assert len(photos) == 2
        assert {p.width for p in photos} == {64}
        hashes = [p.pixel_hash for p in photos]
        for photo in photos:
            state = s.get(ScanState, photo.primary_path)
            assert state is not None
            assert (state.stages_done & Stage.BROWSABLE) == Stage.BROWSABLE

    for pixel_hash in hashes:
        for size in settings.config.thumbnails.sizes:
            assert settings.paths.thumbnail_file(pixel_hash, size).is_file()


def test_fast_path_skips_unchanged_files(settings: Settings) -> None:
    _jpeg(_lib(settings) / "a.jpg")
    _jpeg(_lib(settings) / "b.jpg", (1, 2, 3))

    first = scan_once(settings)
    assert first.indexed == 2

    second = scan_once(settings)
    assert second.seen == 2
    assert second.indexed == 0
    assert second.skipped == 2


def test_identical_bytes_at_two_paths_is_one_photo(settings: Settings) -> None:
    lib = _lib(settings)
    _jpeg(lib / "original.jpg", (77, 88, 99))
    shutil.copy2(lib / "original.jpg", lib / "copy.jpg")

    scan_once(settings)

    with _session(settings) as s:
        assert len(s.exec(select(Photo)).all()) == 1
        paths = s.exec(select(PhotoPath)).all()
        assert len(paths) == 2
        assert all(p.present for p in paths)


def test_move_repoints_paths_and_keeps_identity(settings: Settings) -> None:
    lib = _lib(settings)
    _jpeg(lib / "before.jpg", (5, 10, 15))
    scan_once(settings)
    with _session(settings) as s:
        original_hash = s.exec(select(Photo)).one().pixel_hash

    (lib / "before.jpg").rename(lib / "after.jpg")
    scan_once(settings)

    with _session(settings) as s:
        photo = s.exec(select(Photo)).one()
        assert photo.pixel_hash == original_hash
        assert photo.missing_since is None
        assert photo.primary_path.endswith("after.jpg")
        by_path = {Path(p.path).name: p.present for p in s.exec(select(PhotoPath)).all()}
        assert by_path == {"before.jpg": False, "after.jpg": True}


def test_deleted_file_marked_missing_not_purged(settings: Settings) -> None:
    lib = _lib(settings)
    _jpeg(lib / "keep.jpg", (1, 1, 1))
    _jpeg(lib / "drop.jpg", (2, 2, 2))
    scan_once(settings)

    (lib / "drop.jpg").unlink()
    stats = scan_once(settings)
    assert stats.marked_missing == 1

    with _session(settings) as s:
        photos = {Path(p.primary_path).name: p for p in s.exec(select(Photo)).all()}
        assert len(photos) == 2  # nothing purged
        assert photos["keep.jpg"].missing_since is None
        assert photos["drop.jpg"].missing_since is not None


def test_corrupt_file_records_error_and_no_photo(settings: Settings) -> None:
    lib = _lib(settings)
    _jpeg(lib / "good.jpg")
    (lib / "bad.jpg").write_bytes(b"\xff\xd8\xff\xe0 not a real jpeg")

    stats = scan_once(settings)
    assert stats.indexed == 1
    assert stats.errors == 1

    with _session(settings) as s:
        assert len(s.exec(select(Photo)).all()) == 1
        bad = s.get(ScanState, str(lib / "bad.jpg"))
        assert bad is not None and bad.error is not None
        assert bad.pixel_hash is None


@pytest.mark.skipif(not _HAS_EXIFTOOL, reason="exiftool not on PATH")
def test_embedded_metadata_lands_on_photo_row(settings: Settings) -> None:
    lib = _lib(settings)
    photo = lib / "dated.jpg"
    _jpeg(photo)
    subprocess.run(
        [
            "exiftool",
            "-overwrite_original_in_place",
            "-EXIF:DateTimeOriginal=2019:05:20 14:00:00",
            "-EXIF:Make=Nikon",
            "-EXIF:Model=Z6",
            str(photo),
        ],
        capture_output=True,
        check=True,
    )

    scan_once(settings)
    with _session(settings) as s:
        row = s.exec(select(Photo)).one()
        assert row.taken_at is not None and row.taken_at.year == 2019
        assert row.camera_make == "Nikon"
        assert row.camera_model == "Z6"
