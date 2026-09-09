"""Database layer: SQLModel schema, engine/pragma setup, session helpers."""

from sortandpost.db.engine import init_db, make_engine, session_scope
from sortandpost.db.ratings import set_rating
from sortandpost.db.tags import add_manual_tag, remove_manual_tag

__all__ = [
    "add_manual_tag",
    "init_db",
    "make_engine",
    "remove_manual_tag",
    "session_scope",
    "set_rating",
]
