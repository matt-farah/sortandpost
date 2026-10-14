"""Read-only queries behind the thumbnail grid and photo-detail pages.

Kept separate from the NiceGUI layer so the ordering/pagination rules are unit
testable without a browser. Ordering: newest ``taken_at`` first, photos with no
date last, ``pixel_hash`` as the stable tie-break.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from sqlalchemy import Select, or_
from sqlmodel import Session, col, func, select

from sortandpost import missing
from sortandpost.db.models import Photo, PhotoPath, Rating, ScanState
from sortandpost.grouping import GroupRow

_ALL_ROWS = 1 << 62  # "no limit" for SQLite, without widening list_photos' signature


def _visible[S: Select[Any]](stmt: S, include_missing: bool, only_missing: bool) -> S:
    """*stmt* limited by the "missing on this computer" rule (``missing``)."""
    clause = missing.visibility_clause(include_missing=include_missing, only_missing=only_missing)
    return stmt if clause is None else stmt.where(clause)


def photo_count(
    session: Session, *, include_missing: bool = False, only_missing: bool = False
) -> int:
    stmt = _visible(select(func.count()).select_from(Photo), include_missing, only_missing)
    return int(session.exec(stmt).one())


def list_photos(
    session: Session,
    *,
    limit: int = 120,
    offset: int = 0,
    include_missing: bool = False,
    only_missing: bool = False,
) -> list[Photo]:
    stmt = _visible(select(Photo), include_missing, only_missing)
    stmt = (
        stmt.order_by(
            col(Photo.taken_at).is_(None),
            col(Photo.taken_at).desc(),
            col(Photo.pixel_hash),
        )
        .offset(offset)
        .limit(limit)
    )
    return list(session.exec(stmt).all())


def list_photo_ids(
    session: Session, *, include_missing: bool = False, only_missing: bool = False
) -> list[str]:
    """Every photo's ``pixel_hash`` in the same order as :func:`list_photos`.
    Used by the grid when stacking is on -- it pages over this in memory after
    dropping hidden stack members."""
    stmt = _visible(select(Photo.pixel_hash), include_missing, only_missing)
    stmt = stmt.order_by(
        col(Photo.taken_at).is_(None),
        col(Photo.taken_at).desc(),
        col(Photo.pixel_hash),
    )
    return list(session.exec(stmt).all())


def list_group_rows(
    session: Session, *, include_missing: bool = False, only_missing: bool = False
) -> list[GroupRow]:
    """The fields :mod:`sortandpost.grouping` needs, in the same order as
    :func:`list_photo_ids` (newest ``taken_at`` first). The grid re-filters this
    to its representative order before grouping."""
    return [
        GroupRow(
            pixel_hash=p.pixel_hash,
            taken_at=p.taken_at,
            primary_path=p.primary_path,
            camera_make=p.camera_make,
            camera_model=p.camera_model,
            file_mtime_ns=p.file_mtime_ns,
        )
        for p in list_photos(
            session, limit=_ALL_ROWS, include_missing=include_missing, only_missing=only_missing
        )
    ]


# A photo is *unrated* when it has no ``ratings`` row at all, or one with
# ``stars == 0`` (photos not missing on this computer only).
_UNRATED = or_(col(Rating.pixel_hash).is_(None), col(Rating.stars) == 0)


def unrated_count(session: Session) -> int:
    """How many present photos still have no star rating."""
    stmt = (
        select(func.count())
        .select_from(Photo)
        .outerjoin(Rating, col(Rating.pixel_hash) == col(Photo.pixel_hash))
        .where(missing.shown_clause(), _UNRATED)
    )
    return int(session.exec(stmt).one())


def unrated_photo_ids(
    session: Session,
    *,
    limit: int = 60,
    exclude: Collection[str] = (),
) -> list[str]:
    """A random-ordered batch of unrated photos' ``pixel_hash`` values, for the
    ``/rate`` triage page. *exclude* drops hashes already pulled this session so
    repeat visits within one page load keep advancing."""
    stmt = (
        select(Photo.pixel_hash)
        .outerjoin(Rating, col(Rating.pixel_hash) == col(Photo.pixel_hash))
        .where(missing.shown_clause(), _UNRATED)
    )
    if exclude:
        stmt = stmt.where(col(Photo.pixel_hash).not_in(list(exclude)))
    stmt = stmt.order_by(func.random()).limit(limit)
    return list(session.exec(stmt).all())


def online_only_count(session: Session) -> int:
    """How many library files are cloud placeholders the indexer skipped
    (recorded but not downloaded -- see :mod:`sortandpost.indexer.placeholder`)."""
    stmt = select(func.count()).select_from(ScanState).where(col(ScanState.online_only).is_(True))
    return int(session.exec(stmt).one())


def get_photo(session: Session, pixel_hash: str) -> Photo | None:
    return session.get(Photo, pixel_hash)


def get_paths(session: Session, pixel_hash: str) -> list[PhotoPath]:
    stmt = select(PhotoPath).where(PhotoPath.pixel_hash == pixel_hash).order_by(col(PhotoPath.path))
    return list(session.exec(stmt).all())


def get_rating(session: Session, pixel_hash: str) -> int | None:
    row = session.get(Rating, pixel_hash)
    return row.stars if row is not None else None
