"""Edit requests — the "Take over editing" half of the editing checkout.

An always-on writer (a Docker server) would otherwise keep every other machine
read-only forever. A machine that wants to edit writes its **own** request file,
``sync/locks/request-<device_id[:8]>.json``; it never touches the lock file,
which only its holder writes, so the sync tool never sees two machines editing
one file. The holder notices the request on its next tick, gives its user a
grace period to answer "Keep editing", then publishes and rewrites its lock as a
grant to the requester (:func:`sortandpost.sync.lock.handoff_lock`). See
:class:`sortandpost.sync.manager.SyncManager` for both sides.

The requester re-stamps ``heartbeat_at`` while it waits, so a request from a
machine that went away goes stale (``lock_stale_after_sec``) and is ignored.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sortandpost.sync.clock import isoformat, parse
from sortandpost.sync.lock import machine_name
from sortandpost.sync.publish import replace_file


@dataclass(frozen=True)
class EditRequest:
    device_id: str
    hostname: str
    requested_at: str  # ISO-8601 UTC; with device_id, identifies the request
    heartbeat_at: str  # ISO-8601 UTC

    @property
    def name(self) -> str:
        return machine_name(self.hostname, self.device_id)

    def is_fresh(self, now: datetime, stale_after_sec: float) -> bool:
        return now - parse(self.heartbeat_at) <= timedelta(seconds=stale_after_sec)


def request_file(locks_dir: Path, device_id: str) -> Path:
    return locks_dir / f"request-{device_id[:8]}.json"


def write_request(locks_dir: Path, req: EditRequest) -> None:
    locks_dir.mkdir(parents=True, exist_ok=True)
    path = request_file(locks_dir, req.device_id)
    tmp = path.with_name(f"{path.name}.tmp-{req.device_id}")
    tmp.write_text(json.dumps(asdict(req), indent=2, sort_keys=True), encoding="utf-8")
    replace_file(tmp, path)


def new_request(*, device_id: str, hostname: str, now: datetime) -> EditRequest:
    stamp = isoformat(now)
    return EditRequest(device_id, hostname, requested_at=stamp, heartbeat_at=stamp)


def clear_request(locks_dir: Path, device_id: str) -> None:
    request_file(locks_dir, device_id).unlink(missing_ok=True)


def read_requests(locks_dir: Path) -> list[EditRequest]:
    """Every readable request, oldest first. Unreadable / half-synced files are
    skipped (the sync tool may still be delivering them)."""
    out: list[EditRequest] = []
    for p in sorted(locks_dir.glob("request-*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            out.append(
                EditRequest(
                    device_id=str(data["device_id"]),
                    hostname=str(data.get("hostname") or ""),
                    requested_at=str(data["requested_at"]),
                    heartbeat_at=str(data.get("heartbeat_at") or data["requested_at"]),
                )
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue
    out.sort(key=lambda r: (r.requested_at, r.device_id))
    return out
