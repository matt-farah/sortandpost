"""The explicit "Delete photos" action (docs/architecture.md constraint #1, exception 2).

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

Before purging, each photo's user state (rating, tags, CLIP decisions) is saved
into the trash batch's manifest, so :func:`restore_photo_state` can put it back
after a "Put back" (see :mod:`sortandpost.undelete`).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session, col, select

from sortandpost.clip.store import EmbeddingStore, load_meta
from sortandpost.db.models import (
    ChangeOp,
    EmbeddingRow,
    Photo,
    PhotoPath,
    PhotoTag,
    Prediction,
    PredictionState,
    Rating,
    RatingSource,
    ScanState,
    Tag,
    TagKind,
    TagSource,
    Tombstone,
)
from sortandpost.hashing import pixel_hash_of_image
from sortandpost.paths import Paths
from sortandpost.sync.changelog import ensure_writable, record_change
from sortandpost.sync.clock import parse as parse_ts
from sortandpost.trash import TrashedItem, TrashResult, move_to_trash

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
    roots: Sequence[Path],
    clip_model: str,
    thumb_sizes: Sequence[int],
    device_id: str,
    now: datetime,
    trash_files: bool = True,
    emit_change: bool = True,
) -> DeleteResult:
    """Delete *pixel_hashes* on this machine. Only files under *roots* (this
    machine's library folders) are moved -- see :func:`_trash_with_state`.
    Caller commits the session."""
    if emit_change:
        ensure_writable()  # before any file moves
    wanted: list[str] = list(dict.fromkeys(pixel_hashes))
    result = DeleteResult()
    if not wanted:
        return result

    existing = [h for h in wanted if session.get(Photo, h) is not None]

    if trash_files and existing:
        result.trash = _trash_with_state(
            session,
            existing,
            paths=paths,
            roots=roots,
            thumb_sizes=thumb_sizes,
            now=now,
            reason="delete",
        )

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
    roots: Sequence[Path],
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
        result.trash = _trash_with_state(
            session,
            existing,
            paths=paths,
            roots=roots,
            thumb_sizes=thumb_sizes,
            now=now,
            reason="tombstone",
        )

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


def lift_tombstone(
    session: Session, *, pixel_hash: str, device_id: str, now: datetime, n_paths: int
) -> None:
    """Undo a delete in the DB: drop the tombstone (so the scanner indexes the
    file again) and append ``op=restore`` to ``changes`` (constraint #4). Merge
    replay settles ``unindex`` vs ``restore`` last-writer-wins by timestamp.
    Caller commits."""
    tomb = session.get(Tombstone, pixel_hash)
    if tomb is not None:
        session.delete(tomb)
    record_change(
        session,
        device_id=device_id,
        op=ChangeOp.restore,
        pixel_hash=pixel_hash,
        ts=now,
        field="delete",
        new_value=str(n_paths),
    )


def capture_photo_state(session: Session, pixel_hash: str) -> dict[str, Any]:
    """The user-authored rows the purge is about to drop, as plain JSON."""
    rating = session.get(Rating, pixel_hash)
    tags: list[dict[str, Any]] = []
    for pt in session.exec(select(PhotoTag).where(PhotoTag.pixel_hash == pixel_hash)).all():
        tag = session.get(Tag, pt.tag_id)
        if tag is None:
            continue
        tags.append(
            {
                "name": tag.name,
                "facet": tag.facet,
                "kind": str(tag.kind),
                "source": str(pt.source),
                "score": pt.score,
                "added_at": pt.added_at.isoformat(),
                "added_by_device": pt.added_by_device,
                "written_to_file": pt.written_to_file,
            }
        )
    preds = [
        {
            "tag_name": p.tag_name,
            "score": p.score,
            "model": p.model,
            "run_at": p.run_at.isoformat(),
            "state": str(p.state),
        }
        for p in session.exec(select(Prediction).where(Prediction.pixel_hash == pixel_hash)).all()
    ]
    return {
        "rating": None
        if rating is None
        else {
            "stars": rating.stars,
            "updated_at": rating.updated_at.isoformat(),
            "updated_by_device": rating.updated_by_device,
            "source": str(rating.source),
        },
        "tags": tags,
        "predictions": preds,
    }


def restore_photo_state(session: Session, pixel_hash: str, state: dict[str, Any]) -> bool:
    """Re-insert the rows :func:`capture_photo_state` saved, onto a photo that
    was just re-indexed. The saved rating wins over whatever the re-index read
    from the file (it was the DB's truth at delete time); tags and predictions
    are only added where missing. Returns ``False`` if the photo row is absent
    (the re-index failed). Caller commits."""
    if session.get(Photo, pixel_hash) is None:
        return False

    r = state.get("rating")
    if r:
        row = session.get(Rating, pixel_hash)
        if row is None:
            row = Rating(
                pixel_hash=pixel_hash,
                stars=int(r["stars"]),
                updated_at=parse_ts(r["updated_at"]),
                updated_by_device=str(r["updated_by_device"]),
            )
            session.add(row)
        row.stars = int(r["stars"])
        row.updated_at = parse_ts(r["updated_at"])
        row.updated_by_device = str(r["updated_by_device"])
        row.source = RatingSource(r.get("source", "app"))

    for t in state.get("tags", []):
        tag = session.exec(select(Tag).where(Tag.name == t["name"])).first()
        if tag is None:
            tag = Tag(name=t["name"], facet=t.get("facet"), kind=TagKind(t.get("kind", "manual")))
            session.add(tag)
            session.flush()
        assert tag.id is not None
        have = session.exec(
            select(PhotoTag).where(PhotoTag.pixel_hash == pixel_hash, PhotoTag.tag_id == tag.id)
        ).first()
        if have is None:
            session.add(
                PhotoTag(
                    pixel_hash=pixel_hash,
                    tag_id=tag.id,
                    source=TagSource(t.get("source", "manual")),
                    score=t.get("score"),
                    added_at=parse_ts(t["added_at"]),
                    added_by_device=str(t["added_by_device"]),
                    written_to_file=bool(t.get("written_to_file", False)),
                )
            )

    for p in state.get("predictions", []):
        have_pred = session.exec(
            select(Prediction).where(
                Prediction.pixel_hash == pixel_hash,
                Prediction.tag_name == p["tag_name"],
                Prediction.model == p["model"],
            )
        ).first()
        if have_pred is None:
            session.add(
                Prediction(
                    pixel_hash=pixel_hash,
                    tag_name=p["tag_name"],
                    score=float(p["score"]),
                    model=p["model"],
                    run_at=parse_ts(p["run_at"]),
                    state=PredictionState(p["state"]),
                )
            )
        else:
            have_pred.state = PredictionState(p["state"])
    return True


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _under_roots(path: Path, roots: Sequence[Path]) -> bool:
    """Is *path* inside one of *roots*? Both sides are resolved, so a symlinked
    root (stored under its own spelling by the scanner) still matches."""
    try:
        target = os.path.normcase(os.path.realpath(path))
    except OSError:
        return False
    for root in roots:
        try:
            base = os.path.normcase(os.path.realpath(root))
        except OSError:
            continue
        if target == base or target.startswith(base.rstrip(os.sep) + os.sep):
            return True
    return False


def _still_same_photo(row: PhotoPath) -> bool:
    """Is the file at ``row.path`` still the photo ``row.pixel_hash`` names?
    Unchanged ``(size, mtime_ns)`` is trusted, like the scanner's fast path;
    anything else is decoded and re-hashed (the pixel hash is the identity,
    constraint #3). A file that won't decode is not ours to move."""
    try:
        st = Path(row.path).stat()
    except OSError:
        return False
    if st.st_size == row.size and st.st_mtime_ns == row.mtime_ns:
        return True
    # Imported here: sortandpost.indexer imports this module.
    from sortandpost.indexer.decode import DecodeError, load_image

    try:
        with load_image(Path(row.path)) as img:
            return pixel_hash_of_image(img) == row.pixel_hash
    except (DecodeError, OSError) as exc:
        log.warning("not trashing %s: could not re-check its pixels (%s)", row.path, exc)
        return False


def _trash_with_state(
    session: Session,
    hashes: Sequence[str],
    *,
    paths: Paths,
    roots: Sequence[Path],
    thumb_sizes: Sequence[int],
    now: datetime,
    reason: str,
) -> TrashResult | None:
    """Move the on-disk copies of *hashes* into one managed trash batch, saving
    each photo's user state + smallest thumbnail beside them for a put-back.

    Only a file inside one of this machine's library *roots* that still holds
    the deleted pixels is moved. ``photo_paths`` is shared across machines, so a
    path recorded elsewhere -- or a file replaced since the last scan -- may now
    name an unrelated file; those are reported as failed items and left alone."""
    rows = session.exec(
        select(PhotoPath).where(
            col(PhotoPath.pixel_hash).in_(list(hashes)),
            col(PhotoPath.present).is_(True),
        )
    ).all()
    on_disk = [r for r in rows if Path(r.path).exists()]
    if not on_disk:
        return None
    eligible: list[PhotoPath] = []
    refused: list[TrashedItem] = []
    for r in on_disk:
        if not _under_roots(Path(r.path), roots):
            log.warning("not trashing %s: outside this machine's library folders", r.path)
            refused.append(
                TrashedItem(
                    r.path, ok=False, error="outside the library folders", pixel_hash=r.pixel_hash
                )
            )
        elif not _still_same_photo(r):
            log.warning("not trashing %s: it no longer holds photo %s", r.path, r.pixel_hash)
            refused.append(
                TrashedItem(
                    r.path,
                    ok=False,
                    error="the file has changed since it was indexed",
                    pixel_hash=r.pixel_hash,
                )
            )
        else:
            eligible.append(r)

    result = TrashResult()
    if eligible:
        hash_of = {r.path: r.pixel_hash for r in eligible}
        wanted = set(hash_of.values())
        small = min(thumb_sizes) if thumb_sizes else None
        thumbs = {}
        if small is not None:
            for h in wanted:
                t = paths.thumbnail_file(h, small)
                if t.is_file():
                    thumbs[h] = t
        result = move_to_trash(
            [r.path for r in eligible],
            local_dir=paths.local,
            now=now,
            pixel_hash_of=hash_of,
            photo_state={h: capture_photo_state(session, h) for h in wanted},
            thumbs=thumbs,
            reason=reason,
        )
    result.items.extend(refused)
    return result


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
