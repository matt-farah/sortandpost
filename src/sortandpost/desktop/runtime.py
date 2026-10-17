"""Per-machine runtime files of the desktop app, in ``default_local_root()``
(never synced):

* ``app.lock`` -- held (OS file lock) by the running tray, so a second launch
  just opens the browser on the first one instead of starting another server.
  The OS drops the lock when the process dies, so a crash never leaves it stuck.
* ``runtime.json`` -- ``{pid, host, port, token, version}`` of the running tray +
  server, for that second launch (mode 0600: the token gates the control API).
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import socket
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO

from sortandpost.paths import default_local_root

LOCK_NAME = "app.lock"
RUNTIME_NAME = "runtime.json"
DEFAULT_PORT = 8080
# What the browser is pointed at. Any ``*.localhost`` name resolves to this
# computer with no DNS or hosts-file entry (RFC 6761; macOS and every current
# browser do it), so it's a readable address that needs no setup and works
# offline. The tray itself talks to the server on 127.0.0.1.
BROWSER_HOST = "sortandpost.localhost"
PORT_SPAN = 20  # 8080-8099


@dataclass(frozen=True)
class RuntimeInfo:
    pid: int
    host: str
    port: int
    token: str
    version: str

    @property
    def url(self) -> str:
        return browser_url(self.port)


def browser_url(port: int) -> str:
    return f"http://{BROWSER_HOST}:{port}/"


def runtime_dir() -> Path:
    return default_local_root()


def new_token() -> str:
    return secrets.token_urlsafe(32)


class InstanceLock:
    """An exclusive, non-blocking OS lock on ``app.lock``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: IO[str] | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115 - held for the process lifetime
        try:
            _lock_file(fh)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        with contextlib.suppress(OSError):
            _unlock_file(self._fh)
        self._fh.close()
        self._fh = None


if sys.platform == "win32":  # pragma: no cover - exercised in the Windows pass (P6)
    import msvcrt

    def _lock_file(fh: IO[str]) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(fh: IO[str]) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_file(fh: IO[str]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(fh: IO[str]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def instance_lock(base: Path | None = None) -> InstanceLock:
    return InstanceLock((base or runtime_dir()) / LOCK_NAME)


def write_runtime(info: RuntimeInfo, base: Path | None = None) -> Path:
    path = (base or runtime_dir()) / RUNTIME_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(asdict(info), fh)
    os.replace(tmp, path)
    return path


def read_runtime(base: Path | None = None) -> RuntimeInfo | None:
    path = (base or runtime_dir()) / RUNTIME_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return RuntimeInfo(
            pid=int(data["pid"]),
            host=str(data["host"]),
            port=int(data["port"]),
            token=str(data["token"]),
            version=str(data.get("version", "")),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def clear_runtime(pid: int, base: Path | None = None) -> None:
    """Remove ``runtime.json`` if it is still ours (``pid``)."""
    info = read_runtime(base)
    if info is not None and info.pid == pid:
        with contextlib.suppress(OSError):
            ((base or runtime_dir()) / RUNTIME_NAME).unlink()


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    """Whether the server could listen on ``port`` now. Asked the way uvicorn
    binds: with SO_REUSEADDR off Windows, so the TIME_WAIT connections a server
    that just restarted leaves behind don't count as busy (without it the
    restart moved to the next port and the open tab never found it). Windows
    doesn't block on TIME_WAIT, and its SO_REUSEADDR would steal a live port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if sys.platform != "win32":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def pick_port(preferred: int = DEFAULT_PORT, span: int = PORT_SPAN, host: str = "127.0.0.1") -> int:
    """``preferred`` if it's free, else the next free port in the span, else any
    free port the OS hands out."""
    for port in range(preferred, preferred + span):
        if port_free(port, host):
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])
