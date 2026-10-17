"""Where the external tools we shell out to live.

A native / Docker run finds ``exiftool`` on ``PATH``. The packaged desktop app
(V2 P5) ships its own copy inside the bundle, so it works on a machine that
never installed exiftool. Every caller goes through :func:`exiftool_path` /
:func:`exiftool_available` instead of hard-coding ``"exiftool"``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
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


def no_console_flags() -> int:
    """``creationflags`` for starting a console tool (``exiftool.exe``) from the
    windowless packaged Windows app, which would otherwise flash a console window
    for it. ``0`` (the default) everywhere else."""
    if sys.platform == "win32":
        return subprocess.CREATE_NO_WINDOW
    return 0


# PyInstaller's gdk-pixbuf runtime hook writes the bundle's loader cache to a
# temp file and points this at it: a host GTK app would load our loaders.
_BUNDLE_ONLY_VARS = frozenset({"GDK_PIXBUF_MODULE_FILE"})


def host_env(
    environ: dict[str, str] | None = None, *, bundle: Path | None = None
) -> dict[str, str]:
    """The environment for starting a *host* program -- ``xdg-open``, a browser,
    an "Open with" app, perl running exiftool -- from the packaged Linux app.

    PyInstaller's bootloader and runtime hooks point ``LD_LIBRARY_PATH``,
    ``GI_TYPELIB_PATH``, ``GTK_*``, ``SSL_CERT_FILE`` & co. into the bundle; a
    host program started with them loads the bundle's GTK / GLib instead of its
    own and can crash. So: a variable with a saved ``<NAME>_ORIG`` gets that
    value back, ``_PYI_*`` / ``_MEI*`` go, so do :data:`_BUNDLE_ONLY_VARS` (set
    by a runtime hook to a file outside the bundle), and bundle entries are
    dropped from every other value (a ``:``-separated list keeps its other
    entries). A copy of
    the environment, unchanged, everywhere else (source runs, macOS, Windows).
    Our own server child keeps the bundle's environment: it *is* the bundle."""
    env = dict(os.environ if environ is None else environ)
    if bundle is None:
        bundle = bundle_dir() if sys.platform.startswith("linux") else None
    if bundle is None:
        return env
    base = str(bundle).rstrip(os.sep)

    def _inside(entry: str) -> bool:
        return entry == base or entry.startswith(base + os.sep)

    for key in list(env):
        if key.endswith("_ORIG"):
            continue
        if key + "_ORIG" in env:
            env[key] = env[key + "_ORIG"]
        elif key.startswith(("_PYI_", "_MEI")) or key in _BUNDLE_ONLY_VARS:
            del env[key]
        else:
            entries = env[key].split(os.pathsep)
            kept = [e for e in entries if not _inside(e)]
            if len(kept) != len(entries):
                if any(kept):
                    env[key] = os.pathsep.join(kept)
                else:
                    del env[key]
    for key in [k for k in env if k.endswith("_ORIG")]:
        del env[key]
    return env
