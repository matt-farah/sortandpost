"""First-run onboarding (V2 #23): state + the non-UI decisions behind the wizard.

Two phases, because the shared folder has to be known before anything is opened:

1. **Setup mode** -- no data folder chosen yet on this machine
   (:meth:`Env.resolve_paths` returns ``None``). The app opens no DB and takes no
   lock; ``/welcome`` only asks for the shared folder, :func:`inspect_folder`
   tells a new library from an existing synced one, the choice is saved
   (:func:`launcher_pref.remember_sync_dir`) and the process restarts itself.
2. **Wizard** -- folders are open but this *machine* hasn't finished setup
   (no ``local/onboarding.json``). Every page redirects to ``/welcome`` and the
   indexer does not start until it's finished. A machine joining an existing
   library only picks its own library folders (+ optional theme); the shared
   settings from ``config.yaml`` are adopted as they are.

Installs that predate onboarding (a ``local/device.json`` already on disk) are
marked finished silently -- see :func:`settle_on_startup`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from sortandpost.library_roots import LibraryRoot, clean_path
from sortandpost.paths import Paths

log = logging.getLogger(__name__)

_MARKER = "onboarding.json"
_STARTED = "onboarding_started.json"  # wizard in progress: was the library new or joined?
# Leftovers a sync tool / OS drops into an otherwise empty folder.
_IGNORABLE = {".ds_store", "desktop.ini", "thumbs.db", ".synologyworkingdirectory"}
# Subfolder used when the picked folder already holds unrelated files.
NEW_LIBRARY_SUBDIR = "sortandpost"


def marker_file(paths: Paths) -> Path:
    return paths.local / _MARKER


def is_complete(paths: Paths) -> bool:
    return marker_file(paths).exists()


def mark_complete(paths: Paths, *, how: str) -> None:
    path = marker_file(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    payload = {"completed_at": datetime.now(UTC).isoformat(timespec="seconds"), "how": how}
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)
    (paths.local / _STARTED).unlink(missing_ok=True)


@dataclass(frozen=True)
class StartupState:
    pending: bool  # this machine still has to finish the wizard
    joined_existing: bool  # the shared folder already held a library (config.yaml)
    fresh_device: bool  # no device.json yet -- a new machine for this data folder


def settle_on_startup(paths: Paths) -> StartupState:
    """Decide onboarding state. Must run **before** :func:`config.bootstrap`,
    which creates ``device.json`` / ``config.yaml`` and would hide the answer.

    Single-folder layout only: a pre-existing ``device.json`` means this machine
    already ran against this data folder before onboarding existed -- mark it
    finished so upgrading never shows the wizard. (The split layout is new with
    onboarding, so there a missing marker always means "not finished" -- e.g.
    the app was closed half-way through the wizard.)"""
    fresh_device = not paths.device_file.exists()
    joined = paths.config_file.exists()
    if is_complete(paths):
        return StartupState(pending=False, joined_existing=joined, fresh_device=fresh_device)
    if not fresh_device and not paths.is_split:
        mark_complete(paths, how="existing-install")
        return StartupState(pending=False, joined_existing=joined, fresh_device=False)
    # Remember whether the library was new or joined the first time we saw it:
    # bootstrap writes config.yaml, so after a restart mid-wizard a brand-new
    # library would otherwise look like an existing one.
    started = paths.local / _STARTED
    try:
        joined = bool(json.loads(started.read_text(encoding="utf-8"))["joined_existing"])
    except (OSError, ValueError, KeyError, TypeError):
        started.parent.mkdir(parents=True, exist_ok=True)
        started.write_text(json.dumps({"joined_existing": joined}), encoding="utf-8")
    return StartupState(pending=True, joined_existing=joined, fresh_device=fresh_device)


# --------------------------------------------------------------------------- #
# picking the shared folder
# --------------------------------------------------------------------------- #
FolderKind = Literal["new", "existing", "invalid"]


@dataclass
class FolderInspection:
    chosen: str  # what the user typed / browsed to
    kind: FolderKind
    sync_dir: Path | None = None  # the folder that will actually be used
    message: str = ""
    photos: int | None = None  # existing library: photos in the published DB
    last_host: str | None = None  # existing library: machine that last held the lock
    devices: list[str] = field(default_factory=list)  # existing: other machines' names


def _is_sync_tree(d: Path) -> bool:
    return (d / "config.yaml").is_file() or (d / "sortandpost.db").is_file()


def _is_empty(d: Path) -> bool:
    try:
        return all(c.name.casefold() in _IGNORABLE for c in d.iterdir())
    except OSError:
        return False


def inspect_folder(
    raw: str, *, local_root: Path | None = None, this_device: str | None = None
) -> FolderInspection:
    """What happens if the user picks *raw* as the shared folder.

    * a sync tree itself (``config.yaml`` / ``sortandpost.db``) -> existing
    * a legacy app-data folder whose ``sync/`` is one -> existing, use ``sync/``
    * a folder holding a ``sortandpost/`` sync tree -> existing, use that
    * missing or empty -> new library right there
    * anything else (unrelated files) -> new library in ``<raw>/sortandpost``
    """
    text = clean_path(raw)
    if not text:
        return FolderInspection(chosen=raw, kind="invalid", message="Enter a folder path.")
    chosen = Path(text).expanduser()
    if not chosen.is_absolute():
        return FolderInspection(
            chosen=raw, kind="invalid", message="Use a full path, e.g. /Users/you/Dropbox/Photos."
        )
    if chosen.exists() and not chosen.is_dir():
        return FolderInspection(chosen=raw, kind="invalid", message="That is a file, not a folder.")
    if local_root is not None and _within(chosen, local_root):
        return FolderInspection(
            chosen=raw,
            kind="invalid",
            message="That folder is this computer's private data folder — pick a shared one.",
        )

    for candidate in (chosen, chosen / "sync", chosen / NEW_LIBRARY_SUBDIR):
        if candidate.is_dir() and _is_sync_tree(candidate):
            return _existing(raw, candidate, this_device)

    if not chosen.exists() or _is_empty(chosen):
        return FolderInspection(
            chosen=raw,
            kind="new",
            sync_dir=chosen,
            message="A new, empty library will be created in this folder.",
        )
    target = chosen / NEW_LIBRARY_SUBDIR
    return FolderInspection(
        chosen=raw,
        kind="new",
        sync_dir=target,
        message=f"This folder already has other files, so the library will go in {target}.",
    )


def _within(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _existing(raw: str, sync_dir: Path, this_device: str | None = None) -> FolderInspection:
    photos = _count_photos(sync_dir / "sortandpost.db")
    last_host = _lock_host(sync_dir / ".sortandpost.lock", this_device)
    devices = sorted(
        d.name.rsplit("-", 1)[0]
        for d in (sync_dir / "devices").glob("*")
        if d.is_dir() and "-" in d.name
    )
    parts = ["Found an existing sortandpost.photos library"]
    if photos is not None:
        parts.append(f"with {photos:,} photo{'s' if photos != 1 else ''}")
    msg = " ".join(parts) + "."
    if last_host:
        msg += f" Last used on {last_host}."
    msg += " Its ratings, tags and settings will be used on this computer too."
    return FolderInspection(
        chosen=raw,
        kind="existing",
        sync_dir=sync_dir,
        message=msg,
        photos=photos,
        last_host=last_host,
        devices=devices,
    )


def _count_photos(db: Path) -> int | None:
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute("SELECT COUNT(*) FROM photos WHERE missing_since IS NULL").fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        log.warning("could not read %s: %s", db, exc)
        return None
    return int(row[0]) if row else None


def _lock_host(lock: Path, this_device: str | None = None) -> str | None:
    """The machine named in the lock, unless it's *this_device* (the wizard runs
    with the library open, so this machine already holds the lock)."""
    try:
        data = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or (this_device and data.get("device_id") == this_device):
        return None
    host = data.get("hostname")
    return host if isinstance(host, str) and host else None


# --------------------------------------------------------------------------- #
# library-folder suggestions for a machine joining an existing library
# --------------------------------------------------------------------------- #
def suggested_roots(paths: Paths, device_id: str) -> list[LibraryRoot]:
    """Library folders other machines use that also exist on *this* one (same
    user name + same cloud-drive layout is common across a person's Macs), so a
    second machine can often finish without typing a path."""
    own_suffix = f"-{device_id[:8]}"
    out: list[LibraryRoot] = []
    seen: set[str] = set()
    for lib in sorted(paths.devices_dir.glob("*/library.json")):
        if lib.parent.name.endswith(own_suffix):
            continue
        try:
            data = json.loads(lib.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows = data.get("roots") if isinstance(data, dict) else None
        for row in rows if isinstance(rows, list) else []:
            host = row.get("host") if isinstance(row, dict) else None
            if not isinstance(host, str) or host in seen:
                continue
            if Path(host).is_dir():
                seen.add(host)
                label = str(row.get("label") or "")
                out.append(LibraryRoot(container=host, host=host, label=label))
    return out


# --------------------------------------------------------------------------- #
# finding the shared folder for the user
# --------------------------------------------------------------------------- #
def cloud_folder_candidates(home: Path | None = None) -> list[Path]:
    """Cloud-sync folders present on this machine -- where a shared library
    usually lives (macOS File Provider drives, Dropbox, OneDrive, Google
    Drive, iCloud Drive, Nextcloud, ...)."""
    home = home or Path.home()
    found: list[Path] = []
    cloud_storage = home / "Library" / "CloudStorage"  # macOS 12.3+ File Provider
    with contextlib.suppress(OSError):
        if cloud_storage.is_dir():
            found += sorted(p for p in cloud_storage.iterdir() if p.is_dir())
    names = ["Dropbox", "Google Drive", "Nextcloud", "pCloud Drive", "SynologyDrive", "Box"]
    with contextlib.suppress(OSError):
        names += sorted(p.name for p in home.glob("OneDrive*") if p.is_dir())
    for name in names:
        p = home / name
        if p.is_dir():
            found.append(p)
    icloud = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    if icloud.is_dir():
        found.append(icloud)
    return list(dict.fromkeys(found))
