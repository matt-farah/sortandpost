"""Send a composed post to robopost: export → upload → create.

Only ever run from an explicit click behind a confirm dialog, off the web event
loop (``run.io_bound``), and only while this machine holds the write lock.

Double-post safety: robopost's API cannot read, edit or cancel a post, so the
create call is never retried. The post is marked ``sending`` *before* it; if the
create call's outcome is unknown (network error / timeout / 5xx, or the process dies)
the post stays ``sending`` and the UI asks the user to check robopost and say
whether it went through (:func:`resolve_interrupted`). A 4xx reply means
nothing was created, so the post becomes ``failed`` and
can be retried; uploads already done are reused through ``media_cache``.
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
from sortandpost.post_export import export_jpeg, export_key, resolve_sources
from sortandpost.posts import (
    Post,
    SendMode,
    build_payload,
    get_post,
    load_channel_cache,
    save_post,
    validate,
)
from sortandpost.robopost import RobopostClient, RobopostError, redact
from sortandpost.secrets_store import get_robopost_key
from sortandpost.sync.clock import utcnow

log = logging.getLogger(__name__)

# (src, dest) pairs -> exported; raises on the first failure.
Exporter = Callable[[list[tuple[str, str]], int, int], None]
ClientFactory = Callable[[str], RobopostClient]

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


def pool_exporter(jobs: list[tuple[str, str]], long_edge: int, quality: int) -> None:
    """Default :data:`Exporter`: decode + re-encode in worker processes (hard
    constraint #7). ``spawn`` so it is safe from the app's threads."""
    if not jobs:
        return
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=min(2, len(jobs)), mp_context=ctx) as pool:
        futures = [pool.submit(export_jpeg, src, dest, long_edge, quality) for src, dest in jobs]
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
    client_factory: ClientFactory = RobopostClient,
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
    key, _source = get_robopost_key()
    if not key:
        return SendResult(False, "No robopost API key. Add one in Settings → Posting.", post)
    channels, _ = load_channel_cache(paths)
    sources = resolve_sources(engine, post.photos)
    unavailable = {h for h, p in sources.items() if p is None}
    errors, _warnings = validate(post, channels, mode, now=now, unavailable=unavailable)
    if errors:
        return SendResult(False, errors[0], post)

    post.status = "sending"
    post.sending_mode = mode
    save_post(paths, post, device_id=settings.device_id)

    cfg = settings.config.posting
    ekey = export_key(cfg.export_long_edge, cfg.export_quality)
    out_dir = paths.post_exports_dir / post.id
    need = [
        h
        for h in post.photos
        if post.media_cache.get(h, {}).get("export_key") != ekey
        # entries from before the storage_object_id fix hold an unusable id
        or not post.media_cache[h].get("storage_object_id")
    ]
    jobs: dict[str, Path] = {
        h: out_dir / f"{i:02d}_{h[:8]}.jpg" for i, h in enumerate(post.photos) if h in need
    }
    phase = "export"
    try:
        exporter(
            [(str(sources[h]), str(dest)) for h, dest in jobs.items()],
            cfg.export_long_edge,
            cfg.export_quality,
        )
        phase = "upload"
        with client_factory(key) as rp:
            for h, dest in jobs.items():
                storage_id = rp.upload_media(dest)
                post.media_cache[h] = {
                    "export_key": ekey,
                    "storage_object_id": storage_id,
                    "uploaded_at": utcnow().isoformat(),
                }
                save_post(paths, post, device_id=settings.device_id)
            storage_ids = [post.media_cache[h]["storage_object_id"] for h in post.photos]
            payload = build_payload(post, channels, mode, storage_ids, now=now)
            phase = "create"
            ids = rp.create_post(payload)
    except RobopostError as exc:
        return _failed(settings, post, mode, phase, exc.friendly, unknown=_unknown(phase, exc))
    except Exception as exc:
        log.exception("sending post %s failed during %s", post.id, phase)
        msg = redact(f"{type(exc).__name__}: {exc}", key)
        return _failed(settings, post, mode, phase, msg, unknown=phase == "create")

    post.status = "sent_draft" if mode == "draft" else "scheduled"
    post.sending_mode = None
    post.log.append(_log_entry(settings, "send", mode=mode, robopost_post_ids=ids))
    save_post(paths, post, device_id=settings.device_id)
    what = "a draft" if mode == "draft" else f"{max(1, len(ids))} scheduled post(s)"
    return SendResult(True, f"Sent to robopost as {what}.", post)


def _unknown(phase: str, exc: RobopostError) -> bool:
    """Did the create call maybe succeed? A network failure (no HTTP reply) or
    a 5xx during create leaves the outcome unknown -- robopost may have created
    the post before failing. Only a 4xx is a definite "nothing was created"."""
    return phase == "create" and (exc.status == 0 or exc.status >= 500)


def _failed(
    settings: Settings, post: Post, mode: SendMode, phase: str, message: str, *, unknown: bool
) -> SendResult:
    if unknown:
        # Leave it `sending`: the post may exist in robopost. The user settles it.
        post.log.append(_log_entry(settings, "send_unknown", mode=mode, phase=phase, error=message))
        save_post(settings.paths, post, device_id=settings.device_id)
        return SendResult(
            False,
            "robopost didn't answer, so the post may or may not have been created. "
            "Check robopost, then mark this post as sent or not sent.",
            post,
        )
    post.status = "failed"
    post.sending_mode = None
    post.log.append(_log_entry(settings, "send_failed", mode=mode, phase=phase, error=message))
    save_post(settings.paths, post, device_id=settings.device_id)
    return SendResult(False, message, post)


def resolve_interrupted(settings: Settings, post_id: str, *, was_sent: bool) -> Post | None:
    """Settle a post stuck in ``sending`` (crash / unknown outcome) from the
    user's answer after checking robopost. A no-op while a send is in flight."""
    post = get_post(settings.paths, post_id)
    if post is None or post.status != "sending" or is_in_flight(post_id):
        return post
    mode = post.sending_mode or "draft"
    if was_sent:
        post.status = "sent_draft" if mode == "draft" else "scheduled"
    else:
        post.status = "failed"
    post.sending_mode = None
    post.log.append(_log_entry(settings, "resolved", mode=mode, was_sent=was_sent))
    return save_post(settings.paths, post, device_id=settings.device_id)
