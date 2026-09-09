"""Compressed DB snapshots under ``sync/snapshots/``.

Every ``snapshot_every_publishes`` publishes we also drop a zstd-compressed copy
of the DB here and prune to ``snapshot_keep``. This is the only recovery path
that ever forces a rescan: if ``PRAGMA quick_check`` fails on startup we restore
the newest snapshot and let the next reconcile scan catch up.

``zstandard`` is a declared dependency; if it is somehow missing we fall back to
uncompressed ``.db`` snapshots rather than losing the recovery path entirely.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

try:  # declared dep; degrade to uncompressed snapshots if absent
    import zstandard
except ModuleNotFoundError:  # pragma: no cover - zstandard is in pyproject
    zstandard = None  # type: ignore[assignment]

_STEM = "sortandpost-"
_TS_FMT = "%Y%m%d-%H%M%S"
_NAME_RE = re.compile(r"^sortandpost-\d{8}-\d{6}(?:-\d+)?\.db(?:\.zst)?$")


def _compress(raw: bytes) -> bytes:
    return zstandard.ZstdCompressor(level=10).compress(raw)


def _decompress(raw: bytes) -> bytes:
    return zstandard.ZstdDecompressor().decompress(raw)


def snapshot_name(now: datetime, *, compressed: bool) -> str:
    suffix = ".db.zst" if compressed else ".db"
    return f"{_STEM}{now.strftime(_TS_FMT)}{suffix}"


def write_snapshot(db_path: Path, snapshots_dir: Path, *, now: datetime) -> Path:
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    raw = db_path.read_bytes()
    compressed = zstandard is not None
    dest = snapshots_dir / snapshot_name(now, compressed=compressed)
    # Same-second snapshots (tests, rapid publishes) get a numeric uniquifier.
    counter = 1
    while dest.exists():
        stem = snapshot_name(now, compressed=compressed).rsplit(".db", 1)[0]
        tail = ".db.zst" if compressed else ".db"
        dest = snapshots_dir / f"{stem}-{counter}{tail}"
        counter += 1
    payload = _compress(raw) if compressed else raw
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(payload)
    tmp.replace(dest)
    return dest


def list_snapshots(snapshots_dir: Path) -> list[Path]:
    """Snapshot files, oldest first (name sorts chronologically)."""
    if not snapshots_dir.is_dir():
        return []
    return sorted(p for p in snapshots_dir.iterdir() if p.is_file() and _NAME_RE.match(p.name))


def newest_snapshot(snapshots_dir: Path) -> Path | None:
    snaps = list_snapshots(snapshots_dir)
    return snaps[-1] if snaps else None


def prune_snapshots(snapshots_dir: Path, keep: int) -> list[Path]:
    """Delete all but the *keep* newest snapshots. Returns what was removed."""
    snaps = list_snapshots(snapshots_dir)
    if keep < 0 or len(snaps) <= keep:
        return []
    doomed = snaps[: len(snaps) - keep]
    for p in doomed:
        p.unlink(missing_ok=True)
    return doomed


def restore_snapshot(snapshot_path: Path, dest_db_path: Path) -> None:
    """Decompress (if needed) *snapshot_path* onto *dest_db_path* atomically."""
    raw = snapshot_path.read_bytes()
    if snapshot_path.name.endswith(".zst"):
        if zstandard is None:  # pragma: no cover - defensive
            raise RuntimeError("cannot restore a .zst snapshot without zstandard installed")
        raw = _decompress(raw)
    dest_db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_db_path.with_name(dest_db_path.name + ".restore-tmp")
    tmp.write_bytes(raw)
    tmp.replace(dest_db_path)
