"""Move deleted photo files to a recoverable, per-machine "oops" buffer.

This is the **only** module that removes a user's photo file, and it never
``unlink``s (docs/architecture.md hard constraint #1, exception 2). Every delete moves the
files into a managed ``<APP_DATA>/local/trash/<batch>/`` folder with a
``manifest.json``. ``local/`` is never synced, so trashed files never ride the
sync tool, and nothing here ever writes under a library root.

The managed trash is a short-lived undo buffer, not a history:

* :func:`put_back` reverses a batch -- files return to their original paths
  (refused if something now occupies that path or its folder is gone);
* :func:`flush_to_system_trash` hands every batch to the OS Trash / Recycle Bin
  (macOS / Windows, via ``send2trash``) and forgets it. It is a user action on
  ``/settings`` today and is meant to become an on-quit cleanup step. On Linux
  it is unavailable: ``send2trash`` may create a ``.Trash-<uid>`` dir at the top
  of whatever volume holds the file, which could be a library volume.

Each batch also keeps the photos' user state (rating, tags, CLIP decisions) and a
small thumbnail copy, so a put-back restores exactly what the delete removed.

Layout::

    trash/<YYYYMMDD-HHMMSS-ffffff>/
      manifest.json
      files/<seq>/<original file name>   # original name, so the OS Trash shows it
      thumbs/<pixel_hash>.webp
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_MANIFEST = "manifest.json"
_VERSION = 2


@dataclass
class TrashedItem:
    original: str
    ok: bool
    stored: str | None = None  # absolute path inside the managed batch folder
    error: str | None = None
    pixel_hash: str | None = None


@dataclass
class TrashResult:
    backend: str = "managed"
    items: list[TrashedItem] = field(default_factory=list)
    trash_dir: str | None = None  # the managed batch folder (None when nothing moved)

    @property
    def moved(self) -> list[str]:
        return [i.original for i in self.items if i.ok]

    @property
    def failed(self) -> list[TrashedItem]:
        return [i for i in self.items if not i.ok]

    @property
    def batch_id(self) -> str | None:
        return Path(self.trash_dir).name if self.trash_dir else None


@dataclass
class TrashBatch:
    """One delete action's worth of trashed files, read from its manifest."""

    path: Path
    created_at: str
    reason: str  # "delete" (a user click here) | "tombstone" (a delete from another machine)
    items: list[dict[str, Any]]  # {original, stored, pixel_hash, trashed_at}
    photos: dict[str, dict[str, Any]]  # pixel_hash -> saved user state (see deletion.py)

    @property
    def id(self) -> str:
        return self.path.name

    @property
    def hashes(self) -> list[str]:
        seen = dict.fromkeys(str(i["pixel_hash"]) for i in self.items if i.get("pixel_hash"))
        return list(seen)

    def thumb(self, pixel_hash: str) -> Path | None:
        p = self.path / "thumbs" / f"{pixel_hash}.webp"
        return p if p.is_file() else None


@dataclass
class PutBackResult:
    items: list[TrashedItem] = field(default_factory=list)
    # pixel_hash -> saved user state, for every hash with at least one file back
    photos: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def restored(self) -> list[TrashedItem]:
        return [i for i in self.items if i.ok]

    @property
    def failed(self) -> list[TrashedItem]:
        return [i for i in self.items if not i.ok]

    @property
    def hashes(self) -> list[str]:
        return list(dict.fromkeys(i.pixel_hash for i in self.restored if i.pixel_hash))


def trash_root(local_dir: Path) -> Path:
    return Path(local_dir) / "trash"


