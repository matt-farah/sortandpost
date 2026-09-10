"""Manual tag writes -- the UI's path for user-authored keywords.

Mirrors :mod:`sortandpost.db.ratings`: every mutation writes ``photo_tags`` **and**
a ``changes`` row in the same transaction (hard constraint #4). When an
:class:`ExiftoolClient` is supplied the photo's full keyword set (manual +
CLIP-approved) is re-embedded into every present, embed-capable, non-RAW copy
(hard constraints #2, #5). The exiftool write blocks, so callers on the web
event loop hop through ``run.io_bound`` (hard constraint #7).

CLIP's ``/review`` approve/reject has its own near-identical helpers in
:mod:`sortandpost.clip.tagging`; the keyword-embedding rule is intentionally the
same on both paths.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from sqlmodel import Session, col, select

from sortandpost.db.models import ChangeOp, PhotoPath, PhotoTag, Tag, TagKind, TagSource
from sortandpost.metadata import ExiftoolClient, MetadataError, can_embed_metadata, is_raw
from sortandpost.sync.changelog import record_change

log = logging.getLogger(__name__)

# Tag names that are UI/workflow markers rather than descriptive keywords. They
# live in ``photo_tags`` (so they ride the ``changes`` log and conflict merge and
# filter for free) but are stripped before anything is written into a photo file
# -- see :mod:`sortandpost.db.flags`.
RESERVED_TAG_NAMES = frozenset({"to-edit"})


def embeddable_keywords(names: list[str]) -> list[str]:
    """Drop reserved workflow markers from a keyword list bound for a file."""
    return [n for n in names if n.casefold() not in RESERVED_TAG_NAMES]


def add_manual_tag(
    session: Session,
    *,
    pixel_hash: str,
    tag_name: str,
    device_id: str,
    now: datetime,
    exiftool: ExiftoolClient | None = None,
) -> bool:
    """Add *tag_name* to *pixel_hash* as a manual tag.

    Returns ``True`` if it was newly added; a duplicate is a no-op that writes
    nothing. The caller commits.
    """
    name = tag_name.strip()
    if not name:
        raise ValueError("tag name must be non-empty")

    tag = _get_or_create_tag(session, name)
    assert tag.id is not None
    existing = session.exec(
        select(PhotoTag).where(PhotoTag.pixel_hash == pixel_hash, PhotoTag.tag_id == tag.id)
    ).first()
    if existing is not None:
        return False

    session.add(
        PhotoTag(
            pixel_hash=pixel_hash,
            tag_id=tag.id,
            source=TagSource.manual,
            added_at=now,
            added_by_device=device_id,
            written_to_file=False,
        )
    )
    session.flush()
    record_change(
        session,
        device_id=device_id,
        op=ChangeOp.tag_add,
        pixel_hash=pixel_hash,
        ts=now,
        field=name,
        new_value=name,
    )
    if exiftool is not None and embed_keywords(session, pixel_hash, exiftool):
        _mark_written(session, pixel_hash)
    return True


def remove_manual_tag(
    session: Session,
    *,
    pixel_hash: str,
    tag_name: str,
    device_id: str,
    now: datetime,
    exiftool: ExiftoolClient | None = None,
) -> bool:
    """Remove a *manual* tag from *pixel_hash*. CLIP-sourced ``photo_tags`` are
    left alone (reject them in ``/review``). Returns ``True`` if a row was
    removed. The caller commits."""
    name = tag_name.strip()
    tag = session.exec(select(Tag).where(Tag.name == name)).first()
    if tag is None or tag.id is None:
        return False
    row = session.exec(
        select(PhotoTag).where(
            PhotoTag.pixel_hash == pixel_hash,
            PhotoTag.tag_id == tag.id,
            PhotoTag.source == TagSource.manual,
        )
    ).first()
    if row is None:
        return False

    session.delete(row)
    session.flush()
    record_change(
        session,
        device_id=device_id,
        op=ChangeOp.tag_remove,
        pixel_hash=pixel_hash,
        ts=now,
        field=name,
        old_value=name,
    )
    if exiftool is not None:
        embed_keywords(session, pixel_hash, exiftool)  # rewrites the reduced set
    return True


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _get_or_create_tag(session: Session, name: str) -> Tag:
    tag = session.exec(select(Tag).where(Tag.name == name)).first()
    if tag is None:
        tag = Tag(name=name, kind=TagKind.manual)
        session.add(tag)
        session.flush()
    return tag


def photo_keywords(session: Session, pixel_hash: str) -> list[str]:
    """Every tag name on *pixel_hash* (manual + CLIP-approved), sorted."""
    stmt = (
        select(Tag.name)
        .join(PhotoTag, col(PhotoTag.tag_id) == col(Tag.id))
        .where(PhotoTag.pixel_hash == pixel_hash)
    )
    return sorted(session.exec(stmt).all())


def embed_keywords(session: Session, pixel_hash: str, exiftool: ExiftoolClient) -> bool:
    """Write the photo's full keyword set into every writable copy. RAW /
    non-embed-capable / absent copies are skipped. Returns ``True`` if at least
    one file was updated."""
    keywords = embeddable_keywords(photo_keywords(session, pixel_hash))
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


def _mark_written(session: Session, pixel_hash: str) -> None:
    for row in session.exec(select(PhotoTag).where(PhotoTag.pixel_hash == pixel_hash)).all():
        row.written_to_file = True
