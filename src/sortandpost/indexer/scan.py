"""One full scan pass over the library roots (``python -m sortandpost.indexer --once``).

Walk every root, run the per-file pipeline, commit after each file so a kill at
any point loses at most one file's work (hard constraint #6), then run the
presence sweep. CLIP (stage 7) is deliberately out of scope here -- browsing
works as soon as this returns.
"""

from __future__ import annotations

import logging
import shutil
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session

from sortandpost.config import Settings
from sortandpost.db import init_db, make_engine
from sortandpost.db.models import ScanState
from sortandpost.indexer.pipeline import (
    IndexOutcome,
    PipelineConfig,
    index_file,
    sweep_presence,
)
from sortandpost.indexer.scanner import iter_image_files
from sortandpost.metadata import ExiftoolClient

log = logging.getLogger(__name__)


@dataclass
class ScanStats:
    seen: int = 0
    indexed: int = 0
    skipped: int = 0
    new_photos: int = 0
    thumbnails: int = 0
    errors: int = 0
    marked_absent: int = 0
    marked_missing: int = 0

    def __str__(self) -> str:
        return (
            f"{self.seen} files seen, {self.indexed} indexed "
            f"({self.new_photos} new, {self.thumbnails} thumbnails), "
            f"{self.skipped} unchanged, {self.errors} errors; "
            f"{self.marked_absent} paths now absent, {self.marked_missing} photos now missing"
        )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def scan_once(settings: Settings) -> ScanStats:
    paths = settings.paths
    engine = make_engine(paths.working_db)
    init_db(engine)  # no-op once Alembic has run; safety net for native --once

    roots = [Path(r) for r in settings.config.library_roots]
    tcfg = settings.config.thumbnails
    pcfg = PipelineConfig(
        thumbnail_sizes=tuple(tcfg.sizes),
        thumbnail_format=tcfg.format,
        thumbnail_quality=tcfg.quality,
        thumbnail_path=paths.thumbnail_file,
    )

    have_exiftool = shutil.which("exiftool") is not None
    if not have_exiftool:
        log.warning("exiftool not on PATH; skipping embedded-metadata reads this pass")
    et_ctx = (
        ExiftoolClient(write_windows_compat_tags=settings.config.rating.write_windows_compat_tags)
        if have_exiftool
        else nullcontext(None)
    )

    stats = ScanStats()
    seen_paths: set[str] = set()
    with Session(engine) as session, et_ctx as exiftool:
        for path in iter_image_files(roots, exclude=[paths.app_data]):
            seen_paths.add(str(path))
            stats.seen += 1
            try:
                outcome = index_file(session, path, config=pcfg, exiftool=exiftool, now=_utcnow())
                session.commit()
            except Exception as exc:  # never let one bad file kill the pass
                session.rollback()
                log.exception("unhandled error indexing %s", path)
                _record_failure(session, path, exc)
                session.commit()
                stats.errors += 1
                continue
            _tally(stats, outcome)

        result = sweep_presence(session, seen_paths, now=_utcnow())
        session.commit()
        stats.marked_absent = result.marked_absent
        stats.marked_missing = result.marked_missing

    log.info("scan complete: %s", stats)
    return stats


def _tally(stats: ScanStats, outcome: IndexOutcome) -> None:
    if outcome.error is not None:
        stats.errors += 1
    elif outcome.skipped:
        stats.skipped += 1
    else:
        stats.indexed += 1
        stats.thumbnails += len(outcome.thumbnails_written)
        if outcome.new_photo:
            stats.new_photos += 1


def _record_failure(session: Session, path: Path, exc: BaseException) -> None:
    key = str(path)
    scan = session.get(ScanState, key)
    if scan is None:
        scan = ScanState(path=key)
        session.add(scan)
    scan.error = f"{type(exc).__name__}: {exc}"[:500]
    scan.attempts += 1
