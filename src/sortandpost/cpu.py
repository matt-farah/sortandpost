"""CPU budget for the indexer: the ``clip.speed`` slow / fast switch.

``slow`` (the default) keeps a background scan from hogging the machine: about
30% of the cores for decode workers and CLIP threads, a small CLIP batch, and a
lowered scheduling priority. ``fast`` is the old behaviour -- every core but one.

Pure policy plus a couple of best-effort OS calls; deliberately free of ``torch``
and of any ``sortandpost`` imports so :mod:`sortandpost.config` can use it.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger(__name__)

ClipSpeed = Literal["slow", "fast"]

SLOW_CORE_FRACTION = 0.3
SLOW_CLIP_BATCH_SIZE = 8
NICE_INCREMENT = 10
_WIN_BELOW_NORMAL_PRIORITY_CLASS = 0x00004000


@dataclass(frozen=True)
class CpuProfile:
    speed: ClipSpeed
    threads: int  # torch intra-op threads for CLIP
    batch_size: int  # images per CLIP forward pass
    lower_priority: bool  # run at reduced OS scheduling priority


def _cores(cores: int | None) -> int:
    return max(1, cores if cores is not None else (os.cpu_count() or 2))


def default_workers(speed: ClipSpeed, cores: int | None = None) -> int:
    """Decode-worker processes when ``indexer.workers`` is ``auto``."""
    n = _cores(cores)
    if speed == "slow":
        return max(1, round(SLOW_CORE_FRACTION * n))
    return max(1, n - 1)


def clip_profile(speed: ClipSpeed, batch_size: int, cores: int | None = None) -> CpuProfile:
    """Resolve ``clip.speed`` + the configured ``clip.batch_size`` into concrete numbers.
    A configured batch smaller than the slow cap is respected."""
    if speed == "slow":
        return CpuProfile(
            speed="slow",
            threads=default_workers("slow", cores),
            batch_size=max(1, min(batch_size, SLOW_CLIP_BATCH_SIZE)),
            lower_priority=True,
        )
    return CpuProfile(
        speed="fast",
        threads=default_workers("fast", cores),
        batch_size=max(1, batch_size),
        lower_priority=False,
    )


def lower_process_priority() -> None:
    """Drop the *whole current process* to below-normal priority. Only for
    processes we own outright, i.e. decode pool workers. Best effort."""
    try:
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetPriorityClass(
                kernel32.GetCurrentProcess(), _WIN_BELOW_NORMAL_PRIORITY_CLASS
            )
        else:
            os.nice(NICE_INCREMENT)
    except (OSError, AttributeError) as exc:
        log.debug("could not lower process priority: %s", exc)


def set_thread_priority(lowered: bool) -> None:
    """Set the *calling thread's* niceness, for CLIP running inside the app process
    (renicing the process would also slow the web UI). Linux only: elsewhere
    nice is process-wide, so this is a no-op. Raising priority back needs
    privileges, so a slow -> fast switch on Linux may keep the lowered
    priority until the app restarts."""
    if not sys.platform.startswith("linux"):
        return
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), NICE_INCREMENT if lowered else 0)
    except OSError as exc:
        log.debug("could not set thread priority: %s", exc)
