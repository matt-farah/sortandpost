"""One full scan pass over the library roots (``python -m sortandpost.indexer --once``).

Walk every root, run the per-file pipeline, commit after each file so a kill at
any point loses at most one file's work (hard constraint #6), then run the
presence sweep. CLIP (stage 7) is deliberately out of scope here -- browsing
works as soon as this returns.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, col, select

from sortandpost.clip.model import ClipModel, ClipModelUnavailable
from sortandpost.clip.store import EmbeddingStore, check_compatible, ensure_meta
from sortandpost.config import Settings
from sortandpost.db import init_db, make_engine
from sortandpost.db.models import EmbeddingRow, EmbeddingsMeta, Photo, ScanState
from sortandpost.indexer.decode import DecodeError, load_image
from sortandpost.indexer.pipeline import (
    IndexOutcome,
    PipelineConfig,
    Stage,
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


# --------------------------------------------------------------------------- #
# CLIP pass (pipeline stage 7) -- deferred so browsing lands first
# --------------------------------------------------------------------------- #
@dataclass
class ClipStats:
    embedded: int = 0
    errors: int = 0
    total_rows: int = 0
    skipped_no_model: bool = False

    def __str__(self) -> str:
        if self.skipped_no_model:
            return "CLIP pass skipped: open-clip-torch / torch not installed"
        return (
            f"CLIP: {self.embedded} newly embedded, {self.errors} errors; "
            f"{self.total_rows} rows total"
        )


def scan_clip_once(
    settings: Settings,
    *,
    reembed: bool = False,
    model: ClipModel | None = None,
) -> ClipStats:
    """Embed every browsable photo that has no row in the current model's matrix.

    Idempotent and resumable: a photo already in the store (or, after ``reembed``,
    freshly cleared) is the unit of work, committed per batch. Raises
    :class:`~sortandpost.clip.store.ModelMismatch` if ``embeddings_meta`` was
    written by a different model and ``reembed`` is not set.
    """
    paths = settings.paths
    ccfg = settings.config.clip

    if model is None:
        model = ClipModel(ccfg, paths.models_dir)
    try:
        model.load()
    except ClipModelUnavailable as exc:
        log.warning("CLIP pass skipped: %s", exc)
        return ClipStats(skipped_no_model=True)
    dim = model.dim

    engine = make_engine(paths.working_db)
    init_db(engine)

    matrix_path = paths.image_embeddings_file(ccfg.model)
    rowmap_path = paths.embeddings_rowmap_file

    stats = ClipStats()
    with Session(engine) as session:
        if reembed:
            _reset_clip_state(session, ccfg.model)
            session.commit()
            matrix_path.unlink(missing_ok=True)
            rowmap_path.unlink(missing_ok=True)
        else:
            check_compatible(session, model=ccfg.model, pretrained=ccfg.pretrained, dim=dim)

        with EmbeddingStore.open(matrix_path, rowmap_path, dim=dim, model=ccfg.model) as store:
            candidates = _clip_candidates(session, store)
            batch_size = max(1, ccfg.batch_size)
            for chunk in _chunked(candidates, batch_size):
                images = []
                hashes = []
                for pixel_hash, primary_path in chunk:
                    try:
                        images.append(load_image(Path(primary_path)))
                        hashes.append(pixel_hash)
                    except DecodeError as exc:
                        log.warning("CLIP decode failed for %s: %s", primary_path, exc)
                        stats.errors += 1
                if not images:
                    continue
                try:
                    vecs = model.encode_images(images)
                finally:
                    for img in images:
                        img.close()

                for pixel_hash, vec in zip(hashes, vecs, strict=True):
                    row_index = store.upsert(pixel_hash, vec)
                    _mark_embedding_row(session, pixel_hash, row_index, ccfg.model)
                    _mark_clip_done(session, pixel_hash)
                    stats.embedded += 1
                store.flush()
                ensure_meta(
                    session,
                    model=ccfg.model,
                    pretrained=ccfg.pretrained,
                    dim=dim,
                    count=len(store),
                    now=_utcnow(),
                )
                session.commit()
            stats.total_rows = len(store)

    log.info("clip pass complete: %s", stats)
    return stats


def _chunked(items: list[tuple[str, str]], size: int) -> Iterator[list[tuple[str, str]]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _clip_candidates(session: Session, store: EmbeddingStore) -> list[tuple[str, str]]:
    """Present, browsable photos with no row yet in *store*, one entry per hash."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    browsable = int(Stage.BROWSABLE)
    rows = session.exec(select(ScanState).where(col(ScanState.pixel_hash).is_not(None))).all()
    for scan in rows:
        pixel_hash = scan.pixel_hash
        if pixel_hash is None or pixel_hash in seen:
            continue
        if (scan.stages_done & browsable) != browsable:
            continue
        if store.row_for(pixel_hash) is not None:
            continue
        photo = session.get(Photo, pixel_hash)
        if photo is None or photo.missing_since is not None:
            continue
        seen.add(pixel_hash)
        out.append((pixel_hash, photo.primary_path))
    return out


def _mark_embedding_row(session: Session, pixel_hash: str, row_index: int, model: str) -> None:
    row = session.get(EmbeddingRow, pixel_hash)
    if row is None:
        session.add(EmbeddingRow(pixel_hash=pixel_hash, row_index=row_index, model=model))
    else:
        row.row_index = row_index
        row.model = model


def _mark_clip_done(session: Session, pixel_hash: str) -> None:
    for scan in session.exec(select(ScanState).where(ScanState.pixel_hash == pixel_hash)).all():
        scan.stages_done |= int(Stage.CLIP)


def _reset_clip_state(session: Session, model: str) -> None:
    for row in session.exec(select(EmbeddingRow)).all():
        session.delete(row)
    meta = session.get(EmbeddingsMeta, model)
    if meta is not None:
        session.delete(meta)
    for scan in session.exec(select(ScanState)).all():
        scan.stages_done &= ~int(Stage.CLIP)
