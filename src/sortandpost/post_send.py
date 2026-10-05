"""Send a composed post through its posting back-end: export → upload → create.

Only ever run from an explicit click behind a confirm dialog, off the web event
loop (``run.io_bound``), and only while this machine holds the write lock. The
back-end is :func:`sortandpost.posting.provider_for` the post; nothing here
depends on which one it is.

Double-post safety (every provider): the create call is never retried. The
post is marked ``sending`` *before* it; if the create call's outcome is unknown
(network error / timeout / 5xx, or the process dies) the post stays ``sending``
and the UI asks the user to check the provider and say whether it went through
(:func:`resolve_interrupted`). A 4xx reply means
nothing was created, so the post becomes ``failed`` and
can be retried; uploads already done (to the same provider, with the same
export settings) are reused through ``media_cache``. A provider's known rate
limit is checked for the whole send before the first call.

A post that mixes Instagram with other channels may be sent as two creates
(:func:`sortandpost.posts.plan_send`), each tracked as its own part: the
other channels first, then Instagram. A failed or unknown first create stops
the send, so the second is never started on top of a part in doubt. A retry
only goes to channels no sent part has reached.
"""

from __future__ import annotations

import logging
import multiprocessing
import shutil
import threading
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine

from sortandpost.config import Settings
from sortandpost.post_export import (
    IgSpec,
    export_jpeg,
    export_key,
    resolve_sources,
    upright_sizes,
)
from sortandpost.posting import active_provider, limiter, open_client, provider_for
from sortandpost.posting.client import PostingClient, redact
from sortandpost.posting.model import MediaRef, PostingError
from sortandpost.posting.spec import ProviderSpec
from sortandpost.posts import (
    Post,
    SendGroup,
    SendMode,
    get_post,
    load_channel_cache,
    make_request,
    plan_send,
    save_post,
    validate,
)
from sortandpost.secrets_store import get_key
from sortandpost.sync.clock import utcnow

log = logging.getLogger(__name__)

# (src, dest) pairs, long edge, quality, Instagram canvas -> exported; raises on
# the first failure.
Exporter = Callable[[list[tuple[str, str]], int, int, IgSpec | None], None]
# (settings, provider, api key) -> client; tests plug in a mock transport
ClientFactory = Callable[[Settings, ProviderSpec, str], PostingClient]

_IN_FLIGHT: set[str] = set()
_IN_FLIGHT_LOCK = threading.Lock()


def is_in_flight(post_id: str) -> bool:
    """True while *this process* is sending *post_id* (a ``sending`` status
    with no send in flight here means it was interrupted)."""
    with _IN_FLIGHT_LOCK:
        return post_id in _IN_FLIGHT


@dataclass
class SendResult:
    ok: bool
    message: str
    post: Post | None = None


def pool_exporter(
    jobs: list[tuple[str, str]], long_edge: int, quality: int, ig: IgSpec | None = None
) -> None:
    """Default :data:`Exporter`: decode + re-encode in worker processes (hard
    constraint #7). ``spawn`` so it is safe from the app's threads."""
    if not jobs:
        return
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=min(2, len(jobs)), mp_context=ctx) as pool:
        futures = [
            pool.submit(export_jpeg, src, dest, long_edge, quality, ig) for src, dest in jobs
        ]
        for fut in futures:
            fut.result()


def _log_entry(settings: Settings, action: str, **extra: Any) -> dict[str, Any]:
    return {"ts": utcnow().isoformat(), "device_id": settings.device_id, "action": action, **extra}


def send_post(
    settings: Settings,
    engine: Engine,
    post_id: str,
    mode: SendMode,
    *,
    writable: bool,
    exporter: Exporter = pool_exporter,
    client_factory: ClientFactory = open_client,
    now: datetime | None = None,
) -> SendResult:
    paths = settings.paths
    if not writable:
        return SendResult(False, "This machine is read-only right now; posts can't be sent.")
    with _IN_FLIGHT_LOCK:
        if post_id in _IN_FLIGHT:
            return SendResult(False, "This post is already being sent.")
        _IN_FLIGHT.add(post_id)
    try:
        return _send(settings, engine, post_id, mode, exporter, client_factory, now)
    finally:
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT.discard(post_id)
        # Never leave exports behind on failure either; they're cheap to redo.
        shutil.rmtree(paths.post_exports_dir / post_id, ignore_errors=True)


