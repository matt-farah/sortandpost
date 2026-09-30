"""The menu-bar / tray process of the desktop app (see :mod:`sortandpost.desktop`).

Owns: the single-instance lock, the server child (:class:`ServerProcess`), the
menu, and the Quit sequence -- optionally "Move N deleted photos to the system
Trash?" (only ever on the user's click, constraint #1), then a clean server stop
(indexer stop, final publish, lock release -- constraints #4 / #9).

pystray drives the menu. Its callbacks run on the UI (main) thread, so anything
slow runs on a worker thread and hops back with :meth:`TrayApp._on_main`.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

from sortandpost import __version__
from sortandpost.desktop import login_item
from sortandpost.desktop.control import RESTART_EXIT_CODE
from sortandpost.desktop.icon import render_tray_icon
from sortandpost.desktop.runtime import (
    InstanceLock,
    RuntimeInfo,
    browser_url,
    clear_runtime,
    instance_lock,
    new_token,
    pick_port,
    port_free,
    read_runtime,
    runtime_dir,
    write_runtime,
)
from sortandpost.desktop.supervisor import HOST, ServerProcess
from sortandpost.logging_setup import configure

log = logging.getLogger(__name__)

STATUS_POLL_SEC = 5.0


def _logs_dir() -> Path:
    return runtime_dir() / "logs"


def _open_path(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform == "win32":  # pragma: no cover - Windows only
        os.startfile(str(path))  # type: ignore[attr-defined,unused-ignore]
    else:  # pragma: no cover - P7
        subprocess.Popen(["xdg-open", str(path)])


def _ask(title: str, message: str, *, yes: str, no: str) -> bool:
    if sys.platform == "darwin":
        from sortandpost.desktop import _macos

        return _macos.ask(title, message, yes=yes, no=no)
    if sys.platform == "win32":  # pragma: no cover - Windows only
        from sortandpost.desktop import _windows

        return _windows.ask(title, message, yes=yes, no=no)
    return False  # no system Trash flush on Linux anyway


# What the OS calls its trash, for the Quit question.
TRASH_NAME = "Recycle Bin" if sys.platform == "win32" else "Trash"


class TrayApp:
    def __init__(self, server: ServerProcess, lock: InstanceLock) -> None:
        import pystray

        self._pystray = pystray
        self.server = server
        self.lock = lock
        self.status: dict[str, Any] | None = None
        self.ready = False
        self.crashed = False
        self.quitting = False
        self._stop_polling = threading.Event()
        self._shutdown_done = threading.Event()
        self._shutdown_lock = threading.Lock()
        self._quit_started = threading.Event()
        item, sep = pystray.MenuItem, pystray.Menu.SEPARATOR
        self.icon = pystray.Icon(
            "sortandpost",
            render_tray_icon(),
            "sortandpost",
            menu=pystray.Menu(
                item(lambda _i: self.status_text(), None, enabled=False),
                item("Open sortandpost", self._menu_open, default=True, enabled=self._can_open),
                sep,
                item(
                    lambda _i: "Resume indexing" if self._paused() else "Pause indexing",
                    self._menu_toggle_pause,
                    visible=lambda _i: self._indexer_controls(),
                ),
                item(
                    "Start at login",
                    self._menu_toggle_login,
                    checked=lambda _i: login_item.is_enabled(),
                    visible=login_item.supported(),
                ),
                item("Show logs", self._menu_logs),
                item(
                    "Restart sortandpost",
                    self._menu_restart,
                    visible=lambda _i: self.crashed and not self.quitting,
                ),
                sep,
                item("Quit sortandpost", self._menu_quit, enabled=lambda _i: not self.quitting),
            ),
        )

    # -- menu state --------------------------------------------------------- #
    def status_text(self) -> str:
        if self.quitting:
            return "Quitting…"
        if self.crashed:
            return "sortandpost stopped unexpectedly"
        if not self.ready:
            return "Starting…"
        st = self.status or {}
        if st.get("setup") or st.get("onboarding"):
            return "Waiting for setup to finish"
        if not st.get("writable", True):
            return f"Read-only · {st.get('holder') or 'another computer'} is using this library"
        if st.get("paused"):
            return "Indexing paused"
        return str(st.get("activity") or "Up to date")

    def _can_open(self, _item: object) -> bool:
        return self.ready and not self.crashed and not self.quitting

    def _paused(self) -> bool:
        return bool((self.status or {}).get("paused"))

    def _indexer_controls(self) -> bool:
        st = self.status or {}
        return self.ready and not self.crashed and not st.get("setup") and not self.quitting

    def _refresh_menu(self) -> None:
        self._on_main(self.icon.update_menu)

    def _on_main(self, fn: Any) -> None:
        if sys.platform == "darwin":
            from sortandpost.desktop import _macos

            _macos.call_on_main(fn)
        else:  # pragma: no cover
            # win32: menu callbacks already run on pystray's message-loop thread,
            # and update_menu only rebuilds an HMENU, which any thread may do.
            fn()

    # -- actions ------------------------------------------------------------ #
    def open_browser(self) -> None:
        webbrowser.open(browser_url(self.server.port))

    def _menu_open(self, _icon: object, _item: object) -> None:
        self.open_browser()

    def _menu_toggle_pause(self, _icon: object, _item: object) -> None:
        want = not self._paused()

        def _work() -> None:
            if self.server.set_paused(want):
                self.status = {**(self.status or {}), "paused": want}
            self._refresh_menu()

        threading.Thread(target=_work, daemon=True).start()

    def _menu_toggle_login(self, _icon: object, _item: object) -> None:
        bundle = login_item.app_bundle()
        if login_item.is_enabled():
            login_item.disable()
            log.info("start at login: off")
        elif bundle is not None:
            login_item.enable(bundle)
            log.info("start at login: on (%s)", bundle)
        self._refresh_menu()

    def _menu_logs(self, _icon: object, _item: object) -> None:
        logs = (self.status or {}).get("logs_dir")
        _open_path(Path(logs) if logs else _logs_dir())

    def _menu_restart(self, _icon: object, _item: object) -> None:
        self.crashed = False
        self.ready = False
        self._refresh_menu()
        threading.Thread(target=self._restart_server, daemon=True).start()

    def _restart_server(self, *, open_browser: bool = True) -> None:
        if not port_free(self.server.port, HOST):
            self.server.port = pick_port()
            self._write_runtime()
        self.server.start()
        self._watch_for_restart()
        self._await_ready(open_browser=open_browser)

    def _watch_for_restart(self) -> None:
        """The server exits with RESTART_EXIT_CODE to be started afresh (a new
        data folder); start the new one right away. Its browser page reloads by
        itself once the new server answers."""
        proc = self.server.proc

        def _wait() -> None:
            if proc is None or proc.wait() != RESTART_EXIT_CODE:
                return
            if self.quitting or self.server.quitting or self.server.proc is not proc:
                return
            log.info("server asked for a restart")
            self.ready = False
            self._refresh_menu()
            self._restart_server(open_browser=False)

        threading.Thread(target=_wait, name="sortandpost-server-watch", daemon=True).start()

    def _menu_quit(self, _icon: object, _item: object) -> None:
        if self.quitting or self._quit_started.is_set():
            return  # a second click while the question is up
        self._quit_started.set()
        if sys.platform == "win32":
            # The menu callback runs inside pystray's window procedure, while
            # Windows waits for it to return: a modal box opened there can't be
            # clicked. Ask from a thread of its own (MessageBoxW runs its own
            # message loop) and let the menu handler return at once.
            threading.Thread(target=self._quit_sequence, name="sortandpost-quit-ask").start()
        else:
            self._quit_sequence()  # macOS: AppKit's alert must run on the main thread

    def _quit_sequence(self) -> None:
        move_trash = False
        try:
            st = self.server.status() if self.ready and not self.crashed else None
            n = int((st or {}).get("trash_photos") or 0)
        except Exception:  # never let the trash question stand between the user and Quit
            log.exception("quit: could not read the server status")
            st, n = None, 0
        if n and (st or {}).get("system_trash"):
            move_trash = _ask(
                f"Move deleted photos to the {TRASH_NAME}?",
                f"{n} photo(s) you deleted are still in sortandpost's “Recently deleted”, "
                f"where they can be put back. Move them to the {TRASH_NAME} now? "
                f"(You can still restore them from the {TRASH_NAME} afterwards.)",
                yes=f"Move to {TRASH_NAME}",
                no="Keep",
            )
        self.quitting = True
        self._refresh_menu()

        def _work() -> None:
            if move_trash:
                res = self.server.flush_trash()
                log.info("quit: moved deleted photos to the system Trash: %s", res)
            self.shutdown()
            self.icon.stop()

        threading.Thread(target=_work, name="sortandpost-quit", daemon=True).start()

    def shutdown(self) -> None:
        """Stop the server cleanly and release our runtime files. Idempotent;
        used by Quit, by an OS-initiated terminate (logout) and at exit."""
        with self._shutdown_lock:
            if self._shutdown_done.is_set():
                return
            self.quitting = True
            self._stop_polling.set()
            clean = self.server.stop()
            log.info("server stopped (%s)", "clean" if clean else "killed")
            clear_runtime(os.getpid())
            self.lock.release()
            self._shutdown_done.set()

    # -- background --------------------------------------------------------- #
    def _write_runtime(self) -> None:
        write_runtime(
            RuntimeInfo(
                pid=os.getpid(),
                host=HOST,
                port=self.server.port,
                token=self.server.token,
                version=__version__,
            )
        )

    def _await_ready(self, *, open_browser: bool) -> None:
        if self.server.wait_ready():
            self.ready = True
            self.status = self.server.status()
            if open_browser:
                self.open_browser()
        elif not self.quitting:
            self.crashed = True
            log.error("server failed to start (exit %s)", self.server.exit_code())
        self._refresh_menu()

    def _poll(self) -> None:
        while not self._stop_polling.wait(STATUS_POLL_SEC):
            if self.quitting:
                return
            try:
                self._poll_once()
            except Exception:  # one bad tick must not end the polling for good
                log.exception("tray status poll failed")

    def _poll_once(self) -> None:
        if (
            self.ready
            and not self.server.alive()
            and not self.server.quitting
            and self.server.exit_code() != RESTART_EXIT_CODE  # _watch_for_restart's
        ):
            self.crashed, self.ready = True, False
            log.error("server exited unexpectedly (code %s)", self.server.exit_code())
        elif self.ready:
            st = self.server.status()
            if st is not None:
                self.status = st
        self._refresh_menu()

    def _setup(self, icon: Any) -> None:
        icon.visible = True
        if sys.platform == "win32":  # pragma: no cover - Windows only
            from sortandpost.desktop import _windows

            _windows.name_window(icon)
        self._watch_for_restart()
        self._await_ready(open_browser=True)
        threading.Thread(target=self._poll, name="sortandpost-tray-poll", daemon=True).start()

    def run(self) -> None:
        self._write_runtime()
        if sys.platform == "darwin":
            from sortandpost.desktop import _macos

            _macos.use_menu_bar_only()
            _macos.install_app_delegate(
                on_reopen=lambda: self.open_browser() if self.ready else None,
                on_terminate=self.shutdown,
            )
        elif sys.platform == "win32":  # pragma: no cover - Windows only
            from sortandpost.desktop import _windows

            _windows.hold_app_mutex()
            _windows.install_session_end(self.icon, on_end=self.shutdown)
        try:
            self.icon.run(setup=self._setup)
        finally:
            self.shutdown()  # no-op after Quit; covers any other way the loop ends


def _reuse_running(info: RuntimeInfo | None) -> int:
    if info is None:
        log.warning("sortandpost is already running, but its address is unknown")
        return 1
    log.info("sortandpost is already running (pid %d); opening %s", info.pid, info.url)
    webbrowser.open(info.url)
    return 0


def run_tray() -> int:
    """Entry point of the desktop app (``sortandpost --tray``, and the default
    for the packaged app)."""
    configure(_logs_dir(), filename="desktop.log")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # the 5 s status poll
    lock = instance_lock()
    if not lock.acquire():
        return _reuse_running(read_runtime())
    log.info("sortandpost %s desktop app starting", __version__)
    server = ServerProcess(
        port=pick_port(), token=new_token(), log_file=_logs_dir() / "server-output.log"
    )
    server.start()
    TrayApp(server, lock).run()
    return 0
