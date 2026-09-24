"""Saved searches -- named library views, stored in ``sync/searches.json``.

A saved search is a name plus the canonical query string of a
:class:`~sortandpost.library_view.LibraryQuery` (filters + text query, never the
group-by view), so it opens as ``/?<query>``. Older files stored ``/search``'s
separate fields (``q``, ``stars`` = minimum, ``camera`` ...); those are read and
converted on load.

They ride along in the sync folder like ``tags.yaml``: user-editable, shared
between machines, but **not** merged. Two machines editing the list at the same
time produces a sync-tool conflict copy (rare for a convenience list; the copy is
kept for manual recovery). This is deliberately lighter than the DB + ``changes``
log that ratings/tags use.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass

from sortandpost.library_view import LibraryQuery
from sortandpost.paths import Paths

log = logging.getLogger(__name__)

_LEGACY_KEYS = ("q", "stars", "camera", "gps", "dups", "missing", "mark")


@dataclass
class SavedSearch:
    """One named library view. ``query`` is ``LibraryQuery.query_string(view=False)``."""

    name: str
    query: str = ""

    @classmethod
    def of(cls, name: str, lq: LibraryQuery) -> SavedSearch:
        return cls(name=name, query=lq.query_string(view=False))

    @property
    def library_query(self) -> LibraryQuery:
        return LibraryQuery.from_query_string(self.query)

    def is_empty(self) -> bool:
        return not self.library_query.has_filters()

    def url(self) -> str:
        return f"/?{self.query}" if self.query else "/"

    def summary(self) -> str:
        """Short human description of the filters, for a tooltip."""
        return self.library_query.summary()


def legacy_query(raw: dict[str, object]) -> LibraryQuery:
    """The old ``/search`` fields -> a :class:`LibraryQuery`. ``stars`` was a
    *minimum* there (``3`` = 3★ and up); ``camera`` was free text and becomes an
    exact camera choice."""
    pairs: list[tuple[str, str]] = []
    q = raw.get("q")
    if isinstance(q, str) and q.strip():
        pairs.append(("q", q.strip()))
    stars = raw.get("stars")
    if isinstance(stars, str) and stars.strip().isdigit():
        stars = int(stars)
    if isinstance(stars, int) and not isinstance(stars, bool) and 1 <= stars <= 5:
        pairs.append(("stars", ",".join(str(n) for n in range(stars, 6))))
    camera = raw.get("camera")
    if isinstance(camera, str) and camera.strip():
        pairs.append(("camera", camera.strip()))
    for key in ("gps", "dups", "missing", "mark"):
        v = raw.get(key)
        if v is True or (isinstance(v, str) and v.strip().lower() in ("1", "true", "yes")):
            pairs.append((key, "true"))
    return LibraryQuery.from_pairs(pairs)


def _coerce(raw: dict[str, object]) -> SavedSearch | None:
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    query = raw.get("query")
    if isinstance(query, str):
        lq = LibraryQuery.from_query_string(query)
    elif any(k in raw for k in _LEGACY_KEYS):
        lq = legacy_query(raw)
    else:
        lq = LibraryQuery()
    return SavedSearch.of(name.strip(), lq.without_view())


def load_searches(paths: Paths) -> list[SavedSearch]:
    path = paths.saved_searches_file
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return []
    rows = data.get("searches") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    out: list[SavedSearch] = []
    for raw in rows:
        if isinstance(raw, dict):
            s = _coerce(raw)
            if s is not None:
                out.append(s)
    out.sort(key=lambda s: s.name.casefold())
    return out


def save_searches(paths: Paths, searches: list[SavedSearch]) -> None:
    path = paths.saved_searches_file
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"searches": [asdict(s) for s in searches]}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def add_search(paths: Paths, search: SavedSearch) -> list[SavedSearch]:
    """Add *search*, replacing any existing one with the same name
    (case-insensitive). Returns the new list."""
    kept = [s for s in load_searches(paths) if s.name.casefold() != search.name.casefold()]
    kept.append(search)
    kept.sort(key=lambda s: s.name.casefold())
    save_searches(paths, kept)
    return kept


def remove_search(paths: Paths, name: str) -> list[SavedSearch]:
    kept = [s for s in load_searches(paths) if s.name.casefold() != name.casefold()]
    save_searches(paths, kept)
    return kept
