"""Continuous indexer (build-order step 6).

``run_indexer`` blocks until its ``stop`` event is set. Each cycle:

1. skip entirely while ``is_writable()`` is False -- another machine (or nobody)
   holds the write lock, so we must not touch the DB (hard constraint #9);
2. otherwise run a full reconcile ``scan_once`` (+ the deferred CLIP pass), which
   is cheap when nothing changed thanks to the ``scan_state`` fast path.

Between full scans (``full_scan_interval``) it also reacts to filesystem events
under the library roots, **debounced**: an event schedules a pass for
``debounce_sec`` of quiet from now, **scoped** to the paths the events named
(a folder is walked, a file checked on its own). Too many paths at once falls
back to a full pass, and the periodic full pass is the safety net for any event
the watcher misses (queue overflow, coalesced FSEvents). Filesystem watching
needs ``watchdog``; when it is not installed the loop degrades to timer-only
and logs once.

The watcher only covers library folders that are there. Every
``root_check_sec`` the loop re-checks which ones are (a drive plugged in or
unplugged, a folder added in Settings); on a change it re-arms the watcher and
runs a full pass straight away.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sortandpost import missing
from sortandpost.clip.model import ClipModel
from sortandpost.clip.store import ModelMismatch
from sortandpost.config import Settings
from sortandpost.indexer.scan import scan_clip_once, scan_once
from sortandpost.indexer.scanner import IMAGE_SUFFIXES
from sortandpost.library_roots import container_roots

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
    # how often to check which library folders are mounted: one coming back
    # (drive plugged in) or going away is scanned at once, not at the next
    # full pass, and the watcher is re-armed for it (V3 P9)
    root_check_sec: float = 5.0

    @classmethod
    def from_settings(cls, settings: Settings) -> IndexerLoopConfig:
        icfg = settings.config.indexer
        return cls(
            full_scan_interval_sec=max(1, icfg.full_scan_interval_min * 60),
            debounce_sec=max(0.0, icfg.debounce_sec),
            watch=icfg.watch,
        )


# More changed paths than this in one debounce window and a scoped pass stops
# being cheaper than just walking everything: run a full scan instead.
_MAX_SCOPE_PATHS = 500


@dataclass(frozen=True)
class PendingScan:
    """What a quiet debounce window hands the loop: the changed paths, or
    ``full`` when there were too many to track (or a scan must redo everything)."""

    paths: frozenset[Path] = frozenset()
    full: bool = False


class _Debouncer:
    """Collects the paths from a burst of FS events into one pending scan,
    due once ``quiet_sec`` pass with no new event."""

    def __init__(
        self,
        quiet_sec: float,
        *,
        time_fn: Callable[[], float] = time.monotonic,
        max_paths: int = _MAX_SCOPE_PATHS,
    ) -> None:
        self._quiet = quiet_sec
        self._time = time_fn
        self._max_paths = max_paths
        self._lock = threading.Lock()
        self._due_at: float | None = None
        self._paths: set[Path] = set()
        self._full = False

    def bump(self, paths: Iterable[Path] = (), *, full: bool = False) -> None:
        with self._lock:
            self._due_at = self._time() + self._quiet
            self._add(paths, full)

    def take_if_ready(self) -> PendingScan | None:
        """The pending scan (and re-arm) once the quiet period since the last
        ``bump`` elapsed; else None."""
        with self._lock:
            if self._due_at is None or self._time() < self._due_at:
                return None
            pending = PendingScan(frozenset(self._paths), self._full)
            self._clear()
            return pending

    def requeue(self, pending: PendingScan) -> None:
        """Put back a pass that was cut short (pause / quit), due right away."""
        with self._lock:
            if self._due_at is None:
                self._due_at = self._time()
            self._add(pending.paths, pending.full)

    def clear(self) -> None:
        """Drop everything pending -- a full scan is about to cover it."""
        with self._lock:
            self._clear()

    def _add(self, paths: Iterable[Path], full: bool) -> None:
        if full or self._full:
            self._full = True
            self._paths.clear()
            return
        self._paths.update(paths)
        if len(self._paths) > self._max_paths:
            self._full = True
            self._paths.clear()

    def _clear(self) -> None:
        self._due_at = None
        self._paths.clear()
        self._full = False


class _ScopeFilter:
    """Turns one watchdog event path into the path a scoped scan should look
    at, or None when the event can't matter to the library.

    Dropped: paths outside every root, hidden entries, excluded folder names,
    anything under the app's own folders (so our thumbnail / DB writes never
    trigger a rescan when APP_DATA sits inside a library root), and existing
    non-image files. A path that no longer exists is kept whatever its name --
    Windows reports a deleted *folder* as a file event.

    macOS FSEvents reports resolved paths, so a symlinked root (``/tmp`` ->
    ``/private/tmp``) is mapped back onto the root as configured: ``scan_state``
    keys are built from that spelling, and a resolved one would index every
    file a second time under a new key.
    """

    def __init__(
        self,
        roots: Iterable[Path],
        *,
        exclude: Iterable[Path] = (),
        exclude_names: Iterable[str] = (),
    ) -> None:
        self._roots: list[tuple[Path, Path]] = []
        for root in roots:
            self._roots.append((root, root))
            resolved = root.resolve()
            if resolved != root:
                self._roots.append((resolved, root))
        self._pruned = [p for e in exclude for p in {Path(e), Path(e).resolve()}]
        self._pruned_names = {n.strip().casefold() for n in exclude_names if n.strip()}

    def scope_for(self, raw_path: str, *, is_directory: bool, event_type: str) -> Path | None:
        path = self._map_to_root(Path(raw_path))
        if path is None or any(path.is_relative_to(p) for p in self._pruned):
            return None
        root = next(r for _, r in self._roots if path.is_relative_to(r))
        parts = path.relative_to(root).parts
        if any(p.startswith(".") for p in parts):
            return None
        if any(p.casefold() in self._pruned_names for p in parts[:-1]):
            return None
        try:
            exists_dir = path.is_dir()
            exists_file = path.is_file()
        except OSError:
            return None
        if exists_dir or (is_directory and not exists_file):
            if event_type == "modified":
                return None  # "contents changed" -- the file events cover it
            if parts and parts[-1].casefold() in self._pruned_names:
                return None
            return path
        if exists_file and path.suffix.lower() not in IMAGE_SUFFIXES:
            return None
        return path

    def _map_to_root(self, path: Path) -> Path | None:
        for spelled, root in self._roots:
            if path.is_relative_to(spelled):
                return root / path.relative_to(spelled)
        return None


# Events that can't change what's on disk (watchdog >= 4 also reports opens /
# read-only closes on some platforms).
_IGNORED_EVENTS = frozenset({"opened", "closed_no_write"})


class _EventHandler(FileSystemEventHandler):  # type: ignore[misc]  # base is Any without watchdog stubs
    def __init__(self, debouncer: _Debouncer, scope_filter: _ScopeFilter) -> None:
        self._debouncer = debouncer
        self._filter = scope_filter

    def on_any_event(self, event: Any) -> None:
        event_type = str(getattr(event, "event_type", ""))
        if event_type in _IGNORED_EVENTS:
            return
        is_dir = bool(getattr(event, "is_directory", False))
        raw = [getattr(event, "src_path", ""), getattr(event, "dest_path", "")]
        scopes = []
        for p in raw:
            if not p:
                continue
            scope = self._filter.scope_for(
                os.fsdecode(p), is_directory=is_dir, event_type=event_type
            )
            if scope is not None:
                scopes.append(scope)
        if scopes:
            self._debouncer.bump(scopes)


def run_indexer(
    settings: Settings,
    *,
    is_writable: Callable[[], bool],
    stop: threading.Event,
    loop_config: IndexerLoopConfig | None = None,
    clip_model: ClipModel | None = None,
    paused: threading.Event | None = None,
) -> None:
    """Run scan cycles until ``stop`` is set. While ``paused`` is set (the
    desktop tray's "Pause indexing") no new cycle starts; one already running
    finishes, so a pause never leaves a file half-indexed (constraint #6).
    FS events that arrive meanwhile stay pending and trigger a cycle on resume."""
    cfg = loop_config or IndexerLoopConfig.from_settings(settings)
    roots = _online_roots(settings)
    debouncer = _Debouncer(cfg.debounce_sec)

    def _watch(online: list[Path]) -> Observer | None:
        if not cfg.watch:
            return None
        scope_filter = _ScopeFilter(
            online,
            exclude=settings.paths.roots(),
            exclude_names=settings.config.indexer.exclude_dir_names,
        )
        return _start_observer(online, debouncer, scope_filter)

    observer = _watch(roots)
    if cfg.watch and observer is None and not watchdog_available():
        log.info("watchdog not installed; continuous indexer runs on the timer only")
    next_root_check = time.monotonic() + cfg.root_check_sec

    # One CLIP model for the whole loop. ``ClipModel.load()`` is a no-op after the
    # first call, so every cycle after the first reuses the weights already in
    # memory instead of re-instantiating the architecture and re-reading the
    # safetensors file (~2-4s of wasted CPU on every otherwise-idle cycle). The
    # app hands in its process-wide model so search and the indexer share one
    # copy; the standalone ``python -m sortandpost.indexer`` gets its own here.
    model = None
    if cfg.run_clip:
        model = clip_model or ClipModel(settings.config.clip, settings.paths.models_dir)

    def _halt() -> bool:
        return stop.is_set() or (paused is not None and paused.is_set())

    next_full = 0.0  # run immediately on start
    try:
        while not stop.is_set():
            if time.monotonic() >= next_root_check:
                next_root_check = time.monotonic() + cfg.root_check_sec
                online = _online_roots(settings)
                if online != roots:
                    log.info(
                        "library folders now available: %s (was %s); rescanning",
                        [str(r) for r in online],
                        [str(r) for r in roots],
                    )
                    roots = online
                    _stop_observer(observer)
                    observer = _watch(roots)
                    missing.invalidate()
                    next_full = 0.0
            if is_writable() and not _halt():
                if time.monotonic() >= next_full:
                    # A full pass covers every event so far; ones arriving
                    # while it runs stay pending for a scoped pass after.
                    debouncer.clear()
                    if not _run_cycle(settings, clip_model=model, interrupt=_halt):
                        continue  # cut short: next_full stays due, so it reruns on resume
                    next_full = time.monotonic() + cfg.full_scan_interval_sec
                elif (pending := debouncer.take_if_ready()) is not None:
                    scope = None if pending.full else sorted(pending.paths)
                    if not _run_cycle(settings, clip_model=model, interrupt=_halt, scope=scope):
                        debouncer.requeue(pending)  # rerun on resume
                        continue
                    if pending.full:
                        next_full = time.monotonic() + cfg.full_scan_interval_sec
            stop.wait(cfg.poll_sec)
    finally:
        _stop_observer(observer)


def _online_roots(settings: Settings) -> list[Path]:
    """The library folders that are there right now (in Settings order)."""
    return [r for r in container_roots(settings.library_roots) if r.is_dir()]


def _stop_observer(observer: Observer | None) -> None:
    if observer is not None:
        observer.stop()
        observer.join(timeout=5)


def _run_cycle(
    settings: Settings,
    *,
    clip_model: ClipModel | None,
    interrupt: Callable[[], bool] | None = None,
    scope: list[Path] | None = None,
) -> bool:
    """One scan + CLIP pass (a scoped one when ``scope`` is given). False if
    ``interrupt`` cut it short (pause / quit)."""
    try:
        stats = scan_once(settings, interrupt=interrupt, scope=scope)
        log.info("indexer cycle: %s", stats)
        if stats.interrupted:
            return False
        if clip_model is not None and settings.config.clip.enabled:
            clip_stats = scan_clip_once(settings, model=clip_model, interrupt=interrupt)
            log.info("indexer cycle: %s", clip_stats)
            if clip_stats.interrupted:
                return False
    except ModelMismatch as exc:
        log.error("CLIP pass needs --reembed: %s", exc)
    except Exception:  # a bad cycle must not kill the loop
        log.exception("indexer cycle failed")
    return True


def _start_observer(
    roots: list[Path], debouncer: _Debouncer, scope_filter: _ScopeFilter
) -> Observer | None:
    if Observer is None or not roots:
        return None
    observer = Observer()
    handler = _EventHandler(debouncer, scope_filter)
    for root in roots:
        observer.schedule(handler, str(root), recursive=True)
    observer.daemon = True
    observer.start()
    return observer
