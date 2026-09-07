"""The ``meta`` key/value table: the monotonic ``db_generation`` counter plus
schema bookkeeping.

``db_generation`` is the load-bearing value for the sync model: it is bumped on
every publish, mirrored into the lock file, and compared on startup to decide
whether the synced DB is newer than the local working copy.
"""

from __future__ import annotations

from sqlmodel import Session, select

from sortandpost.db.models import Meta

GENERATION_KEY = "db_generation"
SCHEMA_VERSION_KEY = "schema_version"


def get_meta(session: Session, key: str, default: str | None = None) -> str | None:
    row = session.get(Meta, key)
    return row.value if row is not None else default


def set_meta(session: Session, key: str, value: str) -> None:
    row = session.get(Meta, key)
    if row is None:
        session.add(Meta(key=key, value=value))
    else:
        row.value = value


def get_generation(session: Session) -> int:
    raw = get_meta(session, GENERATION_KEY)
    try:
        return int(raw) if raw is not None else 0
    except ValueError:
        return 0


def set_generation(session: Session, value: int) -> None:
    set_meta(session, GENERATION_KEY, str(value))


def bump_generation(session: Session) -> int:
    """Increment and persist ``db_generation``; return the new value.

    The caller is responsible for committing (typically as part of the same
    transaction that publishes the DB).
    """
    nxt = get_generation(session) + 1
    set_generation(session, nxt)
    return nxt


def all_meta(session: Session) -> dict[str, str]:
    return {row.key: row.value for row in session.exec(select(Meta)).all()}
