"""The explicit "Delete photos" action (CLAUDE.md constraint #1, exception 2).

A user-invoked delete on the machine that runs it:

1. moves the present file copies of each selected ``pixel_hash`` to trash
   (:mod:`sortandpost.trash` -- OS Trash or a managed, reversible folder);
2. purges the derived DB rows (``photos``, ``photo_paths``, ``ratings``,
   ``photo_tags``, ``predictions``, ``embedding_rows``, ``scan_state``),
   compacts the embedding matrix, and deletes the content-addressed thumbnails;
3. appends an ``unindex`` row to ``changes`` (constraint #4) and writes a
   ``tombstones`` row.

Other machines learn of the delete through ``unindex`` merge replay (which writes
the same tombstone) and apply it lazily via :func:`apply_pending_tombstones` on
their next scan -- purging their own derived rows and trashing their own local
copy of the file if it is still present.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlmodel import Session, col, select

from sortandpost.clip.store import EmbeddingStore, load_meta
from sortandpost.db.models import (
    ChangeOp,
    EmbeddingRow,
    Photo,
    PhotoPath,
    PhotoTag,
    Prediction,
    Rating,
    ScanState,
    Tombstone,
)
from sortandpost.paths import Paths
from sortandpost.sync.changelog import record_change
from sortandpost.trash import TrashResult, move_to_trash

log = logging.getLogger(__name__)


@dataclass
class DeleteResult:
    purged: list[str] = field(default_factory=list)  # pixel_hashes whose rows were removed
    tombstoned: list[str] = field(default_factory=list)  # pixel_hashes a tombstone was written for
    thumbnails_removed: int = 0
    embeddings_removed: int = 0
    trash: TrashResult | None = None

    @property
    def files_trashed(self) -> int:
        return len(self.trash.moved) if self.trash else 0

    @property
    def files_failed(self) -> int:
        return len(self.trash.failed) if self.trash else 0


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #
def delete_photos(
    session: Session,
    *,
    pixel_hashes: Sequence[str],
    paths: Paths,
    clip_model: str,
    thumb_sizes: Sequence[int],
    device_id: str,
    now: datetime,
    trash_files: bool = True,
    emit_change: bool = True,
) -> DeleteResult:
    """Delete *pixel_hashes* on this machine. Caller commits the session."""
    wanted: list[str] = list(dict.fromkeys(pixel_hashes))
    result = DeleteResult()
    if not wanted:
        return result

    existing = [h for h in wanted if session.get(Photo, h) is not None]

    if trash_files and existing:
        present_paths = _present_paths(session, existing)
        if present_paths:
            result.trash = move_to_trash(present_paths, local_dir=paths.local, now=now)

    for h in wanted:
        n_paths = len(_all_paths(session, h))
        if session.get(Photo, h) is not None:
            result.thumbnails_removed += _purge_derived_rows(session, h, paths, thumb_sizes)
            result.purged.append(h)
        if emit_change:
            record_change(
                session,
                device_id=device_id,
                op=ChangeOp.unindex,
                pixel_hash=h,
                ts=now,
                field="delete",
                new_value=str(n_paths),
            )
        session.merge(
            Tombstone(
                pixel_hash=h,
                deleted_at=now,
                deleted_by_device=device_id,
                applied=True,
            )
        )
        result.tombstoned.append(h)

    result.embeddings_removed = _compact_embeddings(session, paths, clip_model, set(existing))
    return result


def apply_pending_tombstones(
    session: Session,
    *,
    paths: Paths,
    clip_model: str,
    thumb_sizes: Sequence[int],
    now: datetime,
    trash_local_files: bool,
) -> DeleteResult:
    """Apply every tombstone this machine has not applied yet: purge derived rows,
    compact embeddings, and (if *trash_local_files*) move this machine's still-
    present local copy of each file to trash. Caller commits."""
    pending = session.exec(select(Tombstone).where(col(Tombstone.applied).is_(False))).all()
    result = DeleteResult()
    if not pending:
        return result

    hashes = [t.pixel_hash for t in pending]
    existing = [h for h in hashes if session.get(Photo, h) is not None]

    if trash_local_files and existing:
        on_disk = [p for p in _present_paths(session, existing) if Path(p).exists()]
        if on_disk:
            result.trash = move_to_trash(on_disk, local_dir=paths.local, now=now)

    for t in pending:
        if session.get(Photo, t.pixel_hash) is not None:
            result.thumbnails_removed += _purge_derived_rows(
                session, t.pixel_hash, paths, thumb_sizes
            )
            result.purged.append(t.pixel_hash)
        t.applied = True

    result.embeddings_removed = _compact_embeddings(session, paths, clip_model, set(existing))
    if result.purged:
        log.info("applied %d tombstone(s): %s", len(result.purged), result.purged)
    return result


def is_tombstoned(session: Session, pixel_hash: str) -> bool:
    return session.get(Tombstone, pixel_hash) is not None


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _present_paths(session: Session, hashes: Sequence[str]) -> list[str]:
    rows = session.exec(
        select(PhotoPath).where(
            col(PhotoPath.pixel_hash).in_(list(hashes)),
            col(PhotoPath.present).is_(True),
        )
    ).all()
    return [r.path for r in rows]


def _all_paths(session: Session, pixel_hash: str) -> list[str]:
    rows = session.exec(select(PhotoPath).where(PhotoPath.pixel_hash == pixel_hash)).all()
    return [r.path for r in rows]


def _purge_derived_rows(
    session: Session, pixel_hash: str, paths: Paths, thumb_sizes: Sequence[int]
) -> int:
    """Drop every derived row for *pixel_hash* and its thumbnail files. Returns
    the number of thumbnail files removed. The ``changes`` and ``tombstones``
    rows are the caller's job and are kept."""
    for model in (Rating, PhotoTag, Prediction, EmbeddingRow, ScanState, PhotoPath):
        for row in session.exec(select(model).where(col(model.pixel_hash) == pixel_hash)).all():
            session.delete(row)
    photo = session.get(Photo, pixel_hash)
    if photo is not None:
        session.delete(photo)

    removed = 0
    tdir = paths.thumbnails_dir / pixel_hash[:2]
    if tdir.is_dir():
        for f in tdir.glob(f"{pixel_hash}_*"):
            try:
                f.unlink()
                removed += 1
            except OSError as exc:  # pragma: no cover
                log.warning("could not remove thumbnail %s: %s", f, exc)
    return removed


def _compact_embeddings(session: Session, paths: Paths, model: str, dropped: set[str]) -> int:
    """Compact the CLIP matrix after *dropped* were removed and re-point the
    surviving ``embedding_rows``. Returns how many dropped hashes had an
    embedding. A missing matrix / meta row is a no-op."""
    if not dropped:
        return 0
    meta = load_meta(session, model)
    matrix_path = paths.image_embeddings_file(model)
    if meta is None or not matrix_path.exists():
        return 0

    with EmbeddingStore.open(
        matrix_path, paths.embeddings_rowmap_file, dim=meta.dim, model=model
    ) as store:
        had = sum(1 for h in dropped if store.row_for(h) is not None)
        if not had:
            return 0
        new_index = store.remove_many(dropped)
        meta.count = len(new_index)

    for er in session.exec(select(EmbeddingRow)).all():
        idx = new_index.get(er.pixel_hash)
        if idx is not None and er.row_index != idx:
            er.row_index = idx
    return had
