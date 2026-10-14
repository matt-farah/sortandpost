"""Per-device library folders -- ``sync/devices/<name>-<id8>/library.json``.

The photo library usually lives in a third-party cloud-sync folder that mounts
at a different absolute path on every machine (``/Users/...`` on macOS,
``C:\\Users\\...`` on Windows, ``/library`` inside Docker). A single shared
``library_roots`` in ``config.yaml`` therefore cannot be right everywhere, so
each device gets its own list here instead.

The file rides in ``sync/`` (so the sync tool backs it up and a fresh install on
the same machine finds its own folders), but it is keyed per device -- a device
only ever writes its own subfolder, so two devices editing at once can never
produce a conflict copy.

Each entry is a :class:`LibraryRoot`:

* ``container`` -- the path the **scanner** walks. Under Docker this is a
  bind-mount target (``/library``); native it is just the real folder.
* ``host`` -- the real path on *this* computer, used for display, "copy path",
  and the file-manager helper (:mod:`sortandpost.helper`). Same as ``container``
  on a native run.
* ``label`` -- free text for the UI.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from sortandpost.paths import Paths

log = logging.getLogger(__name__)

_ID_LEN = 8  # how many hex chars of the device_id go in the folder name


def clean_path(raw: str) -> str:
    """Trim whitespace and a single pair of wrapping quotes -- users paste paths
    straight from a shell or a "copy as pathname" that keeps the quotes."""
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    return s


def running_in_docker() -> bool:
    """Best-effort "are we inside the container?" -- decides whether the Settings
    UI asks for a separate bind-mount target or just uses the host path."""
    if Path("/.dockerenv").exists():
        return True
    try:
        return "docker" in Path("/proc/1/cgroup").read_text(encoding="utf-8")
    except OSError:
        return False


@dataclass
class LibraryRoot:
    """One library folder for the current device."""

    container: str
    host: str
    label: str = ""


# --------------------------------------------------------------------------- #
# device folder
# --------------------------------------------------------------------------- #
def _slug(value: str) -> str:
    """A filesystem-safe, lowercase slug for a hostname."""
    out = re.sub(r"[^a-z0-9]+", "-", value.strip().casefold()).strip("-")
    return out or "device"


def device_dir(paths: Paths, device_id: str, hostname: str) -> Path:
    """Resolve (and create) this device's config folder under ``sync/devices``.

    Match is by the ``-<device_id[:8]>`` suffix, so a folder is still adopted
    after the machine's hostname changes (macOS silently renames hosts on some
    networks). Only if nothing matches is a fresh
    ``<slug(hostname)>-<device_id[:8]>`` folder created.
    """
    suffix = device_id[:_ID_LEN]
    root = paths.devices_dir
    root.mkdir(parents=True, exist_ok=True)
    for existing in sorted(root.iterdir()):
        if existing.is_dir() and existing.name.endswith(f"-{suffix}"):
            return existing
    target = root / f"{_slug(hostname)}-{suffix}"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _library_file(paths: Paths, device_id: str, hostname: str) -> Path:
    return device_dir(paths, device_id, hostname) / "library.json"


# --------------------------------------------------------------------------- #
# load / save
# --------------------------------------------------------------------------- #
def _coerce(raw: object) -> LibraryRoot | None:
    if isinstance(raw, str):  # legacy: a bare path string
        s = clean_path(raw)
        return LibraryRoot(container=s, host=s) if s else None
    if not isinstance(raw, dict):
        return None
    container = raw.get("container") or raw.get("path")
    if not isinstance(container, str) or not clean_path(container):
        return None
    container = clean_path(container)
    host = raw.get("host")
    host = clean_path(host) if isinstance(host, str) and clean_path(host) else container
    label = raw.get("label")
    label = label.strip() if isinstance(label, str) else ""
    return LibraryRoot(container=container, host=host, label=label)


def load_roots(paths: Paths, device_id: str, hostname: str) -> list[LibraryRoot]:
    """This device's library folders. Missing / unreadable / malformed file -> []."""
    path = _library_file(paths, device_id, hostname)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return []
    rows = data.get("roots") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    out: list[LibraryRoot] = []
    seen: set[str] = set()
    for raw in rows:
        root = _coerce(raw)
        if root is not None and root.container not in seen:
            seen.add(root.container)
            out.append(root)
    return out


def save_roots(paths: Paths, device_id: str, hostname: str, roots: list[LibraryRoot]) -> None:
    path = _library_file(paths, device_id, hostname)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "roots": [{"container": r.container, "host": r.host, "label": r.label} for r in roots]
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def other_device_roots(paths: Paths, device_id: str) -> list[str]:
    """Every *other* device's library folders, as their ``container`` paths --
    the spelling their scanner wrote into ``photo_paths``. Read-only; an
    unreadable file is skipped (see :mod:`sortandpost.missing`)."""
    own_suffix = f"-{device_id[:_ID_LEN]}"
    out: list[str] = []
    if not paths.devices_dir.is_dir():
        return out
    for lib in sorted(paths.devices_dir.glob("*/library.json")):
        if lib.parent.name.endswith(own_suffix):
            continue
        try:
            data = json.loads(lib.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows = data.get("roots") if isinstance(data, dict) else data
        for raw in rows if isinstance(rows, list) else []:
            root = _coerce(raw)
            if root is not None and root.container not in out:
                out.append(root.container)
    return out


# --------------------------------------------------------------------------- #
# helpers used by the scanner / UI
# --------------------------------------------------------------------------- #
def container_roots(roots: list[LibraryRoot]) -> list[Path]:
    """Just the ``container`` paths -- what the indexer scans."""
    return [Path(r.container) for r in roots]


def _is_windows_path(p: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", p)) or p.startswith("\\\\")


def to_host_path(container_path: str, roots: list[LibraryRoot]) -> str:
    """Translate a stored (container) path to the real path on this computer.

    Longest matching ``container`` prefix wins; the remainder is re-joined with
    the host's separator (so a Windows ``host`` yields back-slashes). Returns
    *container_path* unchanged when nothing matches (native runs, or a path that
    predates the mapping).
    """
    best: LibraryRoot | None = None
    for r in roots:
        cp = PurePosixPath(r.container)
        try:
            PurePosixPath(container_path).relative_to(cp)
        except ValueError:
            continue
        if best is None or len(r.container) > len(best.container):
            best = r
    if best is None:
        return container_path
    rel = PurePosixPath(container_path).relative_to(PurePosixPath(best.container))
    if not rel.parts:
        return best.host
    if _is_windows_path(best.host):
        return str(PureWindowsPath(best.host, *rel.parts))
    return str(PurePosixPath(best.host, *rel.parts))


def compose_volume_line(root: LibraryRoot) -> str:
    """The ``docker-compose.yml`` volume entry that would mount this folder."""
    return f'- "{root.host}:{root.container}:rw"'
