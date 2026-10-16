"""EXIF reconciliation on re-index (pipeline stage 6, build-order step 6).

When a re-index reads a file whose embedded metadata disagrees with the DB:

* **Rating** -- if the file carries an ``XMP:Rating`` different from the DB value,
  the newer side wins: file ``mtime`` newer than the rating row's ``updated_at``
  => the file wins (import it, emit a ``rate`` change row); otherwise the DB wins
  (rewrite the file). A DB rating with no counterpart in the file is written out
  so Lightroom / Explorer see it.
* **A file's first index** (a path the app hasn't indexed with these pixels
  before) only reads: it never writes the DB rating into the file and never
  clears a DB rating with the file's ``0``. Duplicate copies of one photo can
  carry different ratings, and on a new library the DB row is always newer
  than every file, so "DB wins" used to stamp the first-scanned copy's rating
  onto every other copy (Issues.md S6). Between copies the newest file wins:
  a file-sourced DB rating is beaten only by a copy newer than every other
  present copy, whatever order the scan met them in.
* **A rating outside 0-5** in the file (a Lightroom reject ``-1``, a ``99``) is
  never overwritten by reconcile; it reads as unrated / 5.
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
    PhotoPath,
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
    write_back: bool = True,
    first_index: bool = False,
) -> ReconcileOutcome:
    """*write_back* ``False`` = never write the DB rating into this file (a
    stacked photo that isn't its stack's embed target); file -> DB import still
    runs. *first_index* = this path is new to the app (see the module doc)."""
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
        embed_capable=(
            write_back
            and not first_index
            and not md.rating_unsupported
            and can_embed_metadata(path)
        ),
        first_index=first_index,
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
    first_index: bool,
) -> None:
    file_stars = md.rating  # 0-5, or None when the file carries no rating tag
    row = session.get(Rating, pixel_hash)
    if row is not None and first_index and row.source is RatingSource.file:
        # Another copy's rating: only a copy newer than all the others beats it.
        db_ns = _newest_other_copy_ns(session, pixel_hash, path)
    elif row is not None:
        db_ns = _dt_to_ns(row.updated_at)

    # A file with no rating tag *and* one that embeds ``Rating=0`` (many cameras
    # write a literal 0 on every frame) both mean "unrated". Only a real DB
    # rating is worth pushing back out.
    if not file_stars:  # None or 0
        if row is not None and row.stars > 0:
            if file_stars == 0 and not first_index and file_mtime_ns > db_ns:
                # The file explicitly cleared a rating we still hold -- honour it.
                _import_rating(
                    session, pixel_hash, 0, device_id=device_id, now=now, old=row.stars, row=row
                )
                out.rating_imported = True
            elif embed_capable:
                _write_rating(exiftool, path, row.stars, out)
        return

    if row is None:
        _import_rating(session, pixel_hash, file_stars, device_id=device_id, now=now, old=None)
        out.rating_imported = True
        return

    if file_stars == row.stars:
        return

    if file_mtime_ns > db_ns:
        _import_rating(
            session, pixel_hash, file_stars, device_id=device_id, now=now, old=row.stars, row=row
        )
        out.rating_imported = True
    elif embed_capable:
        _write_rating(exiftool, path, row.stars, out)


def _newest_other_copy_ns(session: Session, pixel_hash: str, path: Path) -> int:
    """Newest ``mtime_ns`` among *pixel_hash*'s other present copies."""
    mtimes = session.exec(
        select(PhotoPath.mtime_ns).where(
            PhotoPath.pixel_hash == pixel_hash,
            col(PhotoPath.present).is_(True),
            PhotoPath.path != str(path),
        )
    ).all()
    return max(mtimes, default=0)


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
