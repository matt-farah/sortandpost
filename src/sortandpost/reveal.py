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
from pathlib import Path
from shutil import which

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


def _spawn(args: list[str]) -> None:
    subprocess.Popen(args, stdout=_DEVNULL, stderr=_DEVNULL)  # fire and forget


def reveal_in_file_manager(path: Path) -> None:
    """Open the OS file manager with *path* selected (or its folder on Linux)."""
    path = Path(path).resolve()
    match _os():
        case "darwin":
            _spawn(["open", "-R", str(path)])
        case "win":
            _spawn(["explorer", f"/select,{path}"])
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
