"""SQLite engine construction with the pragmas the sync model depends on.

``journal_mode=DELETE`` (not WAL) so no ``-wal``/``-shm`` sidecars can leak into
the synced folder; ``synchronous=FULL`` and ``foreign_keys=ON`` for integrity.
The app runs against ``local/sortandpost.db`` and publishes snapshots to
``sync/sortandpost.db`` separately — see :mod:`sortandpost.sync`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

# Import models so SQLModel.metadata is fully populated on `import sortandpost.db`.
from sortandpost.db import models as models


def make_engine(db_path: Path, *, echo: bool = False) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        echo=echo,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA journal_mode=DELETE")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    """Create any missing tables. In production Alembic owns the schema; this is
    for tests and first-run bootstrap before migrations are wired."""
    SQLModel.metadata.create_all(engine)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
