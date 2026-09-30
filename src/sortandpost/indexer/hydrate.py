"""'Download & index' for cloud online-only placeholder files.

The scanner records a cloud stub as
:attr:`~sortandpost.db.models.ScanState.online_only` and skips it (see
:mod:`sortandpost.indexer.placeholder`). :func:`hydrate_online_only` is the
on-demand action: for every recorded stub under this device's roots it reads a
bounded slice of the file to make the sync client fetch the real bytes, then
clears the flag and the fast-path stat so the following
:func:`~sortandpost.indexer.scan.scan_once` indexes it like any changed file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from sqlmodel import Session, col, select

from sortandpost.config import Settings
from sortandpost.db import init_db, make_engine
from sortandpost.db.models import ScanState
from sortandpost.library_roots import container_roots

log = logging.getLogger(__name__)

_READ_CHUNK = 1 << 20  # 1 MiB per read
_MAX_READ = 64 << 20  # stop after 64 MiB -- opening + a small read usually suffices


@dataclass
class HydrateStats:
    requested: int = 0
    hydrated: int = 0
    failed: int = 0

    def __str__(self) -> str:
        return (
            f"{self.hydrated}/{self.requested} online-only file(s) downloaded, {self.failed} failed"
        )


def _pull_bytes(path: Path) -> None:
    """Read *path* so a cloud sync client fetches its data. Bounded read."""
    read = 0
    with path.open("rb") as fh:
        while read < _MAX_READ:
            chunk = fh.read(_READ_CHUNK)
            if not chunk:
                break
            read += len(chunk)


def _under_roots(key: str, roots: list[str]) -> bool:
    if not roots:
        return True
    path = Path(key)  # OS-native: `\` separators + case-insensitive on Windows
    return any(path.is_relative_to(r) for r in roots)


def hydrate_online_only(settings: Settings) -> HydrateStats:
    """Force every recorded online-only file under this device's roots to
    download and clear its flag. Does **not** scan -- the caller runs
    :func:`~sortandpost.indexer.scan.scan_once` afterwards."""
    engine = make_engine(settings.paths.working_db)
    init_db(engine)
    roots = [str(r) for r in container_roots(settings.library_roots)]

    stats = HydrateStats()
    with Session(engine) as session:
        rows = session.exec(select(ScanState).where(col(ScanState.online_only).is_(True))).all()
        for scan in rows:
            if not _under_roots(scan.path, roots):
                continue
            stats.requested += 1
            try:
                _pull_bytes(Path(scan.path))
            except OSError as exc:
                log.warning("could not hydrate %s: %s", scan.path, exc)
                stats.failed += 1
                continue
            # No longer a stub: clear the flag and the stat fast-path so the next
            # scan_once decodes it for real.
            scan.online_only = False
            scan.stages_done = 0
            scan.size = None
            scan.mtime_ns = None
            scan.error = None
            scan.attempts = 0
            stats.hydrated += 1
        session.commit()

    log.info("hydrate complete: %s", stats)
    return stats
