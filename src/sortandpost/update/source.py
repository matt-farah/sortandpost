"""Who installed this copy -- and so whether it may replace itself.

A package manager owns its files: replacing them behind its back makes it
"upgrade" to a version it thinks is missing, or fail its own checks. So the app
installs an update itself only where nothing else manages it:

=========  =====================================================  ============
source     how it's recognised                                    self-update
=========  =====================================================  ============
direct     the packaged app (DMG, the Windows installer, an        yes
           AppImage)
brew       a ``Caskroom/sortandpost`` folder (Homebrew cask)       yes [1]_
winget     -- runs the same Windows installer, so it reads as      yes [2]_
           ``direct``
apt        an ``install-source`` file saying ``apt`` beside the    no
           executable (the ``.deb`` build writes it)
zip        Windows, run from anywhere but the installer's folder   no
docker     :func:`~sortandpost.library_roots.running_in_docker`    no
source     not frozen (``uv run``)                                 no
=========  =====================================================  ============

.. [1] the cask declares ``auto_updates true``, Homebrew's standard for apps
   that update themselves, so ``brew upgrade`` leaves it alone.
.. [2] winget reads the installed version from the installer's uninstall
   entry, which the update rewrites, so the two agree.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal

from sortandpost.desktop.login_item import app_bundle
from sortandpost.library_roots import running_in_docker

Source = Literal["direct", "brew", "apt", "zip", "docker", "source"]
SOURCE_FILE = "install-source"
_BREW_PREFIXES = (Path("/opt/homebrew"), Path("/usr/local"))


def install_source() -> Source:
    if running_in_docker():
        return "docker"
    if not getattr(sys, "frozen", False):
        return "source"
    marker = Path(sys.executable).resolve().parent / SOURCE_FILE
    try:
        if marker.read_text(encoding="utf-8").strip() == "apt":
            return "apt"
    except OSError:
        pass
    if sys.platform == "darwin":
        brew = os.environ.get("HOMEBREW_PREFIX")
        prefixes = (Path(brew), *_BREW_PREFIXES) if brew else _BREW_PREFIXES
        if any((p / "Caskroom" / "sortandpost").is_dir() for p in prefixes):
            return "brew"
    if sys.platform == "win32" and not _in_installer_folder(Path(sys.executable)):
        return "zip"
    return "direct"


def _in_installer_folder(exe: Path) -> bool:
    """The Windows installer puts the app in ``%LOCALAPPDATA%\\Programs\\sortandpost``
    (packaging/windows/sortandpost.iss); a copy unzipped elsewhere would get a
    second install there instead of an update."""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return False
    want = (Path(local) / "Programs" / "sortandpost").resolve()
    return exe.resolve().parent == want


def install_target() -> Path | None:
    """What an update replaces: the ``.app``, the ``.exe`` or the
    ``.AppImage``; ``None`` when not running from one."""
    return app_bundle()


def can_self_update(source: Source | None = None) -> bool:
    """May this copy download and install an update itself? Only the packaged
    desktop app with a tray (the tray runs the installer after the server has
    stopped) and nothing else managing it. A Linux AppImage also needs its
    folder to be writable."""
    from sortandpost.desktop.control import control_token

    src = install_source() if source is None else source
    if src not in ("direct", "brew") or control_token() is None:
        return False
    target = install_target()
    if target is None:
        return False
    if sys.platform.startswith("linux"):
        return os.access(target.parent, os.W_OK)
    return True


def manual_hint(source: Source) -> str:
    """How to update when the app can't do it itself."""
    return {
        "apt": "Update it with your package manager (sudo apt update && sudo apt upgrade).",
        "docker": "Pull the new image: docker compose pull && docker compose up -d.",
        "source": "Pull the new version: git pull && uv sync --locked.",
        "zip": "Download the new version from the release page.",
    }.get(source, "Download the new version from the release page.")
