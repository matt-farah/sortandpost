"""The continuous indexer loop: FS-event debounce + writability gating."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from sqlmodel import Session, select

from sortandpost.config import Settings
from sortandpost.db import make_engine
from sortandpost.db.models import Photo
from sortandpost.indexer.service import (
    IndexerLoopConfig,
    _Debouncer,
    run_indexer,
    watchdog_available,
)


# --------------------------------------------------------------------------- #
# debouncer
# --------------------------------------------------------------------------- #
def test_debouncer_waits_for_quiet_then_rearms() -> None:
    clock = {"t": 0.0}
    deb = _Debouncer(2.0, time_fn=lambda: clock["t"])

    assert deb.take_if_ready() is False  # nothing pending

    deb.bump()
    clock["t"] = 1.9
    assert deb.take_if_ready() is False  # still inside the quiet window
    clock["t"] = 2.0
    assert deb.take_if_ready() is True  # quiet elapsed
    assert deb.take_if_ready() is False  # consumed / re-armed


def test_debouncer_later_bump_pushes_the_deadline_out() -> None:
    clock = {"t": 0.0}
    deb = _Debouncer(2.0, time_fn=lambda: clock["t"])
    deb.bump()  # due at 2.0
    clock["t"] = 1.5
    deb.bump()  # due at 3.5
    clock["t"] = 2.5
    assert deb.take_if_ready() is False
    clock["t"] = 3.5
    assert deb.take_if_ready() is True


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
    with Session(make_engine(settings.paths.working_db)) as s:
        return len(s.exec(select(Photo)).all())


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
