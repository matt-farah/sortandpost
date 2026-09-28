"""Alembic environment.

DB URL resolution order:
  1. ``SORTANDPOST_DB_URL`` env var
  2. the working DB of whatever data folder the app itself would open
     (``Env.resolve_paths``: ``APP_DATA``, else the folder chosen in the
     welcome wizard / settings, else ``.env``); ``./appdata`` if none

``target_metadata`` is SQLModel's registry, so ``alembic revision --autogenerate``
diffs against :mod:`sortandpost.db.models`.
"""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

import sortandpost.db.models  # noqa: F401  (populate SQLModel.metadata)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def _db_url() -> str:
    if url := os.environ.get("SORTANDPOST_DB_URL"):
        return url
    from sortandpost.config import Env
    from sortandpost.paths import Paths

    paths = Env().resolve_paths() or Paths(app_data=Path("appdata").resolve())
    paths.local.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{paths.working_db}"


def run_migrations_offline() -> None:
    context.configure(
        url=_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,  # SQLite needs batch mode for ALTER
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _db_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
