"""One full scan pass over the library roots (``python -m sortandpost.indexer --once``).

Walk every root, run the per-file pipeline, commit after each file so a kill at
any point loses at most one file's work (hard constraint #6), then run the
presence sweep. CLIP (stage 7) is deliberately out of scope here -- browsing
works as soon as this returns.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import threading
from collections.abc import Callable, Generator, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path

import numpy as np
from sqlmodel import Session, col, select

from sortandpost import binaries, missing
from sortandpost.clip.model import (
    ClipModel,
    ClipModelUnavailable,
    apply_cpu_profile,
    clip_available,
)
from sortandpost.clip.store import EmbeddingStore, check_compatible, ensure_meta
from sortandpost.clip.tagging import pick_terms, upsert_prediction
from sortandpost.clip.vocab import Vocab, ensure_text_matrix, load_vocab
from sortandpost.config import Settings, TaggingConfig
from sortandpost.cpu import clip_profile, set_thread_priority
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
from sortandpost.indexer.progress import PROGRESS
from sortandpost.indexer.scanner import collapse_scope, iter_image_files, iter_scoped_files
from sortandpost.indexer.workers import DecodedFile, DecodeJob, decode_file, decode_local
from sortandpost.library_roots import container_roots, running_in_docker
from sortandpost.metadata import ExiftoolClient
from sortandpost.paths import Paths
from sortandpost.procwatch import pool_worker_init

log = logging.getLogger(__name__)

# Below this many files needing work, the spawn cost of a process pool is not
# worth it -- decode them inline on the calling thread instead.
_MIN_FILES_FOR_POOL = 8

# The folder walk reports progress every this many files (it is a tight loop).
_PROGRESS_EVERY = 25

# Serializes scan_once/scan_clip_once against each other and themselves within
# this process. The background indexer loop, a manual "Run indexer pass now"
# click, and the online-only hydrate pass each open their own Session against
# the same working DB; without this, two of them running at once can each
# get-or-create the same ScanState/EmbeddingRow (neither sees the other's
# uncommitted insert) and collide with a UNIQUE constraint on commit.
_INDEX_LOCK = threading.RLock()


@dataclass
class ScanStats:
    seen: int = 0
    indexed: int = 0
    skipped: int = 0
    tombstoned: int = 0  # files skipped because the user deleted that pixel_hash
    tombstones_applied: int = 0  # tombstones this pass purged + trashed
    online_only: int = 0  # cloud placeholder files recorded but not downloaded
    new_photos: int = 0
    thumbnails: int = 0
    errors: int = 0
    marked_absent: int = 0
    marked_missing: int = 0
    paths_pruned: int = 0  # stale rows of photos that moved (see sweep_presence)
    ratings_imported: int = 0
    ratings_written: int = 0
    keywords_imported: int = 0
    interrupted: bool = False  # stopped early (pause / quit); no presence sweep this pass
    scoped: int | None = None  # a scoped pass: how many paths it walked (None = full scan)

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
        online = f"; {self.online_only} online-only (skipped)" if self.online_only else ""
        pruned = f"; {self.paths_pruned} moved path(s) forgotten" if self.paths_pruned else ""
        scope = f"scoped to {self.scoped} path(s): " if self.scoped is not None else ""
        return (
            f"{scope}{self.seen} files seen, {self.indexed} indexed "
            f"({self.new_photos} new, {self.thumbnails} thumbnails), "
            f"{self.skipped} unchanged, {self.errors} errors; "
            f"{self.marked_absent} paths now absent, {self.marked_missing} photos now missing"
            f"{reconciled}{tomb}{online}{pruned}" + (" (interrupted)" if self.interrupted else "")
        )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def scan_once(
    settings: Settings,
    *,
    workers: int | None = None,
    interrupt: Callable[[], bool] | None = None,
    scope: Iterable[Path | str] | None = None,
) -> ScanStats:
    """One pass over the library roots (stages 1-6).

    ``scope`` (the indexer loop's debounced filesystem events) limits the pass
    to those paths: a folder is walked, a file checked on its own, and the
    presence sweep judges only paths at or under them. A scope that covers a
    root is a full pass. The periodic full pass stays the safety net for any
    event the watcher missed.

    Stages 2-4 (decode, pixel + perceptual hash, thumbnails) are CPU-bound and
    run in a ``ProcessPoolExecutor`` (hard constraint #7); the main process does
    the fast-path check, the DB upserts and the ``exiftool`` reconcile serially.
    ``workers`` overrides ``config.indexer.workers`` (``1`` forces the inline
    path -- used by the tests and as an escape hatch). A pool is only spun up
    when enough files actually need work to amortise the ``spawn`` cost.

    Serialized process-wide by :data:`_INDEX_LOCK`: the background loop and a
    user-triggered "Run indexer pass now" both call this with their own
    ``Session``, and two such sessions racing on the same working DB produce
    ``UNIQUE constraint failed`` errors (get-or-create checks in one session
    can't see the other's uncommitted inserts). The cross-machine write lock
    (hard constraint #9) doesn't cover this -- it's intra-process.

    ``interrupt`` is polled between files (the app's quit / "Pause indexing").
    Once it returns True the pass stops after the file in hand -- every file
    already done is committed, the rest are picked up next pass (constraint #6).
    An interrupted pass skips the presence sweep: it hasn't seen every path, so
    sweeping would wrongly mark the unvisited ones missing.
    """
    with _INDEX_LOCK:
        try:
            return _scan_once_locked(settings, workers=workers, interrupt=interrupt, scope=scope)
        finally:
            PROGRESS.finish()


def _scan_once_locked(
    settings: Settings,
    *,
    workers: int | None = None,
    interrupt: Callable[[], bool] | None = None,
    scope: Iterable[Path | str] | None = None,
) -> ScanStats:
    stop_now = interrupt or (lambda: False)
    paths = settings.paths
    engine = make_engine(paths.working_db)
    init_db(engine)  # no-op once Alembic has run; safety net for native --once

    roots = container_roots(settings.library_roots)
    tcfg = settings.config.thumbnails
    pcfg = PipelineConfig(
        thumbnail_sizes=tuple(tcfg.sizes),
        thumbnail_format=tcfg.format,
        thumbnail_quality=tcfg.quality,
        thumbnail_path=paths.thumbnail_file,
        # A container can't make the host's sync client download a stub (the read
        # is an I/O error), so "download" would only turn stubs into errors.
        online_only_mode="skip" if running_in_docker() else settings.config.indexer.online_only,
        stacking=settings.config.stacking,
    )
    n_workers = settings.config.worker_count() if workers is None else max(1, workers)
    lower_priority = settings.config.clip.speed == "slow"
    set_thread_priority(lower_priority)  # inline decoding + any pool we spawn inherit it (Linux)

    have_exiftool = binaries.exiftool_available()
    if not have_exiftool:
        log.warning("exiftool not found; skipping embedded-metadata reads this pass")
    et_ctx = (
        ExiftoolClient(write_windows_compat_tags=settings.config.rating.write_windows_compat_tags)
        if have_exiftool
        else nullcontext(None)
    )

    scoped = collapse_scope(scope, roots) if scope is not None else None
    exclude_names = settings.config.indexer.exclude_dir_names
    if scoped is None:
        walk = iter_image_files(roots, exclude=paths.roots(), exclude_names=exclude_names)
    else:
        walk = iter_scoped_files(roots, scoped, exclude=paths.roots(), exclude_names=exclude_names)

    stats = ScanStats(scoped=len(scoped) if scoped is not None else None)
    seen_paths: set[str] = set()
    PROGRESS.begin("discovering")
    with Session(engine) as session, et_ctx as exiftool:
        # -- discover + fast-path filter (main thread) ------------------------ #
        # A full pass loads every ScanState in one query; a scoped one (a few
        # files) looks each up instead of loading the whole library.
        scan_rows = (
            {s.path: s for s in session.exec(select(ScanState)).all()} if scoped is None else None
        )
        todo: list[str] = []
        suspects: set[str] = set()  # killed a decode worker before: never decode inline
        for path in walk:
            if stop_now():
                stats.interrupted = True
                break
            key = str(path)
            seen_paths.add(key)
            stats.seen += 1
            if stats.seen % _PROGRESS_EVERY == 0:
                PROGRESS.update(done=stats.seen, current=path.name)
            try:
                st = path.stat()
            except OSError as exc:
                log.warning("stat failed for %s: %s", path, exc)
                _record_failure(session, path, exc)
                session.commit()
                stats.errors += 1
                continue
            scan_row = scan_rows.get(key) if scan_rows is not None else session.get(ScanState, key)
            if not needs_reindex(scan_row, st.st_size, st.st_mtime_ns):
                stats.skipped += 1
                continue
            todo.append(key)
            if scan_row is not None and (scan_row.error or "").startswith(_CRASH_ERROR):
                suspects.add(key)

        # -- stages 2-4 in parallel, stages 5-6 + DB serial here ------------- #
        if stats.interrupted:
            todo = []
        PROGRESS.begin("indexing", total=len(todo))
        stream = _decode_stream(
            todo, pcfg, paths, n_workers, lower_priority=lower_priority, suspects=suspects
        )
        for n_done, decoded in enumerate(stream, start=1):
            if stop_now():
                stats.interrupted = True
                stream.close()  # waits only for the few files already in flight
                break
            PROGRESS.update(done=n_done, current=Path(decoded.path).name)
            try:
                outcome = persist_decoded(
                    session,
                    decoded,
                    exiftool=exiftool,
                    device_id=settings.device_id,
                    now=_utcnow(),
                    stacking=pcfg.stacking,
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

        if stats.interrupted:
            log.info("scan interrupted: %s", stats)
            return stats

        # only this computer's paths: the other devices' copies are in the
        # shared photo_paths too, and never under these roots (V3 P9)
        judged = missing.for_settings(settings).judged
        result = sweep_presence(
            session,
            seen_paths,
            now=_utcnow(),
            scope=scoped,
            judged=judged,
            prunable=_really_gone(roots),
        )
        session.commit()
        stats.marked_absent = result.marked_absent
        stats.marked_missing = result.marked_missing
        stats.paths_pruned = result.pruned

        # Apply any not-yet-applied tombstones (from a user delete on this
        # machine, or one that arrived via merge): purge the derived rows and
        # move this machine's still-present copy of the file to trash.
        tomb = apply_pending_tombstones(
            session,
            paths=paths,
            roots=roots,
            clip_model=settings.config.clip.model,
            thumb_sizes=tuple(tcfg.sizes),
            now=_utcnow(),
            trash_local_files=True,
        )
        session.commit()
        stats.tombstones_applied = len(tomb.purged)

    log.info("scan complete: %s", stats)
    return stats


def _really_gone(roots: list[Path]) -> Callable[[str], bool]:
    """For the presence sweep's pruning: is *path* under one of this machine's
    library folders that is online right now, with nothing at that path? An
    offline drive's paths look absent too, but must never be pruned -- nor an
    empty root (a Docker bind mount of a host folder that isn't there)."""
    online = [os.path.normcase(str(r)) for r in roots if _has_entries(r)]

    def _check(path: str) -> bool:
        p = os.path.normcase(path)
        under = any(p.startswith(r.rstrip(os.sep) + os.sep) for r in online)
        return under and not os.path.lexists(path)

    return _check


def _has_entries(root: Path) -> bool:
    try:
        with os.scandir(root) as it:
            return next(it, None) is not None
    except OSError:
        return False


def _decode_stream(
    paths_to_do: list[str],
    pcfg: PipelineConfig,
    paths: Paths,
    n_workers: int,
    *,
    lower_priority: bool = False,
    suspects: frozenset[str] | set[str] = frozenset(),
) -> Generator[DecodedFile]:
    """Yield a :class:`DecodedFile` per input path -- via a process pool when the
    batch is large enough, otherwise inline. Bounded in-flight work keeps peak
    memory flat regardless of library size. If the pool cannot run (``spawn``
    can't re-import ``__main__``, sandbox limits, ...) the scan falls back to
    inline decoding rather than failing.

    One bad file never aborts the pass: any exception becomes a failed
    :class:`DecodedFile` (Issues.md S2). A file that kills a worker process
    (segfault, OOM) is recorded as crashed rather than retried in this process,
    and *suspects* -- files that crashed a worker on an earlier pass -- always
    decode in a throwaway one-worker pool, never inline."""
    if not paths_to_do:
        return

    job = DecodeJob(
        local_dir=str(paths.local),
        sync_dir=str(paths.sync),
        thumb_sizes=pcfg.thumbnail_sizes,
        thumb_fmt=pcfg.thumbnail_format,
        thumb_quality=pcfg.thumbnail_quality,
        online_only_mode=pcfg.online_only_mode,
    )
    ctx = multiprocessing.get_context("spawn")  # safe from the app's indexer thread

    def _inline(key: str) -> DecodedFile:
        if key in suspects:
            return _decode_isolated(job, key, ctx, lower_priority=lower_priority)
        try:
            return decode_local(
                Path(key),
                thumb_path_fn=pcfg.thumbnail_path,
                sizes=pcfg.thumbnail_sizes,
                fmt=pcfg.thumbnail_format,
                quality=pcfg.thumbnail_quality,
                online_only_mode=pcfg.online_only_mode,
            )
        except Exception as exc:  # never let one bad file kill the pass
            log.exception("decode failed on %s", key)
            return _worker_crash(key, exc, prefix="decode crashed")

    if n_workers <= 1 or len(paths_to_do) < _MIN_FILES_FOR_POOL:
        for key in paths_to_do:
            yield _inline(key)
        return

    max_inflight = n_workers * 2
    log.info("indexing %d files across %d worker processes", len(paths_to_do), n_workers)
    done_keys: set[str] = set()
    pending: dict[Future[DecodedFile], str] = {}
    try:
        with ProcessPoolExecutor(
            max_workers=n_workers,
            mp_context=ctx,
            initializer=pool_worker_init,
            initargs=(lower_priority,),
        ) as pool:
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
                        pending[fut] = key  # still unresolved: handled below
                        raise
                    except Exception as exc:  # a single worker's un-picklable failure
                        log.exception("decode worker crashed on %s", key)
                        result = _worker_crash(key, exc)
                    done_keys.add(key)
                    yield result
                    nxt = next(stream, None)
                    if nxt is not None:
                        pending[pool.submit(decode_file, job, nxt)] = nxt
    except BrokenProcessPool as exc:
        if not done_keys:
            # Nothing ever came back: the pool can't run here at all (spawn
            # can't re-import __main__, sandbox limits, ...). Decode inline.
            log.warning("decode process pool unusable (%s); indexing inline", exc)
            for key in paths_to_do:
                yield _inline(key)
            return
        # A worker died mid-pass: one of the files in flight killed it. Don't
        # retry those here -- a segfault / OOM in this process takes the app
        # down. Keep any that finished, record the rest as crashed
        # (``attempts`` caps the retries; the next pass isolates them) and
        # carry on with a fresh pool.
        crashed = 0
        for fut, key in pending.items():
            done_keys.add(key)
            if fut.done() and not fut.cancelled() and fut.exception() is None:
                yield fut.result()
            else:
                crashed += 1
                yield _worker_crash(key, exc)
        log.warning("a decode worker died (%s); %d file(s) recorded as crashed", exc, crashed)
        remaining = [k for k in paths_to_do if k not in done_keys]
        yield from _decode_stream(
            remaining,
            pcfg,
            paths,
            n_workers,
            lower_priority=lower_priority,
            suspects=suspects,
        )


def _decode_isolated(
    job: DecodeJob,
    key: str,
    ctx: multiprocessing.context.BaseContext,
    *,
    lower_priority: bool,
) -> DecodedFile:
    """Decode one suspect file in its own one-worker pool, so a crash takes
    down only that throwaway process."""
    try:
        with ProcessPoolExecutor(
            max_workers=1,
            mp_context=ctx,
            initializer=pool_worker_init,
            initargs=(lower_priority,),
        ) as pool:
            return pool.submit(decode_file, job, key).result()
    except Exception as exc:  # BrokenProcessPool included
        log.warning("decode of %s crashed its worker again: %s", key, exc)
        return _worker_crash(key, exc)


# What a crashed decode leaves in ``scan_state.error`` ("<stage>: <message>"):
# such a file is decoded only in an isolated worker on later passes.
_CRASH_ERROR = "decode: worker crashed"


def _worker_crash(key: str, exc: BaseException, *, prefix: str = "worker crashed") -> DecodedFile:
    try:
        st = Path(key).stat()
        size, mtime_ns = st.st_size, st.st_mtime_ns
    except OSError:
        size = mtime_ns = 0
    return DecodedFile(
        path=key,
        size=size,
        mtime_ns=mtime_ns,
        error=f"{prefix}: {type(exc).__name__}: {exc}"[:400],
        failed_stage="decode",
    )


def _tally(stats: ScanStats, outcome: IndexOutcome) -> None:
    if outcome.error is not None:
        stats.errors += 1
    elif outcome.online_only:
        stats.online_only += 1
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
    interrupted: bool = False
    disabled: bool = False  # ``clip.enabled`` is off

    def __str__(self) -> str:
        if self.disabled:
            return "CLIP pass skipped: AI search and tagging are turned off"
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
    interrupt: Callable[[], bool] | None = None,
) -> ClipStats:
    """Embed every browsable photo with no row in the current model's matrix, and
    score it against the zero-shot vocabulary.

    Idempotent and resumable: a photo already in the store (or, after ``reembed``,
    freshly cleared) is the unit of work, committed per batch. Raises
    :class:`~sortandpost.clip.store.ModelMismatch` if ``embeddings_meta`` was
    written by a different model and ``reembed`` is not set.

    Shares :data:`_INDEX_LOCK` with :func:`scan_once` (see its docstring): both
    do get-or-create DB writes that aren't safe from two concurrent sessions.
    """
    if not settings.config.clip.enabled:
        return ClipStats(disabled=True)
    with _INDEX_LOCK:
        try:
            return _scan_clip_once_locked(
                settings, reembed=reembed, model=model, interrupt=interrupt
            )
        finally:
            PROGRESS.finish()


def _scan_clip_once_locked(
    settings: Settings,
    *,
    reembed: bool = False,
    model: ClipModel | None = None,
    interrupt: Callable[[], bool] | None = None,
) -> ClipStats:
    stop_now = interrupt or (lambda: False)
    paths = settings.paths
    ccfg = settings.config.clip
    tcfg = settings.config.tagging

    # Snapshot the CPU budget once per pass: a slow/fast change in Settings
    # applies from the next pass, never mid-pass.
    profile = clip_profile(ccfg.speed, ccfg.batch_size)

    if model is None:
        model = ClipModel(ccfg, paths.models_dir)
    if (
        isinstance(model, ClipModel)  # not a test double
        and not model.loaded
        and clip_available()
        and not model.weights_present()
    ):
        PROGRESS.begin("downloading_model")  # first run: tell the status bar why it waits
    try:
        model.load()
    except ClipModelUnavailable as exc:
        log.warning("CLIP pass skipped: %s", exc)
        return ClipStats(skipped_no_model=True)
    apply_cpu_profile(profile)
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
            batch_size = profile.batch_size
            PROGRESS.begin("embedding", total=len(candidates))
            n_done = 0
            for chunk in _chunked(candidates, batch_size):
                if stop_now():  # between batches: each one is committed whole
                    stats.interrupted = True
                    break
                PROGRESS.update(done=n_done, current=Path(chunk[0][1]).name)
                n_done += len(chunk)
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
                PROGRESS.update(done=n_done)
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
