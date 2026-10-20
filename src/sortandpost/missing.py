"""Which photos are missing *on this computer* (V3 P9).

``photo_paths`` lives in the shared DB, so it holds every device's copies. Each
path is judged by its owner, told apart by where it sits:

* **mine** -- under one of this computer's library folders: this computer's
  scanner marks it present / absent, and it counts toward "missing here";
* **another device's** -- under another device's folders
  (``sync/devices/*/library.json``) and none of mine: left alone, so this
  computer never marks the other one's photos absent in the shared DB;
* **orphan** -- under nobody's folders (a library folder removed in Settings):
  judged by whichever computer scans, so its photos still go missing.

A photo is **missing here** when it has a judged path and none of them is
present -- or when it has no path at all and no present copy anywhere
(``missing_since``): its only file was saved over with different pixels, which
made a new photo and moved the ``photo_paths`` row to it (``replaced``). A
photo whose files are only on another computer is not missing here; it shows in
the grid like any other (Reveal / Open / posting skip it).
``photos.missing_since`` keeps its shared meaning -- no present copy anywhere --
and is what the scanner and merge maintain.

Known limitation: two computers whose library folders have the *same* path
spelling (the same user name and folder on two Macs, or Docker's ``/library``
on both) can't be told apart -- such a path counts as mine on both, and the two
already share one ``photo_paths`` row (``path`` is unique).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, Table, and_, exists, false, func, not_, or_, true
from sqlmodel import Session, col, select

from sortandpost.db.models import Photo, PhotoPath
from sortandpost.library_roots import container_roots, other_device_roots

if TYPE_CHECKING:
    from sortandpost.config import Settings


class MissingReason(StrEnum):
    """Why a photo is missing here. The value is the text the UI shows."""

    offline = "folder offline"  # its library folder isn't there right now
    gone = "file gone"  # the folder is there, the file isn't
    removed = "folder removed"  # no longer under any library folder
    replaced = "file replaced"  # its file was saved over with a different picture


def _clean(root: str) -> str:
    return root.rstrip("/\\") or root


def _sep(root: str) -> str:
    # another device's folder may be a Windows path seen from a Mac, or back
    return "\\" if "\\" in root or (len(root) > 1 and root[1] == ":") else "/"


def _prefix(root: str) -> str:
    return root + _sep(root)


def _is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(_prefix(root))


def _under_clause(column: ColumnElement[str], roots: Iterable[str]) -> ColumnElement[bool]:
    # substr, not LIKE: SQLite's LIKE is case-insensitive
    clauses = [
        or_(column == r, func.substr(column, 1, len(_prefix(r))) == _prefix(r)) for r in roots
    ]
    return or_(*clauses) if clauses else false()


@dataclass(frozen=True)
class PathScope:
    """This computer's view of ``photo_paths`` (see the module docstring)."""

    mine: tuple[str, ...]
    others: tuple[str, ...] = ()
    offline: frozenset[str] = field(default_factory=frozenset)  # mine, not mounted

    @classmethod
    def build(cls, mine: Iterable[str | Path], others: Iterable[str] = ()) -> PathScope:
        own = tuple(dict.fromkeys(_clean(str(r)) for r in mine))
        rest = tuple(dict.fromkeys(r for o in others if (r := _clean(o)) not in own))
        offline = frozenset(r for r in own if not Path(r).is_dir())
        return cls(mine=own, others=rest, offline=offline)

    def root_of(self, path: str) -> str | None:
        """The (deepest) of my library folders *path* is under, if any."""
        hits = [r for r in self.mine if _is_under(path, r)]
        return max(hits, key=len) if hits else None

    def judged(self, path: str) -> bool:
        """Whether this computer marks *path* present / absent."""
        if self.root_of(path) is not None:
            return True
        return not any(_is_under(path, o) for o in self.others)

    def judged_clause(self, column: ColumnElement[str]) -> ColumnElement[bool]:
        """SQL twin of :meth:`judged`."""
        if not self.others:
            return true()
        return or_(_under_clause(column, self.mine), not_(_under_clause(column, self.others)))

    def reason(self, paths: Iterable[str]) -> MissingReason:
        """Why a missing photo with these (absent, judged) paths is missing."""
        roots = [self.root_of(p) for p in paths]
        if any(r is not None and r in self.offline for r in roots):
            return MissingReason.offline
        if any(r is not None for r in roots):
            return MissingReason.gone
        return MissingReason.removed


