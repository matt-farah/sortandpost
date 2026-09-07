from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from sortandpost.db.models import Change, ChangeOp
from sortandpost.sync.changelog import change_key, record_change, sort_key

WHEN = datetime(2026, 9, 6, 8, 0, 0, tzinfo=UTC)


def test_record_change_appends_a_row(engine: Engine) -> None:
    with Session(engine) as s:
        record_change(
            s,
            device_id="dev-a",
            op=ChangeOp.rate,
            pixel_hash="p" * 64,
            ts=WHEN,
            field="stars",
            old_value=None,
            new_value="4",
        )
        s.commit()
    with Session(engine) as s:
        rows = s.exec(select(Change)).all()
        assert len(rows) == 1
        assert rows[0].op is ChangeOp.rate
        assert rows[0].new_value == "4"


def test_change_key_ignores_the_autoincrement_id() -> None:
    a = Change(id=1, ts=WHEN, device_id="d", op=ChangeOp.rate, pixel_hash="x" * 64, new_value="5")
    b = Change(id=999, ts=WHEN, device_id="d", op=ChangeOp.rate, pixel_hash="x" * 64, new_value="5")
    c = Change(id=2, ts=WHEN, device_id="d", op=ChangeOp.rate, pixel_hash="x" * 64, new_value="3")
    assert change_key(a) == change_key(b)
    assert change_key(a) != change_key(c)


def test_sort_key_orders_by_ts_then_device() -> None:
    early = Change(ts=WHEN, device_id="a", op=ChangeOp.rate, pixel_hash="x" * 64)
    later = Change(
        ts=datetime(2026, 9, 6, 9, tzinfo=UTC), device_id="a", op=ChangeOp.rate, pixel_hash="x" * 64
    )
    tie_b = Change(ts=WHEN, device_id="b", op=ChangeOp.rate, pixel_hash="x" * 64)
    assert sorted([later, tie_b, early], key=sort_key) == [early, tie_b, later]
