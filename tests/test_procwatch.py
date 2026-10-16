from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from sortandpost import procwatch

# A parent that starts a spawn pool with the app's initializer, prints its
# worker's pid, then waits to be killed.
_PARENT = """
import multiprocessing, os, sys, time
from concurrent.futures import ProcessPoolExecutor
from sortandpost.procwatch import pool_worker_init

if __name__ == "__main__":
    ctx = multiprocessing.get_context("spawn")
    pool = ProcessPoolExecutor(max_workers=1, mp_context=ctx, initializer=pool_worker_init)
    print(pool.submit(os.getpid).result(), flush=True)
    time.sleep(60)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL / os.kill(pid, 0) are POSIX")
def test_pool_worker_exits_when_its_parent_is_killed() -> None:
    parent = subprocess.Popen([sys.executable, "-c", _PARENT], stdout=subprocess.PIPE, text=True)
    try:
        assert parent.stdout is not None
        worker = int(parent.stdout.readline())
        assert _alive(worker)
        parent.send_signal(signal.SIGKILL)
        parent.wait(timeout=10)
        deadline = time.monotonic() + 15
        while _alive(worker) and time.monotonic() < deadline:
            time.sleep(0.2)
        assert not _alive(worker), "the pool worker outlived its killed parent"
    finally:
        if parent.poll() is None:
            parent.kill()


def test_pool_worker_init_does_not_watch_a_parent_it_cannot_see(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[object] = []
    monkeypatch.setattr(procwatch, "parent_alive_check", lambda pid: lambda: False)
    monkeypatch.setattr(procwatch.threading, "Thread", lambda **kw: started.append(kw))
    procwatch.pool_worker_init()  # must not exit this process, nor start a watch
    assert started == []
