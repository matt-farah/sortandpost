"""Continuous indexer (build-order step 6).

``run_indexer`` blocks until its ``stop`` event is set. Each cycle:

1. skip entirely while ``is_writable()`` is False -- another machine (or nobody)
   holds the write lock, so we must not touch the DB (hard constraint #9);
2. otherwise run a full reconcile ``scan_once`` (+ the deferred CLIP pass), which
   is cheap when nothing changed thanks to the ``scan_state`` fast path.

Between full scans (``full_scan_interval``) it also reacts to filesystem events
under the library roots, **debounced**: an event schedules the next scan for
``debounce_sec`` of quiet from now. Filesystem watching needs ``watchdog``; when
it is not installed the loop degrades to timer-only and logs once.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sortandpost.clip.model import ClipModel
from sortandpost.clip.store import ModelMismatch
from sortandpost.config import Settings
from sortandpost.indexer.scan import scan_clip_once, scan_once

log = logging.getLogger(__name__)

try:  # optional -- the loop works without it, just timer-driven
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    _WATCHDOG_ERROR: ModuleNotFoundError | None = None
except ModuleNotFoundError as exc:  # pragma: no cover - exercised only without watchdog
    Observer = None
    FileSystemEventHandler = object
    _WATCHDOG_ERROR = exc


def watchdog_available() -> bool:
    return _WATCHDOG_ERROR is None


@dataclass
class IndexerLoopConfig:
    full_scan_interval_sec: float
    debounce_sec: float = 2.0
    poll_sec: float = 1.0
    watch: bool = True
    run_clip: bool = True

    @classmethod
    def from_settings(cls, settings: Settings) -> IndexerLoopConfig:
        icfg = settings.config.indexer
        return cls(
            full_scan_interval_sec=max(1, icfg.full_scan_interval_min * 60),
            debounce_sec=max(0.0, icfg.debounce_sec),
            watch=icfg.watch,
        )


class _Debouncer:
    """Collapses a burst of FS events into one 'rescan due at T' signal."""

    def __init__(self, quiet_sec: float, *, time_fn: Callable[[], float] = time.monotonic) -> None:
        self._quiet = quiet_sec
        self._time = time_fn
        self._lock = threading.Lock()
        self._due_at: float | None = None

    def bump(self) -> None:
        with self._lock:
            self._due_at = self._time() + self._quiet

    def take_if_ready(self) -> bool:
        """True (and re-arm) once the quiet period since the last ``bump`` elapsed."""
        with self._lock:
            if self._due_at is not None and self._time() >= self._due_at:
                self._due_at = None
                return True
            return False


class _EventHandler(FileSystemEventHandler):  # type: ignore[misc]  # base is Any without watchdog stubs
    def __init__(self, debouncer: _Debouncer) -> None:
        self._debouncer = debouncer

    def on_any_event(self, event: object) -> None:
        del event  # every create / modify / move / delete counts the same
        self._debouncer.bump()


def run_indexer(
    settings: Settings,
    *,
    is_writable: Callable[[], bool],
    stop: threading.Event,
    loop_config: IndexerLoopConfig | None = None,
    clip_model: ClipModel | None = None,
) -> None:
    cfg = loop_config or IndexerLoopConfig.from_settings(settings)
    roots = [Path(r) for r in settings.config.library_roots if Path(r).is_dir()]

    debouncer = _Debouncer(cfg.debounce_sec)
    observer = _start_observer(roots, debouncer) if cfg.watch else None
    if cfg.watch and observer is None and not watchdog_available():
        log.info("watchdog not installed; continuous indexer runs on the timer only")

    # One CLIP model for the whole loop. ``ClipModel.load()`` is a no-op after the
    # first call, so every cycle after the first reuses the weights already in
    # memory instead of re-instantiating the architecture and re-reading the
    # safetensors file (~2-4s of wasted CPU on every otherwise-idle cycle). The
    # app hands in its process-wide model so search and the indexer share one
    # copy; the standalone ``python -m sortandpost.indexer`` gets its own here.
    model = None
    if cfg.run_clip:
        model = clip_model or ClipModel(settings.config.clip, settings.paths.models_dir)

    next_full = 0.0  # run immediately on start
    try:
        while not stop.is_set():
            if is_writable():
                now = time.monotonic()
                if now >= next_full or debouncer.take_if_ready():
                    _run_cycle(settings, clip_model=model)
                    next_full = time.monotonic() + cfg.full_scan_interval_sec
            stop.wait(cfg.poll_sec)
    finally:
        if observer is not None:
            observer.stop()
            observer.join(timeout=5)


def _run_cycle(settings: Settings, *, clip_model: ClipModel | None) -> None:
    try:
        stats = scan_once(settings)
        log.info("indexer cycle: %s", stats)
        if clip_model is not None:
            clip_stats = scan_clip_once(settings, model=clip_model)
            log.info("indexer cycle: %s", clip_stats)
    except ModelMismatch as exc:
        log.error("CLIP pass needs --reembed: %s", exc)
    except Exception:  # a bad cycle must not kill the loop
        log.exception("indexer cycle failed")


def _start_observer(roots: list[Path], debouncer: _Debouncer) -> Observer | None:
    if Observer is None or not roots:
        return None
    observer = Observer()
    handler = _EventHandler(debouncer)
    for root in roots:
        observer.schedule(handler, str(root), recursive=True)
    observer.daemon = True
    observer.start()
    return observer
