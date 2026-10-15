"""The continuous indexer loop: FS-event debounce + writability gating."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from sortandpost.config import Settings
from sortandpost.db import make_engine
from sortandpost.db.models import Photo
from sortandpost.indexer.service import (
    IndexerLoopConfig,
    PendingScan,
    _Debouncer,
    _ScopeFilter,
    run_indexer,
    watchdog_available,
)


# --------------------------------------------------------------------------- #
# debouncer
# --------------------------------------------------------------------------- #
def test_debouncer_waits_for_quiet_then_rearms() -> None:
    clock = {"t": 0.0}
    deb = _Debouncer(2.0, time_fn=lambda: clock["t"])

    assert deb.take_if_ready() is None  # nothing pending

    deb.bump([Path("/lib/a.jpg")])
    clock["t"] = 1.9
    assert deb.take_if_ready() is None  # still inside the quiet window
    clock["t"] = 2.0
    assert deb.take_if_ready() == PendingScan(frozenset({Path("/lib/a.jpg")}))
    assert deb.take_if_ready() is None  # consumed / re-armed


def test_debouncer_later_bump_pushes_the_deadline_out_and_collects_paths() -> None:
    clock = {"t": 0.0}
    deb = _Debouncer(2.0, time_fn=lambda: clock["t"])
    deb.bump([Path("/lib/a.jpg")])  # due at 2.0
    clock["t"] = 1.5
    deb.bump([Path("/lib/b"), Path("/lib/a.jpg")])  # due at 3.5
    clock["t"] = 2.5
    assert deb.take_if_ready() is None
    clock["t"] = 3.5
    pending = deb.take_if_ready()
    assert pending is not None and not pending.full
    assert pending.paths == {Path("/lib/a.jpg"), Path("/lib/b")}


def test_debouncer_too_many_paths_becomes_a_full_scan() -> None:
    clock = {"t": 0.0}
    deb = _Debouncer(0.0, time_fn=lambda: clock["t"], max_paths=3)
    deb.bump([Path(f"/lib/{i}.jpg") for i in range(4)])
    deb.bump([Path("/lib/more.jpg")])  # stays full
    assert deb.take_if_ready() == PendingScan(full=True)


def test_debouncer_requeue_is_due_at_once_and_clear_drops_everything() -> None:
    clock = {"t": 10.0}
    deb = _Debouncer(5.0, time_fn=lambda: clock["t"])
    deb.requeue(PendingScan(frozenset({Path("/lib/x")})))
    assert deb.take_if_ready() == PendingScan(frozenset({Path("/lib/x")}))
    deb.bump([Path("/lib/y")])
    deb.clear()
    clock["t"] = 100.0
    assert deb.take_if_ready() is None


# --------------------------------------------------------------------------- #
# event -> scope filter
# --------------------------------------------------------------------------- #
def _filter(tmp_path: Path, **kw: object) -> tuple[_ScopeFilter, Path]:
    lib = tmp_path / "lib"
    (lib / "sub").mkdir(parents=True)
    Image.new("RGB", (4, 4)).save(lib / "sub" / "a.jpg")
    (lib / "sub" / "notes.txt").write_text("x")
    return _ScopeFilter([lib], **kw), lib  # type: ignore[arg-type]


def test_scope_filter_keeps_images_folders_and_vanished_paths(tmp_path: Path) -> None:
    f, lib = _filter(tmp_path)
    img = str(lib / "sub" / "a.jpg")
    assert f.scope_for(img, is_directory=False, event_type="modified") == lib / "sub" / "a.jpg"
    assert f.scope_for(str(lib / "sub"), is_directory=True, event_type="created") == lib / "sub"
    # gone: kept whatever it looks like (Windows reports a deleted folder as a file)
    gone = str(lib / "Trip")
    assert f.scope_for(gone, is_directory=False, event_type="deleted") == lib / "Trip"


def test_scope_filter_drops_noise(tmp_path: Path) -> None:
    appdata = tmp_path / "lib" / "appdata"
    f, lib = _filter(tmp_path, exclude=[appdata], exclude_names=["RAW"])
    appdata.mkdir()
    cases = [
        (str(tmp_path / "elsewhere.jpg"), False, "created"),  # outside every root
        (str(lib / "sub" / "notes.txt"), False, "modified"),  # existing non-image
        (str(lib / ".hidden" / "a.jpg"), False, "created"),  # hidden folder
        (str(lib / "RAW" / "a.jpg"), False, "created"),  # excluded folder name
        (str(lib / "RAW"), True, "deleted"),  # the excluded folder itself
        (str(appdata / "thumbnails" / "x.webp"), False, "created"),  # our own writes
        (str(lib / "sub"), True, "modified"),  # "folder contents changed"
    ]
    for raw, is_dir, kind in cases:
        assert f.scope_for(raw, is_directory=is_dir, event_type=kind) is None, raw


def test_scope_filter_maps_a_resolved_path_back_onto_a_symlinked_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "sub").mkdir(parents=True)
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    f = _ScopeFilter([link])
    got = f.scope_for(str(real / "sub"), is_directory=True, event_type="created")
    assert got == link / "sub"  # the spelling scan_state keys use


def test_watchdog_available_is_a_bool() -> None:
    assert isinstance(watchdog_available(), bool)


# --------------------------------------------------------------------------- #
# run_indexer
# --------------------------------------------------------------------------- #
@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    library = tmp_path / "library"
    library.mkdir()
    Image.new("RGB", (16, 16), (10, 120, 200)).save(library / "a.jpg", "JPEG")
    monkeypatch.setenv("APP_DATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("PORT", raising=False)
    resolved = Settings.resolve()
    resolved.set_library_roots([library])
    return resolved


def _photo_count(settings: Settings) -> int:
    if not settings.paths.working_db.exists():
        return 0
    try:
        with Session(make_engine(settings.paths.working_db)) as s:
            return len(s.exec(select(Photo)).all())
    except OperationalError:  # the indexer thread created the file but not the tables yet
        return 0


def _run_in_thread(settings: Settings, *, is_writable, stop) -> threading.Thread:
    cfg = IndexerLoopConfig(
        full_scan_interval_sec=0.2, debounce_sec=0.1, poll_sec=0.02, watch=False, run_clip=False
    )
    t = threading.Thread(
        target=run_indexer,
        kwargs={"settings": settings, "is_writable": is_writable, "stop": stop, "loop_config": cfg},
        daemon=True,
    )
    t.start()
    return t


def test_loop_indexes_then_stops(settings: Settings) -> None:
    stop = threading.Event()
    thread = _run_in_thread(settings, is_writable=lambda: True, stop=stop)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _photo_count(settings) == 0:
            time.sleep(0.05)
        assert _photo_count(settings) == 1
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_loop_does_nothing_while_read_only(settings: Settings) -> None:
    stop = threading.Event()
    thread = _run_in_thread(settings, is_writable=lambda: False, stop=stop)
    try:
        time.sleep(0.6)
        assert _photo_count(settings) == 0
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_loop_promotes_when_writability_flips(settings: Settings) -> None:
    writable = threading.Event()
    stop = threading.Event()
    thread = _run_in_thread(settings, is_writable=writable.is_set, stop=stop)
    try:
        time.sleep(0.4)
        assert _photo_count(settings) == 0  # gated off
        writable.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _photo_count(settings) == 0:
            time.sleep(0.05)
        assert _photo_count(settings) == 1
    finally:
        stop.set()
        thread.join(timeout=5)


class _CountingClipModel:
    """Counts how many times the weights are actually (re)loaded."""

    def __init__(self, dim: int = 8) -> None:
        self._dim = dim
        self._loaded = False
        self.load_calls = 0

    @property
    def dim(self) -> int:
        return self._dim

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        self.load_calls += 1

    def encode_images(self, images: list[object]) -> np.ndarray:
        return np.zeros((len(images), self._dim), dtype=np.float32)

    def encode_text(self, texts: list[str]) -> np.ndarray:
        return np.zeros((len(texts), self._dim), dtype=np.float32)


def test_loop_loads_the_clip_model_once_across_many_cycles(settings: Settings) -> None:
    """Regression: the loop must not re-instantiate CLIP every cycle."""
    model = _CountingClipModel()
    cfg = IndexerLoopConfig(
        full_scan_interval_sec=0.05, debounce_sec=0.1, poll_sec=0.02, watch=False, run_clip=True
    )
    stop = threading.Event()
    thread = threading.Thread(
        target=run_indexer,
        kwargs={
            "settings": settings,
            "is_writable": lambda: True,
            "stop": stop,
            "loop_config": cfg,
            "clip_model": model,
        },
        daemon=True,
    )
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _photo_count(settings) == 0:
            time.sleep(0.05)
        time.sleep(0.6)  # let several more cycles turn over
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert model.load_calls == 1


# --------------------------------------------------------------------------- #
# pause (desktop tray) + interrupt
# --------------------------------------------------------------------------- #
def test_loop_does_nothing_while_paused_then_resumes(settings: Settings) -> None:
    paused = threading.Event()
    paused.set()
    stop = threading.Event()
    cfg = IndexerLoopConfig(
        full_scan_interval_sec=0.2, debounce_sec=0.1, poll_sec=0.02, watch=False, run_clip=False
    )
    thread = threading.Thread(
        target=run_indexer,
        kwargs={
            "settings": settings,
            "is_writable": lambda: True,
            "stop": stop,
            "loop_config": cfg,
            "paused": paused,
        },
        daemon=True,
    )
    thread.start()
    try:
        time.sleep(0.5)
        assert _photo_count(settings) == 0  # paused: no cycle started
        paused.clear()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _photo_count(settings) == 0:
            time.sleep(0.05)
        assert _photo_count(settings) == 1
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_interrupted_scan_skips_the_presence_sweep(settings: Settings) -> None:
    """A scan cut short hasn't seen every file, so it must not mark the unseen
    ones missing (and it must not index anything past the interrupt)."""
    from sortandpost.indexer.scan import scan_once

    first = scan_once(settings)
    assert first.indexed == 1 and not first.interrupted

    stats = scan_once(settings, interrupt=lambda: True)
    assert stats.interrupted
    assert stats.seen == 0
    assert stats.marked_absent == 0 and stats.marked_missing == 0
    with Session(make_engine(settings.paths.working_db)) as s:
        photo = s.exec(select(Photo)).one()
    assert photo.missing_since is None


def test_interrupt_stops_indexing_mid_list(settings: Settings, tmp_path: Path) -> None:
    from sortandpost.indexer.scan import scan_once

    library = tmp_path / "library"
    for i in range(4):
        Image.new("RGB", (16, 16), (i * 40, 10, 10)).save(library / f"b{i}.jpg", "JPEG")
    calls = {"n": 0}

    def _after_discovery() -> bool:
        calls["n"] += 1
        return calls["n"] > 5 + 2  # 5 files discovered, then stop after 2 indexed

    stats = scan_once(settings, workers=1, interrupt=_after_discovery)
    assert stats.interrupted
    assert 0 < _photo_count(settings) < 5
    rest = scan_once(settings, workers=1)  # resumable: the next pass finishes the job
    assert not rest.interrupted
    assert _photo_count(settings) == 5


@pytest.mark.skipif(not watchdog_available(), reason="needs watchdog")
def test_fs_event_runs_a_scoped_pass_not_a_full_one(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sortandpost.indexer import service
    from sortandpost.indexer.scan import scan_once as real_scan_once

    scopes: list[list[Path] | None] = []

    def _recording_scan(settings: Settings, **kw: object):  # type: ignore[no-untyped-def]
        scopes.append(kw.get("scope"))  # type: ignore[arg-type]
        return real_scan_once(settings, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "scan_once", _recording_scan)
    library = Path(settings.library_roots[0].container)
    cfg = IndexerLoopConfig(
        full_scan_interval_sec=3600, debounce_sec=0.2, poll_sec=0.02, watch=True, run_clip=False
    )
    stop = threading.Event()
    thread = threading.Thread(
        target=run_indexer,
        kwargs={
            "settings": settings,
            "is_writable": lambda: True,
            "stop": stop,
            "loop_config": cfg,
        },
        daemon=True,
    )
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _photo_count(settings) == 0:
            time.sleep(0.05)
        assert scopes == [None]  # the startup full pass
        time.sleep(0.5)  # let the observer settle
        (library / "trip").mkdir()
        Image.new("RGB", (16, 16), (200, 10, 10)).save(library / "trip" / "b.jpg", "JPEG")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _photo_count(settings) < 2:
            time.sleep(0.05)
        assert _photo_count(settings) == 2
    finally:
        stop.set()
        thread.join(timeout=5)
    assert scopes[0] is None
    assert len(scopes) >= 2 and all(s is not None for s in scopes[1:])
    walked = {p for s in scopes[1:] for p in (s or [])}
    assert walked and all(p.is_relative_to(library) for p in walked)


def test_folder_coming_back_is_scanned_at_once(tmp_path: Path, settings: Settings) -> None:
    """A library folder that wasn't there when the loop started (an unplugged
    drive) is scanned as soon as it appears -- not at the next full pass."""
    drive = tmp_path / "drive"
    settings.set_library_roots([Path(settings.library_roots[0].container), drive])
    cfg = IndexerLoopConfig(
        full_scan_interval_sec=3600,
        debounce_sec=0.1,
        poll_sec=0.02,
        watch=False,
        run_clip=False,
        root_check_sec=0.1,
    )
    stop = threading.Event()
    thread = threading.Thread(
        target=run_indexer,
        kwargs={
            "settings": settings,
            "is_writable": lambda: True,
            "stop": stop,
            "loop_config": cfg,
        },
        daemon=True,
    )
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _photo_count(settings) == 0:
            time.sleep(0.05)
        assert _photo_count(settings) == 1  # the first full pass

        drive.mkdir()
        Image.new("RGB", (32, 32), (5, 200, 90)).save(drive / "plugged.jpg", "JPEG")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _photo_count(settings) < 2:
            time.sleep(0.05)
        assert _photo_count(settings) == 2
    finally:
        stop.set()
        thread.join(timeout=5)


def test_rebuild_request_runs_a_reembed_on_the_indexer_thread(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The UI only files a request; the loop thread runs the re-embed (V3 P10)."""
    from sortandpost.indexer import service
    from sortandpost.indexer.scan import ClipStats

    calls: list[tuple[str, bool]] = []

    def _fake_clip(settings: Settings, **kw: object) -> ClipStats:
        calls.append((threading.current_thread().name, bool(kw.get("reembed"))))
        return ClipStats()

    monkeypatch.setattr(service, "scan_clip_once", _fake_clip)
    settings.config.clip.enabled = True
    rebuild = service.RebuildControl()
    rebuild.mismatch = "old model"
    cfg = IndexerLoopConfig(
        full_scan_interval_sec=3600, debounce_sec=0.1, poll_sec=0.02, watch=False, run_clip=True
    )
    stop = threading.Event()
    thread = threading.Thread(
        target=run_indexer,
        kwargs={
            "settings": settings,
            "is_writable": lambda: True,
            "stop": stop,
            "loop_config": cfg,
            "clip_model": object(),  # never used: scan_clip_once is faked
            "rebuild": rebuild,
        },
        name="idx-test",
        daemon=True,
    )
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not calls:
            time.sleep(0.02)  # the startup cycle's ordinary CLIP pass
        rebuild.request()
        assert rebuild.busy
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any(r for _, r in calls):
            time.sleep(0.02)
    finally:
        stop.set()
        thread.join(timeout=5)
    assert ("idx-test", True) in calls
    assert not rebuild.busy and rebuild.mismatch is None
