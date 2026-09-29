"""The packaged desktop app's non-UI parts (V2 P5): runtime files, the server's
control API, the login item, bundled-binary lookup and the shutdown order."""

from __future__ import annotations

import os
import plistlib
import socket
import stat
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
    control.start_parent_watch(100, orphaned.set, interval=0.01, getppid=lambda: ppid["v"])
    time.sleep(0.05)
    assert not orphaned.is_set()
    ppid["v"] = 1  # re-parented to launchd: the tray died
    assert orphaned.wait(1.0)


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


# --------------------------------------------------------------------------- #
# bundled exiftool lookup
# --------------------------------------------------------------------------- #
def test_exiftool_path_precedence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(binaries.EXIFTOOL_ENV, raising=False)
    assert binaries.exiftool_path() == "exiftool"  # from source: whatever is on PATH

    bundled = tmp_path / "exiftool" / "exiftool"
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
