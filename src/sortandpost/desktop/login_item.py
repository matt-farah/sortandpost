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
* Linux (V3 P7): an XDG autostart entry,
  ``${XDG_CONFIG_HOME:-~/.config}/autostart/app.sortandpost.desktop``, that runs
  the AppImage (``$APPIMAGE``: the file the user launched, not its temporary
  mount). A bare PyInstaller folder has no stable path, so no login item there.
"""

from __future__ import annotations

import contextlib
import os
import plistlib
import sys
from pathlib import Path

LABEL = "app.sortandpost"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "sortandpost"


def app_bundle() -> Path | None:
    """What to start at login: ``…/sortandpost.app`` (macOS),
    ``…/sortandpost.exe`` (Windows) or the ``.AppImage`` (Linux) when running
    from the packaged app."""
    if not getattr(sys, "frozen", False):
        return None
    if sys.platform.startswith("linux"):
        appimage = os.environ.get("APPIMAGE", "").strip()
        return Path(appimage) if appimage and Path(appimage).is_file() else None
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


# -- Linux: XDG autostart ---------------------------------------------------- #
def autostart_path(home: Path | None = None) -> Path:
    if home is not None:
        config = home / ".config"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
        config = Path(xdg) if xdg else Path.home() / ".config"
    return config / "autostart" / f"{LABEL}.desktop"


def _exec_quote(path: Path) -> str:
    """A desktop entry ``Exec`` argument, per the spec: double-quoted with ``"``
    `` ` `` ``$`` ``\\`` backslash-escaped, ``%`` doubled (field codes), then
    every backslash doubled again (the string-value escape layer)."""
    quoted = '"' + "".join("\\" + c if c in '"`$\\' else c for c in str(path)) + '"'
    return quoted.replace("%", "%%").replace("\\", "\\\\")


def autostart_entry(appimage: Path) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=sortandpost.photos\n"
        "Comment=Start sortandpost.photos at login\n"
        f"Exec={_exec_quote(appimage)}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


# -- Windows: HKCU Run value ------------------------------------------------- #
def run_command(exe: Path) -> str:
    return f'"{exe}"'


def _run_key_get() -> str | None:
    if sys.platform != "win32":  # callers check; this also tells mypy winreg exists
        raise OSError("the Run key is Windows-only")
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, RUN_VALUE)
    except OSError:
        return None
    return str(value)


def _run_key_set(command: str | None) -> None:
    if sys.platform != "win32":
        raise OSError("the Run key is Windows-only")
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if command is None:
            with contextlib.suppress(FileNotFoundError):
                winreg.DeleteValue(key, RUN_VALUE)
        else:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command)


# -- public ------------------------------------------------------------------ #
def _file_path(home: Path | None) -> Path:
    """The login item's file: the autostart entry on Linux, else the plist."""
    return autostart_path(home) if sys.platform.startswith("linux") else agent_path(home)


def is_enabled(home: Path | None = None) -> bool:
    if sys.platform == "win32" and home is None:
        return _run_key_get() is not None
    return _file_path(home).is_file()


def enable(bundle: Path, home: Path | None = None) -> Path:
    if sys.platform == "win32" and home is None:
        _run_key_set(run_command(bundle))
        return bundle
    path = _file_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if sys.platform.startswith("linux"):
        tmp.write_text(autostart_entry(bundle), encoding="utf-8")
    else:
        with open(tmp, "wb") as fh:
            plistlib.dump(agent_plist(bundle), fh)
    tmp.replace(path)
    return path


def disable(home: Path | None = None) -> None:
    if sys.platform == "win32" and home is None:
        _run_key_set(None)
        return
    with contextlib.suppress(FileNotFoundError):
        _file_path(home).unlink()
