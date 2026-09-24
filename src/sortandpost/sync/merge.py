"""Conflict-copy detection and the deterministic merge (CLAUDE.md → "Sync model").

Two machines can both edit if the sync tool is slow to propagate, leaving a
"conflicted copy" in ``sync/``. Merge is deterministic:

1. The copy with the highest ``db_generation`` is the base (chosen by the caller;
   this module merges *into* whatever session it is handed).
2. User state is reconciled **last-writer-wins per key**, keyed by
   ``(updated_at, updated_by_device)`` for ratings and by change timestamp for
   tag / CLIP state. The ``changes`` logs are unioned (de-duped on
   :func:`~sortandpost.sync.changelog.change_key`) so later third-party merges
   still see every mutation.
3. Derived tables (``photos``, ``photo_paths``, ``predictions``) are unioned by
   key. Both machines scanned overlapping sets, so a union is safe; anything
   still missing is picked up by the next reconcile scan.

Thumbnails and embeddings need no merge (content-addressed / append-only).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlmodel import Session, col, select

from sortandpost.db.meta import get_generation, set_generation
from sortandpost.db.models import (
    Change,
    ChangeOp,
    Photo,
    PhotoPath,
    PhotoTag,
    Prediction,
    PredictionState,
    Rating,
    Tag,
    Tombstone,
)
from sortandpost.sync.changelog import change_key, sort_key

_CANONICAL = "sortandpost.db"
_CONFLICT_RE = re.compile(
    r"(conflict|conflicted copy|\(\d+\)|-\d+ \(|\.sync-conflict)",
    re.IGNORECASE,
)


def find_conflict_copies(sync_dir: Path, *, canonical: str = _CANONICAL) -> list[Path]:
    """SQLite files in *sync_dir* that look like sync-tool conflict copies."""
    out: list[Path] = []
    for p in sorted(sync_dir.glob("*.db")):
        if p.name == canonical or p.name.startswith("."):
            continue
        if p.name.startswith("sortandpost") or _CONFLICT_RE.search(p.stem):
            out.append(p)
    return out


@dataclass
class MergeSummary:
    base_generation: int = 0
    merged_generation: int = 0
    sources: list[str] = field(default_factory=list)
    changes_unioned: int = 0
    ratings_updated: int = 0
    tags_updated: int = 0
    predictions_updated: int = 0
    photos_added: int = 0
    photo_paths_added: int = 0

    def add(self, other: MergeSummary) -> None:
        self.changes_unioned += other.changes_unioned
        self.ratings_updated += other.ratings_updated
        self.tags_updated += other.tags_updated
        self.predictions_updated += other.predictions_updated
        self.photos_added += other.photos_added
        self.photo_paths_added += other.photo_paths_added


# --------------------------------------------------------------------------- #
# Per-source merge
# --------------------------------------------------------------------------- #
def merge_session(target: Session, source: Session, *, now: datetime) -> MergeSummary:
    """Fold everything in *source* into *target*. Caller commits."""
    s = MergeSummary()
    s.photos_added = _union_photos(target, source)
    s.photo_paths_added = _union_photo_paths(target, source)
    s.predictions_updated = _union_predictions(target, source)
    s.ratings_updated = _merge_ratings(target, source)
    s.tags_updated = _merge_tags(target, source)
    s.changes_unioned = _union_changes(target, source)
    _replay_state_changes(target, source)
    return s


def _union_photos(target: Session, source: Session) -> int:
    existing = {p.pixel_hash: p for p in target.exec(select(Photo)).all()}
    added = 0
    for sp in source.exec(select(Photo)).all():
        tp = existing.get(sp.pixel_hash)
        if tp is None:
            target.add(_clone(sp, Photo))
            added += 1
            continue
        # deterministic, commutative refresh of the volatile columns
        if sp.last_indexed_at > tp.last_indexed_at:
            tp.last_indexed_at = sp.last_indexed_at
        tp.missing_since = _min_opt(tp.missing_since, sp.missing_since)
    return added


def _union_photo_paths(target: Session, source: Session) -> int:
    existing = {pp.path for pp in target.exec(select(PhotoPath)).all()}
    added = 0
    for sp in source.exec(select(PhotoPath)).all():
        if sp.path in existing:
            continue
        target.add(
            PhotoPath(
                pixel_hash=sp.pixel_hash,
                path=sp.path,
                size=sp.size,
                mtime_ns=sp.mtime_ns,
                present=sp.present,
            )
        )
        added += 1
    return added


def _union_predictions(target: Session, source: Session) -> int:
    existing = {(p.pixel_hash, p.tag_name, p.model) for p in target.exec(select(Prediction)).all()}
    added = 0
    for sp in source.exec(select(Prediction)).all():
        if (sp.pixel_hash, sp.tag_name, sp.model) in existing:
            continue
        target.add(
            Prediction(
                pixel_hash=sp.pixel_hash,
                tag_name=sp.tag_name,
                score=sp.score,
                model=sp.model,
                run_at=sp.run_at,
                state=sp.state,
            )
        )
        added += 1
    return added


def _merge_ratings(target: Session, source: Session) -> int:
    existing = {r.pixel_hash: r for r in target.exec(select(Rating)).all()}
    updated = 0
    for sr in source.exec(select(Rating)).all():
        tr = existing.get(sr.pixel_hash)
        if tr is None:
            target.add(_clone(sr, Rating))
            updated += 1
        elif (sr.updated_at, sr.updated_by_device) > (tr.updated_at, tr.updated_by_device):
            tr.stars = sr.stars
            tr.updated_at = sr.updated_at
            tr.updated_by_device = sr.updated_by_device
            tr.source = sr.source
            updated += 1
    return updated


def _merge_tags(target: Session, source: Session) -> int:
    """Union the vocabulary + manual ``photo_tags``; removals come from replay."""
    tag_ids: dict[str, int] = {}
    for t in target.exec(select(Tag)).all():
        if t.id is not None:
            tag_ids[t.name] = t.id
    for st in source.exec(select(Tag)).all():
        if st.name not in tag_ids:
            new = Tag(name=st.name, facet=st.facet, kind=st.kind)
            target.add(new)
            target.flush()
            assert new.id is not None
            tag_ids[st.name] = new.id

    src_tag_name = {t.id: t.name for t in source.exec(select(Tag)).all()}
    have = {(pt.pixel_hash, pt.tag_id) for pt in target.exec(select(PhotoTag)).all()}
    updated = 0
    for spt in source.exec(select(PhotoTag)).all():
        name = src_tag_name.get(spt.tag_id)
        if name is None:
            continue
        tid = tag_ids[name]
        if (spt.pixel_hash, tid) in have:
            continue
        target.add(
            PhotoTag(
                pixel_hash=spt.pixel_hash,
                tag_id=tid,
                source=spt.source,
                score=spt.score,
                added_at=spt.added_at,
                added_by_device=spt.added_by_device,
                written_to_file=spt.written_to_file,
            )
        )
        have.add((spt.pixel_hash, tid))
        updated += 1
    return updated


def _union_changes(target: Session, source: Session) -> int:
    seen = {change_key(c) for c in target.exec(select(Change)).all()}
    added = 0
    for sc in source.exec(select(Change)).all():
        if change_key(sc) in seen:
            continue
        target.add(
            Change(
                ts=sc.ts,
                device_id=sc.device_id,
                op=sc.op,
                pixel_hash=sc.pixel_hash,
                field=sc.field,
                old_value=sc.old_value,
                new_value=sc.new_value,
            )
        )
        seen.add(change_key(sc))
        added += 1
    return added


def _replay_state_changes(target: Session, source: Session) -> None:
    """Apply tag removals / CLIP state transitions from the source's log.

    Rating and tag *additions* are already covered by the table unions above;
    this only handles the mutations a union cannot represent (a row going away
    or changing state), last-writer-wins by change timestamp.
    """
    rows = sorted(source.exec(select(Change)).all(), key=sort_key)
    tag_id_by_name = {t.name: t.id for t in target.exec(select(Tag)).all()}
    for c in rows:
        if c.op is ChangeOp.tag_remove:
            name = c.new_value or c.field
            tid = tag_id_by_name.get(name or "")
            if tid is None:
                continue
            pt = target.exec(
                select(PhotoTag).where(PhotoTag.pixel_hash == c.pixel_hash, PhotoTag.tag_id == tid)
            ).first()
            if pt is not None:
                target.delete(pt)
        elif c.op in (ChangeOp.clip_approve, ChangeOp.clip_reject):
            state = (
                PredictionState.approved
                if c.op is ChangeOp.clip_approve
                else PredictionState.rejected
            )
            for pred in target.exec(
                select(Prediction).where(
                    Prediction.pixel_hash == c.pixel_hash,
                    Prediction.tag_name == (c.field or c.new_value or ""),
                )
            ).all():
                pred.state = state
    _replay_deletes(target, rows)


def _replay_deletes(target: Session, source_rows: list[Change]) -> None:
    """Settle explicit deletes (``unindex``) vs put-backs (``restore``) for every
    photo the source touched, last-writer-wins by :func:`sort_key` over the
    *unioned* log -- so an older ``unindex`` from a conflict copy can't re-bury a
    photo the base already put back, and vice versa.

    Latest is ``unindex``: record the tombstone so this machine purges its
    derived rows and lazily trashes its own local copy on its next scan (see
    :func:`sortandpost.db.deletion.apply_pending_tombstones`), and mark the photo
    missing in the meantime so it drops out of views. Latest is ``restore``:
    drop the tombstone so the next scan indexes the file again.
    """
    hashes = {c.pixel_hash for c in source_rows if c.op in (ChangeOp.unindex, ChangeOp.restore)}
    if not hashes:
        return
    target.flush()  # the unioned change rows must be visible to the query below
    for h in sorted(hashes):
        rows = target.exec(
            select(Change).where(
                Change.pixel_hash == h,
                col(Change.op).in_([ChangeOp.unindex, ChangeOp.restore]),
            )
        ).all()
        if not rows:
            continue
        last = max(rows, key=sort_key)
        tomb = target.get(Tombstone, h)
        if last.op is ChangeOp.unindex:
            if tomb is None:
                target.add(
                    Tombstone(
                        pixel_hash=h,
                        deleted_at=last.ts,
                        deleted_by_device=last.device_id,
                        applied=False,
                    )
                )
            photo = target.get(Photo, h)
            if photo is not None and photo.missing_since is None:
                photo.missing_since = last.ts
        elif tomb is not None:
            target.delete(tomb)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def merge_all(
    target: Session,
    sources: list[tuple[str, Session]],
    *,
    now: datetime,
    base_generation: int,
    source_generations: list[int],
) -> MergeSummary:
    total = MergeSummary(base_generation=base_generation)
    for label, sess in sources:
        total.sources.append(label)
        total.add(merge_session(target, sess, now=now))
    merged_gen = max([base_generation, *source_generations], default=base_generation) + 1
    set_generation(target, merged_gen)
    total.merged_generation = merged_gen
    return total


def current_generation(session: Session) -> int:
    return get_generation(session)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _clone(row: object, model: type) -> object:
    data = {k: getattr(row, k) for k in model.model_fields}  # type: ignore[attr-defined]
    return model(**data)


def _min_opt(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)
