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

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, col, select

from sortandpost.db.models import ChangeOp, PhotoPath, PhotoTag, Tag, TagKind, TagSource
from sortandpost.metadata import ExiftoolClient, MetadataError, can_embed_metadata, is_raw
from sortandpost.sync.changelog import record_change
from sortandpost.tag_names import (
    MAX_TAG_NAME_LEN,
    is_safe_tag_name,
    is_valid_new_tag_name,
    normalize_tag_name,
)

log = logging.getLogger(__name__)

# Tag names that are UI/workflow markers rather than descriptive keywords. They
# live in ``photo_tags`` (so they ride the ``changes`` log and conflict merge and
# filter for free) but are stripped before anything is written into a photo file
# -- see :mod:`sortandpost.db.flags`.
RESERVED_TAG_NAMES = frozenset({"to-edit"})


def embeddable_keywords(names: list[str]) -> list[str]:
    """Drop reserved workflow markers -- and any name that isn't safe to hand
    exiftool (a line break, e.g. from an older DB or a merged copy; see
    :mod:`sortandpost.tag_names`) -- from a keyword list bound for a file."""
    out = []
    for n in names:
        if not is_safe_tag_name(n):
            log.warning("not writing unsafe keyword %r into photo files", n)
        elif n.casefold() not in RESERVED_TAG_NAMES:
            out.append(n)
    return out


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
    name = normalize_tag_name(tag_name)
    if not name:
        raise ValueError("tag name must be non-empty")
    if not is_safe_tag_name(name):
        raise ValueError(f"tag name contains control characters: {name!r}")
    if not is_valid_new_tag_name(name):
        raise ValueError(f"tag name is longer than {MAX_TAG_NAME_LEN} characters")

    tag = get_or_create_tag(session, name)
    assert tag.id is not None
    existing = session.exec(
        select(PhotoTag).where(PhotoTag.pixel_hash == pixel_hash, PhotoTag.tag_id == tag.id)
    ).first()
    if existing is not None:
        return False

    # ON CONFLICT DO NOTHING: another session may have added the same tag to this
    # photo since the check above (Issues.md S9) -- then it's a no-op here.
    session.flush()
    inserted = session.execute(
        sqlite_insert(PhotoTag)
        .values(
            pixel_hash=pixel_hash,
            tag_id=tag.id,
            source=TagSource.manual,
            added_at=now,
            added_by_device=device_id,
            written_to_file=False,
        )
        .on_conflict_do_nothing(index_elements=["pixel_hash", "tag_id"])
    )
    if not inserted.rowcount:  # type: ignore[attr-defined]
        return False
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


def move_tags(
    session: Session,
    *,
    src: str,
    dst: str,
    device_id: str,
    now: datetime,
    keep_src: bool = False,
) -> list[str]:
    """Move *src*'s tags onto *dst*, keeping each one's source (manual / clip):
    in a stack, tags live on its top photo only, so making another member the
    top carries them over. DB only -- the caller writes *dst*'s file, and *src*'s
    file is never touched (writing it would flip the top back). Reserved
    workflow markers stay put (they fan out to every member already). Emits
    ``tag_add`` / ``clip_approve`` for *dst* and ``tag_remove`` for *src*
    (hard constraint #4). *keep_src* copies instead: *src* keeps its tags.
    Returns the names moved (or copied). The caller commits."""
    rows = session.exec(
        select(PhotoTag, Tag)
        .join(Tag, col(PhotoTag.tag_id) == col(Tag.id))
        .where(PhotoTag.pixel_hash == src)
    ).all()
    have = {
        pt.tag_id for pt in session.exec(select(PhotoTag).where(PhotoTag.pixel_hash == dst)).all()
    }
    moved: list[str] = []
    for row, tag in rows:
        if tag.name.casefold() in RESERVED_TAG_NAMES:
            continue
        if row.tag_id not in have:
            session.add(
                PhotoTag(
                    pixel_hash=dst,
                    tag_id=row.tag_id,
                    source=row.source,
                    score=row.score,
                    added_at=now,
                    added_by_device=device_id,
                    written_to_file=False,
                )
            )
            record_change(
                session,
                device_id=device_id,
                op=ChangeOp.clip_approve if row.source == TagSource.clip else ChangeOp.tag_add,
                pixel_hash=dst,
                ts=now,
                field=tag.name,
                new_value=tag.name,
            )
        moved.append(tag.name)
        if keep_src:
            continue
        session.delete(row)
        record_change(
            session,
            device_id=device_id,
            op=ChangeOp.tag_remove,
            pixel_hash=src,
            ts=now,
            field=tag.name,
            old_value=tag.name,
        )
    session.flush()
    return moved


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def get_or_create_tag(
    session: Session, name: str, *, kind: TagKind = TagKind.manual, facet: str | None = None
) -> Tag:
    """The ``tags`` row for *name*, created if it doesn't exist yet.

    Race-safe (Issues.md S9): two sessions -- two tabs, or the indexer importing
    file keywords while the user tags -- can both miss the row and both insert
    it. A plain select-then-insert then dies on ``UNIQUE(tags.name)`` and the
    user's action is lost, so the insert is ``ON CONFLICT DO NOTHING`` and the
    row is selected afterwards, whoever created it. *kind* / *facet* only apply
    when this call creates the row."""
    tag = session.exec(select(Tag).where(Tag.name == name)).first()
    if tag is not None:
        return tag
    session.execute(
        sqlite_insert(Tag)
        .values(name=name, kind=kind, facet=facet)
        .on_conflict_do_nothing(index_elements=["name"])
    )
    return session.exec(select(Tag).where(Tag.name == name)).one()


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
