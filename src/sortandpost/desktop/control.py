"""Server side of the desktop app: the token-gated control API the tray calls,
and a parent watch that shuts the server down cleanly if the tray goes away.

Only active when the tray launched this server -- it passes the token and its
own pid in the environment (:data:`TOKEN_ENV`, :data:`PARENT_ENV`). A source /
Docker run has neither, so none of this exists there.

Every route needs the ``X-Sortandpost-Token`` header. A custom header also means
a web page can't make these requests cross-origin without a CORS preflight,
which this API never answers.
"""

from __future__ import annotations

import hmac
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, FastAPI, Header, HTTPException

log = logging.getLogger(__name__)

TOKEN_ENV = "SORTANDPOST_CONTROL_TOKEN"
PARENT_ENV = "SORTANDPOST_PARENT_PID"
TOKEN_HEADER = "X-Sortandpost-Token"
PREFIX = "/_desktop"
# The server's exit status when it wants a fresh start (a different data folder,
# the first-run wizard's folder pick). The tray starts a new server on seeing
# it -- rather than the server re-exec'ing itself, which on Windows would leave
# the tray holding a dead process while the new one runs unsupervised.
RESTART_EXIT_CODE = 75


@dataclass
class ControlHooks:
    """What the server can do for the tray. ``status`` must be cheap."""

    status: Callable[[], dict[str, Any]]
    shutdown: Callable[[], None]  # clean shutdown: final publish + lock release
    set_paused: Callable[[bool], None] | None = None
    flush_trash: Callable[[], dict[str, Any]] | None = None


def control_token() -> str | None:
    token = os.environ.get(TOKEN_ENV, "").strip()
    return token or None


def build_router(token: str, hooks: ControlHooks) -> APIRouter:
    router = APIRouter(prefix=PREFIX)

    def _auth(given: str | None) -> None:
        if given is None or not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(status_code=403)

    # Plain ``def`` routes: FastAPI runs them in its threadpool, so a slow
    # status / trash flush never blocks the event loop (constraint #7).
    @router.get("/status")
    def _status(
        x_sortandpost_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(x_sortandpost_token)
        return hooks.status()

    @router.post("/pause")
    def _pause(x_sortandpost_token: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(x_sortandpost_token)
        if hooks.set_paused is None:
            raise HTTPException(status_code=409, detail="no indexer here")
        hooks.set_paused(True)
        return {"paused": True}

    @router.post("/resume")
    def _resume(x_sortandpost_token: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(x_sortandpost_token)
        if hooks.set_paused is None:
            raise HTTPException(status_code=409, detail="no indexer here")
        hooks.set_paused(False)
        return {"paused": False}

    @router.post("/flush-trash")
    def _flush(x_sortandpost_token: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(x_sortandpost_token)
        if hooks.flush_trash is None:
            raise HTTPException(status_code=409, detail="no trash here")
        return hooks.flush_trash()

    @router.post("/quit")
    def _quit(x_sortandpost_token: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(x_sortandpost_token)
        log.info("quit requested by the desktop tray")
        # Let this response go out first; shutdown runs the on_shutdown hooks.
        threading.Timer(0.2, hooks.shutdown).start()
        return {"quitting": True}

    return router


def install(app: FastAPI, hooks: ControlHooks) -> bool:
    """Mount the control API and start the parent watch, if the tray started
    us. Returns whether it did."""
    token = control_token()
    if token is None:
        return False
    app.include_router(build_router(token, hooks))
    parent = os.environ.get(PARENT_ENV, "").strip()
    if parent.isdigit():
        start_parent_watch(int(parent), hooks.shutdown)
    log.info("desktop control API enabled")
    return True


def parent_alive_check(parent_pid: int) -> Callable[[], bool]:
    """How to tell the tray is still running. POSIX: our parent pid changes
    when it dies (we get re-parented to launchd / init). Windows never
    re-parents, so hold a handle to the tray process -- taken now, while it's
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


def start_parent_watch(
    parent_pid: int,
    on_orphaned: Callable[[], None],
    *,
    interval: float = 2.0,
    alive: Callable[[], bool] | None = None,
) -> threading.Thread:
    """If the tray process dies (crash, force quit, logout), shut down cleanly
    instead of lingering as a second writer nobody can quit
    (:func:`parent_alive_check`)."""
    parent_alive = alive or parent_alive_check(parent_pid)

    def _watch() -> None:
        while True:
            time.sleep(interval)
            if not parent_alive():
                log.warning("desktop tray (pid %d) is gone; shutting down", parent_pid)
                on_orphaned()
                return

    thread = threading.Thread(target=_watch, name="sortandpost-parent-watch", daemon=True)
    thread.start()
    return thread
