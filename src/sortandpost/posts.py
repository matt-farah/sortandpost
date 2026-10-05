"""Social posts composed for robopost.app, stored one file per post in
``sync/posts/<post_id>.json``.

Like collections these ride along in the sync folder but are **not** merged
through the DB ``changes`` log: a post is not ``pixel_hash``-keyed state, it is
a small document. (A deliberate exception to CLAUDE.md constraint #4, the same
one collections and saved searches have.) One file per post rather than one
``posts.json`` so two machines editing *different* posts never produce a sync
conflict copy -- losing a post's send log could lead to a double post.

A post's life: ``draft`` → (Send as draft | Schedule) → ``sending`` →
``sent_draft`` / ``scheduled`` (or ``failed``, which can be retried). robopost's
API cannot read, edit or cancel a post afterwards, so a sent post is frozen
here; "Duplicate" starts a new draft from it. ``sending`` is written *before*
the create call and is never retried automatically: a send interrupted by a
crash is resolved by hand ("it was sent" / "it wasn't").

**Split sends.** When the chosen channels mix Instagram with other networks and
the photos need Instagram sizing, one post becomes two robopost creates (see
:func:`plan_send`): the other channels first, then Instagram. Each create is a
*part* (``Post.parts``) with its own ``sending → sent | failed`` state and its
channel ids, so a retry or "it was sent / it wasn't" only touches the part in
doubt, and channels a ``sent`` part already reached are never sent to again.
A post with a sent part but channels still to go is ``failed`` (retryable,
"partly sent").
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import secrets
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from sortandpost.collections import clean_name
from sortandpost.paths import Paths
from sortandpost.post_export import (
    DEFAULT_IG_FORMAT,
    IG_FORMATS,
    IgSpec,
    parse_color,
    resolve_ig_spec,
)
from sortandpost.robopost import Channel
from sortandpost.sync.clock import utcnow

log = logging.getLogger(__name__)

PostStatus = Literal["draft", "sending", "sent_draft", "scheduled", "failed"]
SendMode = Literal["draft", "schedule"]
PartStatus = Literal["sending", "sent", "failed"]

SENT_STATUSES: frozenset[str] = frozenset({"sent_draft", "scheduled"})
EDITABLE_STATUSES: frozenset[str] = frozenset({"draft", "failed"})

_ID_RE = re.compile(r"^[0-9a-f]{16}$")

# Hard per-network image limits (block sending) and soft ones (warn only).
IMAGE_LIMITS_HARD = {"instagram": 10}
IMAGE_LIMITS_SOFT = {"twitter": 4, "x": 4, "bluesky": 4, "threads": 20, "linkedin": 20}
TEXT_LIMITS_SOFT = {
    "instagram": 2200,
    "twitter": 280,
    "x": 280,
    "bluesky": 300,
    "threads": 500,
    "pinterest": 500,
}
POST_TYPES = ("POST", "REELS", "STORIES")  # instagram; facebook uses the first two
IG_MAX_BORDER = 100
MIN_LEAD = timedelta(minutes=2)  # a schedule time must be at least this far ahead


def _new_id() -> str:
    return secrets.token_hex(8)


def _now() -> str:
    return utcnow().isoformat()


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


@dataclass
class Post:
    title: str
    id: str = field(default_factory=_new_id)
    source_collection_id: str | None = None
    photos: list[str] = field(default_factory=list)  # ordered pixel_hashes
    caption: str = ""
    first_comment: str = ""
    channel_ids: list[str] = field(default_factory=list)
    # {"instagram": {"postType": "POST"}, "facebook": {"postType": "POST"},
    #  "pinterest": {"pinTitle": "", "destinationLink": ""}}
    network_settings: dict[str, dict[str, str]] = field(default_factory=dict)
    schedule_at: str | None = None  # UTC ISO-8601
    status: str = "draft"
    sending_mode: str | None = None  # the mode of the in-flight / interrupted send
    # pixel_hash (or "<pixel_hash>#ig" for an Instagram-sized upload) ->
    # {"export_key", "storage_object_id", "uploaded_at"}
    media_cache: dict[str, dict[str, str]] = field(default_factory=dict)
    # one entry per robopost create attempt, oldest first:
    # {"group", "status": sending|sent|failed, "mode", "channel_ids": [...],
    #  "robopost_post_ids": [...], "started_at"}
    parts: list[dict[str, Any]] = field(default_factory=list)
    log: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    updated_by_device: str = ""

    def __post_init__(self) -> None:
        seen: set[str] = set()
        clean: list[str] = []
        for h in self.photos:
            if isinstance(h, str) and h and h not in seen:
                seen.add(h)
                clean.append(h)
        self.photos = clean
        self.channel_ids = list(dict.fromkeys(c for c in self.channel_ids if c))
        self.title = clean_name(self.title) or "Untitled post"
        if not self.created_at:
            self.created_at = _now()
        if not self.updated_at:
            self.updated_at = self.created_at

    @property
    def editable(self) -> bool:
        return self.status in EDITABLE_STATUSES

    @property
    def sent(self) -> bool:
        return self.status in SENT_STATUSES

    @property
    def partly_sent(self) -> bool:
        """Some channels have this post but others are still to go."""
        return not self.sent and any(pt.get("status") == "sent" for pt in self.parts)

    def sent_channel_ids(self) -> set[str]:
        """Channels a ``sent`` part reached -- never sent to again."""
        return {cid for pt in self.parts if pt.get("status") == "sent" for cid in pt["channel_ids"]}

    def sending_part(self) -> dict[str, Any] | None:
        """The part whose create may or may not have reached robopost."""
        return next((pt for pt in self.parts if pt.get("status") == "sending"), None)

    def cover(self) -> str | None:
        return self.photos[0] if self.photos else None

    def schedule_dt(self) -> datetime | None:
        return parse_utc(self.schedule_at)

    def setting(self, network: str, key: str, default: str = "") -> str:
        return self.network_settings.get(network, {}).get(key, default)

    # Instagram sizing (see post_export.IgSpec). Stored as strings in
    # network_settings["instagram"]; unknown / bad values fall back to defaults.
    def ig_story(self) -> bool:
        return self.setting("instagram", "postType", "POST") == "STORIES"

    def ig_format(self) -> str:
        fmt = self.setting("instagram", "format", DEFAULT_IG_FORMAT)
        if self.ig_story():
            return "story"
        return fmt if fmt in IG_FORMATS and fmt != "story" else DEFAULT_IG_FORMAT

    def ig_border(self) -> int:
        try:
            return max(0, min(IG_MAX_BORDER, int(self.setting("instagram", "border", "0"))))
        except ValueError:
            return 0

    def ig_color(self) -> str:
        """``#rrggbb`` pad colour (white when unset or invalid)."""
        r, g, b = parse_color(self.setting("instagram", "padColor", "#ffffff"))
        return f"#{r:02x}{g:02x}{b:02x}"

    def ig_spec(self, sizes: list[tuple[int, int]]) -> IgSpec | None:
        """The canvas this post's Instagram photos go out at (``None`` = they
        already have an Instagram ratio and are used as they are). *sizes* =
        the photos' upright ``(width, height)``."""
        return resolve_ig_spec(
            sizes,
            fmt=self.ig_format(),
            border=self.ig_border(),
            rgb=parse_color(self.ig_color()),
            story=self.ig_story(),
        )


