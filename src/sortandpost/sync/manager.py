"""``SyncManager`` — the startup sequence, heartbeat, and publish loop that make
``sync/`` a safe cross-machine source of truth (CLAUDE.md → "Sync model").

Lifecycle:

* :meth:`start` — acquire the lock (or fall to read-only), merge any conflict
  copies, reconcile the working copy against the published generation,
  ``quick_check`` (restore a snapshot on failure), then spin up the background
  thread that heartbeats the lock and publishes when the DB is dirty & idle.
* :meth:`refresh` — re-check the lock; a read-only instance promotes itself once
  the holder's lock is free or stale.
* :meth:`stop` — final publish, release the lock, join the thread.

Every timestamp goes through an injected clock so the whole thing is testable
without real time.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import socket
import threading
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import Session

from sortandpost.config import Settings
from sortandpost.db import make_engine
from sortandpost.db.meta import bump_generation
from sortandpost.sync.clock import Clock, isoformat, utcnow
from sortandpost.sync.lock import (
    LockInfo,
    clear_lock,
    held_by_other,
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
    sha256_file,
)
from sortandpost.sync.snapshots import (
    newest_snapshot,
    prune_snapshots,
    restore_snapshot,
    write_snapshot,
)

log = logging.getLogger(__name__)


class WriteMode(StrEnum):
    writable = "writable"
    read_only = "read-only"


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

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._publishes_since_snapshot = 0
        self._publish_count = 0
        self._last_publish_hash: str | None = None
        self._last_publish_at: datetime | None = None

    # ------------------------------------------------------------------ #
    # public lifecycle
    # ------------------------------------------------------------------ #
    def start(self, *, background: bool = True) -> WriteMode:
        self.paths.ensure_dirs()
        mode = self._acquire()
        if mode is WriteMode.writable:
            self.last_merge = self._merge_conflicts()
            self._reconcile_generation()
            self._verify_or_restore()
            if background:
                self._start_thread()
        else:
            self._pull_if_newer()
        log.info("sync started in %s mode (lock held by %s)", mode, self._holder_name())
        return mode

    def refresh(self) -> WriteMode:
        """Re-check the lock; promote read-only -> writable when it frees up."""
        if self.mode is WriteMode.writable:
            return self.mode
        if self._acquire() is WriteMode.writable:
            self.last_merge = self._merge_conflicts()
            self._reconcile_generation()
            self._verify_or_restore()
            self._start_thread()
        else:
            self._pull_if_newer()
        return self.mode

    def heartbeat(self) -> None:
        if self.mode is not WriteMode.writable or self.lock_holder is None:
            return
        now = self._clock()
        refreshed = dataclasses.replace(
            self.lock_holder,
            heartbeat_at=isoformat(now),
            db_generation=read_generation(self.paths.working_db),
        )
        write_lock(self.paths.lock_file, refreshed)
        self.lock_holder = refreshed

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
        if self._publishes_since_snapshot >= self.cfg.snapshot_every_publishes:
            self._publishes_since_snapshot = 0
            write_snapshot(wdb, self.paths.snapshots_dir, now=now)
            prune_snapshots(self.paths.snapshots_dir, self.cfg.snapshot_keep)
        return True

    def snapshot(self) -> Path:
        path = write_snapshot(self.paths.working_db, self.paths.snapshots_dir, now=self._clock())
        prune_snapshots(self.paths.snapshots_dir, self.cfg.snapshot_keep)
        return path

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self.mode is WriteMode.writable:
            try:
                self.publish(force=True)
            finally:
                clear_lock(self.paths.lock_file, device_id=self.device_id)
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
        if held_by_other(
            info,
            device_id=self.device_id,
            now=now,
            stale_after_sec=self.cfg.lock_stale_after_sec,
        ):
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
        return self.mode

    def _merge_conflicts(self) -> MergeSummary | None:
        copies = find_conflict_copies(self.paths.sync)
        if not copies:
            return None

        wdb = self.paths.working_db
        pdb = self.paths.published_db
        candidates = [p for p in (wdb, pdb, *copies) if p.exists()]
        gens = {p: read_generation(p) for p in candidates}
        # highest generation wins; keep the working copy on a tie
        base = max(candidates, key=lambda p: (gens[p], p == wdb))
        if base != wdb:
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

        self._archive_conflict_copies(copies)
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
            pull_published(self.paths.published_db, self.paths.working_db, device_id=self.device_id)
            self._reset_working_engine()
            self._last_publish_hash = sha256_file(self.paths.working_db)
            self._last_publish_at = self._clock()
        else:
            # local is level or ahead: make sure sync/ carries our state
            self.publish(force=True)

    def _verify_or_restore(self) -> None:
        if quick_check(self.paths.working_db):
            return
        snap = newest_snapshot(self.paths.snapshots_dir)
        if snap is None:
            raise RuntimeError("working DB failed quick_check and no snapshot exists to restore")
        log.error("working DB failed quick_check; restoring %s", snap.name)
        restore_snapshot(snap, self.paths.working_db)
        self._reset_working_engine()
        self.restored_from_snapshot = snap
        self._last_publish_hash = None  # force a re-publish of the restored state

    def _pull_if_newer(self) -> None:
        if read_generation(self.paths.published_db) > read_generation(self.paths.working_db):
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
        """
        if self._engine is not None:
            self._engine.dispose()

    def _archive_conflict_copies(self, copies: list[Path]) -> None:
        dest_dir = self.paths.merged_snapshots_dir
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
        if self.lock_holder is None:
            return "nobody"
        if self.lock_holder.device_id == self.device_id:
            return f"this machine ({self.hostname})"
        return self.lock_holder.hostname or self.lock_holder.device_id
