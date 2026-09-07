"""The ``changes`` append-only log — hard constraint #4.

Every user-authored mutation (rate, tag add/remove, CLIP approve/reject, unindex)
MUST call :func:`record_change` in the *same transaction* that writes the domain
table. Conflict-copy merge is deterministic only because it can replay this log;
a mutation that skips it is silently lost on a cross-machine merge.

The star widget / tag editors that call this arrive in build-order step 5; step 4
builds the log plumbing and the merge replay that consumes it.
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Session

from sortandpost.db.models import Change, ChangeOp

ChangeKey = tuple[str, str, str, str, str | None, str | None]


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
