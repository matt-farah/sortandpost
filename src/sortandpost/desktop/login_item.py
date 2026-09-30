"""The tray's "Start at login" checkbox (off by default). Only offered for the
packaged app: a source checkout has no stable executable to point at.

* macOS: a per-user LaunchAgent, ``~/Library/LaunchAgents/app.sortandpost.plist``,
  that runs ``open -a <sortandpost.app>`` at login -- going through
  LaunchServices, so it starts exactly like a double-click (single instance,
  menu-bar only).
* Windows: a ``sortandpost`` value under
  ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`` holding the quoted
  ``sortandpost.exe`` path (per user, no admin rights). A second instance just
  opens the browser, so a manual launch after login is harmless.

Linux lands with its packaging (P7).
"""

from __future__ import annotations

import contextlib
import plistlib
import sys
from pathlib import Path

LABEL = "app.sortandpost"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "sortandpost"


def app_bundle() -> Path | None:
    """What to start at login: ``…/sortandpost.app`` (macOS) or
    ``…/sortandpost.exe`` (Windows) when running from the packaged app."""
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()
    if sys.platform == "win32":
        return exe
    if sys.platform != "darwin":
        return None
    bundle = exe.parents[2] if len(exe.parents) > 2 else None  # …/Contents/MacOS/sortandpost
    return bundle if bundle is not None and bundle.suffix == ".app" else None


def supported() -> bool:
    return app_bundle() is not None


# -- macOS: LaunchAgent ------------------------------------------------------ #
def agent_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def agent_plist(bundle: Path) -> dict[str, object]:
    return {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/open", "-a", str(bundle)],
        "RunAtLoad": True,
        "ProcessType": "Interactive",
    }


# -- Windows: HKCU Run value ------------------------------------------------- #
def run_command(exe: Path) -> str:
    return f'"{exe}"'


def _run_key_get() -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, RUN_VALUE)
    except OSError:
        return None
    return str(value)


def _run_key_set(command: str | None) -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if command is None:
            with contextlib.suppress(FileNotFoundError):
                winreg.DeleteValue(key, RUN_VALUE)
        else:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command)


# -- public ------------------------------------------------------------------ #
def is_enabled(home: Path | None = None) -> bool:
    if sys.platform == "win32" and home is None:
        return _run_key_get() is not None
    return agent_path(home).is_file()


def enable(bundle: Path, home: Path | None = None) -> Path:
    if sys.platform == "win32" and home is None:
        _run_key_set(run_command(bundle))
        return bundle
    path = agent_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".plist.tmp")
    with open(tmp, "wb") as fh:
        plistlib.dump(agent_plist(bundle), fh)
    tmp.replace(path)
    return path


def disable(home: Path | None = None) -> None:
    if sys.platform == "win32" and home is None:
        _run_key_set(None)
        return
    with contextlib.suppress(FileNotFoundError):
        agent_path(home).unlink()
