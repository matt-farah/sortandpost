"""``sync/.sortandpost.lock`` — the single-writer lock (docs/architecture.md → "Sync model").

The lock is a small JSON file. Its authority comes from ``heartbeat_at``: the
holder rewrites it every ``lock_heartbeat_sec``; a lock whose heartbeat is older
than ``lock_stale_after_sec`` is assumed abandoned (crash / sleep) and may be
taken over. Writes are atomic (temp file + ``os.replace``) so a reader on
another machine never sees a half-written lock.

**Handoff** (the editing checkout, :mod:`sortandpost.sync.handoff`): a holder that
steps down doesn't delete the lock, it rewrites it as a handoff --
``handoff_to`` names the device it was handed to (a *grant*), or is ``""`` when
it was released to anyone (a clean quit). ``handoff_generation`` is the
``db_generation`` it published last, and the next writer waits until its own copy
of ``sync/sortandpost.db`` has caught up to it: the sync tool may deliver the lock
before the database, and taking over from a stale copy would lose the edits. A
handoff is not heartbeated, so after ``lock_stale_after_sec`` it is stale like
any lock and the generation wait ends (the published DB may never arrive).
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from sortandpost.sync.clock import isoformat, parse
from sortandpost.sync.publish import replace_file


@dataclass(frozen=True)
class LockInfo:
    device_id: str
    hostname: str
    pid: int
    acquired_at: str  # ISO-8601 UTC
    heartbeat_at: str  # ISO-8601 UTC
    db_generation: int
    # Handoff (see the module docstring). None = a normal, held lock.
    handoff_to: str | None = None
    handoff_hostname: str = ""
    handoff_generation: int = 0
    # The holder's "Keep editing" answer to one edit request (device + its
    # requested_at), so that requester stops waiting.
    denied_device: str | None = None
    denied_request_at: str | None = None

    @property
    def is_handoff(self) -> bool:
        return self.handoff_to is not None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> LockInfo:
        data = json.loads(text)
        return cls(
            device_id=str(data["device_id"]),
            hostname=str(data.get("hostname", "")),
            pid=int(data.get("pid", 0)),
            acquired_at=str(data["acquired_at"]),
            heartbeat_at=str(data["heartbeat_at"]),
            db_generation=int(data.get("db_generation", 0)),
            handoff_to=_opt_str(data.get("handoff_to")),
            handoff_hostname=str(data.get("handoff_hostname") or ""),
            handoff_generation=int(data.get("handoff_generation") or 0),
            denied_device=_opt_str(data.get("denied_device")),
            denied_request_at=_opt_str(data.get("denied_request_at")),
        )

    def heartbeat_age(self, now: datetime) -> timedelta:
        return now - parse(self.heartbeat_at)


_LOCAL_SUFFIXES = (".local", ".lan", ".home", ".localdomain")


def machine_name(hostname: str, device_id: str = "") -> str:
    """A hostname as people say it ("Matthews-MacBook-Air-2", not
    "...-Air-2.local"), or the device id's start when there is none."""
    name = hostname.strip()
    for suffix in _LOCAL_SUFFIXES:
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name or device_id[:8]


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def read_lock(path: Path) -> LockInfo | None:
    """Return the current lock, or ``None`` if it is absent or unreadable."""
    try:
        return LockInfo.from_json(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, KeyError):
        return None


def write_lock(path: Path, info: LockInfo) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{info.device_id}")
    tmp.write_text(info.to_json(), encoding="utf-8")
    replace_file(tmp, path)  # the sync client may be reading it (Windows)


def clear_lock(path: Path, *, device_id: str) -> bool:
    """Remove the lock iff we own it. Returns whether a file was removed."""
    info = read_lock(path)
    if info is not None and info.device_id == device_id:
        path.unlink(missing_ok=True)
        return True
    return False


def is_stale(info: LockInfo, *, now: datetime, stale_after_sec: float) -> bool:
    return info.heartbeat_age(now) > timedelta(seconds=stale_after_sec)


class LockStatus(StrEnum):
    free = "free"  # no lock, a stale one, our own, or a handoff we may take now
    held = "held"  # another live device holds it (or it was granted to another)
    waiting = "waiting"  # handed to us / released, but our sync/ DB isn't caught up yet


def lock_status(
    info: LockInfo | None,
    *,
    device_id: str,
    now: datetime,
    stale_after_sec: float,
    published_generation: int,
) -> LockStatus:
    """May *device_id* take the lock described by *info* right now?

    ``published_generation`` is the ``db_generation`` of this machine's copy of
    ``sync/sortandpost.db`` -- what a handoff waits for.
    """
    if info is None:
        return LockStatus.free
    stale = is_stale(info, now=now, stale_after_sec=stale_after_sec)
    if not info.is_handoff:
        if info.device_id == device_id or stale:
            return LockStatus.free
        return LockStatus.held
    if info.handoff_to not in ("", device_id):
        # granted to someone else: theirs until it goes stale
        return LockStatus.free if stale else LockStatus.held
    if stale or info.device_id == device_id or published_generation >= info.handoff_generation:
        return LockStatus.free
    return LockStatus.waiting


def held_by_other(
    info: LockInfo | None,
    *,
    device_id: str,
    now: datetime,
    stale_after_sec: float,
) -> bool:
    """True when a *different, still-live* device holds the lock (a handoff
    counts as held only when it was granted to someone else)."""
    if info is None or (info.device_id == device_id and not info.is_handoff):
        return False
    if info.is_handoff and info.handoff_to in ("", device_id):
        return False
    return not is_stale(info, now=now, stale_after_sec=stale_after_sec)


def handoff_lock(
    info: LockInfo, *, to: str, hostname: str, generation: int, now: datetime
) -> LockInfo:
    """*info* (our lock) rewritten as a handoff: to device *to*, or ``""`` for anyone."""
    return dataclasses.replace(
        info,
        heartbeat_at=isoformat(now),
        db_generation=generation,
        handoff_to=to,
        handoff_hostname=hostname,
        handoff_generation=generation,
        denied_device=None,
        denied_request_at=None,
    )


def new_lock(
    *,
    device_id: str,
    hostname: str,
    db_generation: int,
    now: datetime,
) -> LockInfo:
    stamp = isoformat(now)
    return LockInfo(
        device_id=device_id,
        hostname=hostname,
        pid=os.getpid(),
        acquired_at=stamp,
        heartbeat_at=stamp,
        db_generation=db_generation,
    )
