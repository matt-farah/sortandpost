"""Is a folder inside a third-party file-sync tool's folder?

The machine-local data (``local/``: the working SQLite DB and its journal, the
managed trash, CLIP weights) must never sit inside a synced folder. Sync tools
corrupt SQLite files written in place, and on-demand ("Files On-Demand" /
File Provider) clients can evict a file's bytes to the cloud mid-write -- on
2026-10-08 Synology Drive evicted the working DB's rollback journal before it
had uploaded it, and every later open failed with ``disk I/O error``. They also
refuse ``send2trash`` ("The volume doesn't have a trash").

:func:`cloud_provider` names the sync tool that owns a path, from well-known
locations and the marker files those tools drop at their root. It never opens
or downloads anything, and any doubt answers ``None``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# macOS: every File Provider client (OneDrive, Google Drive, Dropbox, Box,
# Synology Drive, pCloud, ...) mounts under ~/Library/CloudStorage/<Vendor>-<acct>;
# iCloud Drive is ~/Library/Mobile Documents.
_MAC_CLOUDSTORAGE = ("Library", "CloudStorage")
_MAC_ICLOUD = ("Library", "Mobile Documents")

# The vendor part of a CloudStorage folder name -> a friendly name.
_VENDORS = {
    "onedrive": "OneDrive",
    "googledrive": "Google Drive",
    "dropbox": "Dropbox",
    "box": "Box",
    "synologydrive": "Synology Drive",
    "pcloud": "pCloud",
    "icloud": "iCloud Drive",
}

# Folder names those tools use directly under the home folder (classic clients,
# Windows, Linux).
_HOME_FOLDERS = {
    "dropbox": "Dropbox",
    "onedrive": "OneDrive",
    "google drive": "Google Drive",
    "my drive": "Google Drive",
    "synologydrive": "Synology Drive",
    "icloud drive": "iCloud Drive",
    "iclouddrive": "iCloud Drive",
    "box": "Box",
    "box sync": "Box",
    "pcloud drive": "pCloud",
    "nextcloud": "Nextcloud",
    "owncloud": "ownCloud",
    "mega": "MEGA",
    "megasync": "MEGA",
}

# Marker files / folders a sync tool keeps at the root of what it syncs.
_MARKERS = {
    ".dropbox": "Dropbox",
    ".dropbox.cache": "Dropbox",
    ".SynologyWorkingDirectory": "Synology Drive",
    ".tmp.drivedownload": "Google Drive",
    ".tmp.driveupload": "Google Drive",
    ".stfolder": "Syncthing",
    ".owncloudsync.log": "Nextcloud",
}

# Windows: environment variables the clients set to their root folder.
_WIN_ENV_ROOTS = {
    "OneDrive": "OneDrive",
    "OneDriveConsumer": "OneDrive",
    "OneDriveCommercial": "OneDrive",
}


def _vendor_name(folder: str) -> str:
    vendor = folder.split("-", 1)[0].casefold()
    return _VENDORS.get(vendor, folder.split("-", 1)[0] or "a cloud folder")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        return path.is_relative_to(root)
    except (TypeError, ValueError):
        return False


def _normalize(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError):
        return path.expanduser().absolute()


def cloud_provider(path: Path, *, home: Path | None = None) -> str | None:
    """The sync tool whose folder holds *path* (``"Synology Drive"``, ``"OneDrive"``,
    ...), or ``None``. *path* need not exist yet."""
    p = _normalize(path)
    home = _normalize(home if home is not None else Path.home())

    cloudstorage = home.joinpath(*_MAC_CLOUDSTORAGE)
    if _is_relative_to(p, cloudstorage) and p != cloudstorage:
        return _vendor_name(p.relative_to(cloudstorage).parts[0])
    if _is_relative_to(p, home.joinpath(*_MAC_ICLOUD)):
        return "iCloud Drive"

    if sys.platform.startswith("win"):
        for var, name in _WIN_ENV_ROOTS.items():
            root = os.environ.get(var)
            if root and _is_relative_to(p, _normalize(Path(root))):
                return name

    if _is_relative_to(p, home) and p != home:
        first = p.relative_to(home).parts[0].casefold()
        if first in _HOME_FOLDERS:
            return _HOME_FOLDERS[first]
        if first.startswith("onedrive"):  # "OneDrive - Contoso"
            return "OneDrive"
        if first.startswith("dropbox"):  # "Dropbox (Team)"
            return "Dropbox"

    # Marker files at the sync root, from the nearest existing ancestor up.
    for d in (p, *p.parents):
        if d == d.parent:
            break
        for marker, name in _MARKERS.items():
            try:
                if (d / marker).exists():
                    return name
            except OSError:
                continue
    return None
