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
    resolved.set_library_roots([library])
    return resolved


def _lib(settings: Settings) -> Path:
    return Path(settings.library_roots[0].container)


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


def test_corrupt_file_recovers_once_content_changes_past_max_attempts(
    settings: Settings,
) -> None:
    """Regression: a file that fails to decode repeatedly (e.g. a sync client's
    partial download) must not be abandoned forever once ``attempts`` hits the
    cap -- a later stat change (the download finishing) has to force a retry
    regardless of the error history. See ``needs_reindex``."""
    lib = _lib(settings)
    bad = lib / "syncing.jpg"
    bad.write_bytes(b"\xff\xd8\xff\xe0 not a real jpeg")

    for _ in range(5):  # comfortably past _MAX_ATTEMPTS
        scan_once(settings)

    with _session(settings) as s:
        state = s.get(ScanState, str(bad))
        assert state is not None
        assert state.error is not None
        assert state.attempts >= 3

    _jpeg(bad, (42, 42, 42))  # the "download" finishes with real image bytes
    stats = scan_once(settings)
    assert stats.indexed == 1
    assert stats.errors == 0

    with _session(settings) as s:
        state = s.get(ScanState, str(bad))
        assert state is not None
        assert state.error is None
        assert state.pixel_hash is not None
        assert len(s.exec(select(Photo)).all()) == 1


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


