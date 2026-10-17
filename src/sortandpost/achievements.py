"""Achievements (V3 #2) -- the engine: definitions, the synced profile, and
which achievements are unlocked.

**Definitions** come from a bundled copy of ``dev-notes/achievements/Achievements.md``
(``resources/achievements/achievements.md``): one Markdown table row per
achievement (name, trigger, points, shown, angel / devil text). The free-text
*Trigger* column is parsed into a :class:`Trigger`:

* a **count** -- "Index 69 photos", "Rate 1 photo", "Post 10 posts",
  "Create 50 collections" -- compared against totals computed from existing
  state (:func:`compute_metrics`), so a library that already has 5000 photos
  unlocks the photo achievements the first time it is checked. "-- cannot be
  triggered on first day" holds the unlock back until the library's earliest
  ``photos.first_indexed_at`` is before today;
* an **event** -- turning on Devil mode, the Konami code, searching "github" --
  recorded by :meth:`AchievementTracker.record_event`.

**Unlocks** live per device, in ``sync/devices/<slug>-<id8>/achievements.json``
(``{"unlocked": {key: iso_ts}, "revealed": {key: iso_ts}}`` -- a new unlock stays a
sealed card until the user clicks it open), next to that device's ``library.json``. A
device only ever writes its own file and the truth is the **union** of every
device's file (earliest timestamp wins), so two machines unlocking at once never
conflict and nothing needs a merge. Unlocks are sticky: deleting a collection
later never takes one away. Like collections and posts this is a deliberate
exception to docs/architecture.md constraint #4 -- it is not ``pixel_hash`` state and does
not go in the ``changes`` log.

**Profile** -- the display name, the mode (Angel / Devil / Off) and the
profile picture -- lives in :mod:`sortandpost.profiles`.
Mode Off hides achievements in the UI but they keep unlocking underneath.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from importlib import resources
from pathlib import Path
from typing import Literal

from sqlmodel import Session, col, func, select

from sortandpost.config import StackingConfig
from sortandpost.db.models import Photo, Rating
from sortandpost.library_roots import device_dir
from sortandpost.paths import Paths
from sortandpost.profiles import Mode
from sortandpost.profiles import write_json as _write_json
from sortandpost.stacks import build_stacks
from sortandpost.sync.clock import Clock, utcnow

log = logging.getLogger(__name__)

Metric = Literal["indexed", "rated", "posts", "collections"]
METRICS: tuple[Metric, ...] = ("indexed", "rated", "posts", "collections")

# Event triggers. The UI calls AchievementTracker.record_event with these.
EVENT_DEVIL_MODE = "devil_mode"
EVENT_KONAMI = "konami"
EVENT_GITHUB_SEARCH = "github_search"

_DEFINITIONS = "achievements/achievements.md"
_FILE = "achievements.json"

# "Index 69 photos", "Rate 1 photo", "Post 1 post", "Create 10 collections"
_COUNT_RE = re.compile(
    r"^\s*(index|rate|post|create)\s+(\d+|one|a|an)\s+(photos?|posts?|collections?)\b", re.I
)
_VERB_METRIC: dict[str, Metric] = {"index": "indexed", "rate": "rated", "post": "posts"}
_EVENT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"devil mode", re.I), EVENT_DEVIL_MODE),
    (re.compile(r"konami", re.I), EVENT_KONAMI),
    (re.compile(r"github", re.I), EVENT_GITHUB_SEARCH),
)

# Material icon per trigger kind, for the cards (P3b).
_METRIC_ICONS: dict[str, str] = {
    "indexed": "photo_camera",
    "rated": "star",
    "posts": "send",
    "collections": "collections_bookmark",
    EVENT_DEVIL_MODE: "local_fire_department",
    EVENT_KONAMI: "sports_esports",
    EVENT_GITHUB_SEARCH: "code",
}


# --------------------------------------------------------------------------- #
# definitions
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Trigger:
    metric: Metric | None = None  # a count trigger ...
    threshold: int = 0
    not_first_day: bool = False
    event: str | None = None  # ... or an event trigger


@dataclass(frozen=True)
class Achievement:
    key: str  # stable slug of the name -- the id stored in achievements.json
    name: str
    trigger_text: str
    trigger: Trigger
    points: int
    shown: bool  # False = a gray "?" card until unlocked
    angel: str  # Markdown, as written in the table (``\*`` escapes kept)
    devil: str

    @property
    def icon(self) -> str:
        return _METRIC_ICONS.get(self.trigger.metric or self.trigger.event or "", "emoji_events")

    def description(self, mode: Mode) -> str:
        return self.devil if mode == "devil" else self.angel


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")


def parse_trigger(text: str) -> Trigger | None:
    m = _COUNT_RE.match(text)
    if m:
        verb, noun = m.group(1).casefold(), m.group(3).casefold()
        n = int(m.group(2)) if m.group(2).isdigit() else 1
        metric: Metric = "collections" if noun.startswith("collection") else _VERB_METRIC[verb]
        return Trigger(metric=metric, threshold=n, not_first_day="first day" in text.casefold())
    for pattern, event in _EVENT_PATTERNS:
        if pattern.search(text):
            return Trigger(event=event)
    return None


def _unescape(cell: str) -> str:
    """Markdown cell -> plain text: drop the ``\\`` escapes (``b\\*tch``)."""
    return re.sub(r"\\(.)", r"\1", cell).strip()


def plain_text(markdown: str) -> str:
    """A description without its Markdown emphasis, for a toast caption."""
    return _unescape(re.sub(r"(?<!\\)\*+", "", markdown))


def parse_definitions(markdown: str) -> list[Achievement]:
    """Rows of the achievements table, in file order. A row whose trigger can't
    be understood is logged and skipped -- it could never unlock."""
    out: list[Achievement] = []
    seen: set[str] = set()
    for line in markdown.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip("|"))]
        if len(cells) < 6 or not cells[0] or set(cells[0]) <= set("-: "):
            continue
        if cells[0].casefold() == "name":
            continue  # header row
        name, trigger_text, points, shown = (_unescape(c) for c in cells[:4])
        angel, devil = cells[4], cells[5]
        trigger = parse_trigger(trigger_text)
        key = slug(name)
        try:
            pts = int(points)
        except ValueError:
            pts = 0
        if trigger is None or not key or key in seen:
            log.warning("achievements: skipping row %r (trigger %r)", name, trigger_text)
            continue
        seen.add(key)
        out.append(
            Achievement(
                key=key,
                name=name,
                trigger_text=trigger_text,
                trigger=trigger,
                points=pts,
                shown=shown.strip().upper().startswith("Y"),
                angel=angel,
                devil=devil,
            )
        )
    return out


_DEFS_CACHE: list[Achievement] | None = None


def definitions() -> list[Achievement]:
    """The bundled achievement table (parsed once per process)."""
    global _DEFS_CACHE
    if _DEFS_CACHE is None:
        text = (
            resources.files("sortandpost.resources")
            .joinpath(_DEFINITIONS)
            .read_text(encoding="utf-8")
        )
        _DEFS_CACHE = parse_definitions(text)
    return _DEFS_CACHE


def resource_path(name: str) -> Path:
    """A file in ``resources/achievements/`` (the Angel / Devil art). Bundled as
    a real file -- also under PyInstaller -- so it can be served statically."""
    return Path(str(resources.files("sortandpost.resources").joinpath("achievements", name)))


# --------------------------------------------------------------------------- #
# unlocks -- per device, union across devices
# --------------------------------------------------------------------------- #
_SECTIONS = ("unlocked", "revealed")


def _read_state(path: Path) -> dict[str, dict[str, str]]:
    """One device file: ``{"unlocked": {key: ts}, "revealed": {key: ts}}``.
    ``revealed`` = the user clicked the card open (new unlocks stay sealed)."""
    empty: dict[str, dict[str, str]] = {s: {} for s in _SECTIONS}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty
    except (OSError, ValueError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return empty
    if not isinstance(data, dict):
        return empty
    for section in _SECTIONS:
        rows = data.get(section)
        if isinstance(rows, dict):
            empty[section] = {
                k: v for k, v in rows.items() if isinstance(k, str) and isinstance(v, str)
            }
    return empty


def _union(paths: Paths, section: str) -> dict[str, str]:
    """``key -> earliest time`` over every device's file. A sync tool's
    conflict copy (``achievements (1).json``) is folded in too -- it is only
    ever a union, so reading it can't hurt."""
    out: dict[str, str] = {}
    root = paths.devices_dir
    if not root.is_dir():
        return out
    for dev in root.iterdir():
        if not dev.is_dir():
            continue
        for f in dev.glob("achievements*.json"):
            for key, ts in _read_state(f)[section].items():
                if key not in out or ts < out[key]:
                    out[key] = ts
    return out