# --- storage -----------------------------------------------------------------


def _post_file(paths: Paths, post_id: str) -> Path:
    if not _ID_RE.match(post_id):
        raise ValueError(f"bad post id {post_id!r}")
    return paths.posts_dir / f"{post_id}.json"


def _coerce(raw: dict[str, Any]) -> Post | None:
    known = {f.name for f in fields(Post)}
    data = {k: v for k, v in raw.items() if k in known}
    pid = data.get("id")
    if not isinstance(pid, str) or not _ID_RE.match(pid):
        return None

    def _s(key: str) -> str:
        v = data.get(key)
        return v if isinstance(v, str) else ""

    def _list(key: str) -> list[str]:
        v = data.get(key)
        return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []

    def _dict(key: str) -> dict[str, Any]:
        v = data.get(key)
        return v if isinstance(v, dict) else {}

    ns = {
        str(k): {str(kk): str(vv) for kk, vv in v.items()}
        for k, v in _dict("network_settings").items()
        if isinstance(v, dict)
    }
    mc = {
        str(k): {str(kk): str(vv) for kk, vv in v.items()}
        for k, v in _dict("media_cache").items()
        if isinstance(v, dict)
    }
    status = _s("status")
    logv = data.get("log")
    parts = []
    rawparts = data.get("parts")
    for pt in rawparts if isinstance(rawparts, list) else []:
        if not isinstance(pt, dict) or pt.get("status") not in ("sending", "sent", "failed"):
            continue
        parts.append(
            {
                "group": str(pt.get("group") or ""),
                "status": pt["status"],
                "mode": str(pt.get("mode") or "draft"),
                "channel_ids": [str(c) for c in pt.get("channel_ids") or [] if c],
                "robopost_post_ids": [str(c) for c in pt.get("robopost_post_ids") or [] if c],
                "started_at": str(pt.get("started_at") or ""),
            }
        )
    return Post(
        title=_s("title"),
        id=pid,
        source_collection_id=_s("source_collection_id") or None,
        photos=_list("photos"),
        caption=_s("caption"),
        first_comment=_s("first_comment"),
        channel_ids=_list("channel_ids"),
        network_settings=ns,
        schedule_at=_s("schedule_at") or None,
        status=status
        if status in ("draft", "sending", "sent_draft", "scheduled", "failed")
        else "draft",
        sending_mode=_s("sending_mode") or None,
        media_cache=mc,
        parts=parts,
        log=[e for e in logv if isinstance(e, dict)] if isinstance(logv, list) else [],
        created_at=_s("created_at"),
        updated_at=_s("updated_at"),
        updated_by_device=_s("updated_by_device"),
    )


