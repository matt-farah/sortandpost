"""The packaged desktop app's non-UI parts (V2 P5): runtime files, the server's
control API, the login item, bundled-binary lookup and the shutdown order."""

from __future__ import annotations

import os
import plistlib
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sortandpost import binaries
from sortandpost.desktop import control, login_item, runtime
from sortandpost.desktop.icon import render_icon, render_tray_icon


# --------------------------------------------------------------------------- #
# runtime: single instance, runtime.json, port
# --------------------------------------------------------------------------- #
def test_instance_lock_is_exclusive_and_released(tmp_path: Path) -> None:
    first = runtime.instance_lock(tmp_path)
    second = runtime.instance_lock(tmp_path)
    assert first.acquire()
    assert not second.acquire()  # a second launch finds the app running
    first.release()
    assert second.acquire()
    second.release()


def test_runtime_json_roundtrip_private_and_owned(tmp_path: Path) -> None:
    info = runtime.RuntimeInfo(pid=4242, host="127.0.0.1", port=8081, token="t0k", version="9")
    path = runtime.write_runtime(info, tmp_path)
    if sys.platform != "win32":  # Windows: %LOCALAPPDATA%'s per-user ACL, no POSIX mode
        assert stat.S_IMODE(path.stat().st_mode) == 0o600  # the token gates the control API
    assert runtime.read_runtime(tmp_path) == info
    assert info.url == "http://sortandpost.localhost:8081/"  # what the browser opens

    runtime.clear_runtime(1, tmp_path)  # someone else's pid: left alone
    assert runtime.read_runtime(tmp_path) == info
    runtime.clear_runtime(4242, tmp_path)
    assert runtime.read_runtime(tmp_path) is None


def test_read_runtime_tolerates_garbage(tmp_path: Path) -> None:
    (tmp_path / runtime.RUNTIME_NAME).write_text("{not json", encoding="utf-8")
    assert runtime.read_runtime(tmp_path) is None


def test_pick_port_skips_a_busy_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        assert not runtime.port_free(taken)
        picked = runtime.pick_port(taken, span=5)
        assert picked != taken
        assert runtime.port_free(picked)


# --------------------------------------------------------------------------- #
# control API (server side)
# --------------------------------------------------------------------------- #
class _Hooks:
    def __init__(self) -> None:
        self.paused: list[bool] = []
        self.flushed = 0
        self.shutdown_called = threading.Event()

    def build(self) -> control.ControlHooks:
        return control.ControlHooks(
            status=lambda: {"paused": bool(self.paused and self.paused[-1])},
            shutdown=self.shutdown_called.set,
            set_paused=self.paused.append,
            flush_trash=self._flush,
        )

    def _flush(self) -> dict[str, Any]:
        self.flushed += 1
        return {"moved": 3, "failed": 0}


@pytest.fixture
def api() -> tuple[TestClient, _Hooks]:
    hooks = _Hooks()
    app = FastAPI()
    app.include_router(control.build_router("s3cret", hooks.build()))
    return TestClient(app), hooks


def test_control_rejects_missing_or_wrong_token(api: tuple[TestClient, _Hooks]) -> None:
    client, hooks = api
    for method, path in [
        ("get", "/status"),
        ("post", "/pause"),
        ("post", "/resume"),
        ("post", "/flush-trash"),
        ("post", "/quit"),
    ]:
        url = control.PREFIX + path
        assert getattr(client, method)(url).status_code == 403
        bad = {control.TOKEN_HEADER: "nope"}
        assert getattr(client, method)(url, headers=bad).status_code == 403
    assert hooks.paused == [] and hooks.flushed == 0
    assert not hooks.shutdown_called.is_set()


def test_control_actions_with_token(api: tuple[TestClient, _Hooks]) -> None:
    client, hooks = api
    h = {control.TOKEN_HEADER: "s3cret"}
    assert client.post(control.PREFIX + "/pause", headers=h).json() == {"paused": True}
    assert client.get(control.PREFIX + "/status", headers=h).json() == {"paused": True}
    assert client.post(control.PREFIX + "/resume", headers=h).json() == {"paused": False}
    assert hooks.paused == [True, False]
    assert client.post(control.PREFIX + "/flush-trash", headers=h).json()["moved"] == 3
    assert client.post(control.PREFIX + "/quit", headers=h).json() == {"quitting": True}
    assert hooks.shutdown_called.wait(2.0)


def test_control_not_installed_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(control.TOKEN_ENV, raising=False)
    app = FastAPI()
    assert control.install(app, _Hooks().build()) is False
    assert not any(getattr(r, "path", "").startswith(control.PREFIX) for r in app.routes)


def test_parent_watch_shuts_down_when_orphaned() -> None:
    orphaned = threading.Event()
    ppid = {"v": 100}
    control.start_parent_watch(100, orphaned.set, interval=0.01, alive=lambda: ppid["v"] == 100)
    time.sleep(0.05)
    assert not orphaned.is_set()
    ppid["v"] = 1  # re-parented to launchd: the tray died
    assert orphaned.wait(1.0)


