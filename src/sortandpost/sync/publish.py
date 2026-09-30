"""Atomic DB publish + the raw-SQLite reads the sync layer needs.

Third-party sync tools corrupt SQLite files written in place, so the app never
runs against ``sync/sortandpost.db``. Instead it copies the local working copy
to a temp file in ``sync/``, ``fsync``s it, and ``os.replace()``s it onto the
canonical name — the synced file is therefore always a complete, consistent DB.

``read_generation`` / ``quick_check`` open arbitrary ``.db`` files (the published
copy, conflict copies) with a bare :mod:`sqlite3` connection so no engine pragmas
or SQLModel metadata get involved.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
import sys
import time
from pathlib import Path

_TMP_PREFIX = ".sortandpost.db.tmp-"
_REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.6)  # ~3 s in all


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fsync_path(path: Path) -> None:
    flags = os.O_RDONLY
    if sys.platform == "win32":
        if path.is_dir():
            return  # Windows can't open a directory to flush it; NTFS journals the rename
        flags = os.O_RDWR  # FlushFileBuffers needs a handle with write access
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def replace_file(tmp: Path, dst: Path) -> None:
    """``os.replace``, retried briefly on Windows, where it fails while any
    process has *dst* open -- a sync client uploading it, an antivirus scan, a
    reader that hasn't closed yet. POSIX never needs the retry."""
    delays = _REPLACE_RETRY_DELAYS if sys.platform == "win32" else ()
    for delay in delays:
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            time.sleep(delay)
    os.replace(tmp, dst)


def atomic_replace(src: Path, dst: Path, *, tag: str) -> None:
    """Copy *src* onto *dst* atomically via a temp file beside *dst*."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.tmp-{tag}")
    tmp.write_bytes(src.read_bytes())
    _fsync_path(tmp)
    replace_file(tmp, dst)
    # directory fsync is best-effort (not supported on every filesystem)
    with contextlib.suppress(OSError):
        _fsync_path(dst.parent)


def atomic_publish(working_db: Path, published_db: Path, *, device_id: str) -> None:
    atomic_replace(working_db, published_db, tag=device_id)


def pull_published(published_db: Path, working_db: Path, *, device_id: str) -> None:
    atomic_replace(published_db, working_db, tag=device_id)


def read_generation(db_path: Path) -> int:
    """``meta.db_generation`` for any SQLite file; 0 if absent or unreadable."""
    if not db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return 0
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'db_generation'").fetchone()
    except sqlite3.Error:
        return 0
    finally:
        conn.close()
    try:
        return int(row[0]) if row is not None else 0
    except (TypeError, ValueError):
        return 0


def quick_check(db_path: Path) -> bool:
    if not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return bool(row) and row[0] == "ok"


def stale_tmp_files(sync_dir: Path) -> list[Path]:
    """Leftover ``.tmp-*`` publish files (a publish that died mid-write)."""
    return sorted(sync_dir.glob(f"{_TMP_PREFIX}*")) + sorted(sync_dir.glob(".sortandpost.db.tmp-*"))