# --------------------------------------------------------------------------- #
# Delete: library -> managed batch
# --------------------------------------------------------------------------- #
def move_to_trash(
    paths: Iterable[Path | str],
    *,
    local_dir: Path,
    now: datetime,
    pixel_hash_of: Mapping[str, str] | None = None,
    photo_state: Mapping[str, dict[str, Any]] | None = None,
    thumbs: Mapping[str, Path] | None = None,
    reason: str = "delete",
) -> TrashResult:
    """Move each existing file in *paths* into a new managed batch folder under
    ``local_dir/trash/``. Missing files are reported as failed items, not raised.

    *pixel_hash_of* maps ``str(path) -> pixel_hash``; *photo_state* and *thumbs*
    (keyed by ``pixel_hash``) are saved alongside for :func:`put_back`. The DB
    purge is the caller's job. No batch folder is created if nothing moved.
    """
    wanted = [Path(p) for p in paths]
    hash_of = dict(pixel_hash_of or {})
    result = TrashResult()
    present = []
    for p in wanted:
        if p.exists():
            present.append(p)
        else:
            result.items.append(
                TrashedItem(
                    str(p), ok=False, error="file not found", pixel_hash=hash_of.get(str(p))
                )
            )
    if not present:
        return result

    batch_dir = _new_batch_dir(Path(local_dir), now)
    entries: list[dict[str, Any]] = []
    for seq, p in enumerate(present):
        rel = Path("files") / f"{seq:04d}" / p.name
        dest = batch_dir / rel
        h = hash_of.get(str(p))
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dest))
        except Exception as exc:  # pragma: no cover - fs errors
            log.warning("could not move %s to %s: %s", p, dest, exc)
            result.items.append(TrashedItem(str(p), ok=False, error=str(exc), pixel_hash=h))
            continue
        entries.append(
            {
                "original": str(p),
                "stored": rel.as_posix(),
                "pixel_hash": h,
                "trashed_at": now.isoformat(),
            }
        )
        result.items.append(TrashedItem(str(p), ok=True, stored=str(dest), pixel_hash=h))

    moved_hashes = {e["pixel_hash"] for e in entries if e["pixel_hash"]}
    for h, src in (thumbs or {}).items():
        if h in moved_hashes and Path(src).is_file():
            try:
                (batch_dir / "thumbs").mkdir(exist_ok=True)
                shutil.copy2(src, batch_dir / "thumbs" / f"{h}.webp")
            except OSError as exc:  # pragma: no cover
                log.warning("could not keep thumbnail for %s: %s", h, exc)

    photos = {h: dict(s) for h, s in (photo_state or {}).items() if h in moved_hashes}
    _write_manifest(
        batch_dir,
        {
            "version": _VERSION,
            "created_at": now.isoformat(),
            "reason": reason,
            "items": entries,
            "photos": photos,
        },
    )
    result.trash_dir = str(batch_dir)
    return result


def _new_batch_dir(local_dir: Path, now: datetime) -> Path:
    root = trash_root(local_dir)
    base = now.strftime("%Y%m%d-%H%M%S-%f")
    candidate = root / base
    n = 1
    while candidate.exists():
        candidate = root / f"{base}-{n}"
        n += 1
    candidate.mkdir(parents=True)
    return candidate


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #
def load_batch(batch_dir: Path) -> TrashBatch:
    batch_dir = Path(batch_dir)
    data = json.loads((batch_dir / _MANIFEST).read_text(encoding="utf-8"))
    return TrashBatch(
        path=batch_dir,
        created_at=str(data.get("created_at", "")),
        reason=str(data.get("reason", "delete")),
        items=list(data.get("items", [])),
        photos=dict(data.get("photos", {})),
    )


def list_batches(local_dir: Path) -> list[TrashBatch]:
    """Every readable batch under ``local_dir/trash``, newest first."""
    root = trash_root(local_dir)
    if not root.is_dir():
        return []
    out: list[TrashBatch] = []
    for d in root.iterdir():
        if not (d / _MANIFEST).is_file():
            continue
        try:
            batch = load_batch(d)
        except Exception as exc:
            log.warning("unreadable trash manifest in %s: %s", d, exc)
            continue
        if batch.items:
            out.append(batch)
    out.sort(key=lambda b: (b.created_at, b.id), reverse=True)
    return out


def find_batch(local_dir: Path, batch_id: str) -> TrashBatch | None:
    """The batch named *batch_id*; ``None`` if it is gone or the id is not a
    plain folder name (never lets a caller reach outside ``trash/``)."""
    if not batch_id or "/" in batch_id or "\\" in batch_id or batch_id.startswith("."):
        return None
    d = trash_root(local_dir) / batch_id
    if not (d / _MANIFEST).is_file():
        return None
    try:
        return load_batch(d)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Put back: managed batch -> library
