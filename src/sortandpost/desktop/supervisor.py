"""Tray side: start the web server as a child process, talk to its control API
(:mod:`sortandpost.desktop.control`) and stop it cleanly.

UI-free on purpose, so it's testable and the Windows / Linux trays (P6 / P7)
reuse it unchanged.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import IO, Any

import httpx

from sortandpost.desktop.control import PARENT_ENV, PREFIX, TOKEN_ENV, TOKEN_HEADER

log = logging.getLogger(__name__)

HOST = "127.0.0.1"  # a desktop app never listens on the LAN
QUIT_TIMEOUT_SEC = 60.0  # indexer stop + final publish; generous for a slow sync folder
TERM_TIMEOUT_SEC = 15.0
# Anything a control call can raise. The tray must survive every one of them:
# a Quit or status poll that raises leaves the menu dead.
_ERRORS = (httpx.HTTPError, OSError, ValueError)
_TERMINATE = (
    "terminating it (no final publish; the write lock goes stale)"
    if sys.platform == "win32"
    else "sending SIGTERM"
)


def server_command() -> list[str]:
    """How to start the server: the same frozen executable with ``--serve``, or
    this interpreter running the package from source."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--serve"]
    return [sys.executable, "-m", "sortandpost", "--serve"]


class ServerProcess:
    def __init__(self, *, port: int, token: str, log_file: Path) -> None:
        self.port = port
        self.token = token
        self.log_file = log_file
        self.proc: subprocess.Popen[bytes] | None = None
        self._log_fh: IO[bytes] | None = None
        self.quitting = False  # set once we asked it to stop, so an exit isn't a crash
        # One client for the tray's lifetime, built now. Loopback plain HTTP needs
        # no certificates and must not go through a proxy: ignore the environment
        # (SSL_CERT_FILE points into the bundle, which an installer may replace
        # under a running app) and skip loading a CA store at all.
        self._http = httpx.Client(trust_env=False, verify=False)

    @property
    def base_url(self) -> str:
        return f"http://{HOST}:{self.port}"

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> None:
        env = dict(os.environ)
        env.update(
            {
                "HOST": HOST,
                "PORT": str(self.port),
                TOKEN_ENV: self.token,
                PARENT_ENV: str(os.getpid()),
            }
        )
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        # Last run only: the server logs to its own rotating file; this just
        # catches what escapes logging (an import-time crash, a native abort).
        self._log_fh = open(self.log_file, "wb")  # noqa: SIM115 - lives as long as the child
        self.quitting = False
        self.proc = subprocess.Popen(
            server_command(),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=self._log_fh,
            stderr=subprocess.STDOUT,
            cwd=str(Path.home()),
        )
        log.info("started server pid %d on %s", self.proc.pid, self.base_url)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def exit_code(self) -> int | None:
        return None if self.proc is None else self.proc.poll()

    def wait_ready(self, timeout: float = 120.0) -> bool:
        """Poll until the server answers HTTP (or dies / times out). First launch
        of a frozen torch app can take a while."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.alive():
                return False
            try:
                self._http.get(self.base_url + "/", timeout=2.0, follow_redirects=False)
                return True
            except _ERRORS:
                time.sleep(0.4)
        return False

    def stop(self, *, timeout: float = QUIT_TIMEOUT_SEC) -> bool:
        """Clean stop: ask via the control API (on_shutdown hooks: indexer stop,
        final publish, lock release), wait; then SIGTERM, which uvicorn also
        turns into a clean shutdown; SIGKILL only as a last resort. Returns
        whether it exited cleanly (without a kill).

        On Windows ``terminate()`` is ``TerminateProcess`` -- already a kill,
        with no final publish -- so there the control API is the only clean
        path; the lock it leaves behind goes stale and is taken over later."""
        proc = self.proc
        if proc is None or proc.poll() is not None:
            self._close_log()
            return True
        self.quitting = True
        clean = True
        term_is_kill = sys.platform == "win32"
        try:
            self._post("/quit", timeout=5.0)
        except _ERRORS as exc:
            log.warning("quit request failed (%s); %s", exc, _TERMINATE)
            proc.terminate()
            clean = not term_is_kill
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log.warning("server still running after %.0fs; %s", timeout, _TERMINATE)
            proc.terminate()
            clean = not term_is_kill
            try:
                proc.wait(timeout=TERM_TIMEOUT_SEC)
            except subprocess.TimeoutExpired:
                log.error(
                    "server pid %d ignored SIGTERM; killing it -- its write lock goes "
                    "stale and is taken over on the next start",
                    proc.pid,
                )
                proc.kill()
                proc.wait(timeout=5)
                clean = False
        log.info("server exited with code %s", proc.returncode)
        self._close_log()
        return clean

    def _close_log(self) -> None:
        if self._log_fh is not None:
            with contextlib.suppress(OSError):
                self._log_fh.close()
            self._log_fh = None

    # -- control API -------------------------------------------------------- #
    def _headers(self) -> dict[str, str]:
        return {TOKEN_HEADER: self.token}

    def _post(self, path: str, *, timeout: float = 5.0) -> dict[str, Any]:
        r = self._http.post(self.base_url + PREFIX + path, headers=self._headers(), timeout=timeout)
        r.raise_for_status()
        data: dict[str, Any] = r.json()
        return data

    def status(self, *, timeout: float = 3.0) -> dict[str, Any] | None:
        try:
            r = self._http.get(
                self.base_url + PREFIX + "/status", headers=self._headers(), timeout=timeout
            )
            r.raise_for_status()
            data: dict[str, Any] = r.json()
            return data
        except _ERRORS:
            return None

    def set_paused(self, paused: bool) -> bool:
        try:
            self._post("/pause" if paused else "/resume")
            return True
        except _ERRORS as exc:
            log.warning("pause/resume failed: %s", exc)
            return False

    def flush_trash(self) -> dict[str, Any] | None:
        try:
            return self._post("/flush-trash", timeout=120.0)
        except _ERRORS as exc:
            log.warning("moving deleted photos to the system Trash failed: %s", exc)
            return None
