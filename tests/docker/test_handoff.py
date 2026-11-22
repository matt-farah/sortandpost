"""Layer 4 -- editing checkout between a desktop-style native app and the container on one
shared folder (docs/architecture.md "Editing checkout"; V3 P6d). There's no sync tool in
between, so both see the same ``sync/`` directly: this checks the protocol and the two
apps' handling of it, not a sync tool's delay (that's on the manual checklist)."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.docker import harness
from tests.docker.library import build_library
from tests.docker.test_e2e import browser, page  # noqa: F401  (fixtures)


class NativeApp:
    """``python -m sortandpost`` on this computer, set up like the desktop app's split
    layout on the same shared folder the container uses."""

    def __init__(self, root: Path, sync: Path) -> None:
        self.port = harness.free_port()
        self.env = {
            **os.environ,
            "SORTANDPOST_PREF_DIR": str(root / "prefs"),
            "SORTANDPOST_LOCAL_DIR": str(root / "machine"),
            "HOST": "127.0.0.1",
            "PORT": str(self.port),
            "HF_HUB_OFFLINE": "1",
            "SORTANDPOST_NO_UPDATE_CHECK": "1",
        }
        self.env.pop("APP_DATA", None)
        for key in [k for k in self.env if k.startswith(("PYTEST", "NICEGUI"))]:
            del self.env[key]  # else NiceGUI thinks it runs under its screen tests
        code = (
            "import sys; from pathlib import Path\n"
            "from sortandpost.launcher_pref import remember_sync_dir\n"
            "from sortandpost.paths import Paths\n"
            "from sortandpost.onboarding import mark_complete\n"
            "remember_sync_dir(Path(sys.argv[1]))\n"
            "mark_complete(Paths.for_sync_dir(Path(sys.argv[1])), how='joined')\n"
            "print(Paths.for_sync_dir(Path(sys.argv[1])).local)"
        )
        out = subprocess.run(
            [sys.executable, "-c", code, str(sync)],
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )
        self.local = Path(out.stdout.strip().splitlines()[-1])
        self.log = (root / "native.log").open("w")
        # cwd = root: never pick up the repo's own .env
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "sortandpost"],
            env=self.env,
            cwd=root,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        self.url = f"http://127.0.0.1:{self.port}"
        harness.wait_for(self._up, timeout=90, what="the native app")

    def _up(self) -> bool:
        assert self.proc.poll() is None, "native app exited"
        return httpx.get(self.url + "/healthz", timeout=2).status_code == 200

    def device_id(self) -> str:
        data: dict[str, str] = json.loads((self.local / "device.json").read_text())
        return data["device_id"]

    def stars(self, pixel_hash: str) -> int | None:
        con = sqlite3.connect(f"file:{self.local / 'sortandpost.db'}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT stars FROM ratings WHERE pixel_hash = ?", (pixel_hash,)
            ).fetchone()
        finally:
            con.close()
        return None if row is None else int(row[0])

    def stop(self) -> int:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.log.close()
        return int(self.proc.returncode)


@pytest.fixture
def pair(
    make_stack: Callable[..., harness.Stack], tmp_path: Path
) -> Iterator[tuple[harness.Stack, NativeApp, Any]]:
    stack = make_stack(device_name="sp-nas")
    lib = build_library(stack.library, with_raw=False)
    stack.up()
    stack.wait_indexed(len(lib.files) - 1)
    harness.wait_for(lambda: stack.published_generation() > 0, what="a first publish")
    lock = stack.lock()
    assert lock is not None and lock["hostname"] == "sp-nas"
    native = NativeApp(tmp_path / "native", stack.sync)
    try:
        yield stack, native, lib
    finally:
        native.stop()


def _holder(stack: harness.Stack) -> str | None:
    lock = stack.lock()
    return None if lock is None else str(lock["device_id"])


def test_take_over_editing_and_give_it_back(
    pair: tuple[harness.Stack, NativeApp, Any],
    page: Any,  # noqa: F811
) -> None:
    stack, native, lib = pair
    docker_id = stack.device_id()
    page.goto(native.url + "/")
    page.get_by_text("read-only · sp-nas").first.wait_for()

    page.get_by_role("button", name="Take over editing").click()
    harness.wait_for(
        lambda: list((stack.sync / "locks").glob("request-*.json")), what="the request file"
    )
    # The container hands over by itself after the grace (6 s in the test config) ...
    harness.wait_for(
        lambda: (
            (stack.lock() or {}).get("handoff_to") == native.device_id()
            or _holder(stack) == native.device_id()
        ),
        timeout=60,
        what="the container's grant",
    )
    # ... and the native app takes the lock once its DB has the granted generation.
    harness.wait_for(lambda: _holder(stack) == native.device_id(), timeout=60, what="promotion")
    assert not list((stack.sync / "locks").glob("request-*.json")), "the request wasn't removed"

    ph = stack.hash_for(lib["beach"])
    page.goto(f"{native.url}/photo/{ph}")
    page.get_by_role("button", name="Delete photo").wait_for()
    page.locator("body").click(position={"x": 5, "y": 5})
    page.keyboard.press("5")
    harness.wait_for(lambda: native.stars(ph) == 5, what="the native rating")

    # Quit: final publish + release; the container (not asking) reclaims the free lock.
    assert native.stop() in (0, -signal.SIGTERM)  # uvicorn re-raises SIGTERM when done
    harness.wait_for(lambda: _holder(stack) == docker_id, timeout=90, what="the container back")
    harness.wait_for(
        lambda: stack.scalar(f"SELECT stars FROM ratings WHERE pixel_hash = '{ph}'") == 5,
        timeout=60,
        what="the native rating in the container's DB",
    )


def test_keep_editing_denies_the_request(
    pair: tuple[harness.Stack, NativeApp, Any],
    browser: Any,  # noqa: F811
) -> None:
    stack, native, _lib = pair
    docker_page = browser.new_page()
    native_page = browser.new_page()
    try:
        docker_page.goto(stack.url + "/")
        native_page.goto(native.url + "/")
        native_page.get_by_role("button", name="Take over editing").click()
        docker_page.get_by_role("button", name="Keep editing").click(timeout=30_000)
        harness.wait_for(
            lambda: (stack.lock() or {}).get("denied_device") == native.device_id(),
            what="the deny in the lock",
        )
        native_page.get_by_text("kept editing").first.wait_for(timeout=30_000)
        assert _holder(stack) == stack.device_id()
    finally:
        docker_page.close()
        native_page.close()
