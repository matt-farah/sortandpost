"""The "marked for editing" queue.

A lightweight workflow flag recording which photos the user wants to come back
and edit. It is stored as a **reserved manual tag** (:data:`EDIT_FLAG`) so it
rides the same ``changes`` log and conflict-merge path as every other user
decision (hard constraint #4) and filters for free through
``PhotoFilter.tags_all``. Unlike a real keyword it is **never** written into a
photo file -- ``sortandpost.db.tags.embeddable_keywords`` strips it before any
exiftool write.
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, col, func, select

from sortandpost.db.models import PhotoTag, Tag
from sortandpost.db.tags import add_manual_tag, remove_manual_tag

EDIT_FLAG = "to-edit"


def set_edit_flag(
    session: Session, *, pixel_hash: str, marked: bool, device_id: str, now: datetime
) -> bool:
    """Add or remove the edit flag on *pixel_hash*. Returns ``True`` if it
    changed (a no-op returns ``False``). Never embeds into the file. The caller
    commits."""
    if marked:
        return add_manual_tag(
            session, pixel_hash=pixel_hash, tag_name=EDIT_FLAG, device_id=device_id, now=now
        )
    return remove_manual_tag(
        session, pixel_hash=pixel_hash, tag_name=EDIT_FLAG, device_id=device_id, now=now
    )


def edit_flagged_hashes(session: Session, candidates: list[str] | None = None) -> set[str]:
    """The ``pixel_hash`` set currently flagged for editing, optionally limited to
    *candidates* (useful when painting one grid batch)."""
    stmt = (
        select(PhotoTag.pixel_hash)
        .join(Tag, col(PhotoTag.tag_id) == col(Tag.id))
        .where(func.lower(Tag.name) == EDIT_FLAG)
    )
    if candidates is not None:
        if not candidates:
            return set()
        stmt = stmt.where(col(PhotoTag.pixel_hash).in_(candidates))
    return set(session.exec(stmt).all())


def edit_flag_count(session: Session) -> int:
    """How many photos are currently in the edit queue."""
    return int(
        session.exec(
            select(func.count(func.distinct(PhotoTag.pixel_hash)))
            .select_from(PhotoTag)
            .join(Tag, col(PhotoTag.tag_id) == col(Tag.id))
            .where(func.lower(Tag.name) == EDIT_FLAG)
        ).one()
    )


def clear_all_edit_flags(session: Session, *, device_id: str, now: datetime) -> int:
    """Remove the edit flag from every photo. Returns how many were cleared; each
    emits a ``changes`` row (hard constraint #4). Never touches photo files. The
    caller commits."""
    hashes = edit_flagged_hashes(session)
    for pixel_hash in hashes:
        remove_manual_tag(
            session, pixel_hash=pixel_hash, tag_name=EDIT_FLAG, device_id=device_id, now=now
        )
    return len(hashes)
