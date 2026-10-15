"""The ``changes`` append-only log — hard constraint #4.

Every user-authored mutation (rate, tag add/remove, CLIP approve/reject, unindex)
MUST call :func:`record_change` in the *same transaction* that writes the domain
table. Conflict-copy merge is deterministic only because it can replay this log;
a mutation that skips it is silently lost on a cross-machine merge.

The star widget / tag editors that call this arrive in build-order step 5; step 4
builds the log plumbing and the merge replay that consumes it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlmodel import Session

from sortandpost.db.models import Change, ChangeOp

ChangeKey = tuple[str, str, str, str, str | None, str | None]


class ReadOnlyError(RuntimeError):
    """A user edit was attempted while this machine isn't the writer."""


# The web app installs ``lambda: manager.writes_open`` so an edit that races a
# handover (or a lost lock) is refused instead of landing in a working DB that
# the next pull overwrites. CLIs and tests leave it unset: always writable.
_write_guard: Callable[[], bool] | None = None


def set_write_guard(guard: Callable[[], bool] | None) -> None:
    global _write_guard
    _write_guard = guard


def ensure_writable() -> None:
    """Raise :class:`ReadOnlyError` unless this machine may write user edits.
    Called by :func:`record_change`, and up front by actions that move files
    before they record (delete, put back)."""
    if _write_guard is not None and not _write_guard():
        raise ReadOnlyError("another computer is editing this library right now")


def record_change(
    session: Session,
    *,
    device_id: str,
    op: ChangeOp,
    pixel_hash: str,
    ts: datetime,
    field: str | None = None,
    old_value: str | None = None,
    new_value: str | None = None,
) -> Change:
    ensure_writable()
    row = Change(
        ts=ts,
        device_id=device_id,
        op=op,
        pixel_hash=pixel_hash,
        field=field,
        old_value=old_value,
        new_value=new_value,
    )
    session.add(row)
    return row


def change_key(row: Change) -> ChangeKey:
    """Identity of a change independent of its per-DB autoincrement ``id``.

    Two conflict copies that both descend from a common ancestor share the
    ancestor's change rows verbatim; merge de-dupes on this key so they apply
    once, not twice.
    """
    ts = row.ts.isoformat() if isinstance(row.ts, datetime) else str(row.ts)
    return (ts, row.device_id, str(row.op), row.pixel_hash, row.field, row.new_value)


def sort_key(row: Change) -> tuple[str, str, str]:
    """Total order for replay: by timestamp, then device, then op (LWW)."""
    ts = row.ts.isoformat() if isinstance(row.ts, datetime) else str(row.ts)
    return (ts, row.device_id, str(row.op))