# --------------------------------------------------------------------------- #
# the process's current scope
# --------------------------------------------------------------------------- #
# Set by the app / indexer at startup. Unset (tests, scripts) -> the queries fall
# back to the shared ``missing_since``.
_TTL_SEC = 10.0
_lock = threading.Lock()
_provider: Callable[[], PathScope] | None = None
_cached: tuple[float, PathScope] | None = None


def for_settings(settings: Settings) -> PathScope:
    """A fresh scope from this computer's roots + the other devices' files."""
    return PathScope.build(
        container_roots(settings.library_roots),
        other_device_roots(settings.paths, settings.device_id),
    )


def configure(settings: Settings | None) -> None:
    """Use *settings*' roots from now on (``None`` = unset)."""
    global _provider, _cached
    with _lock:
        _provider = (lambda: for_settings(settings)) if settings is not None else None
        _cached = None


def invalidate() -> None:
    """Forget the cached scope (the library folders just changed)."""
    global _cached
    with _lock:
        _cached = None


def current() -> PathScope | None:
    """The configured scope, rebuilt at most every few seconds (it reads the
    other devices' files and checks whether each root is mounted)."""
    global _cached
    with _lock:
        if _provider is None:
            return None
        now = time.monotonic()
        if _cached is None or now - _cached[0] > _TTL_SEC:
            _cached = (now, _provider())
        return _cached[1]


# --------------------------------------------------------------------------- #
# queries
# --------------------------------------------------------------------------- #
def missing_here_clause(scope: PathScope | None = None) -> ColumnElement[bool]:
    """SQL predicate on :class:`Photo`: missing on this computer."""
    scope = scope or current()
    if scope is None:
        return col(Photo.missing_since).is_not(None)
    table: Table = PhotoPath.__table__  # type: ignore[attr-defined]
    judged = table.alias("judged_path")
    present = table.alias("present_path")
    photo_hash = col(Photo.pixel_hash)
    has_judged = exists().where(
        judged.c.pixel_hash == photo_hash, scope.judged_clause(judged.c.path)
    )
    has_present = exists().where(
        present.c.pixel_hash == photo_hash,
        present.c.present.is_(True),
        scope.judged_clause(present.c.path),
    )
    any_path = table.alias("any_path")
    pathless = and_(
        not_(exists().where(any_path.c.pixel_hash == photo_hash)),
        col(Photo.missing_since).is_not(None),
    )
    return or_(and_(has_judged, not_(has_present)), pathless)


def shown_clause(scope: PathScope | None = None) -> ColumnElement[bool]:
    """SQL predicate on :class:`Photo`: shown in the normal grid here."""
    return not_(missing_here_clause(scope))


def visibility_clause(*, include_missing: bool, only_missing: bool) -> ColumnElement[bool] | None:
    """The grid's missing-photo clause: only them, everything, or (default)
    everything but them. ``None`` = no clause."""
    if only_missing:
        return missing_here_clause()
    if include_missing:
        return None
    return shown_clause()


def missing_count(session: Session) -> int:
    stmt = select(func.count()).select_from(Photo).where(missing_here_clause())
    return int(session.exec(stmt).one())


def offline_missing_count(session: Session) -> int:
    """Photos missing here because a library folder is offline; ``0`` while
    every folder is mounted."""
    scope = current()
    if scope is None or not scope.offline:
        return 0
    hashes = session.exec(select(Photo.pixel_hash).where(missing_here_clause(scope))).all()
    reasons = missing_reasons(session, hashes, scope)
    return sum(r is MissingReason.offline for r in reasons.values())


