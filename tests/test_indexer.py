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


def test_parallel_scan_matches_serial(settings: Settings, caplog: pytest.LogCaptureFixture) -> None:
    """The process-pool path (workers>=2, >= _MIN_FILES_FOR_POOL files) must
    produce exactly what the inline path does: same photos, same hashes, same
    thumbnails, and a clean fast-path skip on re-run."""
    lib = _lib(settings)
    for i in range(12):
        _jpeg(lib / f"img_{i:02d}.jpg", (i * 15 % 256, (i * 7 + 30) % 256, (i * 3 + 90) % 256))

    with caplog.at_level("INFO", logger="sortandpost.indexer.scan"):
        stats = scan_once(settings, workers=4)
    assert "worker processes" in caplog.text  # the pool path really ran
    assert stats.seen == 12
    assert stats.indexed == 12
    assert stats.new_photos == 12
    assert stats.errors == 0

    with _session(settings) as s:
        photos = s.exec(select(Photo)).all()
        assert len(photos) == 12
        for photo in photos:
            state = s.get(ScanState, photo.primary_path)
            assert state is not None
            assert (state.stages_done & Stage.BROWSABLE) == Stage.BROWSABLE
            assert photo.phash is not None
            for size in settings.config.thumbnails.sizes:
                assert settings.paths.thumbnail_file(photo.pixel_hash, size).is_file()

    again = scan_once(settings, workers=4)
    assert again.indexed == 0
    assert again.skipped == 12


def test_parallel_scan_isolates_a_corrupt_file(settings: Settings) -> None:
    lib = _lib(settings)
    for i in range(9):
        _jpeg(lib / f"ok_{i}.jpg", (i * 20 % 256, 40, 90))
    (lib / "bad.jpg").write_bytes(b"\xff\xd8\xff\xe0 not a real jpeg")

    stats = scan_once(settings, workers=4)
    assert stats.seen == 10
    assert stats.indexed == 9
    assert stats.errors == 1

    with _session(settings) as s:
        assert len(s.exec(select(Photo)).all()) == 9
        bad = s.get(ScanState, str(lib / "bad.jpg"))
        assert bad is not None and bad.error is not None and bad.pixel_hash is None


def test_parallel_scan_falls_back_to_inline_when_pool_breaks(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A process pool that cannot start (spawn can't re-import __main__, sandbox
    limits, ...) must not fail the scan -- it finishes inline."""
    from concurrent.futures.process import BrokenProcessPool

    import sortandpost.indexer.scan as scanmod

    lib = _lib(settings)
    for i in range(10):
        _jpeg(lib / f"f{i}.jpg", (i * 20 % 256, 30, 60))

    class _BustedPool:
        def __init__(self, *a: object, **k: object) -> None:
            pass

        def __enter__(self) -> _BustedPool:
            return self

        def __exit__(self, *a: object) -> bool:
            return False

        def submit(self, *a: object, **k: object) -> object:
            raise BrokenProcessPool("simulated: workers cannot spawn")

    monkeypatch.setattr(scanmod, "ProcessPoolExecutor", _BustedPool)

    with caplog.at_level("WARNING", logger="sortandpost.indexer.scan"):
        stats = scan_once(settings, workers=4)
    assert "inline" in caplog.text
    assert stats.indexed == 10
    assert stats.errors == 0
    with _session(settings) as s:
        assert len(s.exec(select(Photo)).all()) == 10


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
