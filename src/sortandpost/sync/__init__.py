"""Cross-machine sync: local working copy + atomic publish + single-writer lock.

See CLAUDE.md → "Sync model". Implemented in build-order step 4.

Responsibilities:
* acquire/refresh ``sync/.sortandpost.lock`` (heartbeat); demote to read-only when
  another live device holds it
* on startup: pull ``sync/sortandpost.db`` -> working copy when its ``db_generation``
  is newer; ``PRAGMA quick_check``; restore newest snapshot on failure
* publish: copy working DB -> ``sync/.sortandpost.db.tmp-<device>`` -> ``os.replace``
* snapshot every N publishes; prune to ``snapshot_keep``
* detect + merge conflicted-copy DBs via the ``changes`` log
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from sortandpost.config import Settings


class WriteMode(StrEnum):
    writable = "writable"
    read_only = "read_only"


@dataclass
class LockInfo:
    device_id: str
    hostname: str
    pid: int
    acquired_at: str
    heartbeat_at: str
    db_generation: int


class SyncManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.mode: WriteMode = WriteMode.read_only

    def start(self) -> WriteMode:
        raise NotImplementedError("build-order step 4: sync layer")

    def publish(self, *, force: bool = False) -> None:
        raise NotImplementedError("build-order step 4: sync layer")

    def snapshot(self) -> Path:
        raise NotImplementedError("build-order step 4: sync layer")

    def heartbeat(self) -> None:
        raise NotImplementedError("build-order step 4: sync layer")

    def merge_conflict_copies(self) -> list[Path]:
        raise NotImplementedError("build-order step 4: sync layer")

    def stop(self) -> None:
        return None
