"""A file saved over in place keeps the user's work.

Identity is the pixel hash (hard constraint #3), so an edit or re-export written
over the same file is a *new* photo: the scan repoints the path's
``photo_paths`` row to it, and the old photo is left with no path (missing,
reason "file replaced" -- never purged, constraint #8). Without help the new
version would start unrated, untagged and in no collection, and an editor that
drops the embedded XMP loses the rating outright.

So when a never-seen photo turns up at a path that held another photo, it gets
that photo's state where it has none of its own: its rating (only if the file
didn't bring one), its tags (added, never removed) and its collections (placed
right after the old one). The old photo keeps everything. Each DB change emits a
``changes`` row (constraint #4); nothing is written into the file here -- the
next re-index writes the rating out like any other DB rating.

:func:`backfill` applies the same rule once to photos replaced before this
existed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import exists
from sqlmodel import Session, col, select

from sortandpost.collections import copy_into_collections
from sortandpost.db.meta import get_meta, set_meta
from sortandpost.db.models import Photo, PhotoPath, Rating, Tombstone
from sortandpost.db.ratings import set_rating
from sortandpost.db.tags import move_tags
from sortandpost.missing import replaced_by
from sortandpost.paths import Paths

log = logging.getLogger(__name__)

BACKFILL_KEY = "replaced_carryover_backfill"


@dataclass
class Carried:
    stars: int | None = None  # the rating carried over, if any
    tags: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return self.stars is not None or bool(self.tags)


def carry_over(session: Session, *, old: str, new: str, device_id: str, now: datetime) -> Carried:
    """Give *new* the rating and tags of *old*, the photo whose file it
    replaced, where *new* has none of its own. The caller commits."""
    out = Carried()
    if old == new or session.get(Tombstone, old) is not None:
        return out
    old_rating = session.get(Rating, old)
    new_rating = session.get(Rating, new)
    if old_rating is not None and old_rating.stars > 0 and not (new_rating and new_rating.stars):
        set_rating(session, pixel_hash=new, stars=old_rating.stars, device_id=device_id, now=now)
        out.stars = old_rating.stars
    out.tags = move_tags(session, src=old, dst=new, device_id=device_id, now=now, keep_src=True)
    if out.any:
        log.info(
            "file saved over: %s takes %s's rating %s + tags %s",
            new[:12],
            old[:12],
            out.stars,
            out.tags,
        )
    return out


def carry_collections(paths: Paths, *, old: str, new: str) -> None:
    """The collections half of :func:`carry_over` (they live in ``sync/``, not
    the DB). Best effort: a failed write is logged, never fails the scan."""
    try:
        copy_into_collections(paths, old, new)
    except OSError as exc:
        log.warning("could not add %s to %s's collections: %s", new[:12], old[:12], exc)


def backfill(session: Session, *, paths: Paths, device_id: str, now: datetime) -> int:
    """Once per library (a ``meta`` flag, so it travels with the DB): carry
    state over for every photo already replaced in place -- no path left, no
    present copy, and its old ``primary_path`` now holds a photo first indexed
    after it was last seen. Returns how many pairs it handled. Caller commits."""
    if get_meta(session, BACKFILL_KEY) is not None:
        return 0
    orphans = session.exec(
        select(Photo).where(
            col(Photo.missing_since).is_not(None),
            ~exists().where(col(PhotoPath.pixel_hash) == col(Photo.pixel_hash)),
        )
    ).all()
    done = 0
    for photo in orphans:
        new = replaced_by(session, photo.pixel_hash, photo.primary_path)
        newer = session.get(Photo, new) if new is not None else None
        if newer is None or newer.first_indexed_at < photo.last_indexed_at:
            continue  # the path went back to an older photo: not a replacement
        old = photo.pixel_hash
        carry_over(session, old=old, new=newer.pixel_hash, device_id=device_id, now=now)
        carry_collections(paths, old=old, new=newer.pixel_hash)
        done += 1
    set_meta(session, BACKFILL_KEY, now.isoformat())
    if done:
        log.info("carried state over for %d photo(s) replaced in place", done)
    return done
