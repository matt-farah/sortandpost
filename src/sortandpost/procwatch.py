"""Notice when the process that started us is gone.

The desktop server watches its tray (``desktop.control``), and every process
pool worker watches the app: a ``spawn`` worker blocks on its task queue and
never learns its parent died, so a ``kill -9`` of the app (or the tray's
last-resort SIGKILL) left the workers -- and multiprocessing's resource tracker,
which waits on them -- running idle forever, re-parented to launchd / init.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable

from sortandpost.cpu import lower_process_priority

_WORKER_POLL_SEC = 2.0


def parent_alive_check(parent_pid: int) -> Callable[[], bool]:
    """How to tell the parent process (the tray, or the app for a pool
    worker) is still running. POSIX: our parent pid changes
    when it dies (we get re-parented to launchd / init). Windows never
    re-parents, so hold a handle to the parent process -- taken now, while it's
    certainly alive, so a recycled pid can't fool us -- and ask whether it has
    exited."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        synchronize, wait_timeout = 0x00100000, 0x102
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        handle = kernel32.OpenProcess(synchronize, False, parent_pid)
        if not handle:
            return lambda: False  # already gone (or not ours to watch)
        return lambda: bool(kernel32.WaitForSingleObject(handle, 0) == wait_timeout)
    return lambda: os.getppid() == parent_pid


def pool_worker_init(lower_priority: bool = False) -> None:
    """``ProcessPoolExecutor`` initializer for every pool the app starts: exit
    this worker as soon as the app that spawned it is gone, and optionally drop
    its priority (``clip.speed: slow``)."""
    if lower_priority:
        lower_process_priority()
    alive = parent_alive_check(os.getppid())
    if not alive():
        return  # can't watch it (Windows handle refused): never exit on a guess

    def _watch() -> None:
        while alive():
            time.sleep(_WORKER_POLL_SEC)
        os._exit(0)  # nothing to clean up: the parent owned every result

    threading.Thread(target=_watch, name="sortandpost-parent-watch", daemon=True).start()
