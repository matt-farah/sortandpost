"""Image stacking -- computed at read time, never stored.

A stack groups photos in the **same parent folder** whose filenames match a rule:

* ``raw_jpeg``      -- a RAW and a non-RAW sharing the exact stem
                       (``IMG_4901.CR2`` + ``IMG_4901.JPG``).
* ``similar_names`` -- non-RAW files whose stems match after stripping explicit
                       copy markers: a leading ``copy ``/``copy of ``, a trailing
                       `` copy``/`` copy 2``/`` copy (3)``/`` - Copy``, or a
                       trailing `` (1)``. Bare trailing digits are never stripped,
                       so ``DSCF4460`` and ``DSCF4461`` never merge.

Grouping runs over ``photo_paths`` (duplicates at several paths are handled) and
is rolled up to distinct ``pixel_hash`` members. The **representative** -- the
face shown in the grid -- is the member whose newest file ``mtime`` is greatest.
Ratings/tags are applied to every member in the DB, but only the stack's
:func:`embed_target` gets the embedded EXIF/XMP write (see ``_apply_rating`` in
the app): writing a member's file bumps its mtime, which would make it the new
representative, and the user's originals stay untouched.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from sqlmodel import Session, col, select

from sortandpost.config import StackingConfig
from sortandpost.db.models import Photo, PhotoPath
from sortandpost.metadata import can_embed_metadata, is_raw

_LEADING_COPY = re.compile(r"^\s*copy(?:\s+of)?\s+", re.IGNORECASE)
# trailing "- Copy", "copy", "copy 2", "copy (3)", or "(1)"
_TRAILING_COPY = re.compile(
    r"\s*(?:[-\u2013]\s*)?(?:copy|\(\d+\))(?:\s*\(\d+\)|\s+\d+)?\s*$",
    re.IGNORECASE,
)


def normalize_stem(stem: str) -> str:
    """Collapse a filename stem to its base by removing explicit copy markers.

    ``"img copy (2)" -> "img"``, ``"copy of img" -> "img"``, ``"DSCF4460" ->
    "dscf4460"`` (unchanged apart from case)."""
    s = stem.strip()
    prev = ""
    while prev != s:
        prev = s
        s = _LEADING_COPY.sub("", s)
        s = _TRAILING_COPY.sub("", s).strip()
    return s.casefold() or stem.strip().casefold()


def _split(path: str) -> tuple[str, str]:
    """(parent, stem) for a path string, tolerant of either separator."""
    pure = PureWindowsPath(path) if "\\" in path else PurePosixPath(path)
    return str(pure.parent), pure.stem


def _keys_for(path: str, is_raw: bool, cfg: StackingConfig) -> set[tuple[str, str]]:
    parent, stem = _split(path)
    keys: set[tuple[str, str]] = set()
    if cfg.raw_jpeg:
        keys.add((parent, "=" + stem.casefold()))
    if cfg.similar_names and not is_raw:
        keys.add((parent, "~" + normalize_stem(stem)))
    return keys


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[object, object] = {}

    def find(self, x: object) -> object:
        self._parent.setdefault(x, x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: object, b: object) -> None:
        self._parent[self.find(a)] = self.find(b)


@dataclass(frozen=True)
class StackIndex:
    """Lookup for a single stacking computation.

    ``rep_of`` / ``members_of`` only contain photos that belong to a stack of two
    or more distinct ``pixel_hash``es; everything else is a singleton and the
    methods below fall back to identity.
    """

    rep_of: dict[str, str]  # member hash -> representative hash (rep maps to itself)
    members_of: dict[str, list[str]]  # rep hash -> [rep, ...others] newest mtime first

    def representative(self, pixel_hash: str) -> str:
        return self.rep_of.get(pixel_hash, pixel_hash)

    def is_hidden_member(self, pixel_hash: str) -> bool:
        """True for a stack member that is not its stack's representative."""
        rep = self.rep_of.get(pixel_hash)
        return rep is not None and rep != pixel_hash

    def members(self, pixel_hash: str) -> list[str]:
        return self.members_of.get(self.representative(pixel_hash), [pixel_hash])

    def size(self, pixel_hash: str) -> int:
        return len(self.members(pixel_hash))


EMPTY = StackIndex({}, {})


def build_stacks(session: Session, cfg: StackingConfig, *, folder: str | None = None) -> StackIndex:
    """Group every present photo per *cfg*. Cheap enough to run on each page
    load at the ~25k-photo design target (one indexed join, then in memory).

    *folder* limits it to the photos directly in that folder -- stacks never
    cross folders, so that's exact for them, and cheap enough per file (the
    indexer). A photo with copies elsewhere is then seen through this folder's
    copy only."""
    if not cfg.enabled or not (cfg.raw_jpeg or cfg.similar_names):
        return EMPTY

    stmt = (
        select(PhotoPath.pixel_hash, PhotoPath.path, PhotoPath.mtime_ns, Photo.is_raw)
        .join(Photo, col(Photo.pixel_hash) == col(PhotoPath.pixel_hash))
        .where(col(PhotoPath.present).is_(True))
    )
    want_parent: str | None = None
    if folder is not None:
        sep = "\\" if "\\" in folder else "/"
        prefix = folder.rstrip(sep) + sep
        want_parent = _split(prefix + "x")[0]
        stmt = stmt.where(col(PhotoPath.path).startswith(prefix, autoescape=True))
    rows = session.exec(stmt).all()
    if want_parent is not None:  # drop photos in subfolders of *folder*
        rows = [r for r in rows if _split(r[1])[0] == want_parent]

    uf = _UnionFind()
    seen_hashes: set[str] = set()
    newest: dict[str, int] = {}
    for pixel_hash, path, mtime_ns, raw in rows:
        seen_hashes.add(pixel_hash)
        newest[pixel_hash] = max(newest.get(pixel_hash, -1), mtime_ns or 0)
        for key in _keys_for(path, bool(raw), cfg):
            uf.union(("h", pixel_hash), ("k", key))

    groups: dict[object, set[str]] = defaultdict(set)
    for pixel_hash in seen_hashes:
        groups[uf.find(("h", pixel_hash))].add(pixel_hash)

    rep_of: dict[str, str] = {}
    members_of: dict[str, list[str]] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        ordered = sorted(members, key=lambda h: (-newest.get(h, 0), h))
        rep = ordered[0]
        members_of[rep] = ordered
        for h in ordered:
            rep_of[h] = rep
    return StackIndex(rep_of, members_of)


def embed_target(session: Session, stacks: StackIndex, pixel_hash: str) -> str:
    """The one member of *pixel_hash*'s stack whose file gets embedded
    ratings / keywords: the newest member with a present copy that can carry
    metadata (not a RAW -- so a RAW+JPEG pair writes its JPEG). A photo that
    isn't stacked, or a stack with no such member, is its own target (the
    writers skip RAW / non-embeddable copies themselves)."""
    members = stacks.members(pixel_hash)
    if len(members) < 2:
        return pixel_hash
    for member in members:  # newest first
        paths = session.exec(
            select(PhotoPath.path).where(
                PhotoPath.pixel_hash == member, col(PhotoPath.present).is_(True)
            )
        ).all()
        for raw in paths:
            path = Path(raw)
            if not is_raw(path) and can_embed_metadata(path) and path.is_file():
                return member
    return pixel_hash
