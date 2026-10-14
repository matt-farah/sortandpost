"""Structured photo filters shared by ``/search`` and filtered browsing (step 7).

A :class:`PhotoFilter` compiles to a set of allowed ``pixel_hash`` values;
``semantic_search`` intersects its ranked hits with that set, and
``browse_photos`` lists photos by ``taken_at`` when there is no text query.
Keeping it a plain predicate over ``pixel_hash`` means an ANN index can still
slot in behind ``semantic_search`` without touching filtering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import ColumnElement, func
from sqlmodel import Session, col, select

from sortandpost import missing
from sortandpost.db.models import (
    Photo,
    PhotoPath,
    PhotoTag,
    Prediction,
    PredictionState,
    Rating,
    Tag,
)
from sortandpost.dedup import NEAR_DUPLICATE_DISTANCE, find_duplicate_groups
from sortandpost.grouping import camera_label


@dataclass
class PhotoFilter:
    min_stars: int | None = None
    # exact star values to keep; 0 means *unrated* (no ratings row, or 0 stars)
    stars_in: frozenset[int] = frozenset()
    has_gps: bool | None = None
    camera: str | None = None  # case-insensitive substring of "make model"
    # exact camera labels (``grouping.camera_label``), OR'd; "" = unknown camera
    cameras: list[str] = field(default_factory=list)
    # a folder (container path): photos with a present file in it or below it
    folder: str | None = None
    taken_from: datetime | None = None
    taken_to: datetime | None = None
    tags_all: list[str] = field(default_factory=list)
    tags_any: list[str] = field(default_factory=list)
    # tag clauses also match un-rejected CLIP suggestions (``predictions``), not
    # only applied ``photo_tags`` -- so "subject" filtering works before review
    tag_suggestions: bool = False
    include_missing: bool = False
    # only photos missing on this computer (``sortandpost.missing``); wins over
    # ``include_missing``
    only_missing: bool = False
    only_duplicates: bool = False
    duplicate_distance: int = NEAR_DUPLICATE_DISTANCE

    def is_active(self) -> bool:
        """Whether any *constraining* clause is set. ``include_missing`` loosens
        rather than constrains, so it does not count here."""
        return any(
            (
                self.min_stars,
                self.stars_in,
                self.has_gps is not None,
                self.camera,
                self.cameras,
                self.folder,
                self.taken_from is not None,
                self.taken_to is not None,
                self.tags_all,
                self.tags_any,
                self.only_duplicates,
                self.only_missing,
            )
        )


def _visibility(flt: PhotoFilter) -> ColumnElement[bool] | None:
    return missing.visibility_clause(
        include_missing=flt.include_missing, only_missing=flt.only_missing
    )


def _lower_coalesce(column: object) -> ColumnElement[str]:
    return func.lower(func.coalesce(column, ""))


def allowed_hashes(session: Session, flt: PhotoFilter) -> set[str] | None:
    """The ``pixel_hash`` set matching *flt*, or ``None`` when *flt* imposes no
    membership constraint at all (``None`` = "don't filter", distinct from an
    empty set = "nothing matches"). Note the default (drop photos missing on
    this computer) still yields a concrete set."""
    if not flt.is_active() and flt.include_missing:
        return None

    stmt = select(Photo.pixel_hash)
    visible = _visibility(flt)
    if visible is not None:
        stmt = stmt.where(visible)
    if flt.has_gps is True:
        stmt = stmt.where(col(Photo.gps_lat).is_not(None), col(Photo.gps_lon).is_not(None))
    elif flt.has_gps is False:
        stmt = stmt.where(col(Photo.gps_lat).is_(None))
    if flt.camera:
        make_model = _lower_coalesce(Photo.camera_make) + " " + _lower_coalesce(Photo.camera_model)
        stmt = stmt.where(make_model.like(f"%{flt.camera.lower()}%"))
    if flt.taken_from is not None:
        stmt = stmt.where(col(Photo.taken_at) >= flt.taken_from)
    if flt.taken_to is not None:
        stmt = stmt.where(col(Photo.taken_at) <= flt.taken_to)

    hashes = set(session.exec(stmt).all())

    if flt.cameras:
        wanted = set(flt.cameras)
        rows = session.exec(select(Photo.pixel_hash, Photo.camera_make, Photo.camera_model)).all()
        hashes &= {h for h, make, model in rows if camera_label(make, model) in wanted}

    if flt.folder:
        hashes &= _in_folder(session, flt.folder)

    if flt.stars_in:
        by_stars = {h: n for h, n in session.exec(select(Rating.pixel_hash, Rating.stars)).all()}
        hashes = {h for h in hashes if by_stars.get(h, 0) in flt.stars_in}

    if flt.min_stars:
        rated = set(
            session.exec(select(Rating.pixel_hash).where(col(Rating.stars) >= flt.min_stars)).all()
        )
        hashes &= rated

    for name in flt.tags_all:
        hashes &= _tagged_with(session, name, suggestions=flt.tag_suggestions)
    if flt.tags_any:
        any_hashes: set[str] = set()
        for name in flt.tags_any:
            any_hashes |= _tagged_with(session, name, suggestions=flt.tag_suggestions)
        hashes &= any_hashes

    if flt.only_duplicates:
        dup = {
            h
            for group in find_duplicate_groups(
                session,
                max_distance=flt.duplicate_distance,
                include_missing=flt.include_missing or flt.only_missing,
            )
            for h in group
        }
        hashes &= dup

    return hashes


def browse_photos(
    session: Session,
    flt: PhotoFilter,
    *,
    limit: int = 120,
    offset: int = 0,
) -> list[Photo]:
    """Filtered photo list ordered newest ``taken_at`` first (undated last), for
    the query-less ``/search`` path."""
    allowed = allowed_hashes(session, flt)
    stmt = select(Photo)
    if allowed is not None:
        if not allowed:
            return []
        stmt = stmt.where(col(Photo.pixel_hash).in_(allowed))
    elif (visible := _visibility(flt)) is not None:
        stmt = stmt.where(visible)
    stmt = (
        stmt.order_by(
            col(Photo.taken_at).is_(None), col(Photo.taken_at).desc(), col(Photo.pixel_hash)
        )
        .offset(offset)
        .limit(limit)
    )
    return list(session.exec(stmt).all())


def text_match(session: Session, query: str) -> set[str]:
    """Plain (non-CLIP) text search, used when ``clip.enabled`` is off: every
    whitespace-separated term must appear (case-insensitive) in the photo's file
    path, an applied tag name, or its camera make / model."""
    terms = [t.lower() for t in query.split() if t]
    if not terms:
        return set()
    out: set[str] | None = None
    for term in terms:
        like = f"%{term.replace('%', '').replace('_', '')}%"
        hit = set(
            session.exec(
                select(PhotoPath.pixel_hash).where(func.lower(PhotoPath.path).like(like))
            ).all()
        )
        hit |= set(
            session.exec(
                select(PhotoTag.pixel_hash)
                .join(Tag, col(PhotoTag.tag_id) == col(Tag.id))
                .where(func.lower(Tag.name).like(like))
            ).all()
        )
        make_model = _lower_coalesce(Photo.camera_make) + " " + _lower_coalesce(Photo.camera_model)
        hit |= set(session.exec(select(Photo.pixel_hash).where(make_model.like(like))).all())
        out = hit if out is None else out & hit
    return out or set()


def _tagged_with(session: Session, name: str, *, suggestions: bool = False) -> set[str]:
    out = set(
        session.exec(
            select(PhotoTag.pixel_hash)
            .join(Tag, col(PhotoTag.tag_id) == col(Tag.id))
            .where(func.lower(Tag.name) == name.lower())
        ).all()
    )
    if suggestions:
        out |= set(
            session.exec(
                select(Prediction.pixel_hash).where(
                    func.lower(Prediction.tag_name) == name.lower(),
                    col(Prediction.state) != PredictionState.rejected,
                )
            ).all()
        )
    return out


def _in_folder(session: Session, folder: str) -> set[str]:
    """Photos with a *present* file at or below *folder*. A prefix compare
    (not ``LIKE``, whose ``%`` / ``_`` would need escaping in real folder names),
    anchored at a separator so ``/a/b`` doesn't also match ``/a/bc``."""
    base = folder.rstrip("/\\")
    if not base:
        return set()
    sep = "\\" if "\\" in base and "/" not in base else "/"
    prefix = base + sep
    rows = session.exec(
        select(PhotoPath.pixel_hash).where(
            col(PhotoPath.present).is_(True),
            func.substr(PhotoPath.path, 1, len(prefix)) == prefix,
        )
    ).all()
    return set(rows)
