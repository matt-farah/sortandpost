"""Photo collections -- named, hand-curated, *ordered* groups of photos, stored
in ``sync/collections.json``.

Like saved searches (and ``tags.yaml``) they ride along in the sync folder:
user-editable, shared between machines, but **not** conflict-merged. Two machines
editing the list at the same moment produce a sync-tool conflict copy, kept for
manual recovery. This is deliberately lighter than the DB + ``changes`` log that
ratings/tags use.

A collection is intentionally *separate* from tagging: membership is an explicit,
manually arranged list -- the order is the sequence the user chose (e.g. for a
social post) -- and each collection carries a free-text ``notes`` field reserved
for future post copy.

The module name shadows nothing: ``from collections.abc import ...`` and
``from collections import ...`` elsewhere in the package are absolute imports and
still resolve to the standard library.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from dataclasses import asdict, dataclass, field, fields

from sortandpost.paths import Paths
from sortandpost.sync.clock import utcnow

log = logging.getLogger(__name__)

NAME_MAX_LEN = 120
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")  # C0 + DEL + C1
_WHITESPACE_RUN = re.compile(r"\s+")


def clean_name(raw: str) -> str:
    """Normalize a user-typed collection name so it can never corrupt the store
    or a URL: strip control characters, collapse internal whitespace to single
    spaces, trim, and cap the length. May return ``""`` (caller rejects that)."""
    if not isinstance(raw, str):
        return ""
    collapsed = _WHITESPACE_RUN.sub(" ", _CONTROL_CHARS.sub(" ", raw)).strip()
    return collapsed[:NAME_MAX_LEN].strip()


def _new_id() -> str:
    return secrets.token_hex(8)


def _now() -> str:
    return utcnow().isoformat()


@dataclass
class Collection:
    """One named collection. ``photos`` is an ordered, de-duplicated list of
    ``pixel_hash`` values -- the identity every other table keys on."""

    name: str
    id: str = field(default_factory=_new_id)
    notes: str = ""
    photos: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        seen: set[str] = set()
        clean: list[str] = []
        for h in self.photos:
            if isinstance(h, str) and h and h not in seen:
                seen.add(h)
                clean.append(h)
        self.photos = clean
        self.name = clean_name(self.name)
        if not self.created_at:
            self.created_at = _now()
        if not self.updated_at:
            self.updated_at = self.created_at

    @property
    def count(self) -> int:
        return len(self.photos)

    def cover(self) -> str | None:
        """The first member -- the collection's cover thumbnail."""
        return self.photos[0] if self.photos else None

    def has(self, pixel_hash: str) -> bool:
        return pixel_hash in self.photos


# --- serialization ------------------------------------------------------------


def _coerce(raw: dict[str, object]) -> Collection | None:
    known = {f.name for f in fields(Collection)}
    data = {k: v for k, v in raw.items() if k in known}

    def _str(key: str) -> str:
        v = data.get(key)
        return v.strip() if isinstance(v, str) else ""

    name = clean_name(_str("name"))
    if not name:
        return None
    photos_raw = data.get("photos")
    photos = (
        [p for p in photos_raw if isinstance(p, str) and p] if isinstance(photos_raw, list) else []
    )
    notes_raw = data.get("notes")
    return Collection(
        name=name,
        id=_str("id") or _new_id(),
        notes=notes_raw if isinstance(notes_raw, str) else "",
        photos=photos,
        created_at=_str("created_at"),
        updated_at=_str("updated_at"),
    )


def load_collections(paths: Paths) -> list[Collection]:
    path = paths.collections_file
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        log.warning("could not read %s: %s", path, exc)
        return []
    except ValueError as exc:
        # Corrupt JSON: never let a bad file be silently overwritten on the next
        # save. Move it aside so it can be recovered by hand, then start clean.
        backup = path.with_name(f"{path.name}.corrupt-{utcnow():%Y%m%d-%H%M%S}")
        try:
            path.replace(backup)
            log.warning("collections file %s was corrupt (%s); moved to %s", path, exc, backup)
        except OSError:
            log.warning("collections file %s was corrupt and could not be backed up: %s", path, exc)
        return []
    rows = data.get("collections") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    out: list[Collection] = []
    for raw in rows:
        if isinstance(raw, dict):
            c = _coerce(raw)
            if c is not None:
                out.append(c)
    out.sort(key=lambda c: c.name.casefold())
    return out


