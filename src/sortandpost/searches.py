"""Saved searches -- named `/search` states, stored in ``sync/searches.json``.

They ride along in the sync folder like ``tags.yaml``: user-editable, shared
between machines, but **not** merged. Two machines editing the list at the same
time produces a sync-tool conflict copy (rare for a convenience list; the copy is
kept for manual recovery). This is deliberately lighter than the DB + ``changes``
log that ratings/tags use.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields
from urllib.parse import quote

from sortandpost.paths import Paths

log = logging.getLogger(__name__)


@dataclass
class SavedSearch:
    """One named `/search` state. Field names match the `/search` query params."""

    name: str
    q: str = ""
    stars: int = 0
    camera: str = ""
    gps: bool = False
    dups: bool = False
    missing: bool = False
    mark: bool = False

    def is_empty(self) -> bool:
        return not (
            self.q.strip()
            or self.stars
            or self.camera.strip()
            or self.gps
            or self.dups
            or self.missing
            or self.mark
        )

    def query_string(self) -> str:
        parts: list[str] = []
        if self.q.strip():
            parts.append(f"q={quote(self.q.strip())}")
        if self.stars:
            parts.append(f"stars={int(self.stars)}")
        if self.camera.strip():
            parts.append(f"camera={quote(self.camera.strip())}")
        if self.gps:
            parts.append("gps=true")
        if self.dups:
            parts.append("dups=true")
        if self.missing:
            parts.append("missing=true")
        if self.mark:
            parts.append("mark=true")
        return "/search?" + "&".join(parts) if parts else "/search"

    def summary(self) -> str:
        """Short human description of the filters, for a tooltip."""
        bits: list[str] = []
        if self.q.strip():
            bits.append(f'"{self.q.strip()}"')
        if self.stars:
            bits.append(f"≥{int(self.stars)}★")
        if self.camera.strip():
            bits.append(self.camera.strip())
        if self.gps:
            bits.append("GPS")
        if self.dups:
            bits.append("dupes")
        if self.missing:
            bits.append("incl. missing")
        if self.mark:
            bits.append("⚑ to edit")
        return " · ".join(bits) or "all photos"


def _coerce(raw: dict[str, object]) -> SavedSearch | None:
    known = {f.name for f in fields(SavedSearch)}
    data = {k: v for k, v in raw.items() if k in known}

    def _str(key: str) -> str:
        v = data.get(key)
        return v.strip() if isinstance(v, str) else ""

    def _int(key: str) -> int:
        v = data.get(key)
        if isinstance(v, bool):  # bool is an int subclass; not what we want here
            return 0
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            try:
                return int(v)
            except ValueError:
                return 0
        return 0

    if not _str("name"):
        return None
    return SavedSearch(
        name=_str("name"),
        q=_str("q"),
        stars=_int("stars"),
        camera=_str("camera"),
        gps=bool(data.get("gps", False)),
        dups=bool(data.get("dups", False)),
        missing=bool(data.get("missing", False)),
        mark=bool(data.get("mark", False)),
    )


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