def test_decompression_bomb_fails_one_file_not_the_scan(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issues.md S2: a DecompressionBombError (not an OSError) on the inline
    path used to abort the whole pass, every pass."""
    lib = _lib(settings)
    _jpeg(lib / "a.jpg", (10, 20, 30))
    _jpeg(lib / "b.jpg", (200, 50, 50))
    Image.new("L", (200, 200)).save(lib / "bomb.png")
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10_000)  # bomb > 2x, jpegs below

    stats = scan_once(settings)
    assert not stats.interrupted
    assert stats.indexed == 2
    assert stats.errors == 1
    with _session(settings) as s:
        bomb = s.get(ScanState, str(lib / "bomb.png"))
        assert bomb is not None and bomb.attempts == 1
        assert "DecompressionBombError" in (bomb.error or "")


def test_inline_decode_exception_is_recorded_not_raised(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sortandpost.indexer.scan as scanmod

    lib = _lib(settings)
    _jpeg(lib / "a.jpg", (10, 20, 30))
    _jpeg(lib / "odd.jpg", (200, 50, 50))
    real = scanmod.decode_local

    def flaky(path: Path, **kw: object) -> object:
        if path.name == "odd.jpg":
            raise RuntimeError("unexpected decoder failure")
        return real(path, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(scanmod, "decode_local", flaky)
    stats = scan_once(settings)
    assert stats.indexed == 1
    assert stats.errors == 1
    assert stats.marked_absent == 0  # the presence sweep ran to completion
    with _session(settings) as s:
        odd = s.get(ScanState, str(lib / "odd.jpg"))
        assert odd is not None and "RuntimeError" in (odd.error or "")


def test_worker_death_records_the_file_and_isolates_it_next_pass(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that kills a pool worker is recorded as crashed -- never retried in
    the app's own process -- and later passes decode it only in an isolated
    worker, even when the pass is small enough to run inline."""
    from concurrent.futures import Future
    from concurrent.futures.process import BrokenProcessPool

    import sortandpost.indexer.scan as scanmod

    lib = _lib(settings)
    for i in range(10):
        _jpeg(lib / f"f{i}.jpg", (i * 20 % 256, 30, 60))
    _jpeg(lib / "killer.jpg", (1, 2, 3))

    class _FakePool:
        def __init__(self, *a: object, **k: object) -> None:
            pass

        def __enter__(self) -> _FakePool:
            return self

        def __exit__(self, *a: object) -> bool:
            return False

        def submit(self, fn: object, job: object, key: str) -> Future[object]:
            fut: Future[object] = Future()
            if key.endswith("killer.jpg"):
                fut.set_exception(BrokenProcessPool("simulated: worker segfaulted"))
            else:
                fut.set_result(fn(job, key))  # type: ignore[operator]
            return fut

    inline_keys: list[str] = []
    real_local = scanmod.decode_local

    def tracking_local(path: Path, **kw: object) -> object:
        inline_keys.append(path.name)
        return real_local(path, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(scanmod, "ProcessPoolExecutor", _FakePool)
    monkeypatch.setattr(scanmod, "decode_local", tracking_local)

    stats = scan_once(settings, workers=4)
    assert "killer.jpg" not in inline_keys
    assert stats.indexed == 10
    assert stats.errors == 1
    with _session(settings) as s:
        killer = s.get(ScanState, str(lib / "killer.jpg"))
        assert killer is not None and killer.attempts == 1
        assert (killer.error or "").startswith(scanmod._CRASH_ERROR)

    # Next pass: only the killer is due, so the pass runs inline -- but the
    # suspect still goes to its own worker.
    isolated: list[str] = []

    def fake_isolated(job: object, key: str, ctx: object, *, lower_priority: bool) -> object:
        isolated.append(Path(key).name)
        return scanmod._worker_crash(key, BrokenProcessPool("again"))

    monkeypatch.setattr(scanmod, "_decode_isolated", fake_isolated)
    again = scan_once(settings, workers=4)
    assert isolated == ["killer.jpg"]
    assert "killer.jpg" not in inline_keys
    assert again.errors == 1
    with _session(settings) as s:
        assert s.get(ScanState, str(lib / "killer.jpg")).attempts == 2


def test_online_only_file_is_recorded_not_indexed(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = _lib(settings)
    _jpeg(lib / "stub.jpg")

    monkeypatch.setattr("sortandpost.indexer.workers.is_online_only", lambda _st: True)
    stats = scan_once(settings)
    assert stats.online_only == 1
    assert stats.indexed == 0

    with _session(settings) as s:
        assert s.exec(select(Photo)).all() == []
        state = s.get(ScanState, str(lib / "stub.jpg"))
        assert state is not None
        assert state.online_only is True
        assert state.attempts == 0

    # A second pass re-checks it cheaply and still does not count it as an error.
    again = scan_once(settings)
    assert again.online_only == 1
    with _session(settings) as s:
        assert s.get(ScanState, str(lib / "stub.jpg")).attempts == 0

    # Once the bytes are local (detector returns False) it indexes normally.
    monkeypatch.setattr("sortandpost.indexer.workers.is_online_only", lambda _st: False)
    final = scan_once(settings)
    assert final.indexed == 1
    with _session(settings) as s:
        assert len(s.exec(select(Photo)).all()) == 1
        assert s.get(ScanState, str(lib / "stub.jpg")).online_only is False


def test_docker_always_skips_online_only_files(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In a container, reading a stub is an I/O error that never downloads it, so
    ``online_only: download`` falls back to recording the stub (docs/docker.md)."""
    lib = _lib(settings)
    _jpeg(lib / "stub.jpg")
    settings.config.indexer.online_only = "download"
    monkeypatch.setattr("sortandpost.indexer.workers.is_online_only", lambda _st: True)
    monkeypatch.setattr("sortandpost.indexer.scan.running_in_docker", lambda: True)

    stats = scan_once(settings)
    assert stats.online_only == 1
    assert stats.indexed == 0


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


# --------------------------------------------------------------------------- #
# scoped passes (the indexer loop's filesystem events)
# --------------------------------------------------------------------------- #
def test_scoped_scan_only_walks_its_scope(settings: Settings) -> None:
    lib = _lib(settings)
    _jpeg(lib / "a" / "one.jpg", (10, 20, 30))
    _jpeg(lib / "b" / "two.jpg", (200, 50, 50))

    stats = scan_once(settings, scope=[lib / "a"])
    assert stats.scoped == 1
    assert stats.seen == 1 and stats.indexed == 1
    with _session(settings) as s:
        assert [p.primary_path for p in s.exec(select(Photo)).all()] == [str(lib / "a" / "one.jpg")]

    # a single file in scope is checked on its own
    stats = scan_once(settings, scope=[lib / "b" / "two.jpg"])
    assert stats.seen == 1 and stats.indexed == 1


def test_scoped_sweep_marks_only_its_scope_missing(settings: Settings) -> None:
    lib = _lib(settings)
    _jpeg(lib / "a" / "one.jpg", (10, 20, 30))
    _jpeg(lib / "b" / "two.jpg", (200, 50, 50))
    scan_once(settings)

    shutil.rmtree(lib / "a")
    (lib / "b" / "two.jpg").unlink()  # also gone, but outside this pass's scope
    stats = scan_once(settings, scope=[lib / "a"])
    assert stats.marked_absent == 1 and stats.marked_missing == 1
    with _session(settings) as s:
        missing = {p.primary_path: p.missing_since is not None for p in s.exec(select(Photo))}
    assert missing == {str(lib / "a" / "one.jpg"): True, str(lib / "b" / "two.jpg"): False}

    full = scan_once(settings)  # the periodic full pass catches the rest
    assert full.marked_missing == 1


def test_scoped_scan_resolves_a_move_between_folders(settings: Settings) -> None:
    lib = _lib(settings)
    _jpeg(lib / "a" / "one.jpg", (10, 20, 30))
    scan_once(settings)

    (lib / "b").mkdir()
    (lib / "a" / "one.jpg").rename(lib / "b" / "one.jpg")
    stats = scan_once(settings, scope=[lib / "a" / "one.jpg", lib / "b" / "one.jpg"])
    assert stats.marked_missing == 0
    with _session(settings) as s:
        photo = s.exec(select(Photo)).one()
        assert photo.primary_path == str(lib / "b" / "one.jpg")
        assert photo.missing_since is None
        present = {pp.path: pp.present for pp in s.exec(select(PhotoPath))}
    assert present == {str(lib / "a" / "one.jpg"): False, str(lib / "b" / "one.jpg"): True}


def test_scoped_scan_rechecks_the_old_photo_when_a_file_is_edited(settings: Settings) -> None:
    lib = _lib(settings)
    _jpeg(lib / "a" / "one.jpg", (10, 20, 30))
    scan_once(settings)
    _jpeg(lib / "a" / "one.jpg", (250, 250, 0))  # new pixels, same path

    scan_once(settings, scope=[lib / "a" / "one.jpg"])
    with _session(settings) as s:
        photos = s.exec(select(Photo)).all()
    assert len(photos) == 2
    assert sorted(p.missing_since is None for p in photos) == [False, True]


def test_scoped_scan_respects_excluded_folders(settings: Settings) -> None:
    settings.config.indexer.exclude_dir_names = ["RAW"]
    lib = _lib(settings)
    _jpeg(lib / "RAW" / "x" / "one.jpg")
    assert scan_once(settings, scope=[lib / "RAW" / "x"]).seen == 0
    assert scan_once(settings, scope=[lib / "RAW"]).seen == 0


def test_scope_covering_a_root_is_a_full_scan(settings: Settings) -> None:
    lib = _lib(settings)
    _jpeg(lib / "a" / "one.jpg")
    stats = scan_once(settings, scope=[lib, lib / "a"])
    assert stats.scoped is None and stats.seen == 1
