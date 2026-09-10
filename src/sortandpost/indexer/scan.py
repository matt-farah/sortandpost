"""One full scan pass over the library roots (``python -m sortandpost.indexer --once``).

Walk every root, run the per-file pipeline, commit after each file so a kill at
any point loses at most one file's work (hard constraint #6), then run the
presence sweep. CLIP (stage 7) is deliberately out of scope here -- browsing
works as soon as this returns.
"""

from __future__ import annotations

import logging
import multiprocessing
import shutil
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path

import numpy as np
from sqlmodel import Session, col, select

from sortandpost.clip.model import ClipModel, ClipModelUnavailable
from sortandpost.clip.store import EmbeddingStore, check_compatible, ensure_meta
from sortandpost.clip.tagging import pick_terms, upsert_prediction
from sortandpost.clip.vocab import Vocab, ensure_text_matrix, load_vocab
from sortandpost.config import Settings, TaggingConfig
from sortandpost.db import init_db, make_engine
from sortandpost.db.deletion import apply_pending_tombstones
from sortandpost.db.models import (
    EmbeddingRow,
    EmbeddingsMeta,
    Photo,
    Prediction,
    PredictionState,
    ScanState,
)
from sortandpost.indexer.decode import DecodeError, load_image
from sortandpost.indexer.pipeline import (
    IndexOutcome,
    PipelineConfig,
    Stage,
    needs_reindex,
    persist_decoded,
    sweep_presence,
)
from sortandpost.indexer.scanner import iter_image_files
from sortandpost.indexer.workers import DecodedFile, DecodeJob, decode_file, decode_local
from sortandpost.metadata import ExiftoolClient

log = logging.getLogger(__name__)

# Below this many files needing work, the spawn cost of a process pool is not
# worth it -- decode them inline on the calling thread instead.
_MIN_FILES_FOR_POOL = 8


@dataclass
class ScanStats:
    seen: int = 0
    indexed: int = 0
    skipped: int = 0
    tombstoned: int = 0  # files skipped because the user deleted that pixel_hash
    tombstones_applied: int = 0  # tombstones this pass purged + trashed
    new_photos: int = 0
    thumbnails: int = 0
    errors: int = 0
    marked_absent: int = 0
    marked_missing: int = 0
    ratings_imported: int = 0
    ratings_written: int = 0
    keywords_imported: int = 0

    def __str__(self) -> str:
        reconciled = ""
        if self.ratings_imported or self.ratings_written or self.keywords_imported:
            reconciled = (
                f"; reconciled {self.ratings_imported} ratings in / "
                f"{self.ratings_written} out, {self.keywords_imported} keywords in"
            )
        tomb = ""
        if self.tombstoned or self.tombstones_applied:
            tomb = (
                f"; {self.tombstoned} skipped as deleted, "
                f"{self.tombstones_applied} tombstone(s) applied"
            )
        return (
            f"{self.seen} files seen, {self.indexed} indexed "
            f"({self.new_photos} new, {self.thumbnails} thumbnails), "
            f"{self.skipped} unchanged, {self.errors} errors; "
            f"{self.marked_absent} paths now absent, {self.marked_missing} photos now missing"
            f"{reconciled}{tomb}"
        )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def scan_once(settings: Settings, *, workers: int | None = None) -> ScanStats:
    """One full pass over the library roots (stages 1-6).

    Stages 2-4 (decode, pixel + perceptual hash, thumbnails) are CPU-bound and
    run in a ``ProcessPoolExecutor`` (hard constraint #7); the main process does
    the fast-path check, the DB upserts and the ``exiftool`` reconcile serially.
    ``workers`` overrides ``config.indexer.workers`` (``1`` forces the inline
    path -- used by the tests and as an escape hatch). A pool is only spun up
    when enough files actually need work to amortise the ``spawn`` cost.
    """
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
    n_workers = settings.config.worker_count() if workers is None else max(1, workers)

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
        # -- discover + fast-path filter (main thread, one ScanState query) --- #
        scan_rows = {s.path: s for s in session.exec(select(ScanState)).all()}
        todo: list[str] = []
        for path in iter_image_files(
            roots,
            exclude=[paths.app_data],
            exclude_names=settings.config.indexer.exclude_dir_names,
        ):
            key = str(path)
            seen_paths.add(key)
            stats.seen += 1
            try:
                st = path.stat()
            except OSError as exc:
                log.warning("stat failed for %s: %s", path, exc)
                _record_failure(session, path, exc)
                session.commit()
                stats.errors += 1
                continue
            if not needs_reindex(scan_rows.get(key), st.st_size, st.st_mtime_ns):
                stats.skipped += 1
                continue
            todo.append(key)

        # -- stages 2-4 in parallel, stages 5-6 + DB serial here ------------- #
        for decoded in _decode_stream(todo, pcfg, paths.app_data, n_workers):
            try:
                outcome = persist_decoded(
                    session,
                    decoded,
                    exiftool=exiftool,
                    device_id=settings.device_id,
                    now=_utcnow(),
                )
                session.commit()
            except Exception as exc:  # never let one bad file kill the pass
                session.rollback()
                log.exception("unhandled error indexing %s", decoded.path)
                _record_failure(session, Path(decoded.path), exc)
                session.commit()
                stats.errors += 1
                continue
            _tally(stats, outcome)

        result = sweep_presence(session, seen_paths, now=_utcnow())
        session.commit()
        stats.marked_absent = result.marked_absent
        stats.marked_missing = result.marked_missing

        # Apply any not-yet-applied tombstones (from a user delete on this
        # machine, or one that arrived via merge): purge the derived rows and
        # move this machine's still-present copy of the file to trash.
        tomb = apply_pending_tombstones(
            session,
            paths=paths,
            clip_model=settings.config.clip.model,
            thumb_sizes=tuple(tcfg.sizes),
            now=_utcnow(),
            trash_local_files=True,
        )
        session.commit()
        stats.tombstones_applied = len(tomb.purged)

    log.info("scan complete: %s", stats)
    return stats