def all_unlocks(paths: Paths) -> dict[str, str]:
    return _union(paths, "unlocked")


def all_reveals(paths: Paths) -> dict[str, str]:
    return _union(paths, "revealed")


# --------------------------------------------------------------------------- #
# metrics -- computed from existing state
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Metrics:
    indexed: int = 0
    rated: int = 0  # photos with stars > 0, one per stack
    posts: int = 0  # posts sent to at least one channel
    collections: int = 0
    first_day: bool = True  # the library was first indexed today (or is empty)

    def value(self, metric: Metric) -> int:
        return int(getattr(self, metric))


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def library_first_day(session: Session, today: date) -> bool:
    """True while the library's first photo was indexed *today* (local date),
    or there are no photos yet -- the "cannot be triggered on first day" rule."""
    first = session.exec(select(func.min(Photo.first_indexed_at))).one()
    if first is None:
        return True
    return _as_utc(first).astimezone().date() >= today


def rated_count(session: Session, stacking: StackingConfig, *, at_least: int | None = None) -> int:
    """Rated photos (``stars > 0``), a stack counting once -- ratings fan out
    to every stack member, so counting rows would count a RAW+JPEG pair twice.
    *at_least*: skip building stacks when the raw row count can't reach it."""
    hashes = list(session.exec(select(Rating.pixel_hash).where(col(Rating.stars) > 0)).all())
    if not hashes or (at_least is not None and len(hashes) < at_least):
        return len(hashes)
    stacks = build_stacks(session, stacking)
    return len({stacks.representative(h) for h in hashes})


