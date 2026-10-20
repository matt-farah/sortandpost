"""What the library grid shows: URL query state -> an ordered photo list.

``/`` is both the library and search (``/search`` redirects here). Everything the
grid shows is described by a :class:`LibraryQuery`, which round-trips through
the URL query string -- so Back, reload, saved searches and the photo page all
rebuild exactly the same view. :func:`resolve_view` turns one into the ordered
representative ``pixel_hash`` list; the grid and the photo page's prev/next
both call it, so arrow keys on ``/photo/...`` page through the filtered set.

Ordering rules:

* text query -> CLIP relevance (best first); group-by is ignored.
* filters only / nothing -> newest ``taken_at`` first; group-by applies --
  by month unless the URL says otherwise (``group=flat`` for one flat grid).

Pure DB + NumPy; no NiceGUI. The text-query path runs CLIP inference, so UI
callers run :func:`resolve_view` off the event loop (hard constraint #7).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import parse_qsl, urlencode

from sqlmodel import Session, col, select

from sortandpost import missing
from sortandpost.clip.model import ClipModelUnavailable
from sortandpost.clip.vocab import Vocab
from sortandpost.config import Settings
from sortandpost.db.browse import list_group_rows, list_photo_ids
from sortandpost.db.flags import EDIT_FLAG
from sortandpost.db.models import (
    Photo,
    PhotoPath,
    PhotoTag,
    Prediction,
    PredictionState,
    Tag,
)
from sortandpost.grouping import (
    GroupMode,
    PhotoGroup,
    camera_label,
    group_photos,
    parse_folder_sort,
    parse_mode,
)
from sortandpost.library_roots import LibraryRoot, container_roots, other_device_roots
from sortandpost.posts import posted_hashes
from sortandpost.search.filters import PhotoFilter, allowed_hashes, text_match
from sortandpost.search.service import TextEncoder, semantic_search
from sortandpost.stacks import StackIndex, build_stacks

SEARCH_LIMIT = 500  # text-query results kept (relevance tails off long before this)
UNRATED = 0  # the "stars" value meaning *no rating*
TAG_MODES = ("any", "all")
DEFAULT_GROUP: GroupMode = "month"  # what ``group=""`` (no param) means
FLAT = "flat"  # the ``group`` value for an ungrouped grid

# --------------------------------------------------------------------------- #
# date ranges
# --------------------------------------------------------------------------- #
# a year, month or day: compact (2025 / 202506 / 20250614) or dashed (2025-06 /
# 2025-06-14) -- never mixed, so "2025-2026" can't read as year 2025, month 20
_TOKEN = re.compile(r"^(\d{4})(?:(\d{2})(\d{2})?|-(\d{2})(?:-(\d{2}))?)?$")
_RANGE_SEP = re.compile(r"\s*(?:\.\.|\u2013|\u2014|\bto\b)\s*", re.IGNORECASE)
_COMPACT_RANGE = re.compile(r"^(\d{4}|\d{6}|\d{8})\s*-\s*(\d{4}|\d{6}|\d{8})$")


def _span(token: str) -> tuple[datetime, datetime]:
    """``2025`` / ``202506`` / ``2025-06`` / ``20250614`` / ``2025-06-14`` ->
    ``(first instant, last instant)`` of that year / month / day."""
    m = _TOKEN.match(token.strip())
    if not m:
        raise ValueError(f"not a date: {token!r}")
    year = int(m.group(1))
    month = m.group(2) or m.group(4)
    day = m.group(3) or m.group(5)
    if month is None:
        start = datetime(year, 1, 1)
        end = datetime(year + 1, 1, 1)
    elif day is None:
        start = datetime(year, int(month), 1)
        end = datetime(year + (int(month) == 12), int(month) % 12 + 1, 1)
    else:
        start = datetime(year, int(month), int(day))
        end = start + timedelta(days=1)
    return start, end - timedelta(microseconds=1)


def parse_date_range(text: str) -> tuple[datetime | None, datetime | None]:
    """A user-typed date filter -> inclusive ``(from, to)`` (naive, like
    ``photos.taken_at``). Accepts a single year / month / day (``2025``,
    ``2025-06``, ``20250614``), a range of two of those joined by ``-`` (compact
    forms only, so ``2025-06`` stays a month), ``..``, ``to`` or an en dash
    (``2025-2026``, ``20260103-20260210``, ``2025-06 .. 2025-08``), and open
    ends (``2025..``, ``..2024``). A reversed range is swapped. Empty -> no
    constraint. Raises ``ValueError`` on anything else."""
    text = text.strip()
    if not text:
        return None, None
    parts = _RANGE_SEP.split(text)
    if len(parts) == 2:
        lo, hi = parts
    elif len(parts) == 1 and _TOKEN.match(text):
        return _span(text)
    elif len(parts) == 1 and (m := _COMPACT_RANGE.match(text)):
        lo, hi = m.group(1), m.group(2)
    else:
        raise ValueError(f"not a date or date range: {text!r}")
    if not lo.strip() and not hi.strip():
        raise ValueError(f"not a date or date range: {text!r}")
    start = _span(lo)[0] if lo.strip() else None
    end = _span(hi)[1] if hi.strip() else None
    if start is not None and end is not None and start > end:
        start, end = _span(hi)[0], _span(lo)[1]
    return start, end


def picker_to_dates(value: object) -> str:
    """A range-calendar value -> the ``dates`` URL param (compact, e.g.
    ``20260103-20260210``). Quasar's range ``QDate`` gives ``{"from", "to"}``,
    or a plain ``"YYYY-MM-DD"`` when one day is picked twice; empty -> ``""``."""
    if isinstance(value, dict):
        lo, hi = str(value.get("from") or ""), str(value.get("to") or "")
    elif isinstance(value, str):
        lo = hi = value
    else:
        return ""
    lo, hi = lo.replace("-", "").replace("/", ""), hi.replace("-", "").replace("/", "")
    if not lo or not hi:
        return ""
    return lo if lo == hi else f"{lo}-{hi}"


# --------------------------------------------------------------------------- #
# the query
# --------------------------------------------------------------------------- #
def _truthy(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class LibraryQuery:
    """Everything that decides what ``/`` shows. Field <-> URL param:

    ``q`` text · ``stars`` comma list (``u`` = unrated) · ``camera`` (repeat) ·
    ``dates`` · ``folder`` · ``tag`` (repeat) + ``tagmode`` any|all · ``gps`` ·
    ``dups`` · ``missing`` · ``mark`` · ``posted`` (``1`` sent to a posting service /
    ``0`` not yet) · ``group`` + ``folder_sort`` (view only; ``group`` unset
    means :data:`DEFAULT_GROUP`, ``flat`` means no sections).
    """

    q: str = ""
    stars: frozenset[int] = frozenset()
    cameras: tuple[str, ...] = ()
    dates: str = ""
    folder: str = ""
    tags: tuple[str, ...] = ()
    tagmode: str = "any"
    gps: bool = False
    dups: bool = False
    missing: bool = False
    mark: bool = False
    posted: str = ""  # "" any · "1" sent to a posting service · "0" not sent yet
    group: str = ""  # view only ("" = DEFAULT_GROUP, "flat"): not saved with a search
    folder_sort: str = ""

    # -- parsing ------------------------------------------------------------ #
    @classmethod
    def from_pairs(cls, pairs: Iterable[tuple[str, str]]) -> LibraryQuery:
        q = dates = folder = group = folder_sort = posted = ""
        tagmode = "any"
        stars: set[int] = set()
        cameras: list[str] = []
        tags: list[str] = []
        flags = {"gps": False, "dups": False, "missing": False, "mark": False}
        for key, raw in pairs:
            value = raw.strip()
            if key == "q":
                q = value
            elif key == "stars":
                for bit in value.split(","):
                    bit = bit.strip().lower()
                    if bit in ("u", "0"):
                        stars.add(UNRATED)
                    elif bit.isdigit() and 1 <= int(bit) <= 5:
                        stars.add(int(bit))
            elif key == "camera" and raw not in cameras:
                cameras.append(raw)  # "" is a real choice: "Unknown camera"
            elif key == "dates":
                dates = value
            elif key == "folder":
                folder = value
            elif key == "tag" and value and value not in tags:
                tags.append(value)
            elif key == "tagmode" and value in TAG_MODES:
                tagmode = value
            elif key in flags:
                flags[key] = _truthy(value)
            elif key == "posted" and value:
                posted = "1" if _truthy(value) else "0"
            elif key == "group":
                group = FLAT if value == FLAT else parse_mode(value) or ""
            elif key == "folder_sort":
                folder_sort = value if value and parse_folder_sort(value) == value else ""
        return cls(
            q=q,
            stars=frozenset(stars),
            cameras=tuple(cameras),
            dates=dates,
            folder=folder,
            tags=tuple(tags),
            tagmode=tagmode,
            group=group,
            folder_sort=folder_sort,
            posted=posted,
            **flags,
        )

    @classmethod
    def from_query_string(cls, qs: str) -> LibraryQuery:
        return cls.from_pairs(parse_qsl(qs.lstrip("?"), keep_blank_values=True))

    # -- serializing -------------------------------------------------------- #
    def pairs(self, *, view: bool = True) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if self.q:
            out.append(("q", self.q))
        if self.stars:
            out.append(
                ("stars", ",".join("u" if s == UNRATED else str(s) for s in sorted(self.stars)))
            )
        out.extend(("camera", c) for c in self.cameras)
        if self.dates:
            out.append(("dates", self.dates))
        if self.folder:
            out.append(("folder", self.folder))
        out.extend(("tag", t) for t in self.tags)
        if len(self.tags) > 1 and self.tagmode != "any":
            out.append(("tagmode", self.tagmode))
        for key in ("gps", "dups", "missing", "mark"):
            if getattr(self, key):
                out.append((key, "true"))
        if self.posted:
            out.append(("posted", self.posted))
        if view and self.group:
            out.append(("group", self.group))
            if self.group == "folder" and self.folder_sort:
                out.append(("folder_sort", self.folder_sort))
        return out

    def query_string(self, *, view: bool = True) -> str:
        """Canonical ``a=b&c=d`` (no leading ``?``); ``""`` for the plain library."""
        return urlencode(self.pairs(view=view), safe=",")

    def url(self, path: str = "/") -> str:
        qs = self.query_string()
        return f"{path}?{qs}" if qs else path

    # -- meaning -------------------------------------------------------------- #
    @property
    def mode(self) -> GroupMode | None:
        """Group-by mode in effect (never with a text query -- relevance wins;
        ``None`` = a flat grid)."""
        if self.q or self.group == FLAT:
            return None
        return parse_mode(self.group or DEFAULT_GROUP)

    def has_filters(self) -> bool:
        """Any filter or a text query (view-only params don't count)."""
        return bool(self.pairs(view=False))

    def date_bounds(self) -> tuple[datetime | None, datetime | None]:
        """Parsed ``dates``; an unparseable value constrains nothing (the UI
        flags it, see :meth:`date_error`)."""
        try:
            return parse_date_range(self.dates)
        except ValueError:
            return None, None

    def date_error(self) -> str | None:
        try:
            parse_date_range(self.dates)
        except ValueError:
            return f"Can't read the date “{self.dates}” — try 2025, 2025-2026 or 20260103-20260210."
        return None

    def to_filter(self, *, suggestions: bool = True) -> PhotoFilter:
        taken_from, taken_to = self.date_bounds()
        tags = list(self.tags)
        tags_all = tags if self.tagmode == "all" else []
        tags_any = tags if self.tagmode == "any" else []
        if self.mark:
            tags_all = [*tags_all, EDIT_FLAG]
        return PhotoFilter(
            stars_in=self.stars,
            cameras=list(self.cameras),
            folder=self.folder or None,
            taken_from=taken_from,
            taken_to=taken_to,
            tags_all=tags_all,
            tags_any=tags_any,
            tag_suggestions=suggestions,
            has_gps=True if self.gps else None,
            only_duplicates=self.dups,
            only_missing=self.missing,
        )

    def without_view(self) -> LibraryQuery:
        return replace(self, group="", folder_sort="")

    def chips(self) -> list[FilterChip]:
        """One removable chip per active filter (per value for cameras / tags),
        each carrying the query without it. The view (group-by) is kept."""
        out: list[FilterChip] = []
        if self.q:
            out.append(FilterChip("search", f"“{self.q}”", replace(self, q="")))
        if self.stars:
            label = ", ".join("unrated" if s == UNRATED else str(s) for s in sorted(self.stars))
            out.append(FilterChip("star", label, replace(self, stars=frozenset())))
        for cam in self.cameras:
            rest = tuple(c for c in self.cameras if c != cam)
            out.append(
                FilterChip("photo_camera", cam or "Unknown camera", replace(self, cameras=rest))
            )
        if self.dates:
            out.append(FilterChip("event", dates_label(self), replace(self, dates="")))
        if self.folder:
            out.append(FilterChip("folder", _leaf(self.folder), replace(self, folder="")))
        for tag in self.tags:
            rest = tuple(t for t in self.tags if t != tag)
            out.append(FilterChip("sell", tag, replace(self, tags=rest)))
        for on, icon, label, without in (
            (self.gps, "place", "Has GPS", replace(self, gps=False)),
            (self.dups, "content_copy", "Detected duplicates", replace(self, dups=False)),
            (self.mark, "flag", "Marked for editing", replace(self, mark=False)),
            (self.missing, "help_outline", "Missing photos", replace(self, missing=False)),
        ):
            if on:
                out.append(FilterChip(icon, label, without))
        if self.posted:
            label = "Posted" if self.posted == "1" else "Not posted"
            out.append(FilterChip("send", label, replace(self, posted="")))
        return out

    def summary(self) -> str:
        """Short human description, e.g. for a saved-search tooltip."""
        bits: list[str] = []
        if self.q:
            bits.append(f"“{self.q}”")
        if self.stars:
            bits.append(
                " ".join("unrated" if s == UNRATED else f"{s}★" for s in sorted(self.stars))
            )
        bits.extend(c or "unknown camera" for c in self.cameras)
        if self.dates:
            bits.append(self.dates)
        if self.folder:
            bits.append(_leaf(self.folder))
        if self.tags:
            bits.append((" & " if self.tagmode == "all" else " | ").join(self.tags))
        for key, label in (
            ("gps", "GPS"),
            ("dups", "detected duplicates"),
            ("missing", "missing photos only"),
            ("mark", "⚑ to edit"),
        ):
            if getattr(self, key):
                bits.append(label)
        if self.posted:
            bits.append("posted" if self.posted == "1" else "not posted")
        return " · ".join(bits) or "all photos"


@dataclass(frozen=True)
class FilterChip:
    """An active filter as the filter bar shows it: Material icon, short
    label, and the query with just this filter removed."""

    icon: str
    label: str
    without: LibraryQuery


# --------------------------------------------------------------------------- #
# resolving a query to the grid's order
# --------------------------------------------------------------------------- #
@dataclass
class LibraryView:
    ids: list[str]  # stack representatives, in display order
    stacks: StackIndex
    total: int  # representatives in the whole (unfiltered) library, for "N of M"
    groups: list[PhotoGroup] = field(default_factory=list)  # group-by only
    notice: str | None = None  # why a text query produced nothing (no CLIP, ...)


def resolve_view(
    session: Session,
    settings: Settings,
    lq: LibraryQuery,
    *,
    encoder: Callable[[], TextEncoder] | None = None,
) -> LibraryView:
    """The grid's ordered representative ``pixel_hash`` list for *lq*.
    *encoder* lazily supplies the CLIP text encoder; ``None`` = CLIP isn't
    installed, so a text query yields a notice instead of results."""
    stacks = build_stacks(session, settings.config.stacking)
    all_ids = list_photo_ids(session, only_missing=lq.missing)
    # missing photos are listed one by one: a stack's top photo may be here
    # while a member's file is gone
    reps_all = [h for h in all_ids if lq.missing or not stacks.is_hidden_member(h)]
    rep = _no_stacks if lq.missing else stacks.representative
    notice: str | None = None
    suggest = settings.config.clip.enabled  # subject filters also match CLIP suggestions

    if lq.q and not settings.config.clip.enabled:
        # AI search is off: plain match on file names, tags and camera, kept in
        # the library's own order (there is no relevance score to rank by).
        ids = _text_matches(session, settings, lq, reps_all, rep)
    elif lq.q:
        # File name / folder / tag / camera matches first (library order), then
        # the CLIP results that aren't already among them.
        named = _text_matches(session, settings, lq, reps_all, rep)
        hits: list[str] = []
        if encoder is None:
            notice = (
                "AI search needs CLIP (torch + open-clip-torch), which isn't installed "
                "here — showing file name, tag and camera matches only."
            )
        else:
            try:
                found = semantic_search(
                    session,
                    settings,
                    encoder(),
                    lq.q,
                    limit=SEARCH_LIMIT,
                    flt=lq.to_filter(suggestions=suggest),
                )
                hits = [h.pixel_hash for h in found]
            except ClipModelUnavailable:
                notice = (
                    "The CLIP model couldn't be loaded — showing file name, tag and "
                    "camera matches only."
                )
        seen = set(named)
        ids = named + [h for h in _first_rep_order(hits, rep) if h not in seen]
    elif lq.has_filters():
        allowed = allowed_hashes(session, lq.to_filter(suggestions=suggest))
        if allowed is None:
            ids = reps_all
        else:
            reps = {rep(h) for h in allowed}
            ids = [h for h in reps_all if h in reps]
    else:
        ids = reps_all

    if lq.posted:
        # A stack counts as posted when any of its members went out.
        sent = posted_hashes(settings.paths)
        want = lq.posted == "1"
        ids = [h for h in ids if any(m in sent for m in stacks.members(h)) == want]

    groups: list[PhotoGroup] = []
    mode = lq.mode
    if mode is not None and ids:
        rows = {r.pixel_hash: r for r in list_group_rows(session, only_missing=lq.missing)}
        groups = group_photos(
            [rows[h] for h in ids if h in rows], mode, folder_sort=parse_folder_sort(lq.folder_sort)
        )
        ids = [h for g in groups for h in g.hashes]

    return LibraryView(ids=ids, stacks=stacks, total=len(reps_all), groups=groups, notice=notice)


def _text_matches(
    session: Session,
    settings: Settings,
    lq: LibraryQuery,
    reps_all: list[str],
    rep: Callable[[str], str],
) -> list[str]:
    """Representatives whose file name / folder / tag / camera match ``lq.q``
    (``text_match``), with the query's filters applied, in library order."""
    roots = [str(r) for r in container_roots(settings.library_roots)]
    roots += other_device_roots(settings.paths, settings.device_id)
    match_reps = {rep(m) for m in text_match(session, lq.q, roots)}
    if lq.has_filters():
        allowed = allowed_hashes(session, lq.to_filter(suggestions=settings.config.clip.enabled))
        if allowed is not None:
            match_reps &= {rep(h) for h in allowed}
    return [h for h in reps_all if h in match_reps]


def _no_stacks(pixel_hash: str) -> str:
    return pixel_hash


def _first_rep_order(hashes: list[str], rep: Callable[[str], str]) -> list[str]:
    """Collapse ranked hits to stack representatives, each at its best hit."""
    out: list[str] = []
    seen: set[str] = set()
    for h in hashes:
        r = rep(h)
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def dates_to_picker(lq: LibraryQuery) -> dict[str, str] | None:
    """The current ``dates`` filter as a range-calendar value (``None`` when
    unset, unparseable, or open-ended -- the calendar can't show those)."""
    lo, hi = lq.date_bounds()
    if lo is None or hi is None:
        return None
    return {"from": f"{lo:%Y-%m-%d}", "to": f"{hi:%Y-%m-%d}"}


def dates_label(lq: LibraryQuery) -> str:
    """Short button text for the ``dates`` filter: ``Jun 14, 2025``,
    ``Jan 3 - Feb 10, 2026``, ``2025 - 2026`` (en dashes) ...; ``""`` when unset."""
    if not lq.dates:
        return ""
    if lq.date_error():
        return lq.dates
    lo, hi = lq.date_bounds()
    if lo is None and hi is not None:
        return f"until {_day(hi)}"
    if hi is None and lo is not None:
        return f"from {_day(lo)}"
    assert lo is not None and hi is not None
    if lo.date() == hi.date():
        return _day(lo)
    whole_years = (lo.month, lo.day) == (1, 1) and (hi.month, hi.day) == (12, 31)
    if whole_years:
        return str(lo.year) if lo.year == hi.year else f"{lo.year} \u2013 {hi.year}"
    if lo.year == hi.year:
        return f"{lo:%b} {lo.day} \u2013 {_day(hi)}"
    return f"{_day(lo)} \u2013 {_day(hi)}"


def _day(d: datetime) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def neighbors(ids: list[str], anchor: str) -> tuple[str | None, str | None]:
    """``(previous, next)`` of *anchor* in *ids* (``None`` at an end, or both
    ``None`` when *anchor* isn't in the list)."""
    try:
        i = ids.index(anchor)
    except ValueError:
        return None, None
    return (ids[i - 1] if i > 0 else None), (ids[i + 1] if i + 1 < len(ids) else None)


def locate(ids: list[str], candidates: Iterable[str]) -> str | None:
    """The first of *candidates* that is in *ids* (``None`` if none is). The
    photo page passes the photo, its stack's representative and the other
    members: a stack's top photo can change after the grid order was taken
    (its newest file wins), and prev/next must still find the photo's place."""
    present = set(ids)
    return next((c for c in candidates if c in present), None)


# --------------------------------------------------------------------------- #
# filter-bar choices
# --------------------------------------------------------------------------- #
def camera_choices(session: Session) -> list[tuple[str, int]]:
    """``(camera label, photo count)`` over present photos, most-used first;
    label ``""`` is "unknown camera"."""
    counts: dict[str, int] = {}
    rows = session.exec(
        select(Photo.camera_make, Photo.camera_model).where(missing.shown_clause())
    ).all()
    for make, model in rows:
        label = camera_label(make, model)
        counts[label] = counts.get(label, 0) + 1
    return sorted(counts.items(), key=lambda kv: (kv[0] == "", -kv[1], kv[0].casefold()))


def tag_choices(session: Session, vocab: Vocab | None = None) -> dict[str, list[str]]:
    """Filterable tag names grouped by facet: applied tags (manual + approved
    CLIP) plus un-rejected CLIP suggestions, minus the internal edit flag.
    Facets follow the vocabulary's order; manual tags without a facet go under
    ``"tags"``."""
    names: dict[str, str | None] = {}
    for name, facet in session.exec(
        select(Tag.name, Tag.facet).join(PhotoTag, col(PhotoTag.tag_id) == col(Tag.id)).distinct()
    ).all():
        if name != EDIT_FLAG:
            names.setdefault(name, facet)
    for name in session.exec(
        select(Prediction.tag_name)
        .where(col(Prediction.state) != PredictionState.rejected)
        .distinct()
    ).all():
        names.setdefault(name, None)

    order = {t.facet: i for i, t in reversed(list(enumerate(vocab.terms)))} if vocab else {}
    out: dict[str, list[str]] = {}
    for name, facet in names.items():
        f = facet or (vocab.facet_of(name) if vocab else None) or "tags"
        out.setdefault(f, []).append(name)
    return {
        f: sorted(v, key=str.casefold)
        for f, v in sorted(out.items(), key=lambda kv: (order.get(kv[0], len(order)), kv[0]))
    }


def _pure(path: str) -> PurePosixPath | PureWindowsPath:
    return PureWindowsPath(path) if "\\" in path else PurePosixPath(path)


def _leaf(path: str) -> str:
    return _pure(path).name or path


def folder_choices(session: Session, roots: list[LibraryRoot]) -> list[tuple[str, str]]:
    """``(folder path, display label)`` for every folder holding a present
    photo, plus its ancestors up to the library root, sorted by label. Labels
    are root-relative (``<root label>/sub/dir``) so two roots stay distinct."""
    root_paths = sorted((_pure(r.container) for r in roots), key=lambda p: -len(p.parts))
    root_label = {str(_pure(r.container)): (r.label or _leaf(r.container)) for r in roots}
    folders: set[str] = set()
    for path in session.exec(select(PhotoPath.path).where(col(PhotoPath.present).is_(True))).all():
        folders.add(str(_pure(path).parent))

    out: dict[str, str] = {}
    for folder in folders:
        p = _pure(folder)
        root = next((r for r in root_paths if p == r or r in p.parents), None)
        if root is None:  # outside every configured root (roots changed) -- show as-is
            out[str(p)] = str(p)
            continue
        cur = p
        while True:
            rel = cur.relative_to(root)
            label = root_label[str(root)] + ("" if str(rel) == "." else f"/{rel.as_posix()}")
            out[str(cur)] = label
            if cur == root:
                break
            cur = cur.parent
    return sorted(out.items(), key=lambda kv: kv[1].casefold())
