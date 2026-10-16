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
* ``host`` -- the real path on *this* computer, used for display and "copy
  path". Same as ``container`` on a native run.
* ``label`` -- free text for the UI.
* ``network`` -- optional, Docker: how other computers reach ``host`` over the
  network (``\\\\nas\\photo\\Film`` or ``smb://nas/photo/Film``). The photo page's
  copy button then copies a file's network path, spelled for the viewer's OS
  (:func:`network_parts` / :func:`format_network_path`).
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Literal
from urllib.parse import quote, unquote, urlsplit

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
    network: str = ""


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
    network = raw.get("network")
    network = clean_path(network) if isinstance(network, str) else ""
    return LibraryRoot(container=container, host=host, label=label, network=network)


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
        "roots": [
            {"container": r.container, "host": r.host, "label": r.label}
            | ({"network": r.network} if r.network else {})
            for r in roots
        ]
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


@dataclass(frozen=True)
class RootCheck:
    """What :func:`check_new_root` decided about a folder the user is adding.

    ``error`` refuses it. ``warning`` lets it through after the user confirms.
    ``root`` is the entry to save (its paths normalized). ``replaces`` lists the
    ``container`` of each existing root inside the new one: they are dropped on
    save, so no file is walked twice.
    """

    root: LibraryRoot | None = None
    error: str = ""
    warning: str = ""
    replaces: tuple[str, ...] = ()


def _name(root: LibraryRoot) -> str:
    return root.label or root.host


def check_new_root(
    root: LibraryRoot,
    existing: list[LibraryRoot],
    app_dirs: list[Path],
    *,
    in_docker: bool,
) -> RootCheck:
    """Validate a library folder before it is saved (Issues.md S7).

    Native, the folder itself is checked: a full path, not a file, not ``/``, a
    drive, the home folder or the folder holding every user's home, not inside
    the app's own data (*app_dirs*: ``local/``, ``sync/``, the per-machine
    folder). A folder that doesn't exist only warns -- it may be an unplugged
    drive or an unmounted share (constraint #8). Under Docker only the
    ``container`` path can be checked (the host path may be another OS's), and a
    path that isn't mounted yet is normal.

    Against *existing*: the same folder (``lib`` vs ``lib/``, or a symlink to it)
    or one inside an existing root is refused; an existing root inside the new
    one is replaced.
    """
    if in_docker:
        raw = clean_path(root.container)
        if not raw or not PurePosixPath(raw).is_absolute():
            return RootCheck(error="The mount point must be a full path, like /library.")
        norm = posixpath.normpath(raw)
        if norm.startswith("//"):  # normpath keeps a leading "//"
            norm = "/" + norm.lstrip("/")
        key: PurePath = PurePosixPath(norm)
        if norm == "/":
            return RootCheck(error="The mount point can't be / -- use e.g. /library.")

        def keyof(r: LibraryRoot) -> PurePath:
            return PurePosixPath(posixpath.normpath(r.container))

        app_keys: list[PurePath] = [PurePosixPath(posixpath.normpath(str(d))) for d in app_dirs]
        saved = LibraryRoot(container=norm, host=root.host, label=root.label, network=root.network)
        warning = ""
    else:
        raw = os.path.expanduser(clean_path(root.host))
        if not raw or not os.path.isabs(raw):
            return RootCheck(
                error="Enter the folder's full path (e.g. ~/Pictures or /Volumes/Photos), "
                "not a relative one."
            )
        norm = os.path.normpath(raw)
        path = Path(norm)
        try:
            exists = path.exists()
            is_dir = path.is_dir()
        except OSError:
            exists = is_dir = False
        if exists and not is_dir:
            return RootCheck(error="That's a file -- pick the folder your photos are in.")
        key = _resolved(path)
        home = _resolved(Path.home())
        if key == Path(key.anchor) or key in (home, home.parent):
            return RootCheck(
                error="That folder is too broad (a whole disk or every home folder) -- "
                "pick the folder your photos are in."
            )

        def keyof(r: LibraryRoot) -> PurePath:
            return _resolved(Path(r.container))

        app_keys = [_resolved(d) for d in app_dirs]
        saved = LibraryRoot(container=norm, host=norm, label=root.label, network=root.network)
        warning = (
            ""
            if exists
            else "This folder isn't there right now. If it's on a drive or network share "
            "that isn't connected, add it anyway -- it's scanned once it's back."
        )

    for d in app_keys:
        if key == d or key.is_relative_to(d):
            return RootCheck(
                error=f"That's the sortandpost.photos data folder ({d}), not a photo folder."
            )
    replaces: list[str] = []
    for other in existing:
        okey = keyof(other)
        if key == okey:
            return RootCheck(error=f"That folder is already in the list ({_name(other)}).")
        if key.is_relative_to(okey):
            return RootCheck(error=f"That folder is already scanned: it's inside {_name(other)}.")
        if okey.is_relative_to(key):
            replaces.append(other.container)
    if replaces:
        names = ", ".join(_name(r) for r in existing if r.container in replaces)
        note = f"It contains {names}, which will be replaced by this folder."
        warning = f"{warning} {note}".strip()
    return RootCheck(root=saved, warning=warning, replaces=tuple(replaces))


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _is_windows_path(p: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", p)) or p.startswith("\\\\")


def _match_root(
    container_path: str, roots: list[LibraryRoot]
) -> tuple[LibraryRoot, PurePosixPath] | None:
    """The root holding *container_path* (longest ``container`` prefix wins) and
    the path relative to it."""
    best: LibraryRoot | None = None
    for r in roots:
        try:
            PurePosixPath(container_path).relative_to(PurePosixPath(r.container))
        except ValueError:
            continue
        if best is None or len(r.container) > len(best.container):
            best = r
    if best is None:
        return None
    return best, PurePosixPath(container_path).relative_to(PurePosixPath(best.container))


def to_host_path(container_path: str, roots: list[LibraryRoot]) -> str:
    """Translate a stored (container) path to the real path on this computer.

    Longest matching ``container`` prefix wins; the remainder is re-joined with
    the host's separator (so a Windows ``host`` yields back-slashes). Returns
    *container_path* unchanged when nothing matches (native runs, or a path that
    predates the mapping).
    """
    match = _match_root(container_path, roots)
    if match is None:
        return container_path
    best, rel = match
    if not rel.parts:
        return best.host
    if _is_windows_path(best.host):
        return str(PureWindowsPath(best.host, *rel.parts))
    return str(PurePosixPath(best.host, *rel.parts))


def compose_volume_line(root: LibraryRoot) -> str:
    """The ``docker-compose.yml`` volume entry that would mount this folder."""
    return f'- "{root.host}:{root.container}:rw"'


# --------------------------------------------------------------------------- #
# network paths (Docker: "copy the path other computers open")
# --------------------------------------------------------------------------- #
NetworkStyle = Literal["windows", "smb"]


def parse_network_path(raw: str) -> tuple[str, ...] | None:
    """``(server, share, *folders)`` from ``\\\\server\\share\\a``, ``//server/share/a``
    or ``smb://[user@]server/share/a`` (percent-escapes decoded). ``None`` unless
    there are at least a server and a share."""
    text = clean_path(raw)
    if text.lower().startswith("smb://"):
        url = urlsplit(text)
        server = url.hostname or ""
        if url.port:
            server = f"{server}:{url.port}"
        rest = [unquote(p) for p in url.path.split("/")]
    elif text.startswith(("\\\\", "//")):
        server, *rest = text[2:].replace("\\", "/").split("/")
    else:
        return None
    parts = tuple(p for p in [server, *rest] if p)
    if not server or len(parts) < 2 or any(p in (".", "..") for p in parts):
        return None
    return parts


def network_parts(container_path: str, roots: list[LibraryRoot]) -> tuple[str, ...] | None:
    """The network path of a stored (container) path, as parts, when its root
    has a ``network`` path set; else ``None``."""
    match = _match_root(container_path, roots)
    if match is None or not match[0].network:
        return None
    base = parse_network_path(match[0].network)
    return None if base is None else base + match[1].parts


def format_network_path(parts: tuple[str, ...], style: NetworkStyle) -> str:
    """``\\\\server\\share\\a.jpg`` for Windows (File Explorer's address bar), or
    ``smb://server/share/a.jpg`` for macOS / Linux (Finder's Connect to Server)."""
    if style == "windows":
        return "\\\\" + "\\".join(parts)
    server, *rest = parts
    return "smb://" + "/".join([quote(server, safe=":"), *(quote(p) for p in rest)])