def compute_metrics(
    session: Session,
    paths: Paths,
    stacking: StackingConfig,
    *,
    today: date | None = None,
    rated_floor: int | None = None,
) -> Metrics:
    # Lazy: importing ``collections`` before ``db`` trips the package's
    # sync <-> db import cycle, and ``app`` imports this module early.
    from sortandpost.collections import load_collections
    from sortandpost.posts import list_posts

    today = today or datetime.now().astimezone().date()
    indexed = int(session.exec(select(func.count()).select_from(Photo)).one())
    posts = sum(1 for p in list_posts(paths) if p.sent or p.partly_sent)
    return Metrics(
        indexed=indexed,
        rated=rated_count(session, stacking, at_least=rated_floor),
        posts=posts,
        collections=len(load_collections(paths)),
        first_day=library_first_day(session, today),
    )


def due(
    defs: Iterable[Achievement], metrics: Metrics, unlocked: Iterable[str]
) -> list[Achievement]:
    """Count achievements *metrics* has earned that aren't unlocked yet."""
    have = set(unlocked)
    out: list[Achievement] = []
    for a in defs:
        t = a.trigger
        if a.key in have or t.metric is None:
            continue
        if metrics.value(t.metric) < t.threshold:
            continue
        if t.not_first_day and metrics.first_day:
            continue
        out.append(a)
    return out


