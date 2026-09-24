"""``SharedExiftool``: one lazily-started, serialized client with idle close."""

from __future__ import annotations

import threading
import time
from typing import ClassVar

import pytest

from sortandpost.metadata import ExiftoolClient
from sortandpost.metadata import shared as shared_mod
from sortandpost.metadata.shared import SharedExiftool


class FakeClient:
    instances: ClassVar[list[FakeClient]] = []

    def __init__(self, path: str) -> None:
        self.path = path
        self.write_windows_compat_tags = True
        self.starts = 0
        self.closed = False
        FakeClient.instances.append(self)

    def start(self) -> None:
        self.starts += 1

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _fake_exiftool(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeClient.instances = []
    monkeypatch.setattr(shared_mod.shutil, "which", lambda _p: "/usr/bin/exiftool")


def _shared(idle: float = 60.0) -> SharedExiftool:
    def make(path: str) -> ExiftoolClient:
        return FakeClient(path)  # type: ignore[return-value]

    return SharedExiftool(idle_seconds=idle, client_factory=make)


def test_reuses_one_client_across_sessions() -> None:
    sx = _shared()
    with sx.session() as a:
        pass
    with sx.session() as b:
        pass
    assert a is b
    assert len(FakeClient.instances) == 1
    assert FakeClient.instances[0].starts == 2  # start() is idempotent on the real client
    sx.close()
    assert FakeClient.instances[0].closed and not sx.running


def test_yields_none_when_exiftool_is_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shared_mod.shutil, "which", lambda _p: None)
    sx = _shared()
    with sx.session() as client:
        assert client is None
    assert FakeClient.instances == []


def test_compat_flag_is_applied_per_session() -> None:
    sx = _shared()
    with sx.session(write_windows_compat_tags=False) as c:
        assert c is not None and c.write_windows_compat_tags is False
    with sx.session(write_windows_compat_tags=True) as c:
        assert c is not None and c.write_windows_compat_tags is True
    sx.close()


def test_sessions_are_serialized_across_threads() -> None:
    sx = _shared()
    inside = 0
    overlap = False
    guard = threading.Lock()

    def worker() -> None:
        nonlocal inside, overlap
        with sx.session():
            with guard:
                inside += 1
                overlap |= inside > 1
            time.sleep(0.02)
            with guard:
                inside -= 1

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not overlap
    sx.close()


def test_idle_client_is_closed_and_restarted_on_demand() -> None:
    sx = _shared(idle=0.05)
    with sx.session():
        pass
    deadline = time.monotonic() + 2
    while sx.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not sx.running and FakeClient.instances[0].closed

    with sx.session() as again:
        assert again is FakeClient.instances[1]
    sx.close()


def test_session_releases_the_lock_when_the_body_raises() -> None:
    sx = _shared()
    with pytest.raises(RuntimeError), sx.session():
        raise RuntimeError("boom")
    with sx.session() as client:  # would deadlock if the lock leaked
        assert client is not None
    sx.close()
