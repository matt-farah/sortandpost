"""Where the external tools we shell out to live.

A native / Docker run finds ``exiftool`` on ``PATH``. The packaged desktop app
(V2 P5) ships its own copy inside the bundle, so it works on a machine that
never installed exiftool. Every caller goes through :func:`exiftool_path` /
:func:`exiftool_available` instead of hard-coding ``"exiftool"``.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

EXIFTOOL_ENV = "SORTANDPOST_EXIFTOOL"


def bundle_dir() -> Path | None:
    """The frozen app's data folder (PyInstaller's ``sys._MEIPASS``), or
    ``None`` when running from source."""
    if not getattr(sys, "frozen", False):
        return None
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else Path(sys.executable).resolve().parent


def _bundled_exiftool() -> Path | None:
    base = bundle_dir()
    if base is None:
        return None
    name = "exiftool.exe" if sys.platform == "win32" else "exiftool"
    candidate = base / "exiftool" / name
    return candidate if candidate.is_file() else None


def exiftool_path() -> str:
    """The exiftool to run. In order: ``$SORTANDPOST_EXIFTOOL``; the copy
    bundled in a packaged app; ``exiftool`` on ``PATH``. Always returns a string,
    which may not exist -- see :func:`exiftool_available`."""
    override = os.environ.get(EXIFTOOL_ENV, "").strip()
    if override:
        return override
    bundled = _bundled_exiftool()
    if bundled is not None:
        return str(bundled)
    return "exiftool"


def exiftool_available() -> bool:
    return shutil.which(exiftool_path()) is not None
