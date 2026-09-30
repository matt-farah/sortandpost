"""``sync/.sortandpost.lock`` — the single-writer lock (CLAUDE.md → "Sync model").

The lock is a small JSON file. Its authority comes from ``heartbeat_at``: the
holder rewrites it every ``lock_heartbeat_sec``; a lock whose heartbeat is older
than ``lock_stale_after_sec`` is assumed abandoned (crash / sleep) and may be
taken over. Writes are atomic (temp file + ``os.replace``) so a reader on
another machine never sees a half-written lock.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
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
        )

    def heartbeat_age(self, now: datetime) -> timedelta:
        return now - parse(self.heartbeat_at)


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


def held_by_other(
    info: LockInfo | None,
    *,
    device_id: str,
    now: datetime,
    stale_after_sec: float,
) -> bool:
    """True when a *different, still-live* device holds the lock."""
    if info is None or info.device_id == device_id:
        return False
    return not is_stale(info, now=now, stale_after_sec=stale_after_sec)


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
