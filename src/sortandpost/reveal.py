"""Open a photo file in the host's native file manager / default app.

This only does something useful when the app runs **natively on the user's own
machine** -- the opener process runs on the server. Inside Docker the openers are
absent (``reveal_supported()`` returns ``False``) and the photo page falls back
to a copy-path button.

The path always comes from our own ``photo_paths`` table, never user input, and
is passed as a distinct argv element (never through a shell).
"""

from __future__ import annotations

import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from shutil import which
from urllib.parse import quote

_DEVNULL = subprocess.DEVNULL


def _os() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("win"):
        return "win"
    return "linux"


def reveal_supported() -> bool:
    """Is there a file manager we can drive on this host?"""
    match _os():
        case "darwin":
            return which("open") is not None
        case "win":
            return which("explorer") is not None
        case _:
            return which("xdg-open") is not None


def _spawn(args: list[str] | str) -> None:
    subprocess.Popen(args, stdout=_DEVNULL, stderr=_DEVNULL)  # fire and forget


def _explorer_select(path: Path) -> str:
    """Explorer's ``/select`` as a raw Windows command line. Explorer parses
    its own command line and wants only the path quoted (``/select,"C:\\a b"``);
    an argv list makes Popen quote the whole ``"/select,C:\\a b"``, which
    Explorer can't parse once the path has a space, so it opens Documents
    instead. ``"`` can't occur in a Windows path, so this quoting is exact."""
    return f'explorer /select,"{path}"'


def reveal_in_file_manager(path: Path) -> None:
    """Open the OS file manager with *path* selected (or its folder on Linux)."""
    path = Path(path).resolve()
    match _os():
        case "darwin":
            _spawn(["open", "-R", str(path)])
        case "win":
            _spawn(_explorer_select(path))
        case _:
            target = path.parent if path.parent.is_dir() else path
            _spawn(["xdg-open", str(target)])


def open_in_default_app(path: Path) -> None:
    """Open *path* in whatever app the OS associates with its type."""
    path = Path(path).resolve()
    match _os():
        case "darwin":
            _spawn(["open", str(path)])
        case "win":
            _spawn(["explorer", str(path)])
        case _:
            _spawn(["xdg-open", str(path)])


def open_with_app(path: Path, app: str) -> None:
    """Open *path* in a specific app (``open_with_app`` pref), not the OS
    default. ``app`` is a ``.app`` bundle path on macOS, an ``.exe`` path on
    Windows, or an executable/``.desktop`` path on Linux."""
    path = Path(path).resolve()
    if _os() == "darwin":
        _spawn(["open", "-a", app, str(path)])
    else:
        _spawn([app, str(path)])


def _applescript_posix_literal(path: Path) -> str:
    escaped = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'POSIX file "{escaped}"'


def _reveal_multiple_darwin(paths: list[Path]) -> None:
    if len(paths) == 1:
        _spawn(["open", "-R", str(paths[0])])
        return
    items = ", ".join(_applescript_posix_literal(p) for p in paths)
    # Multiple -e args are just concatenated lines -- no shell, no stdin needed.
    _spawn(
        [
            "osascript",
            "-e",
            'tell application "Finder"',
            "-e",
            f"reveal {{{items}}}",
            "-e",
            "activate",
            "-e",
            "end tell",
        ]
    )


