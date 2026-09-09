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
Ratings/tags are applied to every member (see ``_apply_rating`` in the app).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

from sqlmodel import Session, col, select

from sortandpost.config import StackingConfig
from sortandpost.db.models import Photo, PhotoPath

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


def build_stacks(session: Session, cfg: StackingConfig) -> StackIndex:
    """Group every present photo per *cfg*. Cheap enough to run on each page
    load at the ~25k-photo design target (one indexed join, then in memory)."""
    if not cfg.enabled or not (cfg.raw_jpeg or cfg.similar_names):
        return EMPTY

    rows = session.exec(
        select(PhotoPath.pixel_hash, PhotoPath.path, PhotoPath.mtime_ns, Photo.is_raw)
        .join(Photo, col(Photo.pixel_hash) == col(PhotoPath.pixel_hash))
        .where(col(PhotoPath.present).is_(True))
    ).all()

    uf = _UnionFind()
    seen_hashes: set[str] = set()
    newest: dict[str, int] = {}
    for pixel_hash, path, mtime_ns, is_raw in rows:
        seen_hashes.add(pixel_hash)
        newest[pixel_hash] = max(newest.get(pixel_hash, -1), mtime_ns or 0)
        for key in _keys_for(path, bool(is_raw), cfg):
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