def _send(
    settings: Settings,
    engine: Engine,
    post_id: str,
    mode: SendMode,
    exporter: Exporter,
    client_factory: ClientFactory,
    now: datetime | None,
) -> SendResult:
    paths = settings.paths
    post = get_post(paths, post_id)
    if post is None:
        return SendResult(False, "Post not found.")
    if post.status == "sending":
        return SendResult(False, "This post's last send isn't settled yet.", post)
    spec = provider_for(post, settings)
    key, _source = get_key(spec)
    if not key:
        return SendResult(False, f"No {spec.name} API key. Add one in Settings → Posting.", post)
    channels, _ = load_channel_cache(paths, spec.key)
    sources = resolve_sources(engine, post.photos)
    unavailable = {h for h, p in sources.items() if p is None}
    errors, _warnings = validate(
        post,
        channels,
        mode,
        spec,
        now=now,
        unavailable=unavailable,
        active=active_provider(settings).key,
    )
    if errors:
        return SendResult(False, errors[0], post)
    sizes = upright_sizes(engine, post.photos)
    groups = plan_send(post, channels, [sizes[h] for h in post.photos if h in sizes])
    wait = limiter(settings, spec).check("create", len(groups), now=now)
    if wait is not None:
        lim = limiter(settings, spec).limit("create")
        return SendResult(
            False,
            f"{spec.name} allows {lim.describe('new posts') if lim else 'only so many posts'}; "
            f"this send needs {len(groups)}. Try again in {_wait_text(wait)}.",
            post,
        )
    if not groups:
        # A partly-sent post whose remaining channels were all unticked.
        _settle(post)
        post.log.append(_log_entry(settings, "finished", mode=mode))
        save_post(paths, post, device_id=settings.device_id)
        return SendResult(True, "Nothing left to send; the post is marked as sent.", post)

    cfg = settings.config.posting
    out_dir = paths.post_exports_dir / post.id
    created: list[SendGroup] = []
    with client_factory(settings, spec, key) as api:
        for group in groups:
            # The part is `sending` (saved) from before its export until its
            # create answers, so a crash anywhere in between is settled by hand.
            part = _start_part(post, group, mode, spec)
            save_post(paths, post, device_id=settings.device_id)
            phase = "export"
            try:
                media = _upload(
                    settings,
                    post,
                    group,
                    sources,
                    out_dir,
                    exporter,
                    api,
                    cfg.export_long_edge,
                    cfg.export_quality,
                )
                req = make_request(post, channels, mode, media, spec, now=now, only=group.channels)
                phase = "create"
                ids = api.create_post(req)
            except PostingError as exc:
                return _failed(
                    settings,
                    post,
                    part,
                    mode,
                    phase,
                    exc.friendly,
                    unknown=_unknown(phase, exc),
                    created=created,
                    spec=spec,
                )
            except Exception as exc:
                log.exception("sending post %s (%s) failed during %s", post.id, group.name, phase)
                msg = redact(f"{type(exc).__name__}: {exc}", key)
                return _failed(
                    settings,
                    post,
                    part,
                    mode,
                    phase,
                    msg,
                    unknown=phase == "create",
                    created=created,
                    spec=spec,
                )
            part["status"] = "sent"
            part["remote_post_ids"] = ids
            post.log.append(
                _log_entry(
                    settings,
                    "send",
                    mode=mode,
                    part=group.name,
                    provider=spec.key,
                    remote_post_ids=ids,
                )
            )
            created.append(group)
            if group is groups[-1]:
                _settle(post)
                save_post(paths, post, device_id=settings.device_id)
            # else: saved with the next part's `sending`, in one write, so no
            # moment on disk is `sending` with nothing in doubt.

    if len(created) > 1:
        what = "drafts" if mode == "draft" else "scheduled posts"
        msg = f"Sent to {spec.name} as {len(created)} {what}: {_names(created)}."
    else:
        msg = (
            f"Sent to {spec.name} as a draft." if mode == "draft" else f"Scheduled in {spec.name}."
        )
    return SendResult(True, msg, post)