def _read(path: Path) -> Post | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        log.warning("could not read %s: %s", path, exc)
        return None
    except ValueError as exc:
        backup = path.with_name(f"{path.name}.corrupt-{utcnow():%Y%m%d-%H%M%S}")
        try:
            path.replace(backup)
            log.warning("post file %s was corrupt (%s); moved to %s", path, exc, backup)
        except OSError:
            log.warning("post file %s was corrupt and could not be backed up: %s", path, exc)
        return None
    return _coerce(raw) if isinstance(raw, dict) else None


def get_post(paths: Paths, post_id: str) -> Post | None:
    try:
        path = _post_file(paths, post_id)
    except ValueError:
        return None
    if not path.is_file():
        return None
    post = _read(path)
    return post if post is not None and post.id == post_id else None


def list_posts(paths: Paths) -> list[Post]:
    """Every post, newest first. Files not named ``<id>.json`` (e.g. a sync
    tool's ``<id> (1).json`` conflict copy) are skipped and logged, never
    merged or deleted -- they stay for manual recovery."""
    d = paths.posts_dir
    if not d.is_dir():
        return []
    out: list[Post] = []
    for path in d.iterdir():
        if path.suffix != ".json" or path.name.startswith("."):
            continue
        if not _ID_RE.match(path.stem):
            log.warning("ignoring unexpected file in posts/: %s (sync conflict copy?)", path.name)
            continue
        post = _read(path)
        if post is not None and post.id == path.stem:
            out.append(post)
    out.sort(key=lambda p: p.created_at, reverse=True)
    return out