def _decode_stream(
    paths_to_do: list[str],
    pcfg: PipelineConfig,
    app_data: Path,
    n_workers: int,
) -> Iterator[DecodedFile]:
    """Yield a :class:`DecodedFile` per input path -- via a process pool when the
    batch is large enough, otherwise inline. Bounded in-flight work keeps peak
    memory flat regardless of library size. If the pool cannot run (``spawn``
    can't re-import ``__main__``, sandbox limits, ...) the scan falls back to
    inline decoding rather than failing."""
    if not paths_to_do:
        return

    def _inline(key: str) -> DecodedFile:
        return decode_local(
            Path(key),
            thumb_path_fn=pcfg.thumbnail_path,
            sizes=pcfg.thumbnail_sizes,
            fmt=pcfg.thumbnail_format,
            quality=pcfg.thumbnail_quality,
        )

    if n_workers <= 1 or len(paths_to_do) < _MIN_FILES_FOR_POOL:
        for key in paths_to_do:
            yield _inline(key)
        return

    job = DecodeJob(
        app_data=str(app_data),
        thumb_sizes=pcfg.thumbnail_sizes,
        thumb_fmt=pcfg.thumbnail_format,
        thumb_quality=pcfg.thumbnail_quality,
    )
    max_inflight = n_workers * 2
    ctx = multiprocessing.get_context("spawn")  # safe from the app's indexer thread
    log.info("indexing %d files across %d worker processes", len(paths_to_do), n_workers)
    done_keys: set[str] = set()
    try:
        with ProcessPoolExecutor(max_workers=n_workers, mp_context=ctx) as pool:
            pending: dict[Future[DecodedFile], str] = {}
            stream = iter(paths_to_do)
            for key in islice(stream, max_inflight):
                pending[pool.submit(decode_file, job, key)] = key
            while pending:
                finished, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                for fut in finished:
                    key = pending.pop(fut)
                    try:
                        result = fut.result()
                    except BrokenProcessPool:
                        raise  # abandon the pool; finish the rest inline below
                    except Exception as exc:  # a single worker's un-picklable failure
                        log.exception("decode worker crashed on %s", key)
                        result = _worker_crash(key, exc)
                    done_keys.add(key)
                    yield result
                    nxt = next(stream, None)
                    if nxt is not None:
                        pending[pool.submit(decode_file, job, nxt)] = nxt
    except BrokenProcessPool as exc:
        remaining = [k for k in paths_to_do if k not in done_keys]
        log.warning(
            "decode process pool unusable (%s); indexing %d remaining file(s) inline",
            exc,
            len(remaining),
        )
        for key in remaining:
            yield _inline(key)


def _worker_crash(key: str, exc: BaseException) -> DecodedFile:
    try:
        st = Path(key).stat()
        size, mtime_ns = st.st_size, st.st_mtime_ns
    except OSError:
        size = mtime_ns = 0
    return DecodedFile(
        path=key,
        size=size,
        mtime_ns=mtime_ns,
        error=f"worker crashed: {type(exc).__name__}: {exc}"[:400],
        failed_stage="decode",
    )


