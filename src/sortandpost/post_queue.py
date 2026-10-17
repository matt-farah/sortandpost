"""The background sender: every confirmed send goes through here.

Send / Schedule in the composer *queues* the post (status ``queued``, see
:func:`queue_post`) and wakes one worker thread, which runs
:func:`sortandpost.post_send.send_post` off the web event loop, so the page
doesn't wait on uploads and the user can move on. Each tab's status bar polls
:func:`events_since` and toasts the outcome.

"Send when online" (``Post.queue_wait_online``) uses the same path: before
sending such a post the worker probes the provider (``check_auth``). When the
provider is unreachable the post stays queued with ``queue_note="offline"``,
and a send that loses the connection before its create call goes back to
the queue (:mod:`sortandpost.post_send`). The worker tries again every
:data:`TICK_SEC`. An immediate send isn't probed. It fails fast offline and
the toast offers "Send when online".

Rules:

- Only the machine that holds the write lock sends (``writable()``, checked
  before every post), whichever machine queued the post. A post queued by
  *another* device is left alone until this machine has been the editor for
  :data:`FOREIGN_SETTLE_SEC`, so the sync tool can deliver that device's last
  writes first (a ``sending`` it wrote must not be read as a still-``queued``
  post, which would double-post).
- A queued **scheduled** post whose time has passed is *held*
  (``queue_note="time passed"``), never sent at a time the user didn't choose.
  The user picks a new time after "Cancel queue" (:func:`unqueue_post`).
- An unknown create outcome stays ``sending`` for the user to settle, exactly
  as before. Only ``queued`` posts are ever picked up, so it is never re-sent.
- A send refused for good before it starts (a validation error at send time)
  leaves the queue as ``failed``, with the reason in its history.
- Handover and shutdown wait for a send in flight (:func:`wait_idle`).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.engine import Engine

from sortandpost.config import Settings
from sortandpost.post_export import resolve_sources
from sortandpost.post_send import (
    ClientFactory,
    Exporter,
    SendResult,
    clear_queue,
    is_in_flight,
    pool_exporter,
    send_post,
)
from sortandpost.posting import active_provider, open_client, provider_for
from sortandpost.posting.model import PostingError
from sortandpost.posting.spec import ProviderSpec
from sortandpost.posts import (
    MIN_LEAD,
    Post,
    SendMode,
    get_post,
    list_posts,
    load_channel_cache,
    save_post,
    validate,
)
from sortandpost.secrets_store import get_key
from sortandpost.sync.clock import utcnow

log = logging.getLogger(__name__)

TICK_SEC = 30  # how often the worker looks at the queue (and re-probes while offline)
FOREIGN_SETTLE_SEC = 90  # be the editor this long before sending another device's posts
NOTE_OFFLINE = "offline"
NOTE_TIME_PASSED = "time passed"
_MAX_EVENTS = 50

_WAKE = threading.Event()
_STOP = threading.Event()
_DRAIN_LOCK = threading.Lock()  # one drain at a time; held for a whole send
_EVENTS_LOCK = threading.Lock()
_EVENTS: list[QueueEvent] = []
_SEQ = 0
_STATUS: dict[str, int] = {"queued": 0, "held": 0}
_SENDING: dict[str, str] = {}  # post id -> title, while the worker sends it


@dataclass(frozen=True)
class QueueEvent:
    """One outcome for the tabs to toast."""

    seq: int
    post_id: str
    title: str
    ok: bool
    message: str
    offline: bool = False  # an immediate send couldn't reach the provider
    held: bool = False  # a scheduled post's time passed while it waited


# --------------------------------------------------------------------------- #
# queue / unqueue (UI actions)
# --------------------------------------------------------------------------- #
def queue_post(
    settings: Settings,
    engine: Engine,
    post_id: str,
    mode: SendMode,
    *,
    wait_online: bool,
    writable: bool,
    now: datetime | None = None,
) -> SendResult:
    """Queue a confirmed send, checked the same way the confirm dialog
    checked it, and wake the worker."""
    post = get_post(settings.paths, post_id)
    if post is None:
        return SendResult(False, "Post not found.")
    if not writable:
        return SendResult(False, "This machine is read-only right now; posts can't be sent.", post)
    if not post.editable:
        what = "already queued" if post.queued else "not a draft"
        return SendResult(False, f"This post is {what}.", post)
    spec = provider_for(post, settings)
    channels, _ = load_channel_cache(settings.paths, spec.key)
    sources = resolve_sources(engine, post.photos)
    errors, _warnings = validate(
        post,
        channels,
        mode,
        spec,
        now=now,
        unavailable={h for h, p in sources.items() if p is None},
        active=active_provider(settings).key,
    )
    if errors:
        return SendResult(False, errors[0], post)
    post.status = "queued"
    post.queued_mode = mode
    post.queued_at = (now or utcnow()).isoformat()
    post.queued_by = settings.device_id
    post.queue_wait_online = wait_online
    post.queue_note = ""
    post.log.append(_log_entry(settings, "queued", mode=mode, wait_online=wait_online))
    save_post(settings.paths, post, device_id=settings.device_id)
    _refresh_status(settings)
    kick()
    if wait_online:
        msg = f"Queued — it goes to {spec.name} as soon as it can be reached."
    else:
        msg = f"Sending to {spec.name} in the background…"
    return SendResult(True, msg, post)


def unqueue_post(settings: Settings, post_id: str) -> Post | None:
    """ "Cancel queue": back to an editable post. A no-op once the worker has
    started sending it."""
    post = get_post(settings.paths, post_id)
    if post is None or not post.queued or is_in_flight(post_id):
        return post
    clear_queue(post)
    post.status = "failed" if post.partly_sent else "draft"
    post.log.append(_log_entry(settings, "unqueued"))
    save_post(settings.paths, post, device_id=settings.device_id)
    _refresh_status(settings)
    return post


# --------------------------------------------------------------------------- #
# the drain
# --------------------------------------------------------------------------- #
def drain_queue(
    settings: Settings,
    engine: Engine,
    *,
    writable: Callable[[], bool],
    foreign_ok: bool = True,
    exporter: Exporter = pool_exporter,
    client_factory: ClientFactory = open_client,
    now: datetime | None = None,
) -> list[QueueEvent]:
    """Send what can go now. Immediate sends first, then "when online" ones,
    oldest first within each. Returns the events it published."""
    out: list[QueueEvent] = []
    with _DRAIN_LOCK:
        queued = [p for p in list_posts(settings.paths) if p.queued]
        queued.sort(key=lambda p: (p.queue_wait_online, p.queued_at))
        reachable: dict[str, bool] = {}
        for post in queued:
            if _STOP.is_set() or not writable():
                break
            if is_in_flight(post.id):
                continue
            if post.queued_by != settings.device_id and not foreign_ok:
                continue
            ev = _drain_one(settings, engine, post, reachable, exporter, client_factory, now)
            if ev is not None:
                out.append(ev)
        _refresh_status(settings)
    return out


def _drain_one(
    settings: Settings,
    engine: Engine,
    post: Post,
    reachable: dict[str, bool],
    exporter: Exporter,
    client_factory: ClientFactory,
    now: datetime | None,
) -> QueueEvent | None:
    mode: SendMode = "schedule" if post.queued_mode == "schedule" else "draft"
    spec = provider_for(post, settings)
    if mode == "schedule":
        when = post.schedule_dt()
        if when is None or when < (now or utcnow()) + MIN_LEAD:
            if _note(settings, post, NOTE_TIME_PASSED):
                return _publish(
                    post,
                    False,
                    f"“{post.title}” wasn't sent: its scheduled time passed while it was "
                    "queued. Cancel the queue and pick a new time.",
                    held=True,
                )
            return None
    if post.queue_wait_online:
        key, _ = get_key(spec)
        if not key:
            _note(settings, post, f"no {spec.name} API key on {settings.hostname}")
            return None
        if spec.key not in reachable:
            reachable[spec.key] = _probe(settings, spec, key, client_factory)
        if not reachable[spec.key]:
            _note(settings, post, NOTE_OFFLINE)
            return None
    _SENDING[post.id] = post.title
    try:
        res = send_post(
            settings,
            engine,
            post.id,
            mode,
            writable=True,  # checked by the caller just before
            exporter=exporter,
            client_factory=client_factory,
            now=now,
        )
    finally:
        _SENDING.pop(post.id, None)
    if res.requeued:
        reachable[spec.key] = False  # the connection dropped: leave the rest for later
        return None
    fresh = get_post(settings.paths, post.id)
    if not res.ok and fresh is not None and fresh.queued:
        # send_post refused before starting: the post is untouched.
        if res.transient and fresh.queue_wait_online:
            _note(settings, fresh, res.message)
            return None
        clear_queue(fresh)
        fresh.status = "failed"
        fresh.log.append(_log_entry(settings, "send_failed", mode=mode, error=res.message))
        save_post(settings.paths, fresh, device_id=settings.device_id)
    return _publish(post, res.ok, res.message, offline=res.offline)


def _probe(settings: Settings, spec: ProviderSpec, key: str, factory: ClientFactory) -> bool:
    """Can *spec* be reached? Only "no reply" counts as offline; a reply of
    any kind (even a refused key) means the send itself should report it."""
    try:
        with factory(settings, spec, key) as api:
            api.check_auth()
    except PostingError as exc:
        return exc.status != 0
    except Exception:
        log.exception("probing %s failed", spec.name)
        return False
    return True


def _note(settings: Settings, post: Post, note: str) -> bool:
    """Record why *post* is still waiting. Only a change is written, so a
    long offline spell doesn't rewrite the synced file every tick. True if it
    changed."""
    if post.queue_note == note:
        return False
    post.queue_note = note
    if note == NOTE_TIME_PASSED:
        post.log.append(_log_entry(settings, "queue_held", mode=post.queued_mode))
    save_post(settings.paths, post, device_id=settings.device_id)
    return True


def _log_entry(settings: Settings, action: str, **extra: object) -> dict[str, object]:
    return {"ts": utcnow().isoformat(), "device_id": settings.device_id, "action": action, **extra}


# --------------------------------------------------------------------------- #
# events + status for the UI
# --------------------------------------------------------------------------- #
def _publish(
    post: Post, ok: bool, message: str, *, offline: bool = False, held: bool = False
) -> QueueEvent:
    global _SEQ
    with _EVENTS_LOCK:
        _SEQ += 1
        ev = QueueEvent(_SEQ, post.id, post.title, ok, message, offline=offline, held=held)
        _EVENTS.append(ev)
        del _EVENTS[:-_MAX_EVENTS]
    return ev


def last_seq() -> int:
    with _EVENTS_LOCK:
        return _SEQ


def events_since(seq: int) -> list[QueueEvent]:
    with _EVENTS_LOCK:
        return [e for e in _EVENTS if e.seq > seq]


def _refresh_status(settings: Settings) -> None:
    queued = [p for p in list_posts(settings.paths) if p.queued]
    _STATUS["queued"] = len(queued)
    _STATUS["held"] = sum(p.queue_note == NOTE_TIME_PASSED for p in queued)


def status_text() -> str:
    """For the status bar: "" when the queue is empty."""
    sending = list(_SENDING.values())
    if sending:
        return f"sending “{sending[0]}”…"
    n, held = _STATUS["queued"], _STATUS["held"]
    if not n:
        return ""
    text = f"{n} post{'s' if n != 1 else ''} queued"
    return text + (f" ({held} need a new time)" if held else "")


# --------------------------------------------------------------------------- #
# the worker
# --------------------------------------------------------------------------- #
def kick() -> None:
    """Look at the queue now (a new send, "Try now", becoming the editor)."""
    _WAKE.set()


def start(settings: Settings, engine: Engine, *, writable: Callable[[], bool]) -> None:
    _STOP.clear()
    try:
        _refresh_status(settings)
    except Exception:
        log.exception("reading the post queue failed")
    threading.Thread(
        target=_run, args=(settings, engine, writable), name="sortandpost-post-queue", daemon=True
    ).start()


def stop() -> None:
    _STOP.set()
    _WAKE.set()


def wait_idle(timeout: float) -> bool:
    """Wait for a send in flight to finish (handover, shutdown). True if the
    worker is idle now."""
    if not _DRAIN_LOCK.acquire(timeout=timeout):
        log.warning("a post is still sending after %.0fs", timeout)
        return False
    _DRAIN_LOCK.release()
    return True


def _run(settings: Settings, engine: Engine, writable: Callable[[], bool]) -> None:
    editor_since: float | None = None
    while not _STOP.is_set():
        try:
            if writable():
                if editor_since is None:
                    editor_since = time.monotonic()
                drain_queue(
                    settings,
                    engine,
                    writable=writable,
                    foreign_ok=time.monotonic() - editor_since >= FOREIGN_SETTLE_SEC,
                )
            else:
                editor_since = None
                _refresh_status(settings)
        except Exception:  # a bad tick must not kill the worker
            log.exception("post queue tick failed")
        _WAKE.wait(TICK_SEC)
        _WAKE.clear()
