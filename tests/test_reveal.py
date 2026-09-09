"""``sortandpost.reveal`` -- native file-manager / open-with helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from sortandpost import reveal


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(reveal, "_spawn", calls.append)
    return calls


@pytest.mark.parametrize(
    ("osname", "which_hits", "expected"),
    [
        ("darwin", {"open"}, True),
        ("darwin", set(), False),
        ("win", {"explorer"}, True),
        ("linux", {"xdg-open"}, True),
        ("linux", set(), False),
    ],
)
def test_reveal_supported(
    monkeypatch: pytest.MonkeyPatch, osname: str, which_hits: set[str], expected: bool
) -> None:
    monkeypatch.setattr(reveal, "_os", lambda: osname)
    monkeypatch.setattr(reveal, "which", lambda name: name if name in which_hits else None)
    assert reveal.reveal_supported() is expected


def test_reveal_in_file_manager_macos_selects_file(
    monkeypatch: pytest.MonkeyPatch, spawned: list[list[str]]
) -> None:
    monkeypatch.setattr(reveal, "_os", lambda: "darwin")
    reveal.reveal_in_file_manager(Path("/photos/a.jpg"))
    assert spawned == [["open", "-R", "/photos/a.jpg"]]


def test_reveal_in_file_manager_windows_selects_file(
    monkeypatch: pytest.MonkeyPatch, spawned: list[list[str]]
) -> None:
    monkeypatch.setattr(reveal, "_os", lambda: "win")
    reveal.reveal_in_file_manager(Path("/photos/a.jpg"))
    assert spawned == [["explorer", "/select,/photos/a.jpg"]]


def test_reveal_in_file_manager_linux_opens_containing_dir(
    monkeypatch: pytest.MonkeyPatch, spawned: list[list[str]], tmp_path: Path
) -> None:
    monkeypatch.setattr(reveal, "_os", lambda: "linux")
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"x")
    reveal.reveal_in_file_manager(photo)
    assert spawned == [["xdg-open", str(tmp_path)]]


def test_open_in_default_app_macos(
    monkeypatch: pytest.MonkeyPatch, spawned: list[list[str]]
) -> None:
    monkeypatch.setattr(reveal, "_os", lambda: "darwin")
    reveal.open_in_default_app(Path("/photos/a.jpg"))
    assert spawned == [["open", "/photos/a.jpg"]]
