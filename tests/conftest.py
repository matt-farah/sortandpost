from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine

from sortandpost.config import bootstrap
from sortandpost.db import init_db, make_engine
from sortandpost.paths import Paths


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = Paths(app_data=tmp_path / "appdata")
    p.ensure_dirs()
    return p


@pytest.fixture
def bootstrapped(paths: Paths) -> Paths:
    bootstrap(paths)
    return paths


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_engine(tmp_path / "db" / "test.db")
    init_db(eng)
    yield eng
    eng.dispose()