# --------------------------------------------------------------------------- #
# tracker -- one per process
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Unlock:
    seq: int  # increases per unlock in this process; the UI toasts seq > last seen
    achievement: Achievement
    at: str


@dataclass
class AchievementTracker:
    """Records this device's unlocks and keeps a short in-memory feed of the
    new ones so each open tab can toast them. Thread-safe: the background check
    and UI events (Konami, Devil mode) can race."""

    paths: Paths
    device_id: str
    hostname: str
    clock: Clock = utcnow
    defs: list[Achievement] = field(default_factory=definitions)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _recent: list[Unlock] = field(default_factory=list, repr=False)
    _seq: int = 0
    _version: int = 0  # bumps on every unlock / reveal in this process

    @property
    def own_file(self) -> Path:
        return device_dir(self.paths, self.device_id, self.hostname) / _FILE

    def unlocked(self) -> dict[str, str]:
        return all_unlocks(self.paths)

    def sealed(self, unlocked: dict[str, str] | None = None) -> list[str]:
        """Unlocked but not yet revealed (clicked open), in table order."""
        have = self.unlocked() if unlocked is None else unlocked
        seen = all_reveals(self.paths)
        return [a.key for a in self.defs if a.key in have and a.key not in seen]

    def reveal(self, key: str) -> None:
        """The user opened *key*'s card. Recorded in this device's file."""
        with self._lock:
            path = self.own_file
            state = _read_state(path)
            if key in state["revealed"]:
                return
            state["revealed"][key] = self.clock().isoformat()
            _write_json(path, state)
            self._version += 1

    def score(self, unlocked: dict[str, str] | None = None) -> int:
        """Points of the unlocked achievements the user has revealed -- a
        sealed one adds its points only once it's clicked open."""
        have = self.unlocked() if unlocked is None else unlocked
        seen = all_reveals(self.paths)
        return sum(a.points for a in self.defs if a.key in have and a.key in seen)

    def total_points(self) -> int:
        return sum(a.points for a in self.defs)

    def rated_floor(self) -> int | None:
        """Smallest rating threshold not yet unlocked (``None`` = all done), so
        a check can skip the stack build when the raw count is below it."""
        have = self.unlocked()
        pending = [
            a.trigger.threshold
            for a in self.defs
            if a.trigger.metric == "rated" and a.key not in have
        ]
        return min(pending) if pending else None

    def _unlock(self, achievements: list[Achievement]) -> list[Unlock]:
        """Add *achievements* to this device's file (caller holds the lock)."""
        if not achievements:
            return []
        now = self.clock().isoformat()
        path = self.own_file
        state = _read_state(path)
        mine = state["unlocked"]
        fresh = [a for a in achievements if a.key not in mine]
        if not fresh:
            return []
        for a in fresh:
            mine[a.key] = now
        _write_json(path, state)
        self._version += 1
        out: list[Unlock] = []
        for a in fresh:
            self._seq += 1
            out.append(Unlock(self._seq, a, now))
            log.info("achievement unlocked: %s (+%d)", a.name, a.points)
        self._recent.extend(out)
        del self._recent[:-50]  # a feed, not a history
        return out

    def check(self, metrics: Metrics) -> list[Unlock]:
        """Unlock every count achievement *metrics* has earned."""
        with self._lock:
            return self._unlock(due(self.defs, metrics, self.unlocked()))

    def record_event(self, event: str) -> list[Unlock]:
        """An event trigger fired (Devil mode, Konami, "github" search)."""
        with self._lock:
            have = self.unlocked()
            hits = [a for a in self.defs if a.trigger.event == event and a.key not in have]
            return self._unlock(hits)

    def since(self, seq: int) -> list[Unlock]:
        """Unlocks made in this process after *seq* (for per-tab toasts)."""
        with self._lock:
            return [u for u in self._recent if u.seq > seq]

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def version(self) -> int:
        return self._version