def save_collections(paths: Paths, collections: list[Collection]) -> None:
    path = paths.collections_file
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"collections": [asdict(c) for c in collections]}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _write_sorted(paths: Paths, collections: list[Collection]) -> None:
    collections.sort(key=lambda c: c.name.casefold())
    save_collections(paths, collections)


# --- lookups + mutations ----------------------------------------------------


def get_collection(paths: Paths, coll_id: str) -> Collection | None:
    for c in load_collections(paths):
        if c.id == coll_id:
            return c
    return None


def collections_containing(paths: Paths, pixel_hash: str) -> list[Collection]:
    return [c for c in load_collections(paths) if c.has(pixel_hash)]


class DuplicateNameError(ValueError):
    """Another collection already has this name (compared case-insensitively)."""

    def __init__(self, existing: Collection) -> None:
        super().__init__(f'a collection named "{existing.name}" already exists')
        self.existing = existing


def find_by_name(
    collections: list[Collection], name: str, *, exclude_id: str | None = None
) -> Collection | None:
    """The collection whose (cleaned) name matches *name* case-insensitively."""
    key = clean_name(name).casefold()
    for c in collections:
        if c.name.casefold() == key and c.id != exclude_id:
            return c
    return None


def create_collection(paths: Paths, name: str, *, notes: str = "") -> Collection:
    """Raises :class:`DuplicateNameError` when the name is taken -- a double
    submit (Enter + the Create button) once made two "Double Exposure"s."""
    coll = Collection(name=clean_name(name), notes=notes.strip())
    existing = load_collections(paths)
    if (dup := find_by_name(existing, coll.name)) is not None:
        raise DuplicateNameError(dup)
    existing.append(coll)
    _write_sorted(paths, existing)
    return coll


def rename_collection(paths: Paths, coll_id: str, new_name: str) -> list[Collection]:
    """Raises :class:`DuplicateNameError` when another collection has *new_name*."""
    new_name = clean_name(new_name)
    colls = load_collections(paths)
    if new_name and (dup := find_by_name(colls, new_name, exclude_id=coll_id)) is not None:
        raise DuplicateNameError(dup)
    for c in colls:
        if c.id == coll_id and new_name:
            c.name = new_name
            c.updated_at = _now()
    _write_sorted(paths, colls)
    return colls


def set_collection_notes(paths: Paths, coll_id: str, notes: str) -> list[Collection]:
    colls = load_collections(paths)
    for c in colls:
        if c.id == coll_id and c.notes != notes:
            c.notes = notes
            c.updated_at = _now()
    _write_sorted(paths, colls)
    return colls


def delete_collection(paths: Paths, coll_id: str) -> list[Collection]:
    colls = [c for c in load_collections(paths) if c.id != coll_id]
    _write_sorted(paths, colls)
    return colls


def add_to_collection(paths: Paths, coll_id: str, pixel_hashes: list[str]) -> int:
    """Append the hashes not already present, preserving order. Returns how many
    were newly added."""
    colls = load_collections(paths)
    added = 0
    for c in colls:
        if c.id != coll_id:
            continue
        have = set(c.photos)
        for h in pixel_hashes:
            if h and h not in have:
                c.photos.append(h)
                have.add(h)
                added += 1
        if added:
            c.updated_at = _now()
    _write_sorted(paths, colls)
    return added


def remove_from_collection(paths: Paths, coll_id: str, pixel_hashes: list[str]) -> int:
    drop = {h for h in pixel_hashes}
    colls = load_collections(paths)
    removed = 0
    for c in colls:
        if c.id != coll_id:
            continue
        before = len(c.photos)
        c.photos = [h for h in c.photos if h not in drop]
        removed = before - len(c.photos)
        if removed:
            c.updated_at = _now()
    _write_sorted(paths, colls)
    return removed


def reorder_collection(paths: Paths, coll_id: str, ordered: list[str]) -> list[Collection]:
    """Set the membership order to *ordered*. Hashes in *ordered* that are not
    current members are ignored; current members missing from *ordered* are kept,
    appended in their previous relative order."""
    colls = load_collections(paths)
    for c in colls:
        if c.id != coll_id:
            continue
        current = set(c.photos)
        new = [h for h in ordered if h in current]
        placed = set(new)
        new.extend(h for h in c.photos if h not in placed)
        if new != c.photos:
            c.photos = new
            c.updated_at = _now()
    _write_sorted(paths, colls)
    return colls
