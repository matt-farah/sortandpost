"""Rating writes -- the one place the UI sets a star rating.

Every call writes the ``ratings`` row *and* appends a ``changes`` row in the same
transaction (hard constraint #4) so a cross-machine merge can replay it. When an
:class:`ExiftoolClient` is supplied the rating is also embedded into every
present, embed-capable, non-RAW copy of the photo (Lightroom / Windows Explorer
portability); RAW copies keep the DB as their only home (hard constraint #2).

The exiftool write blocks, so callers on the web event loop must hop through
``run.io_bound`` (hard constraint #7).
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from sqlmodel import Session, col, select

from sortandpost.db.models import ChangeOp, PhotoPath, Rating, RatingSource
from sortandpost.metadata import ExiftoolClient, MetadataError, can_embed_metadata, is_raw
from sortandpost.sync.changelog import record_change

log = logging.getLogger(__name__)

MIN_STARS = 0
MAX_STARS = 5


def set_rating(
    session: Session,
    *,
    pixel_hash: str,
    stars: int,
    device_id: str,
    now: datetime,
    exiftool: ExiftoolClient | None = None,
) -> bool:
    """Set *pixel_hash*'s rating to *stars* (0 clears it).

    Returns ``True`` if the value changed; a no-op re-rate returns ``False`` and
    writes nothing (no ``changes`` row, no file touch). The caller commits.
    """
    if not MIN_STARS <= stars <= MAX_STARS:
        raise ValueError(f"stars must be {MIN_STARS}-{MAX_STARS}, got {stars}")

    row = session.get(Rating, pixel_hash)
    old = row.stars if row is not None else None
    if old == stars:
        return False

    if row is None:
        session.add(
            Rating(
                pixel_hash=pixel_hash,
                stars=stars,
                updated_at=now,
                updated_by_device=device_id,
                source=RatingSource.app,
            )
        )
    else:
        row.stars = stars
        row.updated_at = now
        row.updated_by_device = device_id
        row.source = RatingSource.app

    record_change(
        session,
        device_id=device_id,
        op=ChangeOp.rate,
        pixel_hash=pixel_hash,
        ts=now,
        field="stars",
        old_value=None if old is None else str(old),
        new_value=str(stars),
    )

    if exiftool is not None:
        _embed_rating(session, pixel_hash, stars, exiftool)
    return True


def _embed_rating(session: Session, pixel_hash: str, stars: int, exiftool: ExiftoolClient) -> None:
    """Mirror the rating into every writable copy. RAW / non-embed-capable /
    absent copies are skipped -- the DB stays the source of truth for those."""
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
            exiftool.write_rating(path, stars)
        except MetadataError as exc:
            log.warning("rating write failed for %s: %s", path, exc)
