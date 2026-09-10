"""Move deleted photo files to a recoverable location.

This is the **only** module that removes a user's photo file, and it never
``unlink``s (CLAUDE.md hard constraint #1, exception 2). Files go to:

* the **OS Trash / Recycle Bin** on macOS and Windows, via ``send2trash``; or
* a managed ``<APP_DATA>/local/trash/<timestamp>/`` folder with a
  ``manifest.json`` everywhere else (Linux desktop, headless, Docker) -- because
  ``send2trash`` on Linux would create a ``.Trash-<uid>`` directory on the
  *library's own filesystem*, which constraint #1 forbids.

Nothing here ever writes under a library root. The managed folder lives entirely
inside the per-machine ``local/`` subtree, which is never synced.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

_MANIFEST = "manifest.json"


@dataclass
class TrashedItem:
    original: str
    ok: bool
    stored: str | None = None  # path inside the managed folder; None for the OS trash
    error: str | None = None


@dataclass
class TrashResult:
    backend: str  # "os" | "managed"
    items: list[TrashedItem] = field(default_factory=list)
    trash_dir: str | None = None  # the managed <timestamp> folder, when backend == "managed"

    @property
    def moved(self) -> list[str]:
        return [i.original for i in self.items if i.ok]

    @property
    def failed(self) -> list[TrashedItem]:
        return [i for i in self.items if not i.ok]


def _use_os_trash() -> bool:
    """macOS / Windows have a well-defined per-user Trash that ``send2trash``
    targets safely. On Linux it may fall back to a ``.Trash-<uid>`` dir on the
    photo volume, so we use the managed folder there instead."""
    if sys.platform == "darwin" or sys.platform.startswith("win"):
        try:
            import send2trash  # noqa: F401
        except Exception:
            return False
        return True
    return False


def trash_backend() -> str:
    return "os" if _use_os_trash() else "managed"


def move_to_trash(
    paths: Iterable[Path | str],
    *,
    local_dir: Path,
    now: datetime | None = None,
) -> TrashResult:
    """Move each existing file in *paths* to trash.

    *local_dir* is ``<APP_DATA>/local`` -- the managed backend writes under
    ``local_dir/trash/<timestamp>/``. Missing files are reported as failed items
    but are not fatal. The DB purge is the caller's job.
    """
    now = now or datetime.utcnow()
    wanted = [Path(p) for p in paths]
    if _use_os_trash():
        return _os_trash(wanted)
    return _managed_trash(wanted, local_dir=Path(local_dir), now=now)


# --------------------------------------------------------------------------- #
# backends
# --------------------------------------------------------------------------- #
def _os_trash(paths: list[Path]) -> TrashResult:
    from send2trash import send2trash

    result = TrashResult(backend="os")
    for p in paths:
        if not p.exists():
            result.items.append(TrashedItem(str(p), ok=False, error="file not found"))
            continue
        try:
            send2trash(str(p))
            result.items.append(TrashedItem(str(p), ok=True))
        except Exception as exc:  # pragma: no cover - platform trash quirks
            log.warning("send2trash failed for %s: %s", p, exc)
            result.items.append(TrashedItem(str(p), ok=False, error=str(exc)))
    return result


def _managed_trash(paths: list[Path], *, local_dir: Path, now: datetime) -> TrashResult:
    trash_dir = local_dir / "trash" / now.strftime("%Y%m%d-%H%M%S")
    trash_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = trash_dir / _MANIFEST
    entries: list[dict[str, str]] = []
    if manifest_path.exists():
        try:
            entries = list(json.loads(manifest_path.read_text(encoding="utf-8")).get("items", []))
        except Exception:
            entries = []

    result = TrashResult(backend="managed", trash_dir=str(trash_dir))
    seq = len(entries)
    for p in paths:
        if not p.exists():
            result.items.append(TrashedItem(str(p), ok=False, error="file not found"))
            continue
        dest = trash_dir / f"{seq:04d}_{p.name}"
        try:
            shutil.move(str(p), str(dest))
        except Exception as exc:  # pragma: no cover - fs errors
            log.warning("could not move %s to %s: %s", p, dest, exc)
            result.items.append(TrashedItem(str(p), ok=False, error=str(exc)))
            continue
        seq += 1
        entries.append({"original": str(p), "stored": dest.name, "trashed_at": now.isoformat()})
        result.items.append(TrashedItem(str(p), ok=True, stored=str(dest)))

    payload = {"created_at": now.isoformat(), "items": entries}
    tmp = manifest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(manifest_path)
    return result


def restore_managed(trash_dir: Path) -> list[TrashedItem]:
    """Move every file recorded in *trash_dir*/manifest.json back to its original
    path. Best-effort; used by tests and a future "undo" affordance."""
    trash_dir = Path(trash_dir)
    manifest_path = trash_dir / _MANIFEST
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    out: list[TrashedItem] = []
    for entry in data.get("items", []):
        src = trash_dir / str(entry["stored"])
        dst = Path(str(entry["original"]))
        if not src.exists():
            out.append(TrashedItem(str(dst), ok=False, error="not in trash"))
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            out.append(TrashedItem(str(dst), ok=True, stored=str(src)))
        except Exception as exc:  # pragma: no cover
            out.append(TrashedItem(str(dst), ok=False, error=str(exc)))
    return out