def test_tray_calls_survive_a_bundle_replaced_underneath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An installer deleted the running bundle's cacert.pem, which SSL_CERT_FILE
    # (set by the frozen app) still names: every call used to raise
    # FileNotFoundError, which killed Quit and the status poll.
    from sortandpost.desktop.supervisor import ServerProcess

    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "gone" / "cacert.pem"))
    port = runtime.pick_port(preferred=18_500)  # nothing listens there
    server = ServerProcess(port=port, token="t", log_file=tmp_path / "out.log")
    assert server.status() is None
    assert server.set_paused(True) is False
    assert server.flush_trash() is None

    def boom(*_a: object, **_k: object) -> None:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(server._http, "get", boom)
    assert server.status() is None  # any OSError, not only HTTP errors


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_quit_asks_off_the_menu_thread_on_windows_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    pytest.importorskip("pystray")
    from sortandpost.desktop import tray as tray_mod
    from sortandpost.desktop.supervisor import ServerProcess

    app = tray_mod.TrayApp(
        ServerProcess(port=1, token="t", log_file=tmp_path / "o.log"),
        runtime.instance_lock(tmp_path),
    )
    ran_on: list[str] = []
    done = threading.Event()

    def sequence() -> None:
        ran_on.append(threading.current_thread().name)
        done.set()

    monkeypatch.setattr(app, "_quit_sequence", sequence)
    monkeypatch.setattr(tray_mod.sys, "platform", platform)
    app._menu_quit(None, None)
    app._menu_quit(None, None)  # clicked again while the question is up: ignored
    assert done.wait(2)
    time.sleep(0.1)
    menu_thread = threading.current_thread().name
    assert len(ran_on) == 1
    assert (ran_on[0] != menu_thread) is (platform == "win32")


def test_restart_under_the_tray_exits_for_the_tray_to_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sortandpost import app

    monkeypatch.setenv(control.TOKEN_ENV, "t0k")
    monkeypatch.setattr(app._RESTART, "is_set", lambda: True)
    monkeypatch.setattr(app.logging, "shutdown", lambda: None)
    monkeypatch.setattr(app.os, "execv", lambda *_a: pytest.fail("must not exec"))
    with pytest.raises(SystemExit) as exc:
        app._reexec_if_requested()
    assert exc.value.code == control.RESTART_EXIT_CODE


@pytest.mark.parametrize(("code", "restarted"), [(control.RESTART_EXIT_CODE, True), (0, False)])
def test_tray_restarts_the_server_only_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, restarted: bool
) -> None:
    pytest.importorskip("pystray")
    from sortandpost.desktop.supervisor import ServerProcess
    from sortandpost.desktop.tray import TrayApp

    server = ServerProcess(port=1, token="t", log_file=tmp_path / "out.log")
    server.proc = subprocess.Popen([sys.executable, "-c", f"raise SystemExit({code})"])
    tray = TrayApp(server, runtime.instance_lock(tmp_path))
    calls: list[bool] = []
    monkeypatch.setattr(tray, "_refresh_menu", lambda: None)
    monkeypatch.setattr(tray, "_restart_server", lambda *, open_browser: calls.append(open_browser))
    tray._watch_for_restart()
    server.proc.wait()
    time.sleep(0.3)
    assert calls == ([False] if restarted else [])  # the page reloads itself: no new tab


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process handle")
def test_parent_alive_check_follows_the_process_on_windows() -> None:
    # Windows never re-parents an orphan, so the check holds a process handle.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        alive = control.parent_alive_check(proc.pid)
        assert alive()
    finally:
        proc.kill()
        proc.wait()
    assert not alive()
    assert not control.parent_alive_check(proc.pid)()  # a pid that's already gone


# --------------------------------------------------------------------------- #
# login item
# --------------------------------------------------------------------------- #
def test_login_item_plist_opens_the_bundle(tmp_path: Path) -> None:
    bundle = Path("/Applications/sortandpost.app")
    assert not login_item.is_enabled(tmp_path)
    path = login_item.enable(bundle, tmp_path)
    assert path == tmp_path / "Library" / "LaunchAgents" / "app.sortandpost.plist"
    data = plistlib.loads(path.read_bytes())
    assert data["Label"] == "app.sortandpost"
    assert data["ProgramArguments"] == ["/usr/bin/open", "-a", str(bundle)]
    assert data["RunAtLoad"] is True
    assert login_item.is_enabled(tmp_path)
    login_item.disable(tmp_path)
    assert not login_item.is_enabled(tmp_path)
    login_item.disable(tmp_path)  # idempotent


def test_login_item_only_for_the_packaged_app() -> None:
    assert not getattr(sys, "frozen", False)
    assert login_item.app_bundle() is None
    assert not login_item.supported()


