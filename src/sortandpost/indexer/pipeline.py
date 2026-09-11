"""Per-file indexing pipeline (build-order step 3: stages 1-6).

``ScanState.stages_done`` is a bitmask of :class:`Stage`, so a file killed
mid-pipeline resumes where it left off. Every stage is idempotent: re-running a
fully indexed file is a cheap stat check, and re-running a partially indexed one
repeats only the unfinished stages.

Identity is always ``pixel_hash``. "Same hash at a new path" and "same hash at
several present paths" are both just multiple :class:`~sortandpost.db.models.PhotoPath`
rows for one :class:`~sortandpost.db.models.Photo`; the move-vs-duplicate
distinction falls out of how many rows are ``present`` after a scan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntFlag
from pathlib import Path

from sqlmodel import Session, col, select

from sortandpost.db.models import Photo, PhotoPath, ScanState, Tombstone
from sortandpost.indexer.reconcile import ReconcileOutcome, reconcile_metadata
from sortandpost.indexer.thumbnails import ThumbPathFn
from sortandpost.indexer.workers import DecodedFile, decode_local
from sortandpost.metadata import EmbeddedMetadata, ExiftoolClient, MetadataError, is_raw

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3


class Stage(IntFlag):
    STAT = 1 << 0  # stat + fast-path (size, mtime_ns) check
    DECODE = 1 << 1  # decode to RGB (Pillow / pillow-heif / rawpy preview)
    HASH = 1 << 2  # pixel_hash = sha256(rgb_bytes); upsert Photo/PhotoPath
    THUMBNAILS = 1 << 3  # write configured WebP sizes, keyed by hash
    METADATA = 1 << 4  # exiftool batch read into Photo columns
    RECONCILE = 1 << 5  # rating: file vs DB (mtime wins tie-break)
    CLIP = 1 << 6  # image embedding + zero-shot predictions

    BROWSABLE = STAT | DECODE | HASH | THUMBNAILS | METADATA | RECONCILE
    COMPLETE = BROWSABLE | CLIP


@dataclass
class IndexOutcome:
    path: Path
    pixel_hash: str | None = None
    skipped: bool = False
    tombstoned: bool = False  # pixel_hash was deleted by the user; not re-indexed
    online_only: bool = False  # a cloud placeholder we recorded but did not download
    new_photo: bool = False
    thumbnails_written: tuple[int, ...] = ()
    reconcile: ReconcileOutcome | None = None
    error: str | None = None


@dataclass
class PipelineConfig:
    thumbnail_sizes: tuple[int, ...]
    thumbnail_format: str
    thumbnail_quality: int
    thumbnail_path: ThumbPathFn
    online_only_mode: str = "skip"  # "skip" defers cloud placeholders; "download" decodes them


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
def index_file(
    session: Session,
    path: Path,
    *,
    config: PipelineConfig,
    exiftool: ExiftoolClient | None,
    device_id: str,
    now: datetime,
) -> IndexOutcome:
    """Run stages 1-6 for one file, serially. Caller commits; caller sweeps.

    The parallel scan splits this: workers run stages 2-4
    (:func:`sortandpost.indexer.workers.decode_local`) and the main process runs
    :func:`persist_decoded` (stages 1, 3-identity, 5-6).
    """
    key = str(path)
    try:
        st = path.stat()
    except OSError as exc:
        return IndexOutcome(path=path, error=f"stat: {exc}")

    scan = session.get(ScanState, key)
    if not needs_reindex(scan, st.st_size, st.st_mtime_ns):
        assert scan is not None
        return IndexOutcome(path=path, pixel_hash=scan.pixel_hash, skipped=True)

    decoded = decode_local(
        path,
        thumb_path_fn=config.thumbnail_path,
        sizes=config.thumbnail_sizes,
        fmt=config.thumbnail_format,
        quality=config.thumbnail_quality,
        online_only_mode=config.online_only_mode,
    )
    return persist_decoded(session, decoded, exiftool=exiftool, device_id=device_id, now=now)


def persist_decoded(
    session: Session,
    decoded: DecodedFile,
    *,
    exiftool: ExiftoolClient | None,
    device_id: str,
    now: datetime,
) -> IndexOutcome:
    """Apply one worker's :class:`DecodedFile` to the DB: stage 3 identity
    (``photos`` / ``photo_paths``), stage 5 metadata read, stage 6 reconcile,
    and the ``scan_state`` bookkeeping. Runs on the main process only (holds the
    ``Session`` and the persistent ``exiftool``). Caller commits."""
    key = decoded.path
    path = Path(key)

    scan = session.get(ScanState, key)
    if scan is None:
        scan = ScanState(path=key)
        session.add(scan)
    scan.size = decoded.size
    scan.mtime_ns = decoded.mtime_ns
    scan.error = None
    scan.online_only = False

    # -- cloud placeholder we chose not to download --------------------- #
    # Not a failure: don't bump `attempts`. `needs_reindex` keeps returning
    # True while the flag is set, so a later hydrate + rescan indexes it.
    if decoded.online_only:
        scan.online_only = True
        scan.stages_done = int(Stage.STAT)
        return IndexOutcome(path=path, online_only=True)

    if decoded.error is not None:
        scan.error = f"{decoded.failed_stage}: {decoded.error}"[:500]
        scan.attempts += 1
        if decoded.failed_stage == "thumbnail":
            scan.pixel_hash = decoded.pixel_hash
            scan.stages_done = int(Stage.STAT | Stage.DECODE | Stage.HASH)
        else:
            scan.stages_done = int(Stage.STAT)
        log.warning("%s failed for %s: %s", decoded.failed_stage, key, decoded.error)
        return IndexOutcome(path=path, pixel_hash=decoded.pixel_hash, error=scan.error)

    assert decoded.pixel_hash is not None
    pixel_hash = decoded.pixel_hash

    # -- deleted by the user: don't resurrect it -------------------------- #
    # The still-present file is trashed by the post-scan tombstone sweep
    # (sortandpost.db.deletion.apply_pending_tombstones).
    if session.get(Tombstone, pixel_hash) is not None:
        scan.pixel_hash = pixel_hash
        scan.stages_done = int(Stage.STAT)
        return IndexOutcome(path=path, pixel_hash=pixel_hash, tombstoned=True)

    # -- stage 3: identity -------------------------------------------------- #
    new_photo = _upsert_photo(
        session,
        pixel_hash,
        path,
        decoded.size,
        decoded.mtime_ns,
        decoded.width,
        decoded.height,
        decoded.fmt,
        now,
    )
    _upsert_path(session, pixel_hash, path, decoded.size, decoded.mtime_ns)
    scan.pixel_hash = pixel_hash

    photo_row = session.get(Photo, pixel_hash)
    if photo_row is not None and photo_row.phash is None and decoded.phash is not None:
        photo_row.phash = decoded.phash

    # -- stage 5: embedded metadata (best effort) ------------------------- #
    md: EmbeddedMetadata | None = None
    if exiftool is not None:
        try:
            md = exiftool.read(path)
            _apply_metadata(session, pixel_hash, md)
        except MetadataError as exc:
            log.warning("metadata read failed for %s: %s", path, exc)
            md = None

    # -- stage 6: rating / keyword reconcile (file vs DB) --------------- #
    reconcile: ReconcileOutcome | None = None
    if md is not None:
        try:
            reconcile = reconcile_metadata(
                session,
                pixel_hash=pixel_hash,
                path=path,
                md=md,
                exiftool=exiftool,
                device_id=device_id,
                file_mtime_ns=decoded.mtime_ns,
                now=now,
            )
        except MetadataError as exc:
            log.warning("metadata reconcile failed for %s: %s", path, exc)

    scan.stages_done = int(Stage.BROWSABLE)
    scan.indexed_at = now
    scan.attempts = 0
    return IndexOutcome(
        path=path,
        pixel_hash=pixel_hash,
        new_photo=new_photo,
        thumbnails_written=decoded.thumbnails_written,
        reconcile=reconcile,
    )


# --------------------------------------------------------------------------- #
# Stage helpers
# --------------------------------------------------------------------------- #
def needs_reindex(scan: ScanState | None, size: int, mtime_ns: int) -> bool:
    if scan is None:
        return True
    if scan.online_only:
        return True  # cheap: the worker re-checks the stat and defers again if still a stub
    if scan.error is not None:
        return scan.attempts < _MAX_ATTEMPTS
    if scan.size != size or scan.mtime_ns != mtime_ns:
        return True
    return (scan.stages_done & Stage.BROWSABLE) != Stage.BROWSABLE


def _upsert_photo(
    session: Session,
    pixel_hash: str,
    path: Path,
    size: int,
    mtime_ns: int,
    width: int,
    height: int,
    fmt: str,
    now: datetime,
) -> bool:
    photo = session.get(Photo, pixel_hash)
    if photo is None:
        session.add(
            Photo(
                pixel_hash=pixel_hash,
                primary_path=str(path),
                width=width,
                height=height,
                format=fmt,
                is_raw=is_raw(path),
                file_size=size,
                file_mtime_ns=mtime_ns,
                first_indexed_at=now,
                last_indexed_at=now,
            )
        )
        return True
    photo.last_indexed_at = now
    photo.missing_since = None
    if photo.primary_path == str(path):
        photo.file_size = size
        photo.file_mtime_ns = mtime_ns
    return False


def _upsert_path(session: Session, pixel_hash: str, path: Path, size: int, mtime_ns: int) -> None:
    key = str(path)
    row = session.exec(select(PhotoPath).where(PhotoPath.path == key)).first()
    if row is None:
        session.add(
            PhotoPath(
                pixel_hash=pixel_hash,
                path=key,
                size=size,
                mtime_ns=mtime_ns,
                present=True,
            )
        )
        return
    # Content at a known path changed identity: repoint it. The now-possibly
    # orphaned old Photo is picked up by the end-of-scan presence sweep.
    row.pixel_hash = pixel_hash
    row.size = size
    row.mtime_ns = mtime_ns
    row.present = True


def _apply_metadata(session: Session, pixel_hash: str, md: EmbeddedMetadata) -> None:
    photo = session.get(Photo, pixel_hash)
    if photo is None:
        return
    if md.taken_at is not None:
        photo.taken_at = md.taken_at
    if md.orientation is not None:
        photo.orientation = md.orientation
    if md.camera_make is not None:
        photo.camera_make = md.camera_make
    if md.camera_model is not None:
        photo.camera_model = md.camera_model
    if md.gps_lat is not None:
        photo.gps_lat = md.gps_lat
    if md.gps_lon is not None:
        photo.gps_lon = md.gps_lon


# --------------------------------------------------------------------------- #
# End-of-scan presence sweep
# --------------------------------------------------------------------------- #
@dataclass
class SweepResult:
    marked_absent: int = 0
    marked_missing: int = 0
    revived: int = 0
    repointed_primary: list[str] = field(default_factory=list)


def sweep_presence(session: Session, seen_paths: set[str], *, now: datetime) -> SweepResult:
    """Reconcile ``present``/``missing_since`` after a full scan pass.

    Any ``photo_paths`` row whose path was not visited this pass becomes absent;
    a :class:`Photo` with no present path gets ``missing_since`` (never purged --
    hard constraint #8). ``primary_path`` is repointed to the lexically first
    present path when the current one is gone.
    """
    result = SweepResult()

    for pp in session.exec(select(PhotoPath)).all():
        should_be_present = pp.path in seen_paths
        if pp.present and not should_be_present:
            pp.present = False
            result.marked_absent += 1
        elif not pp.present and should_be_present:
            pp.present = True

    for photo in session.exec(select(Photo)).all():
        present = session.exec(
            select(PhotoPath).where(
                PhotoPath.pixel_hash == photo.pixel_hash,
                col(PhotoPath.present).is_(True),
            )
        ).all()
        if present:
            if photo.missing_since is not None:
                photo.missing_since = None
                result.revived += 1
            present_paths = sorted(p.path for p in present)
            if photo.primary_path not in present_paths:
                photo.primary_path = present_paths[0]
                result.repointed_primary.append(present_paths[0])
        elif photo.missing_since is None:
            photo.missing_since = now
            result.marked_missing += 1

    return result
