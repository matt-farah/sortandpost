from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import keyring
import keyring.backend
import keyring.errors
import pytest
from sqlalchemy.engine import Engine

from sortandpost.config import bootstrap
from sortandpost.db import init_db, make_engine
from sortandpost.paths import Paths
from sortandpost.secrets_store import forget_cached_key


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--docker",
        action="store_true",
        default=False,
        help="also run tests/docker (builds the image; needs a running Docker daemon)",
    )


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


def _dir_link(link: Path, target: Path) -> None:
    if sys.platform == "win32":
        # A symlink needs Developer Mode / admin on Windows; a junction doesn't.
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.fixture
def dir_link() -> Callable[[Path, Path], None]:
    """``dir_link(link, target)``: make *link* a directory link to *target* -- a
    symlink, or a junction on Windows."""
    return _dir_link


@pytest.fixture(autouse=True)
def _isolated_machine_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No test ever reads the real ``~/.sortandpost`` pointer or writes under the
    real per-machine data folder (``~/Library/Application Support/...``)."""
    monkeypatch.setenv("SORTANDPOST_PREF_DIR", str(tmp_path / "_prefs"))
    monkeypatch.setenv("SORTANDPOST_LOCAL_DIR", str(tmp_path / "_machine"))


@pytest.fixture
def needs_permission_bits(tmp_path: Path) -> None:
    """Skip unless chmod really locks files on the test's own filesystem. It doesn't
    for root, on Windows, or through a Docker Desktop bind mount (virtiofs: a 0444 file
    stays writable from the container -- tests/docker/test_in_container.py)."""
    if sys.platform == "win32":
        pytest.skip("chmod doesn't lock files on Windows")
    probe = tmp_path / ".perm-probe"
    probe.write_bytes(b"x")
    probe.chmod(0o444)
    try:
        with probe.open("r+b"):
            pass
    except PermissionError:
        return
    finally:
        probe.chmod(0o644)
        probe.unlink()
    pytest.skip("this filesystem ignores permission bits (root or a container bind mount)")


# Every module that imported ``running_in_docker`` by name (patch each spelling).
_DOCKER_CHECKS = (
    "sortandpost.library_roots",
    "sortandpost.config",
    "sortandpost.app",
    "sortandpost.posts_ui",
    "sortandpost.update.source",
    "sortandpost.indexer.scan",
)


@pytest.fixture(autouse=True)
def _native_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests describe a native run unless they say otherwise, so the suite passes
    the same when it runs inside the container (tests/docker/test_in_container.py).
    A test about Docker behaviour patches ``running_in_docker`` back to True."""
    import importlib

    for name in _DOCKER_CHECKS:
        module = importlib.import_module(name)
        if hasattr(module, "running_in_docker"):
            monkeypatch.setattr(module, "running_in_docker", lambda: False)


@pytest.fixture(autouse=True)
def memory_keyring(monkeypatch: pytest.MonkeyPatch) -> Iterator[MemoryKeyring]:
    """Every test gets a fresh in-memory keyring and no posting API key env vars."""
    monkeypatch.delenv("ROBOPOST_API_KEY", raising=False)
    monkeypatch.delenv("POSTIZ_API_KEY", raising=False)
    previous = keyring.get_keyring()
    kr = MemoryKeyring()
    keyring.set_keyring(kr)
    forget_cached_key()
    yield kr
    keyring.set_keyring(previous)
    forget_cached_key()