def test_login_item_packaged_windows_exe(monkeypatch: pytest.MonkeyPatch) -> None:
    exe = Path(r"C:\Users\me\AppData\Local\Programs\sortandpost\sortandpost.exe")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "platform", "win32")
    assert login_item.app_bundle() == exe.resolve()
    assert login_item.run_command(exe) == f'"{exe}"'  # quoted: the path may hold spaces


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")
def test_login_item_run_value_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    import winreg

    # A scratch key, never the real Run key.
    scratch = r"Software\sortandpost-tests\Run"
    monkeypatch.setattr(login_item, "RUN_KEY", scratch)
    exe = Path(r"C:\Program Files\sortandpost\sortandpost.exe")
    try:
        assert not login_item.is_enabled()
        login_item.enable(exe)
        assert login_item.is_enabled()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, scratch) as key:
            assert winreg.QueryValueEx(key, "sortandpost")[0] == f'"{exe}"'
        login_item.disable()
        assert not login_item.is_enabled()
        login_item.disable()  # idempotent
    finally:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, scratch)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\sortandpost-tests")


# --------------------------------------------------------------------------- #
# bundled exiftool lookup
# --------------------------------------------------------------------------- #
def test_exiftool_path_precedence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(binaries.EXIFTOOL_ENV, raising=False)
    assert binaries.exiftool_path() == "exiftool"  # from source: whatever is on PATH

    bundled = tmp_path / "exiftool" / ("exiftool.exe" if sys.platform == "win32" else "exiftool")
    bundled.parent.mkdir()
    bundled.write_text("#!/usr/bin/perl\n", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert binaries.exiftool_path() == str(bundled)  # packaged app: its own copy

    monkeypatch.setenv(binaries.EXIFTOOL_ENV, "/opt/custom/exiftool")
    assert binaries.exiftool_path() == "/opt/custom/exiftool"  # explicit override wins


def test_frozen_without_bundled_exiftool_falls_back_to_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(binaries.EXIFTOOL_ENV, raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert binaries.exiftool_path() == "exiftool"


# --------------------------------------------------------------------------- #
# icon
# --------------------------------------------------------------------------- #
def test_icons_render() -> None:
    assert render_icon(64).size == (64, 64)
    tray = render_tray_icon()
    assert tray.mode == "RGBA" and tray.getpixel((0, 0))[3] == 0  # transparent corner


# --------------------------------------------------------------------------- #
# shutdown order: indexer stopped before the final publish
# --------------------------------------------------------------------------- #
def test_shutdown_stops_the_indexer_before_the_final_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sortandpost import app as web_app

    order: list[str] = []
    stop = threading.Event()

    def _indexer() -> None:
        stop.wait(5)
        time.sleep(0.1)  # finishing the file in hand
        order.append("indexer-exited")

    thread = threading.Thread(target=_indexer, daemon=True)
    thread.start()

    class _Manager:
        def stop(self) -> None:
            order.append("final-publish")

    monkeypatch.setattr(web_app.State, "indexer_stop", stop)
    monkeypatch.setattr(web_app.State, "indexer_thread", thread)
    monkeypatch.setattr(web_app.State, "shutting_down", False)
    closed: list[bool] = []
    monkeypatch.setattr(web_app.EXIFTOOL, "close", lambda: closed.append(True))

    web_app._shutdown_all(_Manager())  # type: ignore[arg-type]

    assert order == ["indexer-exited", "final-publish"]
    assert closed == [True]
    assert web_app.State.shutting_down


def test_no_new_indexer_once_shutting_down(monkeypatch: pytest.MonkeyPatch) -> None:
    from sortandpost import app as web_app

    monkeypatch.setattr(web_app.State, "indexer_started", False)
    monkeypatch.setattr(web_app.State, "shutting_down", True)
    web_app._start_indexer_thread(object(), object())  # type: ignore[arg-type]
    assert web_app.State.indexer_started is False


def test_server_command_from_source() -> None:
    from sortandpost.desktop.supervisor import server_command

    assert server_command() == [sys.executable, "-m", "sortandpost", "--serve"]
    assert os.path.isabs(server_command()[0])


def test_quit_reports_photos_the_trash_refused() -> None:
    pytest.importorskip("pystray")
    from sortandpost.desktop.tray import trash_failure_message

    assert trash_failure_message({"moved": 5, "failed": 0}) is None
    msg = trash_failure_message(
        {"moved": 1, "failed": 4, "error": "The volume doesn't have a trash."}
    )
    assert msg is not None
    assert "4 deleted photo(s)" in msg and "(1 were)" in msg
    assert "The volume doesn't have a trash." in msg and "Recently deleted" in msg
    gone = trash_failure_message(None)  # the server didn't answer
    assert gone is not None and "couldn't move" in gone