def save_post(paths: Paths, post: Post, *, device_id: str = "") -> Post:
    """Atomically write *post* (tmp + ``os.replace``), stamping ``updated_at``."""
    path = _post_file(paths, post.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    post.updated_at = _now()
    if device_id:
        post.updated_by_device = device_id
    tmp = path.with_name(f".{post.id}.json.tmp")
    tmp.write_text(json.dumps(asdict(post), indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return post


def delete_post(paths: Paths, post_id: str) -> None:
    """Remove the post record only. Nothing in robopost is touched (the API
    cannot delete a post) and no photo file is involved."""
    with contextlib.suppress(ValueError):
        _post_file(paths, post_id).unlink(missing_ok=True)


def create_post(
    paths: Paths,
    *,
    title: str,
    photos: list[str],
    caption: str = "",
    source_collection_id: str | None = None,
    device_id: str = "",
) -> Post:
    post = Post(
        title=title,
        photos=photos,
        caption=caption,
        source_collection_id=source_collection_id,
    )
    return save_post(paths, post, device_id=device_id)


def duplicate_post(paths: Paths, post_id: str, *, device_id: str = "") -> Post | None:
    """A fresh ``draft`` with the same content -- the way to re-send a post."""
    src = get_post(paths, post_id)
    if src is None:
        return None
    copy = Post(
        title=f"{src.title} (copy)",
        source_collection_id=src.source_collection_id,
        photos=list(src.photos),
        caption=src.caption,
        first_comment=src.first_comment,
        channel_ids=list(src.channel_ids),
        network_settings={k: dict(v) for k, v in src.network_settings.items()},
    )
    return save_post(paths, copy, device_id=device_id)


# --- the "posted" set --------------------------------------------------------

_posted_cache: dict[Path, tuple[int, frozenset[str]]] = {}


def posted_hashes(paths: Paths) -> frozenset[str]:
    """``pixel_hash`` of every photo in a post that was sent to robopost (as a
    draft or scheduled). Cached on the posts folder's mtime (every save is a
    rename into it, which bumps it)."""
    d = paths.posts_dir
    try:
        mtime = d.stat().st_mtime_ns
    except OSError:
        return frozenset()
    hit = _posted_cache.get(d)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    hashes = frozenset(h for p in list_posts(paths) if p.sent or p.partly_sent for h in p.photos)
    _posted_cache[d] = (mtime, hashes)
    return hashes


def posts_containing(paths: Paths, pixel_hash: str) -> list[Post]:
    return [p for p in list_posts(paths) if pixel_hash in p.photos]


# --- channel cache (non-secret; local/robopost.json) --------------------------


def load_channel_cache(paths: Paths) -> tuple[list[Channel], str | None]:
    try:
        raw = json.loads(paths.robopost_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], None
    if not isinstance(raw, dict):
        return [], None
    rows = raw.get("channels_cache")
    chans = (
        [c for c in (Channel.from_json(r) for r in rows if isinstance(r, dict)) if c]
        if isinstance(rows, list)
        else []
    )
    fetched = raw.get("channels_fetched_at")
    return chans, fetched if isinstance(fetched, str) else None


def save_channel_cache(paths: Paths, channels: list[Channel]) -> None:
    path = paths.robopost_file
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "channels_cache": [c.to_json() for c in channels],
        "channels_fetched_at": _now(),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


# --- validation + payload ----------------------------------------------------


def selected_channels(post: Post, channels: list[Channel]) -> list[Channel]:
    by_id = {c.id: c for c in channels}
    return [by_id[cid] for cid in post.channel_ids if cid in by_id]


def unsent_channels(post: Post, channels: list[Channel]) -> list[Channel]:
    """The chosen channels a send would go to: minus any a sent part reached."""
    done = post.sent_channel_ids()
    return [c for c in selected_channels(post, channels) if c.id not in done]


@dataclass(frozen=True)
class SendGroup:
    """One robopost create: these channels, with uploads sized by ``ig`` (or
    the normal export when ``None``). ``name``: instagram | other | all."""

    name: str
    channels: tuple[Channel, ...]
    ig: IgSpec | None = None

    @property
    def is_instagram(self) -> bool:
        return self.name == "instagram"


def plan_send(post: Post, channels: list[Channel], sizes: list[tuple[int, int]]) -> list[SendGroup]:
    """The creates a send makes, in order. Instagram channels get their own
    create with Instagram-sized uploads when the photos need it; the other
    channels go first, so an Instagram problem never holds back the rest.
    Empty when a partly-sent post has no channels left to go."""
    chosen = unsent_channels(post, channels)
    if post.partly_sent and not chosen:
        return []
    ig = tuple(c for c in chosen if c.network == "instagram")
    other = tuple(c for c in chosen if c.network != "instagram")
    spec = post.ig_spec(sizes) if ig else None
    if ig and other and spec is not None:
        return [SendGroup("other", other), SendGroup("instagram", ig, spec)]
    if ig and not other:
        return [SendGroup("instagram", ig, spec)]
    return [SendGroup("all" if ig else "other", tuple(chosen))]


def selected_networks(post: Post, channels: list[Channel]) -> set[str]:
    return {c.network for c in selected_channels(post, channels)}


def validate(
    post: Post,
    channels: list[Channel],
    mode: SendMode,
    *,
    now: datetime | None = None,
    unavailable: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """``(errors, warnings)`` for sending *post* in *mode*. Any error blocks the
    send. *unavailable* = hashes with no present file on this machine."""
    now = now or utcnow()
    errors: list[str] = []
    warnings: list[str] = []
    n = len(post.photos)
    if not post.editable:
        errors.append("This post was already sent. Duplicate it to send again.")
    if n == 0:
        errors.append("Add at least one photo.")
    if unavailable:
        missing = [h for h in post.photos if h in unavailable]
        if missing:
            errors.append(
                f"{len(missing)} photo(s) have no file on this computer (missing or offline)."
            )
    known = {c.id for c in channels}
    done = post.sent_channel_ids()
    unknown = [cid for cid in post.channel_ids if cid not in known and cid not in done]
    if unknown:
        errors.append(
            f"{len(unknown)} selected channel(s) are no longer in robopost. Refresh channels."
        )
    chosen = unsent_channels(post, channels)
    if mode == "schedule" and not chosen and not post.partly_sent:
        errors.append("Pick at least one channel to schedule.")
    for c in chosen:
        if c.must_reconnect:
            errors.append(f"{c.name} ({c.social_network}) must be reconnected in robopost first.")
    when = post.schedule_dt()
    if mode == "schedule":
        if when is None:
            errors.append("Pick a date and time to schedule.")
        elif when < now + MIN_LEAD:
            errors.append("The schedule time must be at least a couple of minutes in the future.")
    elif when is not None and when < now:
        warnings.append("The schedule time is in the past; the draft is sent without it.")
    networks = {c.network for c in chosen}
    for net in sorted(networks):
        hard = IMAGE_LIMITS_HARD.get(net)
        if hard is not None and n > hard:
            errors.append(f"{net.title()} allows at most {hard} photos; this post has {n}.")
        soft = IMAGE_LIMITS_SOFT.get(net)
        if soft is not None and n > soft:
            warnings.append(f"{net.title()} usually allows at most {soft} photos; this has {n}.")
        tl = TEXT_LIMITS_SOFT.get(net)
        if tl is not None and len(post.caption) > tl:
            warnings.append(
                f"Caption is {len(post.caption)} characters; {net.title()} allows {tl}."
            )
    if "pinterest" in networks:
        if not post.setting("pinterest", "pinTitle").strip():
            errors.append("Pinterest needs a pin title.")
        link = post.setting("pinterest", "destinationLink").strip()
        if link and not re.match(r"^https?://\S+$", link):
            errors.append("Pinterest link must start with http:// or https://.")
    if not post.caption.strip():
        warnings.append("The caption is empty.")
    return errors, warnings


def build_payload(
    post: Post,
    channels: list[Channel],
    mode: SendMode,
    storage_ids: list[str],
    *,
    now: datetime | None = None,
    only: tuple[Channel, ...] | None = None,
) -> dict[str, Any]:
    """The ``POST /v1/scheduled_posts/`` body. Per-network settings are only
    included for networks that have a selected channel. *only* = the channels
    of one :class:`SendGroup` (default: every selected channel)."""
    now = now or utcnow()
    payload: dict[str, Any] = {
        "text": post.caption,
        "image_object_ids": list(storage_ids),
        "is_draft": mode == "draft",
    }
    chosen = list(only) if only is not None else selected_channels(post, channels)
    if chosen:
        payload["channel_ids"] = [c.id for c in chosen]
    when = post.schedule_dt()
    if when is not None and (mode == "schedule" or when > now):
        payload["schedule_at"] = when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if post.first_comment.strip():
        payload["first_comment"] = post.first_comment
    networks = {c.network for c in chosen}
    if "instagram" in networks:
        pt = post.setting("instagram", "postType", "POST")
        payload["instagram_settings"] = {"postType": pt if pt in POST_TYPES else "POST"}
    if "facebook" in networks:
        pt = post.setting("facebook", "postType", "POST")
        payload["facebook_settings"] = {"postType": pt if pt in POST_TYPES[:2] else "POST"}
    if "pinterest" in networks:
        pin: dict[str, str] = {"pinTitle": post.setting("pinterest", "pinTitle").strip()}
        link = post.setting("pinterest", "destinationLink").strip()
        if link:
            pin["destinationLink"] = link
        payload["pinterest_settings"] = pin
    return payload
