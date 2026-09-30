"""``sortandpost.reveal`` -- native file-manager / open-with helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sortandpost import reveal

# The macOS / Linux branches, fed POSIX paths that Path.resolve() turns into
# C:\... on Windows (where those branches never run anyway).
posix_paths = pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths")


def _abs(p: str) -> str:
    return str(Path(p).resolve())


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


@posix_paths
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
    assert spawned == [["explorer", f"/select,{_abs('/photos/a.jpg')}"]]


def test_reveal_in_file_manager_linux_opens_containing_dir(
    monkeypatch: pytest.MonkeyPatch, spawned: list[list[str]], tmp_path: Path
) -> None:
    monkeypatch.setattr(reveal, "_os", lambda: "linux")
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"x")
    reveal.reveal_in_file_manager(photo)
    assert spawned == [["xdg-open", str(tmp_path)]]


@posix_paths
def test_open_in_default_app_macos(
    monkeypatch: pytest.MonkeyPatch, spawned: list[list[str]]
) -> None:
    monkeypatch.setattr(reveal, "_os", lambda: "darwin")
    reveal.open_in_default_app(Path("/photos/a.jpg"))
    assert spawned == [["open", "/photos/a.jpg"]]


# --- multi-path reveal / open ------------------------------------------------


def test_reveal_multiple_empty_is_a_noop(
    monkeypatch: pytest.MonkeyPatch, spawned: list[list[str]]
) -> None:
    monkeypatch.setattr(reveal, "_os", lambda: "darwin")
    reveal.reveal_multiple_in_file_manager([])
    assert spawned == []


@posix_paths
def test_reveal_multiple_darwin_single_path_uses_open_dash_r(
    monkeypatch: pytest.MonkeyPatch, spawned: list[list[str]]
) -> None:
    monkeypatch.setattr(reveal, "_os", lambda: "darwin")
    reveal.reveal_multiple_in_file_manager([Path("/photos/a.jpg")])
    assert spawned == [["open", "-R", "/photos/a.jpg"]]


@posix_paths
def test_reveal_multiple_darwin_builds_one_applescript_call(
    monkeypatch: pytest.MonkeyPatch, spawned: list[list[str]]
) -> None:
    monkeypatch.setattr(reveal, "_os", lambda: "darwin")
    reveal.reveal_multiple_in_file_manager([Path("/photos/a.jpg"), Path("/photos/b.jpg")])
    assert len(spawned) == 1
    (args,) = spawned
    assert args[0] == "osascript"
    script = " ".join(args)
    assert 'POSIX file "/photos/a.jpg"' in script
    assert 'POSIX file "/photos/b.jpg"' in script
    assert "reveal {" in script


def test_reveal_multiple_darwin_escapes_quotes_in_path(
    monkeypatch: pytest.MonkeyPatch, spawned: list[list[str]]
) -> None:
    monkeypatch.setattr(reveal, "_os", lambda: "darwin")
    reveal.reveal_multiple_in_file_manager([Path('/photos/a"b.jpg'), Path("/photos/c.jpg")])
    script = " ".join(spawned[0])
    assert 'a\\"b.jpg' in script


def test_reveal_multiple_windows_falls_back_to_one_explorer_per_file(
    monkeypatch: pytest.MonkeyPatch, spawned: list[list[str]]
) -> None:
    monkeypatch.setattr(reveal, "_os", lambda: "win")
    monkeypatch.setattr(reveal, "_win_select_grouped", lambda paths: False)
    reveal.reveal_multiple_in_file_manager([Path("/photos/a.jpg"), Path("/photos/b.jpg")])
    assert spawned == [
        ["explorer", f"/select,{_abs('/photos/a.jpg')}"],
        ["explorer", f"/select,{_abs('/photos/b.jpg')}"],
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="it's on Windows")
def test_win_select_grouped_is_a_noop_off_windows() -> None:
    assert reveal._win_select_grouped([Path("/photos/a.jpg")]) is False


@posix_paths
def test_reveal_multiple_linux_uses_dbus_when_available(
    monkeypatch: pytest.MonkeyPatch, spawned: list[list[str]]
) -> None:
    monkeypatch.setattr(reveal, "_os", lambda: "linux")
    monkeypatch.setattr(reveal, "which", lambda name: "/usr/bin/dbus-send")
    reveal.reveal_multiple_in_file_manager([Path("/photos/a.jpg"), Path("/photos/b.jpg")])
    assert len(spawned) == 1
    (args,) = spawned
    assert args[0] == "dbus-send"
    assert "org.freedesktop.FileManager1.ShowItems" in args
    assert any("file:///photos/a.jpg" in a for a in args)
    assert any("file:///photos/b.jpg" in a for a in args)


def test_reveal_multiple_linux_falls_back_to_unique_parent_folders(
    monkeypatch: pytest.MonkeyPatch, spawned: list[list[str]], tmp_path: Path
) -> None:
    monkeypatch.setattr(reveal, "_os", lambda: "linux")
    monkeypatch.setattr(reveal, "which", lambda name: None)
    (tmp_path / "sub").mkdir()
    a = tmp_path / "a.jpg"
    b = tmp_path / "sub" / "b.jpg"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    reveal.reveal_multiple_in_file_manager([a, a, b])
    assert spawned == [["xdg-open", str(tmp_path)], ["xdg-open", str(tmp_path / "sub")]]


@posix_paths
def test_open_multiple_in_default_app_macos_opens_all_in_one_call(
    monkeypatch: pytest.MonkeyPatch, spawned: list[list[str]]
) -> None:
    monkeypatch.setattr(reveal, "_os", lambda: "darwin")
    reveal.open_multiple_in_default_app([Path("/photos/a.jpg"), Path("/photos/b.jpg")])
    assert spawned == [["open", "/photos/a.jpg", "/photos/b.jpg"]]


@posix_paths
def test_open_multiple_in_default_app_linux_opens_each(
    monkeypatch: pytest.MonkeyPatch, spawned: list[list[str]]
) -> None:
    monkeypatch.setattr(reveal, "_os", lambda: "linux")
    reveal.open_multiple_in_default_app([Path("/photos/a.jpg"), Path("/photos/b.jpg")])
    assert spawned == [["xdg-open", "/photos/a.jpg"], ["xdg-open", "/photos/b.jpg"]]


# --- open with a specific app -------------------------------------------------


@posix_paths
def test_open_with_app_macos_uses_open_dash_a(
    monkeypatch: pytest.MonkeyPatch, spawned: list[list[str]]
) -> None:
    monkeypatch.setattr(reveal, "_os", lambda: "darwin")
    reveal.open_with_app(Path("/photos/a.jpg"), "/Applications/Preview.app")
    assert spawned == [["open", "-a", "/Applications/Preview.app", "/photos/a.jpg"]]


def test_open_with_app_windows_runs_the_exe_directly(
    monkeypatch: pytest.MonkeyPatch, spawned: list[list[str]]
) -> None:
    monkeypatch.setattr(reveal, "_os", lambda: "win")
    reveal.open_with_app(Path("/photos/a.jpg"), "C:\\Apps\\Viewer.exe")
    assert spawned == [["C:\\Apps\\Viewer.exe", _abs("/photos/a.jpg")]]


@posix_paths
def test_open_with_app_linux_runs_the_command_directly(
    monkeypatch: pytest.MonkeyPatch, spawned: list[list[str]]
) -> None:
    monkeypatch.setattr(reveal, "_os", lambda: "linux")
    reveal.open_with_app(Path("/photos/a.jpg"), "/usr/bin/gimp")
    assert spawned == [["/usr/bin/gimp", "/photos/a.jpg"]]


def test_open_multiple_with_app_empty_is_a_noop(
    monkeypatch: pytest.MonkeyPatch, spawned: list[list[str]]
) -> None:
    monkeypatch.setattr(reveal, "_os", lambda: "darwin")
    reveal.open_multiple_with_app([], "/Applications/Preview.app")
    assert spawned == []


@posix_paths
def test_open_multiple_with_app_macos_opens_all_in_one_call(
    monkeypatch: pytest.MonkeyPatch, spawned: list[list[str]]
) -> None:
    monkeypatch.setattr(reveal, "_os", lambda: "darwin")
    reveal.open_multiple_with_app(
        [Path("/photos/a.jpg"), Path("/photos/b.jpg")], "/Applications/Preview.app"
    )
    assert spawned == [
        ["open", "-a", "/Applications/Preview.app", "/photos/a.jpg", "/photos/b.jpg"]
    ]


@posix_paths
def test_open_multiple_with_app_linux_passes_all_paths_to_one_process(
    monkeypatch: pytest.MonkeyPatch, spawned: list[list[str]]
) -> None:
    monkeypatch.setattr(reveal, "_os", lambda: "linux")
    reveal.open_multiple_with_app([Path("/photos/a.jpg"), Path("/photos/b.jpg")], "/usr/bin/gimp")
    assert spawned == [["/usr/bin/gimp", "/photos/a.jpg", "/photos/b.jpg"]]