# --------------------------------------------------------------------------- #
def put_back(batch_dir: Path, *, only_hashes: Iterable[str] | None = None) -> PutBackResult:
    """Move the batch's files back to their original paths.

    Refuses (per file) when something already exists at the original path, or
    when its folder no longer exists -- we never create folders under a library
    root. Restored items leave the manifest; the batch folder is removed once it
    is empty. Returns the saved user state of every hash with a file put back.
    """
    batch = load_batch(batch_dir)
    only = set(only_hashes) if only_hashes is not None else None
    result = PutBackResult()
    remaining: list[dict[str, Any]] = []
    for entry in batch.items:
        h = entry.get("pixel_hash")
        if only is not None and h not in only:
            remaining.append(entry)
            continue
        src = batch.path / str(entry["stored"])
        dst = Path(str(entry["original"]))
        err: str | None = None
        if not src.exists():
            err = "no longer in the trash"
        elif dst.exists():
            err = "a file already exists at the original path"
        elif not dst.parent.is_dir():
            err = "the original folder no longer exists"
        if err is None:
            try:
                shutil.move(str(src), str(dst))
            except Exception as exc:  # pragma: no cover - fs errors
                err = str(exc)
        if err is not None:
            result.items.append(
                TrashedItem(str(dst), ok=False, stored=str(src), error=err, pixel_hash=h)
            )
            if src.exists():
                remaining.append(entry)
            continue
        result.items.append(TrashedItem(str(dst), ok=True, stored=str(src), pixel_hash=h))

    for h in result.hashes:
        state = batch.photos.get(h)
        if state is not None:
            result.photos[h] = state
    _rewrite_or_remove(batch, remaining)
    return result


# --------------------------------------------------------------------------- #
# Flush: managed batch -> OS Trash
# --------------------------------------------------------------------------- #
def system_trash_available() -> bool:
    """macOS / Windows have a well-defined per-user Trash that ``send2trash``
    targets safely. On Linux it may fall back to a ``.Trash-<uid>`` dir on the
    file's own volume, so the flush is not offered there."""
    if sys.platform == "darwin" or sys.platform.startswith("win"):
        try:
            import send2trash  # noqa: F401
        except Exception:
            return False
        return True
    return False


@dataclass
class FlushResult:
    moved: int = 0
    failed: list[TrashedItem] = field(default_factory=list)
    batches_cleared: int = 0


def flush_to_system_trash(
    local_dir: Path, *, batch_ids: Iterable[str] | None = None
) -> FlushResult:
    """Send every (or the named) managed batch's files to the OS Trash and forget
    the batch. After this the app can no longer put those photos back -- use the
    OS Trash's own "Put Back". Raises ``RuntimeError`` where no OS Trash is
    available (see :func:`system_trash_available`)."""
    if not system_trash_available():
        raise RuntimeError("no system Trash on this platform")
    from send2trash import send2trash

    wanted = set(batch_ids) if batch_ids is not None else None
    result = FlushResult()
    for batch in list_batches(local_dir):
        if wanted is not None and batch.id not in wanted:
            continue
        remaining: list[dict[str, Any]] = []
        for entry in batch.items:
            src = batch.path / str(entry["stored"])
            if not src.exists():
                continue
            try:
                send2trash(str(src))
                result.moved += 1
            except Exception as exc:  # pragma: no cover - platform trash quirks
                log.warning("send2trash failed for %s: %s", src, exc)
                result.failed.append(
                    TrashedItem(str(entry["original"]), ok=False, stored=str(src), error=str(exc))
                )
                remaining.append(entry)
        _rewrite_or_remove(batch, remaining)
        if not remaining:
            result.batches_cleared += 1
    return result


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _write_manifest(batch_dir: Path, payload: dict[str, Any]) -> None:
    manifest_path = batch_dir / _MANIFEST
    tmp = manifest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(manifest_path)


def _rewrite_or_remove(batch: TrashBatch, remaining: list[dict[str, Any]]) -> None:
    """Keep only *remaining* items in the batch; drop the whole folder (our own
    manifest + thumbnail copies, all inside ``local/``) once none are left."""
    if not remaining:
        if not any(batch.path.glob("files/*/*")):
            shutil.rmtree(batch.path, ignore_errors=True)
            return
        log.warning("trash batch %s has stray files; keeping it", batch.path)
    keep = {str(e.get("pixel_hash")) for e in remaining}
    data = json.loads((batch.path / _MANIFEST).read_text(encoding="utf-8"))
    data["items"] = remaining
    data["photos"] = {h: s for h, s in batch.photos.items() if h in keep}
    _write_manifest(batch.path, data)
