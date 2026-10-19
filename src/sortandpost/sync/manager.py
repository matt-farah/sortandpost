"""``SyncManager`` — the startup sequence, heartbeat, and publish loop that make
``sync/`` a safe cross-machine source of truth (docs/architecture.md → "Sync model").

Lifecycle:

* :meth:`start` — acquire the lock (or fall to read-only), merge any conflict
  copies, reconcile the working copy against the published generation,
  ``quick_check`` (restore a snapshot on failure), then spin up the background
  thread that heartbeats the lock and publishes when the DB is dirty & idle.
* :meth:`refresh` — re-check the lock; a read-only instance promotes itself once
  the holder's lock is free or stale.
* :meth:`stop` — final publish, release the lock (rewritten as a handoff to
  anyone, so the next writer waits for this publish to sync), join the thread.

Editing checkout (an always-on writer such as a Docker server would otherwise
keep everyone else read-only forever; :mod:`sortandpost.sync.handoff`):

* requester — :meth:`request_edit` writes its own request file; :meth:`refresh`
  keeps it fresh, notices a "Keep editing" answer, and takes the lock once it is
  granted to us *and* our ``sync/sortandpost.db`` has caught up to the grant's
  generation.
* holder — :meth:`incoming_request` reports the oldest live request and the
  seconds left of its grace period; :meth:`deny` answers "Keep editing";
  :meth:`maybe_hand_over` publishes and rewrites the lock as a grant once the
  grace is over (the caller stops its indexer first).
* a read-only machine doesn't reclaim a free lock while another machine's
  request is pending, so the requester gets it.

Every timestamp goes through an injected clock so the whole thing is testable
without real time.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import logging
import socket
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Concatenate

from sqlalchemy.engine import Engine
from sqlmodel import Session

from sortandpost.config import Settings
from sortandpost.db import make_engine
from sortandpost.db.meta import SCHEMA_VERSION, SCHEMA_VERSION_KEY, bump_generation, set_meta
from sortandpost.sync.clock import Clock, isoformat, utcnow
from sortandpost.sync.handoff import (
    EditRequest,
    clear_request,
    new_request,
    read_requests,
    write_request,
)
from sortandpost.sync.lock import (
    LockInfo,
    LockStatus,
    handoff_lock,
    is_stale,
    lock_status,
    machine_name,
    new_lock,
    read_lock,
    write_lock,
)
from sortandpost.sync.merge import MergeSummary, find_conflict_copies, merge_all
from sortandpost.sync.publish import (
    atomic_publish,
    atomic_replace,
    pull_published,
    quick_check,
    read_generation,
    read_meta_int,
    sha256_file,
)
from sortandpost.sync.snapshots import (
    newest_snapshot,
    prune_snapshots,
    restore_snapshot,
    snapshot_time,
    write_snapshot,
)

log = logging.getLogger(__name__)

# After a failed takeover (see _become_writer), wait this long before trying again.
_PROMOTE_RETRY_SEC = 60
# While writable, look for conflict copies that arrived after we took the lock
# this often, and leave one alone until it has been still this long (the sync
# tool may still be writing it).
_LIVE_MERGE_EVERY_SEC = 60
_LIVE_MERGE_SETTLE_SEC = 30


class NeedsUpdate(RuntimeError):
    """The library's schema is newer than this app's (``meta.schema_version``)."""


class WriteMode(StrEnum):
    writable = "writable"
    read_only = "read-only"


def _locked[**P, R](
    fn: Callable[Concatenate[SyncManager, P], R],
) -> Callable[Concatenate[SyncManager, P], R]:
    """Serialize a public method: the lock watch, the heartbeat thread and UI
    clicks all call in, and a handover must not interleave with a publish."""

    def wrapper(self: SyncManager, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._mutex:
            return fn(self, *args, **kwargs)

    functools.update_wrapper(wrapper, fn)
    return wrapper


class SyncManager:
    def __init__(
        self,
        settings: Settings,
        *,
        engine: Engine | None = None,
        clock: Clock = utcnow,
    ) -> None:
        self.settings = settings
        self.paths = settings.paths
        self.cfg = settings.config.sync
        self.device_id = settings.device_id
        self.hostname = socket.gethostname()
        self._clock = clock
        self._external_engine = engine
        self._engine: Engine | None = engine

        self.mode: WriteMode = WriteMode.read_only
        self.lock_holder: LockInfo | None = None
        self.restored_from_snapshot: Path | None = None
        self.last_merge: MergeSummary | None = None
        self.quarantined: list[Path] = []  # conflict copies set aside as unmergeable
        # why the last attempt to become the writer failed (we stayed read-only)
        self.writer_error: str | None = None
        # ...because another computer's newer app upgraded the library's schema
        self.needs_update = False
        self._promote_retry_at: datetime | None = None

        self._stop = threading.Event()
        self._mutex = threading.RLock()
        self._thread: threading.Thread | None = None
        self._publishes_since_snapshot = 0
        self._publish_count = 0
        self._last_publish_hash: str | None = None
        self._last_publish_at: datetime | None = None
        self._next_live_merge: datetime | None = None

        # editing checkout -- requester side
        self.my_request: EditRequest | None = None
        self._request_beat_at: datetime | None = None
        self.denied_by: str | None = None  # the holder answered "Keep editing"
        # (our sync/ DB generation, the handoff's) while a handoff to us waits
        # for the sync tool to deliver the database
        self.waiting_for: tuple[int, int] | None = None
        # holder side
        self._seen_requests: dict[tuple[str, str], datetime] = {}
        self._yielding = False
        self.handed_to: str | None = None  # who we last handed editing to
        self.lost_lock_to: str | None = None  # another machine's lock replaced ours

    # ------------------------------------------------------------------ #
    # public lifecycle
    # ------------------------------------------------------------------ #
    @_locked
    def start(self, *, background: bool = True) -> WriteMode:
        self.paths.ensure_dirs()
        mode = self._acquire()
        if mode is WriteMode.writable:
            mode = self._become_writer(background=background)
        else:
            self._pull_if_newer()
        log.info("sync started in %s mode (lock held by %s)", mode, self._holder_name())
        return mode

    @_locked
    def refresh(self) -> WriteMode:
        """Re-check the lock; promote read-only -> writable when it frees up
        (or is handed to us and our copy of the published DB has caught up)."""
        if self.mode is WriteMode.writable:
            return self.mode
        self._beat_request()
        retry_at = self._promote_retry_at
        if retry_at is not None and self._clock() < retry_at:
            self._pull_if_newer()
        elif self._acquire() is WriteMode.writable:
            self._become_writer(background=True)
        else:
            self._pull_if_newer()
        return self.mode

    def _become_writer(self, *, background: bool) -> WriteMode:
        """Finish taking the lock: merge conflict copies, line the working copy
        up with ``sync/``, check it, and start the heartbeat/publish thread.

        Exception-safe: writable must always mean "heartbeating". If a step
        fails, release the lock (so it isn't left held with no heartbeat while
        this machine edits and another takes it over) and stay read-only; the
        reason is kept in :attr:`writer_error` and the next try waits
        ``_PROMOTE_RETRY_SEC``."""
        try:
            self._check_schema()
            self.last_merge = self._merge_conflicts()
            self._reconcile_generation()
            self._verify_or_restore()
        except Exception as exc:
            self.needs_update = isinstance(exc, NeedsUpdate)
            if self.needs_update:
                log.warning("staying read-only: %s", exc)
                self.writer_error = str(exc)
            else:
                log.exception("could not take over editing; staying read-only")
                self.writer_error = f"{type(exc).__name__}: {exc}"
            self._promote_retry_at = self._clock() + timedelta(seconds=_PROMOTE_RETRY_SEC)
            try:
                self._release()
            except Exception:
                log.exception("could not release the write lock")
            self.mode = WriteMode.read_only
            return self.mode
        self.writer_error = None
        self.needs_update = False
        self._promote_retry_at = None
        if background:
            self._start_thread()
        return self.mode

    @property
    def writes_open(self) -> bool:
        """May user edits be written right now? False while read-only and
        while handing over (between "stop the indexer" and the grant)."""
        return self.mode is WriteMode.writable and not self._yielding

    # ------------------------------------------------------------------ #
    # editing checkout -- requester side
    # ------------------------------------------------------------------ #
    @_locked
    def request_edit(self) -> None:
        """Ask the current writer to hand editing over ("Take over editing")."""
        if self.mode is WriteMode.writable:
            return
        now = self._clock()
        self.my_request = new_request(device_id=self.device_id, hostname=self.hostname, now=now)
        write_request(self.paths.locks_dir, self.my_request)
        self._request_beat_at = now
        self.denied_by = None

    @_locked
    def cancel_request(self) -> None:
        if self.my_request is not None:
            clear_request(self.paths.locks_dir, self.device_id)
        self.my_request = None
        self._request_beat_at = None
        self.waiting_for = None

    def _beat_request(self) -> None:
        req = self.my_request
        if req is None:
            return
        now = self._clock()
        last = self._request_beat_at
        if last is not None and (now - last).total_seconds() < self.cfg.lock_heartbeat_sec:
            return
        self.my_request = dataclasses.replace(req, heartbeat_at=isoformat(now))
        write_request(self.paths.locks_dir, self.my_request)
        self._request_beat_at = now

    def _other_requests(self, now: datetime) -> list[EditRequest]:
        """Live requests from other machines, oldest first."""
        return [
            r
            for r in read_requests(self.paths.locks_dir)
            if r.device_id != self.device_id and r.is_fresh(now, self.cfg.lock_stale_after_sec)
        ]

    # ------------------------------------------------------------------ #
    # editing checkout -- holder side
    # ------------------------------------------------------------------ #
    @_locked
    def incoming_request(self) -> tuple[EditRequest, float] | None:
        """The oldest live request we haven't refused, and the seconds left of
        its grace period (counted on our clock from when we first saw it)."""
        if self.mode is not WriteMode.writable:
            return None
        now = self._clock()
        lock = self.lock_holder
        for req in self._other_requests(now):
            if (
                lock is not None
                and lock.denied_device == req.device_id
                and lock.denied_request_at == req.requested_at
            ):
                continue
            seen = self._seen_requests.setdefault((req.device_id, req.requested_at), now)
            left = self.cfg.handoff_grace_sec - (now - seen).total_seconds()
            return req, max(0.0, left)
        return None

    @_locked
    def deny(self, req: EditRequest) -> None:
        """ "Keep editing": tell *req*'s machine, through our lock, that we said no."""
        if self.mode is not WriteMode.writable or self.lock_holder is None:
            return
        lock = dataclasses.replace(
            self.lock_holder,
            denied_device=req.device_id,
            denied_request_at=req.requested_at,
        )
        write_lock(self.paths.lock_file, lock)
        self.lock_holder = lock

    @_locked
    def maybe_hand_over(self, prepare: Callable[[], None] | None = None) -> bool:
        """Hand editing over if a request's grace period is over. *prepare*
        runs first (writes are already closed): the app stops its indexer
        there. Returns whether we handed over."""
        got = self.incoming_request()
        if got is None or got[1] > 0:
            return False
        req = got[0]
        self._yielding = True
        try:
            if prepare is not None:
                prepare()
            self._yield_to(req)
        finally:
            self._yielding = False
        return True

    def _yield_to(self, req: EditRequest) -> None:
        if self.mode is not WriteMode.writable or self.lock_holder is None:
            return
        self.publish(force=True)
        now = self._clock()
        generation = read_generation(self.paths.working_db)
        grant = handoff_lock(
            self.lock_holder, to=req.device_id, hostname=req.name, generation=generation, now=now
        )
        write_lock(self.paths.lock_file, grant)
        self.lock_holder = grant
        self.mode = WriteMode.read_only
        self.handed_to = req.name
        self._seen_requests.clear()
        log.info("handed editing to %s at db_generation %d", req.name, generation)

    @_locked
    def heartbeat(self) -> None:
        if self.mode is not WriteMode.writable or self.lock_holder is None:
            return
        now = self._clock()
        current = read_lock(self.paths.lock_file)
        if (
            current is not None
            and current.device_id != self.device_id
            and not current.is_handoff
            and not is_stale(current, now=now, stale_after_sec=self.cfg.lock_stale_after_sec)
        ):
            self._lose_lock(current)
            return
        refreshed = dataclasses.replace(
            self.lock_holder,
            heartbeat_at=isoformat(now),
            db_generation=read_generation(self.paths.working_db),
        )
        write_lock(self.paths.lock_file, refreshed)
        self.lock_holder = refreshed

    @_locked
    def publish(self, *, force: bool = False) -> bool:
        """Publish the working DB to ``sync/`` if it is dirty and idle.

        Returns whether a publish actually happened.
        """
        if self.mode is not WriteMode.writable:
            return False
        wdb = self.paths.working_db
        if not wdb.exists():
            return False

        now = self._clock()
        if not force:
            if self._last_publish_at is not None:
                elapsed = (now - self._last_publish_at).total_seconds()
                if elapsed < self.cfg.publish_interval_sec:
                    return False
            if sha256_file(wdb) == self._last_publish_hash:
                return False  # nothing changed since the last publish

        with Session(self._db_engine()) as s:
            generation = bump_generation(s)
            s.commit()

        atomic_publish(wdb, self.paths.published_db, device_id=self.device_id)
        self._last_publish_hash = sha256_file(wdb)
        self._last_publish_at = now
        self._publish_count += 1
        self._write_publish_state(generation, self._last_publish_hash, now)
        self.heartbeat()

        self._publishes_since_snapshot += 1
        if self._snapshot_due(now):
            self.snapshot()
        return True

    def snapshot(self) -> Path:
        """Back up the working DB to ``sync/snapshots/`` now (also "Back up now")."""
        path = write_snapshot(self.paths.working_db, self.paths.snapshots_dir, now=self._clock())
        prune_snapshots(self.paths.snapshots_dir, self.cfg.snapshot_keep)
        self._publishes_since_snapshot = 0
        return path

    def last_snapshot(self) -> tuple[Path, datetime | None] | None:
        snap = newest_snapshot(self.paths.snapshots_dir)
        return (snap, snapshot_time(snap)) if snap is not None else None

    def _snapshot_due(self, now: datetime) -> bool:
        if self._publishes_since_snapshot >= self.cfg.snapshot_every_publishes:
            return True
        hours = self.cfg.snapshot_every_hours
        if hours <= 0:
            return False
        last = self.last_snapshot()
        if last is None or last[1] is None:
            return True
        return (now - last[1]).total_seconds() >= hours * 3600

    @_locked
    def merge_arrived_copies(self) -> MergeSummary | None:
        """While writable, fold in conflict copies that showed up after we took
        the lock. Startup / promotion merge only once, so an always-on writer
        (Docker) would otherwise sit on another machine's edits until a restart.
        Called from the background tick, at most every ``_LIVE_MERGE_EVERY_SEC``.

        Unlike the startup merge the working DB is always the base: it's live
        (the indexer and the UI write to it), so it is never swapped out. The
        merge replays the unioned ``changes`` log last-writer-wins, so the
        result doesn't depend on which copy is the base."""
        if not self.writes_open:
            return None
        now = self._clock()
        if self._next_live_merge is not None and now < self._next_live_merge:
            return None
        self._next_live_merge = now + timedelta(seconds=_LIVE_MERGE_EVERY_SEC)
        cutoff = now.timestamp() - _LIVE_MERGE_SETTLE_SEC
        copies = [c for c in find_conflict_copies(self.paths.sync) if _mtime(c) <= cutoff]
        if not copies:
            return None
        summary = self._merge_conflicts(copies, live=True)
        if summary is not None:
            self.last_merge = summary
        return summary

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        with self._mutex:
            if self.mode is WriteMode.writable:
                try:
                    self.publish(force=True)
                finally:
                    self._release()
            if self.my_request is not None:
                self.cancel_request()
            self.mode = WriteMode.read_only
        if self._external_engine is None and self._engine is not None:
            self._engine.dispose()
            self._engine = None

    # ------------------------------------------------------------------ #
    # startup steps
    # ------------------------------------------------------------------ #
    def _acquire(self) -> WriteMode:
        now = self._clock()
        info = read_lock(self.paths.lock_file)
        self._note_denial(info)
        status = lock_status(
            info,
            device_id=self.device_id,
            now=now,
            stale_after_sec=self.cfg.lock_stale_after_sec,
            published_generation=read_generation(self.paths.published_db),
        )
        self.waiting_for = None
        if status is LockStatus.waiting and info is not None:
            self.waiting_for = (
                read_generation(self.paths.published_db),
                info.handoff_generation,
            )
        if status is not LockStatus.free or self._defer_to_requester(info, now):
            self.lock_holder = info
            self.mode = WriteMode.read_only
            return self.mode
        lock = new_lock(
            device_id=self.device_id,
            hostname=self.hostname,
            db_generation=read_generation(self.paths.working_db),
            now=now,
        )
        write_lock(self.paths.lock_file, lock)
        self.lock_holder = lock
        self.mode = WriteMode.writable
        if self.my_request is not None:
            self.cancel_request()
        self.handed_to = None
        self.lost_lock_to = None
        self._seen_requests.clear()
        return self.mode

    def _note_denial(self, info: LockInfo | None) -> None:
        req = self.my_request
        if (
            req is not None
            and info is not None
            and info.denied_device == self.device_id
            and info.denied_request_at == req.requested_at
        ):
            self.denied_by = machine_name(info.hostname, info.device_id)
            self.cancel_request()

    def _defer_to_requester(self, info: LockInfo | None, now: datetime) -> bool:
        """Leave a free lock alone while another machine is asking for it --
        unless it was handed to us, or we're asking too."""
        if self.my_request is not None:
            return False
        if info is not None and info.is_handoff and info.handoff_to == self.device_id:
            return False
        if info is not None and info.device_id == self.device_id and not info.is_handoff:
            return False  # our own lock (we restarted)
        return bool(self._other_requests(now))

    def _release(self) -> None:
        """Rewrite our lock as a handoff to anyone at the final generation."""
        info = read_lock(self.paths.lock_file)
        if info is None or info.device_id != self.device_id or self.lock_holder is None:
            return
        released = handoff_lock(
            self.lock_holder,
            to="",
            hostname="",
            generation=read_generation(self.paths.working_db),
            now=self._clock(),
        )
        write_lock(self.paths.lock_file, released)
        self.lock_holder = released

    def _lose_lock(self, winner: LockInfo) -> None:
        """Another machine's live lock replaced ours (both took a free lock
        before the sync tool showed each the other's). Step down, and leave
        our working DB in ``sync/`` as a conflict copy so the next promotion's
        merge folds our unpublished edits in."""
        name = machine_name(winner.hostname, winner.device_id)
        log.warning("write lock lost to %s; stepping down to read-only", name)
        stamp = self._clock().strftime("%Y%m%d-%H%M%S")
        copy = self.paths.sync / f"sortandpost ({self.hostname} lost lock {stamp}).db"
        try:
            atomic_publish(self.paths.working_db, copy, device_id=self.device_id)
        except OSError:
            log.exception("could not save the working DB as a conflict copy")
        self.lock_holder = winner
        self.mode = WriteMode.read_only
        self.lost_lock_to = name

    def _merge_conflicts(
        self, copies: list[Path] | None = None, *, live: bool = False
    ) -> MergeSummary | None:
        """Merge *copies* (default: every conflict copy in ``sync/``) into the
        working DB. At startup the highest generation is the base; *live* (we're
        already the writer) always keeps the working DB as the base and leaves
        the published DB out -- it's our own last publish."""
        if copies is None:
            copies = find_conflict_copies(self.paths.sync)
        broken = [c for c in copies if not quick_check(c)]  # not a database / damaged
        if broken:
            self._quarantine(broken)
            copies = [c for c in copies if c not in broken]
        if not copies:
            return None

        wdb = self.paths.working_db
        pdb = self.paths.published_db
        candidates = [p for p in (wdb, *(() if live else (pdb,)), *copies) if p.exists()]
        gens = {p: read_generation(p) for p in candidates}
        # highest generation wins; keep the working copy on a tie
        base = wdb if live else max(candidates, key=lambda p: (gens[p], p == wdb))
        if base != wdb:
            self._reset_working_engine()  # Windows can't replace a file SQLite holds open
            atomic_replace(base, wdb, tag=self.device_id)
            self._reset_working_engine()
            gens[wdb] = gens[base]

        merge_sources = [p for p in candidates if p != wdb and p != base]
        engines = [make_engine(p) for p in merge_sources]
        sessions = [(p.name, Session(e)) for p, e in zip(merge_sources, engines, strict=True)]
        try:
            with Session(self._db_engine()) as target:
                summary = merge_all(
                    target,
                    sessions,
                    now=self._clock(),
                    base_generation=gens[base],
                    source_generations=[gens[p] for p in merge_sources],
                )
                target.commit()
        finally:
            for _, s in sessions:
                s.close()
            for e in engines:
                e.dispose()

        failed = [p for p in merge_sources if p.name in summary.failed]
        self._quarantine(failed)
        self._archive_conflict_copies([c for c in copies if c not in failed])
        self.publish(force=True)
        log.warning(
            "merged %d conflict copy(ies) into the working DB: %s",
            len(copies),
            summary,
        )
        return summary

    def _reconcile_generation(self) -> None:
        remote = read_generation(self.paths.published_db)
        local = read_generation(self.paths.working_db)
        if remote > local:
            self._reset_working_engine()  # Windows can't replace a file SQLite holds open
            pull_published(self.paths.published_db, self.paths.working_db, device_id=self.device_id)
            self._reset_working_engine()
            self._stamp_schema()  # rides along with the next real publish
            self._last_publish_hash = sha256_file(self.paths.working_db)
            self._last_publish_at = self._clock()
        else:
            # local is level or ahead: make sure sync/ carries our state
            self._stamp_schema()
            self.publish(force=True)

    def _check_schema(self) -> None:
        """Refuse to write a library a newer app has upgraded (constraint #9's
        read-only mode until this computer is updated)."""
        found = max(
            read_meta_int(self.paths.working_db, SCHEMA_VERSION_KEY),
            read_meta_int(self.paths.published_db, SCHEMA_VERSION_KEY),
        )
        if found > SCHEMA_VERSION:
            raise NeedsUpdate(
                "another computer updated sortandpost.photos, and this library now "
                "needs the new version -- update this computer to keep editing"
            )

    def _stamp_schema(self) -> None:
        if read_meta_int(self.paths.working_db, SCHEMA_VERSION_KEY) >= SCHEMA_VERSION:
            return
        with Session(self._db_engine()) as s:
            set_meta(s, SCHEMA_VERSION_KEY, str(SCHEMA_VERSION))
            s.commit()

    def _verify_or_restore(self) -> None:
        if quick_check(self.paths.working_db):
            return
        snap = newest_snapshot(self.paths.snapshots_dir)
        if snap is None:
            raise RuntimeError("working DB failed quick_check and no snapshot exists to restore")
        log.error("working DB failed quick_check; restoring %s", snap.name)
        self._reset_working_engine()  # Windows can't replace a file SQLite holds open
        restore_snapshot(snap, self.paths.working_db)
        self._reset_working_engine()
        self.restored_from_snapshot = snap
        self._last_publish_hash = None  # force a re-publish of the restored state

    def _pull_if_newer(self) -> None:
        if read_generation(self.paths.published_db) > read_generation(self.paths.working_db):
            self._reset_working_engine()  # Windows can't replace a file SQLite holds open
            pull_published(self.paths.published_db, self.paths.working_db, device_id=self.device_id)
            self._reset_working_engine()

    # ------------------------------------------------------------------ #
    # background thread
    # ------------------------------------------------------------------ #
    def _start_thread(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sortandpost-sync", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        interval = max(1, self.cfg.lock_heartbeat_sec)
        while not self._stop.wait(interval):
            try:
                self.heartbeat()
                self.publish(force=False)
                self.merge_arrived_copies()
            except Exception:  # a bad tick must not kill the heartbeat
                log.exception("sync tick failed")

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _db_engine(self) -> Engine:
        if self._engine is None:
            self._engine = make_engine(self.paths.working_db)
        return self._engine

    def _reset_working_engine(self) -> None:
        """Drop pooled connections left pointing at the pre-replacement inode.

        ``os.replace`` of ``local/sortandpost.db`` (a pull, a conflict-copy base
        swap, or a snapshot restore) unlinks the file the engine's open SQLite
        connections still hold -- and SQLite then refuses writes to it with
        "attempt to write a readonly database". ``dispose()`` recycles the pool
        without invalidating the engine object (which the web app shares); the
        next connection opens the current file. In-flight transactions on a
        checked-out connection are left to finish and close normally.

        Called *before* the replace too: Windows refuses to replace a file that
        is open at all, so the idle pooled handles must be closed first.
        """
        if self._engine is not None:
            self._engine.dispose()

    def _quarantine(self, copies: list[Path]) -> None:
        """Move conflict copies that can't be merged out of the way, so they
        aren't retried on every start. Kept, never deleted, and listed in
        ``/settings``."""
        if not copies:
            return
        log.error(
            "set aside %d conflict copy(ies) that can't be merged: %s",
            len(copies),
            ", ".join(c.name for c in copies),
        )
        self._archive_conflict_copies(copies, self.paths.unmergeable_dir, self.quarantined)

    def _archive_conflict_copies(
        self,
        copies: list[Path],
        dest_dir: Path | None = None,
        moved: list[Path] | None = None,
    ) -> None:
        dest_dir = dest_dir or self.paths.merged_snapshots_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        for c in copies:
            if not c.exists():
                continue
            dest = dest_dir / c.name
            counter = 1
            while dest.exists():
                dest = dest_dir / f"{c.stem}-{counter}{c.suffix}"
                counter += 1
            c.replace(dest)
            if moved is not None:
                moved.append(dest)

    def _write_publish_state(self, generation: int, digest: str, now: datetime) -> None:
        self.paths.publish_state_file.write_text(
            json.dumps(
                {
                    "db_generation": generation,
                    "sha256": digest,
                    "published_at": isoformat(now),
                    "device_id": self.device_id,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def _holder_name(self) -> str:
        lock = self.lock_holder
        if lock is None:
            return "nobody"
        if lock.is_handoff:
            if lock.handoff_to == "":
                return "nobody"
            if lock.handoff_to == self.device_id:
                return f"this machine ({self.hostname})"
            return machine_name(lock.handoff_hostname, lock.handoff_to or "")
        if lock.device_id == self.device_id:
            return f"this machine ({self.hostname})"
        return machine_name(lock.hostname, lock.device_id)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return float("inf")  # gone (or unreadable) since the listing: not settled