def missing_reasons(
    session: Session, hashes: Iterable[str], scope: PathScope | None = None
) -> dict[str, MissingReason]:
    """``{pixel_hash: reason}`` for those of *hashes* that are missing here."""
    scope = scope or current()
    wanted = list(dict.fromkeys(hashes))
    if not wanted:
        return {}
    if scope is None:
        scope = PathScope.build(())  # no roots: every path is an orphan
        keep = set(
            session.exec(
                select(Photo.pixel_hash).where(
                    col(Photo.pixel_hash).in_(wanted), col(Photo.missing_since).is_not(None)
                )
            ).all()
        )
    else:
        keep = set(wanted)
    by_hash: dict[str, list[PhotoPath]] = {}
    for i in range(0, len(wanted), 500):
        chunk = wanted[i : i + 500]
        for pp in session.exec(select(PhotoPath).where(col(PhotoPath.pixel_hash).in_(chunk))):
            if scope.judged(pp.path):
                by_hash.setdefault(pp.pixel_hash, []).append(pp)
    out: dict[str, MissingReason] = {}
    for h, rows in by_hash.items():
        if h in keep and not any(r.present for r in rows):
            out[h] = scope.reason(r.path for r in rows)
    for h, replaced in _pathless(session, [h for h in wanted if h not in by_hash]).items():
        out[h] = MissingReason.replaced if replaced else MissingReason.gone
    return out


def _pathless(session: Session, hashes: list[str]) -> dict[str, bool]:
    """``{pixel_hash: replaced}`` for those of *hashes* that have no
    ``photo_paths`` row at all and no present copy anywhere. *replaced* = its
    ``primary_path`` now belongs to another photo (the file was saved over)."""
    out: dict[str, bool] = {}
    for i in range(0, len(hashes), 500):
        chunk = hashes[i : i + 500]
        rows = session.exec(
            select(Photo.pixel_hash, Photo.primary_path).where(
                col(Photo.pixel_hash).in_(chunk),
                col(Photo.missing_since).is_not(None),
                not_(exists().where(col(PhotoPath.pixel_hash) == col(Photo.pixel_hash))),
            )
        ).all()
        for h, primary in rows:
            out[h] = replaced_by(session, h, primary) is not None
    return out


def replaced_by(session: Session, pixel_hash: str, primary_path: str) -> str | None:
    """The photo now at *pixel_hash*'s old ``primary_path``, if another one is."""
    other = session.exec(
        select(PhotoPath.pixel_hash).where(
            PhotoPath.path == primary_path, PhotoPath.pixel_hash != pixel_hash
        )
    ).first()
    return other


def missing_here_set(session: Session, hashes: Iterable[str]) -> set[str]:
    """Those of *hashes* that are missing on this computer."""
    wanted = list(dict.fromkeys(hashes))
    out: set[str] = set()
    clause = missing_here_clause()
    for i in range(0, len(wanted), 500):
        chunk = wanted[i : i + 500]
        stmt = select(Photo.pixel_hash).where(col(Photo.pixel_hash).in_(chunk), clause)
        out.update(session.exec(stmt).all())
    return out


# what to do about it, for the photo page
REASON_HINTS: dict[MissingReason, str] = {
    MissingReason.offline: (
        "Its library folder isn't connected right now. Plug in the drive or reconnect "
        "the network folder; the photo comes back on the next scan."
    ),
    MissingReason.gone: (
        "Its library folder is here, but the file isn't. It was moved out of your "
        "library folders or deleted outside the app. A file moved within your library "
        "folders is found again on the next scan."
    ),
    MissingReason.replaced: (
        "Its file was saved over with a different picture (an edit or re-export), "
        "which is now a photo of its own. This version is kept so nothing is lost. "
        "A new version gets this one's rating, tags and collections when it first appears."
    ),
    MissingReason.removed: (
        "Its folder isn't one of this computer's library folders any more (Settings → Library)."
    ),
}
