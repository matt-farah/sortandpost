"""One process-wide, serialized ``exiftool -stay_open`` client.

Every rating / tag / review action used to spawn (and tear down) its own exiftool
process; a per-photo ``/review`` would pay that startup on every click. Here a
single client is started lazily on first use, handed out one caller at a time
(``ExiftoolClient`` is not thread-safe), and closed again after a stretch of
idleness so it never lingers when the user walks away.
"""

from __future__ import annotations

import shutil
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from sortandpost.metadata import ExiftoolClient

IDLE_CLOSE_SEC = 120.0


class SharedExiftool:
    def __init__(
        self,
        *,
        exiftool_path: str = "exiftool",
        idle_seconds: float = IDLE_CLOSE_SEC,
        client_factory: Callable[[str], ExiftoolClient] = lambda path: ExiftoolClient(
            exiftool_path=path
        ),
    ) -> None:
        self._path = exiftool_path
        self._factory = client_factory
        self._idle = idle_seconds
        self._lock = threading.Lock()
        self._client: ExiftoolClient | None = None
        self._last_used = 0.0
        self._timer: threading.Timer | None = None

    @property
    def running(self) -> bool:
        return self._client is not None

    @contextmanager
    def session(self, *, write_windows_compat_tags: bool = True) -> Iterator[ExiftoolClient | None]:
        """Exclusive use of the shared client, or ``None`` when exiftool isn't
        installed (callers then skip the file write and stay DB-only). Blocks
        while another caller holds it -- call off the event loop."""
        if shutil.which(self._path) is None:
            yield None
            return
        with self._lock:
            if self._client is None:
                self._client = self._factory(self._path)
            client = self._client
            client.write_windows_compat_tags = write_windows_compat_tags
            client.start()  # no-op while alive; revives a crashed process
            try:
                yield client
            finally:
                self._last_used = time.monotonic()
                self._arm_idle_timer()

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    # -- internals ----------------------------------------------------------- #

    def _close_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        client, self._client = self._client, None
        if client is not None:
            client.close()

    def _arm_idle_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self._idle, self._close_if_idle)
        self._timer.daemon = True
        self._timer.start()

    def _close_if_idle(self) -> None:
        if not self._lock.acquire(blocking=False):
            return  # in use right now; that session re-arms the timer when it ends
        try:
            if time.monotonic() - self._last_used >= self._idle:
                self._close_locked()
        finally:
            self._lock.release()


EXIFTOOL = SharedExiftool()
