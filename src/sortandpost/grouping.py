"""Library group-by mode -- split the flat thumbnail grid into labelled sections.

Pure logic, no DB / NiceGUI imports so it stays unit-testable. The ``/`` page
loads the photos it would show anyway (present, newest ``taken_at`` first,
``pixel_hash`` tie-break), hands the rows here, and gets back an ordered list of
:class:`PhotoGroup` -- each a header label plus the ``pixel_hash`` values under
it, already in display order. The page flattens that back into its keyboard /
selection index and drops a full-width header in front of each group.

Four modes:

* ``day`` / ``month`` -- header per calendar day / month. The input order already
  is newest-first, so this is just "start a new group when the key changes";
  undated photos collect in a trailing "No date" group.
* ``folder`` -- header per source folder (the file's parent directory). Needs a
  re-sort; the folder order is user-chosen (:data:`FolderSort`).
* ``camera`` -- header per camera make+model, ordered by each group's newest
  photo, with a trailing "Unknown camera" group.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal, NamedTuple

GroupMode = Literal["day", "month", "folder", "camera"]
FolderSort = Literal["newest", "oldest", "az", "za"]

VALID_MODES: tuple[GroupMode, ...] = ("day", "month", "folder", "camera")
VALID_FOLDER_SORTS: tuple[FolderSort, ...] = ("newest", "oldest", "az", "za")

_NO_DATE_KEY = "\x00no-date"  # collects last; cannot collide with a real key
_UNKNOWN_CAMERA_KEY = "\x00unknown-camera"


def parse_mode(raw: str | None) -> GroupMode | None:
    """A query-param string to a valid mode, or ``None`` (flat grid)."""
    return raw if raw in VALID_MODES else None


def parse_folder_sort(raw: str | None) -> FolderSort:
    """A query-param string to a folder sort; unknown / empty falls back to
    ``"newest"``."""
    return raw if raw in VALID_FOLDER_SORTS else "newest"


class GroupRow(NamedTuple):
    """The photo fields grouping needs, in the page's display order."""

    pixel_hash: str
    taken_at: datetime | None
    primary_path: str
    camera_make: str | None
    camera_model: str | None
    file_mtime_ns: int


@dataclass
class PhotoGroup:
    """One section: a header (``label`` + optional ``sublabel``) and its photos
    in display order."""

    key: str
    label: str
    hashes: list[str] = field(default_factory=list)
    sublabel: str | None = None

    @property
    def count(self) -> int:
        return len(self.hashes)


# --- helpers ---------------------------------------------------------------


def _parent_dir(path: str) -> str:
    """The parent directory of *path*, handling both separators regardless of the
    host OS (a synced library indexed on Windows can reach a Linux container)."""
    p: PurePosixPath | PureWindowsPath
    p = PureWindowsPath(path) if "\\" in path else PurePosixPath(path)
    return str(p.parent)


def _leaf(dir_path: str) -> str:
    name = PureWindowsPath(dir_path).name if "\\" in dir_path else PurePosixPath(dir_path).name
    return name or dir_path


def _day_label(d: date, today: date) -> str:
    delta = (today - d).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    return f"{d:%B} {d.day}, {d.year}"


def _camera_label(make: str | None, model: str | None) -> str:
    make = (make or "").strip()
    model = (model or "").strip()
    if model and make and not model.lower().startswith(make.lower()):
        return f"{make} {model}"
    return model or make


# --- the four modes ------------------------------------------------------------


def _group_by_period(rows: Iterable[GroupRow], *, month: bool, today: date) -> list[PhotoGroup]:
    groups: dict[str, PhotoGroup] = {}
    for r in rows:
        if r.taken_at is None:
            key, label = _NO_DATE_KEY, "No date"
        elif month:
            key = f"{r.taken_at:%Y-%m}"
            label = f"{r.taken_at:%B %Y}"
        else:
            d = r.taken_at.date()
            key = d.isoformat()
            label = _day_label(d, today)
        g = groups.get(key)
        if g is None:
            g = groups[key] = PhotoGroup(key=key, label=label)
        g.hashes.append(r.pixel_hash)
    # dict preserves first-seen order (already newest-first); force "No date" last
    ordered = [g for k, g in groups.items() if k != _NO_DATE_KEY]
    if _NO_DATE_KEY in groups:
        ordered.append(groups[_NO_DATE_KEY])
    return ordered


def _group_by_camera(rows: Iterable[GroupRow]) -> list[PhotoGroup]:
    groups: dict[str, PhotoGroup] = {}
    newest: dict[str, datetime] = {}
    for r in rows:
        label = _camera_label(r.camera_make, r.camera_model)
        key = label.casefold() if label else _UNKNOWN_CAMERA_KEY
        g = groups.get(key)
        if g is None:
            g = groups[key] = PhotoGroup(key=key, label=label or "Unknown camera")
        g.hashes.append(r.pixel_hash)
        if r.taken_at is not None and r.taken_at > newest.get(key, datetime.min):
            newest[key] = r.taken_at

    def sort_key(g: PhotoGroup) -> tuple[int, float, str]:
        if g.key == _UNKNOWN_CAMERA_KEY:
            return (1, 0.0, "")
        ts = newest.get(g.key)
        return (0, -ts.timestamp() if ts else 0.0, g.key)

    return sorted(groups.values(), key=sort_key)


def _group_by_folder(rows: Iterable[GroupRow], folder_sort: FolderSort) -> list[PhotoGroup]:
    groups: dict[str, PhotoGroup] = {}
    newest_mtime: dict[str, int] = {}
    for r in rows:
        key = _parent_dir(r.primary_path)
        g = groups.get(key)
        if g is None:
            g = groups[key] = PhotoGroup(key=key, label=_leaf(key), sublabel=key)
        g.hashes.append(r.pixel_hash)
        if r.file_mtime_ns > newest_mtime.get(key, -1):
            newest_mtime[key] = r.file_mtime_ns

    if folder_sort in ("az", "za"):
        out = sorted(groups.values(), key=lambda g: (g.label.casefold(), g.key.casefold()))
        if folder_sort == "za":
            out.reverse()
        return out
    out = sorted(groups.values(), key=lambda g: newest_mtime.get(g.key, -1), reverse=True)
    if folder_sort == "oldest":
        out.reverse()
    return out


def group_photos(
    rows: Sequence[GroupRow],
    mode: GroupMode,
    *,
    folder_sort: FolderSort = "newest",
    today: date | None = None,
) -> list[PhotoGroup]:
    """Split *rows* (already in the page's display order) into ordered
    :class:`PhotoGroup` sections for *mode*. Within every group the photos keep
    their incoming order."""
    if mode == "day":
        return _group_by_period(rows, month=False, today=today or date.today())
    if mode == "month":
        return _group_by_period(rows, month=True, today=today or date.today())
    if mode == "camera":
        return _group_by_camera(rows)
    return _group_by_folder(rows, folder_sort)
