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

from sortandpost.db.models import Photo, PhotoTag, Rating, Tag
from sortandpost.dedup import NEAR_DUPLICATE_DISTANCE, find_duplicate_groups


@dataclass
class PhotoFilter:
    min_stars: int | None = None
    has_gps: bool | None = None
    camera: str | None = None  # case-insensitive substring of "make model"
    taken_from: datetime | None = None
    taken_to: datetime | None = None
    tags_all: list[str] = field(default_factory=list)
    tags_any: list[str] = field(default_factory=list)
    include_missing: bool = False
    only_duplicates: bool = False
    duplicate_distance: int = NEAR_DUPLICATE_DISTANCE

    def is_active(self) -> bool:
        """Whether any *constraining* clause is set. ``include_missing`` loosens
        rather than constrains, so it does not count here."""
        return any(
            (
                self.min_stars,
                self.has_gps is not None,
                self.camera,
                self.taken_from is not None,
                self.taken_to is not None,
                self.tags_all,
                self.tags_any,
                self.only_duplicates,
            )
        )


def _lower_coalesce(column: object) -> ColumnElement[str]:
    return func.lower(func.coalesce(column, ""))


def allowed_hashes(session: Session, flt: PhotoFilter) -> set[str] | None:
    """The ``pixel_hash`` set matching *flt*, or ``None`` when *flt* imposes no
    membership constraint at all (``None`` = "don't filter", distinct from an
    empty set = "nothing matches"). Note the default (drop ``missing_since``
    photos) still yields a concrete set."""
    if not flt.is_active() and flt.include_missing:
        return None

    stmt = select(Photo.pixel_hash)
    if not flt.include_missing:
        stmt = stmt.where(col(Photo.missing_since).is_(None))
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

    if flt.min_stars:
        rated = set(
            session.exec(select(Rating.pixel_hash).where(col(Rating.stars) >= flt.min_stars)).all()
        )
        hashes &= rated

    for name in flt.tags_all:
        hashes &= _tagged_with(session, name)
    if flt.tags_any:
        any_hashes: set[str] = set()
        for name in flt.tags_any:
            any_hashes |= _tagged_with(session, name)
        hashes &= any_hashes

    if flt.only_duplicates:
        dup = {
            h
            for group in find_duplicate_groups(
                session,
                max_distance=flt.duplicate_distance,
                include_missing=flt.include_missing,
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
    elif not flt.include_missing:
        stmt = stmt.where(col(Photo.missing_since).is_(None))
    stmt = (
        stmt.order_by(
            col(Photo.taken_at).is_(None), col(Photo.taken_at).desc(), col(Photo.pixel_hash)
        )
        .offset(offset)
        .limit(limit)
    )
    return list(session.exec(stmt).all())


def _tagged_with(session: Session, name: str) -> set[str]:
    return set(
        session.exec(
            select(PhotoTag.pixel_hash)
            .join(Tag, col(PhotoTag.tag_id) == col(Tag.id))
            .where(func.lower(Tag.name) == name.lower())
        ).all()
    )
