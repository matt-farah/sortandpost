from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import keyring
import keyring.backend
import keyring.errors
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


class MemoryKeyring(keyring.backend.KeyringBackend):
    """In-process keyring so no test ever reads or writes the real OS keychain."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.store:
            raise keyring.errors.PasswordDeleteError(username)
        del self.store[(service, username)]


@pytest.fixture(autouse=True)
def memory_keyring(monkeypatch: pytest.MonkeyPatch) -> Iterator[MemoryKeyring]:
    """Every test gets a fresh in-memory keyring and no ROBOPOST_API_KEY."""
    monkeypatch.delenv("ROBOPOST_API_KEY", raising=False)
    previous = keyring.get_keyring()
    kr = MemoryKeyring()
    keyring.set_keyring(kr)
    yield kr
    keyring.set_keyring(previous)
