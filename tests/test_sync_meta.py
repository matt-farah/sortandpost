from __future__ import annotations

from sqlalchemy.engine import Engine
from sqlmodel import Session

from sortandpost.db.meta import bump_generation, get_generation, get_meta, set_meta


def test_generation_starts_at_zero(engine: Engine) -> None:
    with Session(engine) as s:
        assert get_generation(s) == 0


def test_bump_generation_is_monotonic_and_persists(engine: Engine) -> None:
    with Session(engine) as s:
        assert bump_generation(s) == 1
        assert bump_generation(s) == 2
        s.commit()
    with Session(engine) as s:
        assert get_generation(s) == 2  # last committed value survives the reopen
        assert bump_generation(s) == 3


def test_meta_kv_upsert(engine: Engine) -> None:
    with Session(engine) as s:
        assert get_meta(s, "schema_version") is None
        set_meta(s, "schema_version", "7")
        set_meta(s, "schema_version", "8")
        s.commit()
    with Session(engine) as s:
        assert get_meta(s, "schema_version") == "8"


def test_garbage_generation_reads_as_zero(engine: Engine) -> None:
    with Session(engine) as s:
        set_meta(s, "db_generation", "not-a-number")
        s.commit()
        assert get_generation(s) == 0