def _tally(stats: ScanStats, outcome: IndexOutcome) -> None:
    if outcome.error is not None:
        stats.errors += 1
    elif outcome.tombstoned:
        stats.tombstoned += 1
    elif outcome.skipped:
        stats.skipped += 1
    else:
        stats.indexed += 1
        stats.thumbnails += len(outcome.thumbnails_written)
        if outcome.new_photo:
            stats.new_photos += 1
    if outcome.reconcile is not None:
        stats.ratings_imported += int(outcome.reconcile.rating_imported)
        stats.ratings_written += int(outcome.reconcile.rating_written)
        stats.keywords_imported += outcome.reconcile.keywords_imported


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
    predictions: int = 0
    errors: int = 0
    total_rows: int = 0
    skipped_no_model: bool = False

    def __str__(self) -> str:
        if self.skipped_no_model:
            return "CLIP pass skipped: open-clip-torch / torch not installed"
        return (
            f"CLIP: {self.embedded} newly embedded, {self.predictions} predictions, "
            f"{self.errors} errors; {self.total_rows} rows total"
        )


def scan_clip_once(
    settings: Settings,
    *,
    reembed: bool = False,
    model: ClipModel | None = None,
) -> ClipStats:
    """Embed every browsable photo with no row in the current model's matrix, and
    score it against the zero-shot vocabulary.

    Idempotent and resumable: a photo already in the store (or, after ``reembed``,
    freshly cleared) is the unit of work, committed per batch. Raises
    :class:`~sortandpost.clip.store.ModelMismatch` if ``embeddings_meta`` was
    written by a different model and ``reembed`` is not set.
    """
    paths = settings.paths
    ccfg = settings.config.clip
    tcfg = settings.config.tagging

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

    vocab = load_vocab(paths.tags_file)
    text_matrix = ensure_text_matrix(
        model,
        vocab,
        prompt_templates=tcfg.prompt_templates,
        cache_path=paths.text_embeddings_file(ccfg.model, vocab.vocab_hash),
    )

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

                now = _utcnow()
                for row_i, (pixel_hash, vec) in enumerate(zip(hashes, vecs, strict=True)):
                    row_index = store.upsert(pixel_hash, vec)
                    _mark_embedding_row(session, pixel_hash, row_index, ccfg.model)
                    _mark_clip_done(session, pixel_hash)
                    stats.embedded += 1
                    stats.predictions += _score_photo(
                        session, pixel_hash, vecs[row_i], text_matrix, vocab, ccfg.model, tcfg, now
                    )
                store.flush()
                ensure_meta(
                    session,
                    model=ccfg.model,
                    pretrained=ccfg.pretrained,
                    dim=dim,
                    count=len(store),
                    now=now,
                )
                session.commit()
            stats.total_rows = len(store)

    log.info("clip pass complete: %s", stats)
    return stats


def _score_photo(
    session: Session,
    pixel_hash: str,
    image_vec: np.ndarray,
    text_matrix: np.ndarray,
    vocab: Vocab,
    model: str,
    tcfg: TaggingConfig,
    now: datetime,
) -> int:
    """Write pending predictions for one photo; return how many rows were new."""
    if text_matrix.shape[0] == 0:
        return 0
    scores = np.asarray(text_matrix @ np.asarray(image_vec, dtype=np.float32))
    picked = pick_terms(scores, vocab, threshold=tcfg.score_threshold, top_k=tcfg.top_k)
    new = 0
    for term in picked:
        if upsert_prediction(
            session,
            pixel_hash=pixel_hash,
            tag_name=term.name,
            model=model,
            score=term.score,
            now=now,
        ):
            new += 1
    return new


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
    """Clear derived CLIP state for a full re-embed. User decisions are kept:
    ``approved`` / ``rejected`` predictions (and their ``photo_tags`` / ``changes``
    rows) survive so a rejection stays sticky."""
    for row in session.exec(select(EmbeddingRow)).all():
        session.delete(row)
    meta = session.get(EmbeddingsMeta, model)
    if meta is not None:
        session.delete(meta)
    stale = session.exec(
        select(Prediction).where(
            Prediction.model == model, Prediction.state == PredictionState.pending
        )
    ).all()
    for pred in stale:
        session.delete(pred)
    for scan in session.exec(select(ScanState)).all():
        scan.stages_done &= ~int(Stage.CLIP)
