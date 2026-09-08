"""EXIF reconciliation on re-index (pipeline stage 6, build-order step 6).

When a re-index reads a file whose embedded metadata disagrees with the DB:

* **Rating** -- if the file carries an ``XMP:Rating`` different from the DB value,
  the newer side wins: file ``mtime`` newer than the rating row's ``updated_at``
  => the file wins (import it, emit a ``rate`` change row); otherwise the DB wins
  (rewrite the file). A DB rating with no counterpart in the file is written out
  so Lightroom / Explorer see it.
* **Keywords** -- any ``XMP:Subject`` / ``IPTC:Keywords`` value not already a tag
  on the photo is imported as a ``manual`` tag (+ a ``tag_add`` change row).
  Import is additive only; we never delete a tag because a file lacks it.

RAW files are never touched or read back here (hard constraint #2) -- their
ratings and tags live only in the DB. CLIP predictions are never written to files
(hard constraint #5); only the reconciliation above and the user's explicit
approvals in ``/review`` put anything into a photo file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, col, select

from sortandpost.db.models import (
    ChangeOp,
    PhotoTag,
    Rating,
    RatingSource,
    Tag,
    TagKind,
    TagSource,
)
from sortandpost.metadata import (
    EmbeddedMetadata,
    ExiftoolClient,
    MetadataError,
    can_embed_metadata,
    is_raw,
)
from sortandpost.sync.changelog import record_change

log = logging.getLogger(__name__)


@dataclass
class ReconcileOutcome:
    rating_imported: bool = False  # file -> DB (+ change row)
    rating_written: bool = False  # DB -> file
    keywords_imported: int = 0  # file -> DB manual tags (+ change rows)

    @property
    def touched(self) -> bool:
        return self.rating_imported or self.rating_written or bool(self.keywords_imported)


def reconcile_metadata(
    session: Session,
    *,
    pixel_hash: str,
    path: Path,
    md: EmbeddedMetadata,
    exiftool: ExiftoolClient | None,
    device_id: str,
    file_mtime_ns: int,
    now: datetime,
) -> ReconcileOutcome:
    out = ReconcileOutcome()
    if is_raw(path):
        return out  # RAW: the DB is the only home for its rating/tags

    _reconcile_rating(
        session,
        out,
        pixel_hash=pixel_hash,
        path=path,
        md=md,
        exiftool=exiftool,
        device_id=device_id,
        file_mtime_ns=file_mtime_ns,
        now=now,
        embed_capable=can_embed_metadata(path),
    )
    _reconcile_keywords(session, out, pixel_hash=pixel_hash, md=md, device_id=device_id, now=now)
    return out


# --------------------------------------------------------------------------- #
# rating
# --------------------------------------------------------------------------- #
def _reconcile_rating(
    session: Session,
    out: ReconcileOutcome,
    *,
    pixel_hash: str,
    path: Path,
    md: EmbeddedMetadata,
    exiftool: ExiftoolClient | None,
    device_id: str,
    file_mtime_ns: int,
    now: datetime,
    embed_capable: bool,
) -> None:
    file_stars = md.rating  # 0-5, or None when the file carries no rating tag
    row = session.get(Rating, pixel_hash)

    if file_stars is None:
        # Nothing embedded. Push a real DB rating out so other tools see it.
        if row is not None and row.stars > 0 and embed_capable:
            _write_rating(exiftool, path, row.stars, out)
        return

    if row is None:
        _import_rating(session, pixel_hash, file_stars, device_id=device_id, now=now, old=None)
        out.rating_imported = True
        return

    if file_stars == row.stars:
        return

    if file_mtime_ns > _dt_to_ns(row.updated_at):
        _import_rating(
            session, pixel_hash, file_stars, device_id=device_id, now=now, old=row.stars, row=row
        )
        out.rating_imported = True
    elif embed_capable:
        _write_rating(exiftool, path, row.stars, out)


def _import_rating(
    session: Session,
    pixel_hash: str,
    stars: int,
    *,
    device_id: str,
    now: datetime,
    old: int | None,
    row: Rating | None = None,
) -> None:
    if row is None:
        session.add(
            Rating(
                pixel_hash=pixel_hash,
                stars=stars,
                updated_at=now,
                updated_by_device=device_id,
                source=RatingSource.file,
            )
        )
    else:
        row.stars = stars
        row.updated_at = now
        row.updated_by_device = device_id
        row.source = RatingSource.file
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


def _write_rating(
    exiftool: ExiftoolClient | None, path: Path, stars: int, out: ReconcileOutcome
) -> None:
    if exiftool is None:
        return
    try:
        exiftool.write_rating(path, stars)
        out.rating_written = True
    except MetadataError as exc:
        log.warning("rating write-back failed for %s: %s", path, exc)


# --------------------------------------------------------------------------- #
# keywords (additive import only)
# --------------------------------------------------------------------------- #
def _reconcile_keywords(
    session: Session,
    out: ReconcileOutcome,
    *,
    pixel_hash: str,
    md: EmbeddedMetadata,
    device_id: str,
    now: datetime,
) -> None:
    file_keywords = [k.strip() for k in md.keywords if k.strip()]
    if not file_keywords:
        return

    have = set(
        session.exec(
            select(Tag.name)
            .join(PhotoTag, col(PhotoTag.tag_id) == col(Tag.id))
            .where(PhotoTag.pixel_hash == pixel_hash)
        ).all()
    )
    for keyword in file_keywords:
        if keyword in have:
            continue
        tag = session.exec(select(Tag).where(Tag.name == keyword)).first()
        if tag is None:
            tag = Tag(name=keyword, kind=TagKind.manual)
            session.add(tag)
            session.flush()
        assert tag.id is not None
        session.add(
            PhotoTag(
                pixel_hash=pixel_hash,
                tag_id=tag.id,
                source=TagSource.manual,
                added_at=now,
                added_by_device=device_id,
                written_to_file=True,
            )
        )
        record_change(
            session,
            device_id=device_id,
            op=ChangeOp.tag_add,
            pixel_hash=pixel_hash,
            ts=now,
            field=keyword,
            new_value=keyword,
        )
        have.add(keyword)
        out.keywords_imported += 1


def _dt_to_ns(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1_000_000_000)