def _win_select_grouped(paths: list[Path]) -> bool:
    """``SHOpenFolderAndSelectItems``, one call per parent folder, selecting every
    file in that folder at once. Best effort -- returns ``False`` on any failure
    so the caller can fall back to one Explorer window per file. Windows only;
    the body is skipped by mypy on other platforms via the literal
    ``sys.platform`` guard (see ``sortandpost.cpu.lower_process_priority``).

    The PIDLs are 64-bit pointers: without explicit ``restype`` / ``argtypes``
    ctypes truncates them to ``int`` and the call faults (which made every bulk
    reveal fall back to one window per file). The shell call also needs COM on
    the calling thread -- a NiceGUI worker thread hasn't initialised it."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    try:
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        shell32.ILCreateFromPathW.restype = ctypes.c_void_p
        shell32.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
        shell32.ILFree.restype = None
        shell32.ILFree.argtypes = [ctypes.c_void_p]
        shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long  # HRESULT
        shell32.SHOpenFolderAndSelectItems.argtypes = [
            ctypes.c_void_p,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.DWORD,
        ]
        # S_OK / S_FALSE (already initialised) pair with CoUninitialize;
        # RPC_E_CHANGED_MODE (< 0) means COM is up in another mode -- still usable.
        com_ok = ole32.CoInitialize(None) >= 0
        by_folder: dict[Path, list[Path]] = defaultdict(list)
        for p in paths:
            by_folder[p.parent].append(p)
        try:
            for folder, files in by_folder.items():
                pidl_folder = shell32.ILCreateFromPathW(str(folder))
                if not pidl_folder:
                    return False
                pidl_files: list[int] = []
                try:
                    for f in files:
                        pidl = shell32.ILCreateFromPathW(str(f))
                        if pidl:
                            pidl_files.append(pidl)
                    arr = (ctypes.c_void_p * len(pidl_files))(*pidl_files)
                    hr = shell32.SHOpenFolderAndSelectItems(pidl_folder, len(pidl_files), arr, 0)
                    if hr < 0:
                        return False
                finally:
                    shell32.ILFree(pidl_folder)
                    for pidl in pidl_files:
                        shell32.ILFree(pidl)
        finally:
            if com_ok:
                ole32.CoUninitialize()
        return True
    except (OSError, AttributeError):
        return False


def _reveal_multiple_win(paths: list[Path]) -> None:
    if len(paths) == 1 or not _win_select_grouped(paths):
        for p in paths:
            _spawn(_explorer_select(p))


def _reveal_multiple_linux(paths: list[Path]) -> None:
    if len(paths) == 1:
        reveal_in_file_manager(paths[0])
        return
    if which("dbus-send") is not None:
        uris = ",".join(f'"file://{quote(str(p))}"' for p in paths)
        _spawn(
            [
                "dbus-send",
                "--session",
                "--dest=org.freedesktop.FileManager1",
                "--type=method_call",
                "/org/freedesktop/FileManager1",
                "org.freedesktop.FileManager1.ShowItems",
                f"array:string:{uris}",
                "string:",
            ]
        )
        return
    seen: set[Path] = set()
    for p in paths:
        folder = p.parent if p.parent.is_dir() else p
        if folder not in seen:
            seen.add(folder)
            _spawn(["xdg-open", str(folder)])


def reveal_multiple_in_file_manager(paths: list[Path]) -> None:
    """Open the OS file manager with every path in *paths* selected. macOS uses
    one Finder window (AppleScript ``reveal``); Windows groups by folder (one
    Explorer window per folder, every file in it selected); Linux tries the
    running file manager's D-Bus ``ShowItems`` and falls back to opening each
    distinct parent folder."""
    resolved = [Path(p).resolve() for p in paths]
    if not resolved:
        return
    match _os():
        case "darwin":
            _reveal_multiple_darwin(resolved)
        case "win":
            _reveal_multiple_win(resolved)
        case _:
            _reveal_multiple_linux(resolved)


def open_multiple_in_default_app(paths: list[Path]) -> None:
    """Open every path in *paths* in the OS default app for its type."""
    resolved = [Path(p).resolve() for p in paths]
    if not resolved:
        return
    match _os():
        case "darwin":
            _spawn(["open", *(str(p) for p in resolved)])
        case "win":
            for p in resolved:
                _spawn(["explorer", str(p)])
        case _:
            for p in resolved:
                _spawn(["xdg-open", str(p)])


def open_multiple_with_app(paths: list[Path], app: str) -> None:
    """Open every path in *paths* in a specific app -- see :func:`open_with_app`.
    One process for the whole batch (most apps accept several file args and
    open them as tabs/windows); macOS uses ``open -a`` the same way the OS
    default opener does."""
    resolved = [Path(p).resolve() for p in paths]
    if not resolved:
        return
    if _os() == "darwin":
        _spawn(["open", "-a", app, *(str(p) for p in resolved)])
    else:
        _spawn([app, *(str(p) for p in resolved)])