def _wait_text(seconds: int) -> str:
    minutes = -(-seconds // 60)
    return "a minute" if minutes <= 1 else f"{minutes} minutes"


def _upload(
    settings: Settings,
    post: Post,
    group: SendGroup,
    sources: dict[str, str | None],
    out_dir: Path,
    exporter: Exporter,
    api: PostingClient,
    long_edge: int,
    quality: int,
) -> list[MediaRef]:
    """Export + upload what this group's cache doesn't already hold; returns
    the uploads in photo order. A cache entry counts only for the same
    provider and export settings. Instagram-sized uploads are cached under
    ``<hash>#ig`` so they never stand in for the normal export (or vice versa)."""
    ekey = export_key(long_edge, quality, group.ig)
    suffix = "#ig" if group.ig is not None else ""
    provider = api.spec.key

    def cached(h: str) -> dict[str, str]:
        return post.media_cache.get(h + suffix, {})

    def usable(h: str) -> bool:
        e = cached(h)
        return (
            e.get("export_key") == ekey
            and e.get("provider") == provider
            and bool(e.get("media_id"))
        )

    tag = "_ig" if group.ig is not None else ""
    jobs: dict[str, Path] = {
        h: out_dir / f"{i:02d}_{h[:8]}{tag}.jpg" for i, h in enumerate(post.photos) if not usable(h)
    }
    exporter(
        [(str(sources[h]), str(dest)) for h, dest in jobs.items()], long_edge, quality, group.ig
    )
    for h, dest in jobs.items():
        ref = api.upload_media(dest)
        post.media_cache[h + suffix] = {
            "export_key": ekey,
            "provider": provider,
            "media_id": ref.id,
            "media_path": ref.path,
            "uploaded_at": utcnow().isoformat(),
        }
        save_post(settings.paths, post, device_id=settings.device_id)
    return [MediaRef(cached(h)["media_id"], cached(h).get("media_path", "")) for h in post.photos]


def _start_part(post: Post, group: SendGroup, mode: SendMode, spec: ProviderSpec) -> dict[str, Any]:
    part: dict[str, Any] = {
        "group": group.name,
        "status": "sending",
        "mode": mode,
        "provider": spec.key,
        "channel_ids": [c.id for c in group.channels],
        "remote_post_ids": [],
        "started_at": utcnow().isoformat(),
    }
    post.parts.append(part)
    post.status = "sending"
    post.sending_mode = mode
    post.provider = spec.key  # provider_for kept a partly-sent post on its own one
    return part


def _settle(post: Post) -> None:
    """Recompute ``status`` once no part is in doubt: sent when every chosen
    channel has the post (a no-channel draft: when a part was sent), else
    ``failed`` (retryable; channels already reached are skipped)."""
    sent = [pt for pt in post.parts if pt["status"] == "sent"]
    remaining = [c for c in post.channel_ids if c not in post.sent_channel_ids()]
    post.sending_mode = None
    if sent and not remaining:
        scheduled = any(pt["mode"] == "schedule" for pt in sent)
        post.status = "scheduled" if scheduled else "sent_draft"
    else:
        post.status = "failed"


def _names(groups: list[SendGroup]) -> str:
    return " + ".join(
        "Instagram" if g.is_instagram else ("other channels" if g.name == "other" else "all")
        for g in groups
    )


def _unknown(phase: str, exc: PostingError) -> bool:
    """Did the create call maybe succeed? A network failure (no HTTP reply) or
    a 5xx during create leaves the outcome unknown -- the provider may have
    created the post before failing. Only a 4xx is a definite "nothing was
    created"."""
    return phase == "create" and (exc.status == 0 or exc.status >= 500)


def _failed(
    settings: Settings,
    post: Post,
    part: dict[str, Any],
    mode: SendMode,
    phase: str,
    message: str,
    *,
    unknown: bool,
    created: list[SendGroup],
    spec: ProviderSpec,
) -> SendResult:
    # Only an Instagram part can follow another (plan_send), so a part already
    # created here is the other channels'.
    done = "The other channels' post went through. " if created else ""
    which = "The Instagram post" if created else "The post"
    if unknown:
        # Leave this part (and the post) `sending`: it may exist in robopost.
        post.log.append(
            _log_entry(
                settings, "send_unknown", mode=mode, part=part["group"], phase=phase, error=message
            )
        )
        save_post(settings.paths, post, device_id=settings.device_id)
        return SendResult(
            False,
            f"{done}{spec.name} didn't answer, so {which.lower()} may or may not have been "
            f"created. Check {spec.name}, then mark it as sent or not sent.",
            post,
        )
    part["status"] = "failed"
    _settle(post)
    post.log.append(
        _log_entry(
            settings, "send_failed", mode=mode, part=part["group"], phase=phase, error=message
        )
    )
    save_post(settings.paths, post, device_id=settings.device_id)
    if created:
        message = f"{done}{which} failed: {message} Retrying sends only what's left."
    return SendResult(False, message, post)


def resolve_interrupted(settings: Settings, post_id: str, *, was_sent: bool) -> Post | None:
    """Settle a post stuck in ``sending`` (crash / unknown outcome) from the
    user's answer after checking the provider. Only the part in doubt is settled;
    a part already sent stays sent. A no-op while a send is in flight."""
    post = get_post(settings.paths, post_id)
    if post is None or post.status != "sending" or is_in_flight(post_id):
        return post
    part = post.sending_part()
    mode = (part or {}).get("mode") or post.sending_mode or "draft"
    if part is not None:
        part["status"] = "sent" if was_sent else "failed"
        _settle(post)
    elif post.parts:
        _settle(post)  # nothing in doubt
    else:  # a send from before parts existed
        if was_sent:
            post.status = "sent_draft" if mode == "draft" else "scheduled"
        else:
            post.status = "failed"
        post.sending_mode = None
    post.log.append(
        _log_entry(
            settings,
            "resolved",
            mode=mode,
            was_sent=was_sent,
            **({"part": part["group"]} if part else {}),
        )
    )
    return save_post(settings.paths, post, device_id=settings.device_id)
