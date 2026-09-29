"""Live progress of the running scan / CLIP pass, for the status bar and ``/settings``.

An in-memory, thread-safe struct that ``scan_once`` / ``scan_clip_once`` write and
the UI polls. ``_INDEX_LOCK`` already serializes passes, so there is at most one
in-flight stage to report. It only exists in the process that is indexing: with
the indexer running in the app (the default) the UI sees it; a separate
``python -m sortandpost.indexer`` process has its own copy, and the UI falls back
to the DB totals.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Literal

Stage = Literal["idle", "discovering", "indexing", "downloading_model", "embedding"]


@dataclass(frozen=True)
class ProgressSnapshot:
    stage: Stage = "idle"
    done: int = 0
    total: int = 0
    current: str = ""  # file being worked on (discovering: the folder walk's latest hit)
    started_at: float = 0.0  # time.monotonic() when this stage began

    @property
    def active(self) -> bool:
        return self.stage != "idle"

    @property
    def fraction(self) -> float | None:
        """0..1 when the total is known, else ``None`` (indeterminate)."""
        if self.stage == "discovering" or self.total <= 0:
            return None
        return min(1.0, self.done / self.total)

    def eta_seconds(self, now: float | None = None) -> float | None:
        """Naive linear estimate; ``None`` until there's enough to extrapolate."""
        if self.stage in ("idle", "discovering") or self.done < 2 or self.total <= self.done:
            return None
        elapsed = (time.monotonic() if now is None else now) - self.started_at
        if elapsed <= 0:
            return None
        return elapsed / self.done * (self.total - self.done)

    def label(self) -> str:
        """Short status text, e.g. ``Indexing 120/4,300``."""
        if self.stage == "discovering":
            return f"Scanning folders… {self.done:,} found"
        if self.stage == "indexing":
            return f"Indexing {self.done:,}/{self.total:,}"
        if self.stage == "downloading_model":
            return "Downloading the AI model (~600 MB, one time)…"
        if self.stage == "embedding":
            return f"Embedding {self.done:,}/{self.total:,}"
        return ""


class ProgressTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snap = ProgressSnapshot()

    def begin(self, stage: Stage, total: int = 0) -> None:
        with self._lock:
            self._snap = ProgressSnapshot(stage=stage, total=total, started_at=time.monotonic())

    def update(
        self, *, done: int | None = None, total: int | None = None, current: str | None = None
    ) -> None:
        """Set counters; ``None`` leaves a field alone. Ignored while idle."""
        with self._lock:
            s = self._snap
            if s.stage == "idle":
                return
            self._snap = ProgressSnapshot(
                stage=s.stage,
                done=s.done if done is None else done,
                total=s.total if total is None else total,
                current=s.current if current is None else current,
                started_at=s.started_at,
            )

    def finish(self) -> None:
        with self._lock:
            self._snap = ProgressSnapshot()

    def snapshot(self) -> ProgressSnapshot:
        with self._lock:
            return self._snap


PROGRESS = ProgressTracker()
