"""The tray's "Start at login" checkbox (off by default).

macOS: a per-user LaunchAgent, ``~/Library/LaunchAgents/app.sortandpost.plist``,
that runs ``open -a <sortandpost.app>`` at login -- going through
LaunchServices, so it starts exactly like a double-click (single instance,
menu-bar only). Only offered for the packaged app: a source checkout has no
stable bundle to point at. Windows / Linux land with their packaging (P6 / P7).
"""

from __future__ import annotations

import contextlib
import plistlib
import sys
from pathlib import Path

LABEL = "app.sortandpost"


def app_bundle() -> Path | None:
    """``…/sortandpost.app`` when running from a packaged macOS app."""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()  # …/sortandpost.app/Contents/MacOS/sortandpost
    bundle = exe.parents[2] if len(exe.parents) > 2 else None
    return bundle if bundle is not None and bundle.suffix == ".app" else None


def supported() -> bool:
    return app_bundle() is not None


def agent_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def agent_plist(bundle: Path) -> dict[str, object]:
    return {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/open", "-a", str(bundle)],
        "RunAtLoad": True,
        "ProcessType": "Interactive",
    }


def is_enabled(home: Path | None = None) -> bool:
    return agent_path(home).is_file()


def enable(bundle: Path, home: Path | None = None) -> Path:
    path = agent_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".plist.tmp")
    with open(tmp, "wb") as fh:
        plistlib.dump(agent_plist(bundle), fh)
    tmp.replace(path)
    return path


def disable(home: Path | None = None) -> None:
    with contextlib.suppress(FileNotFoundError):
        agent_path(home).unlink()
