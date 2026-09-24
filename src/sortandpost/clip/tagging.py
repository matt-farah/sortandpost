"""Zero-shot tagging: scoring photos against the vocabulary, and the ``/review``
approve / reject flow (build-order step 5b).

Scoring (called from the indexer's CLIP pass): cosine sim of a photo embedding
against every vocab term; terms ``>= score_threshold`` and within ``top_k`` are
written to ``predictions`` as ``state='pending'``. An existing ``approved`` /
``rejected`` row keeps its state -- a re-run refreshes only the score, so a
rejection is sticky.

Review (called from the UI, off the event loop): the user's decision in
``/review`` is user-authored state, so **every** approve / reject also appends a
``changes`` row (hard constraint #4) -- the conflict merge replays those.

* **approve** -> ``predictions.state='approved'`` + ``photo_tags(source='clip')``
  + embed the full keyword set into every present, embed-capable, non-RAW copy
  (hard constraints #2, #5) + ``ChangeOp.clip_approve``.
* **reject** -> ``predictions.state='rejected'`` (row created if missing, so it
  stays rejected even after the pending row is pruned) + ``ChangeOp.clip_reject``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
from sqlmodel import Session, col, func, select

from sortandpost.clip.vocab import Vocab
from sortandpost.db.models import (
    ChangeOp,
    PhotoPath,
    PhotoTag,
    Prediction,
    PredictionState,
    Tag,
    TagKind,
    TagSource,
)
from sortandpost.db.tags import embeddable_keywords
from sortandpost.metadata import ExiftoolClient, MetadataError, can_embed_metadata, is_raw
from sortandpost.sync.changelog import record_change

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Scoring (indexer CLIP pass)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ScoredTerm:
    name: str
    facet: str
    score: float


def pick_terms(
    scores: np.ndarray, vocab: Vocab, *, threshold: float, top_k: int
) -> list[ScoredTerm]:
    """The keep-list for one photo: terms ``>= threshold``, highest ``top_k`` first."""
    if scores.size == 0:
        return []
    idx = np.where(scores >= threshold)[0]
    idx = idx[np.argsort(-scores[idx])][: max(0, top_k)]
    return [ScoredTerm(vocab.terms[i].name, vocab.terms[i].facet, float(scores[i])) for i in idx]


def upsert_prediction(
    session: Session,
    *,
    pixel_hash: str,
    tag_name: str,
    model: str,
    score: float,
    now: datetime,
) -> bool:
    """Insert a pending prediction, or refresh an existing row's score.

    Returns ``True`` if a new row was inserted. An existing row's ``state`` is
    left untouched: ``pending`` stays pending, ``approved`` / ``rejected`` are
    sticky across re-runs.
    """
    row = session.exec(
        select(Prediction).where(
            Prediction.pixel_hash == pixel_hash,
            Prediction.tag_name == tag_name,
            Prediction.model == model,
        )
    ).first()
    if row is None:
        session.add(
            Prediction(
                pixel_hash=pixel_hash,
                tag_name=tag_name,
                score=score,
                model=model,
                run_at=now,
                state=PredictionState.pending,
            )
        )
        return True
    row.score = score
    row.run_at = now
    return False


# --------------------------------------------------------------------------- #
# Review queue (UI)
# --------------------------------------------------------------------------- #
@dataclass
class ReviewGroup:
    tag_name: str
    facet: str | None
    count: int


@dataclass
class FacetSection:
    facet: str | None  # ``None`` = tags with no facet in the current vocabulary
    groups: list[ReviewGroup]

    @property
    def count(self) -> int:
        return sum(g.count for g in self.groups)


def pending_groups(session: Session, *, model: str, vocab: Vocab) -> list[ReviewGroup]:
    """Pending predictions for *model*, grouped by tag, most-suggested first."""
    rows = session.exec(
        select(Prediction.tag_name, func.count())
        .where(Prediction.model == model, Prediction.state == PredictionState.pending)
        .group_by(Prediction.tag_name)
    ).all()
    groups = [ReviewGroup(tag, vocab.facet_of(tag), int(n)) for tag, n in rows]
    return sorted(groups, key=lambda g: (-g.count, g.tag_name))


def facet_sections(groups: list[ReviewGroup], vocab: Vocab) -> list[FacetSection]:
    """Bucket tag groups under their facet, in the vocabulary's facet order
    (``tags.yaml`` order); facet-less tags come last. Groups keep their order."""
    order: dict[str | None, int] = {}
    for term in vocab.terms:
        order.setdefault(term.facet, len(order))
    buckets: dict[str | None, list[ReviewGroup]] = {}
    for group in groups:
        buckets.setdefault(group.facet, []).append(group)
    ranked = sorted(buckets, key=lambda f: (f is None, order.get(f, len(order)), f or ""))
    return [FacetSection(facet, buckets[facet]) for facet in ranked]


def pending_for_tag(
    session: Session, *, model: str, tag_name: str, limit: int, offset: int = 0
) -> list[tuple[str, float]]:
    """One page of ``(pixel_hash, score)`` still pending for *tag_name*, best score first."""
    rows = session.exec(
        select(Prediction.pixel_hash, Prediction.score)
        .where(
            Prediction.model == model,
            Prediction.tag_name == tag_name,
            Prediction.state == PredictionState.pending,
        )
        .order_by(col(Prediction.score).desc(), col(Prediction.pixel_hash))
        .offset(offset)
        .limit(limit)
    ).all()
    return [(ph, float(score)) for ph, score in rows]


def is_pending(session: Session, *, model: str, pixel_hash: str, tag_name: str) -> bool:
    return (
        session.exec(
            select(Prediction.id).where(
                Prediction.pixel_hash == pixel_hash,
                Prediction.tag_name == tag_name,
                Prediction.model == model,
                Prediction.state == PredictionState.pending,
            )
        ).first()
        is not None
    )


def approve(
    session: Session,
    *,
    device_id: str,
    model: str,
    pixel_hash: str,
    tag_name: str,
    facet: str | None,
    now: datetime,
    exiftool: ExiftoolClient | None,
) -> None:
    pred = session.exec(
        select(Prediction).where(
            Prediction.pixel_hash == pixel_hash,
            Prediction.tag_name == tag_name,
            Prediction.model == model,
        )
    ).first()
    if pred is not None:
        pred.state = PredictionState.approved
    score = pred.score if pred is not None else None

    tag = _get_or_create_tag(session, tag_name, facet)
    assert tag.id is not None
    photo_tag = session.exec(
        select(PhotoTag).where(PhotoTag.pixel_hash == pixel_hash, PhotoTag.tag_id == tag.id)
    ).first()
    if photo_tag is None:
        photo_tag = PhotoTag(
            pixel_hash=pixel_hash,
            tag_id=tag.id,
            source=TagSource.clip,
            score=score,
            added_at=now,
            added_by_device=device_id,
            written_to_file=False,
        )
        session.add(photo_tag)
    session.flush()

    photo_tag.written_to_file = _embed_keywords(session, pixel_hash, exiftool)

    record_change(
        session,
        device_id=device_id,
        op=ChangeOp.clip_approve,
        pixel_hash=pixel_hash,
        ts=now,
        field=tag_name,
        new_value=tag_name,
    )


def reject(
    session: Session,
    *,
    device_id: str,
    model: str,
    pixel_hash: str,
    tag_name: str,
    now: datetime,
) -> None:
    pred = session.exec(
        select(Prediction).where(
            Prediction.pixel_hash == pixel_hash,
            Prediction.tag_name == tag_name,
            Prediction.model == model,
        )
    ).first()
    if pred is None:
        session.add(
            Prediction(
                pixel_hash=pixel_hash,
                tag_name=tag_name,
                score=0.0,
                model=model,
                run_at=now,
                state=PredictionState.rejected,
            )
        )
    else:
        pred.state = PredictionState.rejected

    record_change(
        session,
        device_id=device_id,
        op=ChangeOp.clip_reject,
        pixel_hash=pixel_hash,
        ts=now,
        field=tag_name,
        new_value=tag_name,
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _get_or_create_tag(session: Session, name: str, facet: str | None) -> Tag:
    tag = session.exec(select(Tag).where(Tag.name == name)).first()
    if tag is None:
        tag = Tag(name=name, facet=facet, kind=TagKind.clip_approved)
        session.add(tag)
        session.flush()
    elif facet and tag.facet is None:
        tag.facet = facet
    return tag


def _current_keywords(session: Session, pixel_hash: str) -> list[str]:
    stmt = (
        select(Tag.name)
        .join(PhotoTag, col(PhotoTag.tag_id) == col(Tag.id))
        .where(PhotoTag.pixel_hash == pixel_hash)
    )
    return sorted(session.exec(stmt).all())


def _embed_keywords(session: Session, pixel_hash: str, exiftool: ExiftoolClient | None) -> bool:
    """Write the photo's full keyword set into every writable copy. Returns
    ``True`` if at least one file was updated. RAW / non-embed-capable copies are
    skipped (DB stays the source of truth for those)."""
    if exiftool is None:
        return False
    keywords = embeddable_keywords(_current_keywords(session, pixel_hash))
    wrote = False
    paths = session.exec(
        select(PhotoPath).where(
            PhotoPath.pixel_hash == pixel_hash, col(PhotoPath.present).is_(True)
        )
    ).all()
    for photo_path in paths:
        path = Path(photo_path.path)
        if is_raw(path) or not can_embed_metadata(path) or not path.is_file():
            continue
        try:
            exiftool.write_keywords(path, keywords)
            wrote = True
        except MetadataError as exc:
            log.warning("keyword write failed for %s: %s", path, exc)
    return wrote
