"""``sortandpost.indexer.hydrate.hydrate_online_only`` -- the "Download & index" path."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from sqlmodel import Session, select

from sortandpost.config import Settings
from sortandpost.db import make_engine
from sortandpost.db.models import Photo, ScanState
from sortandpost.indexer import scan_once
from sortandpost.indexer.hydrate import hydrate_online_only


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setenv("APP_DATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("PORT", raising=False)
    resolved = Settings.resolve()
    resolved.set_library_roots([library])
    return resolved


def _lib(settings: Settings) -> Path:
    return Path(settings.library_roots[0].container)


def _jpeg(path: Path) -> None:
    Image.new("RGB", (32, 24), (10, 120, 200)).save(path, "JPEG", quality=90)


def _session(settings: Settings) -> Session:
    return Session(make_engine(settings.paths.working_db))


def test_hydrate_clears_the_flag_and_a_rescan_indexes_the_file(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = _lib(settings)
    _jpeg(lib / "stub.jpg")

    monkeypatch.setattr("sortandpost.indexer.workers.is_online_only", lambda _st: True)
    scan_once(settings)
    with _session(settings) as s:
        assert s.get(ScanState, str(lib / "stub.jpg")).online_only is True

    monkeypatch.setattr("sortandpost.indexer.workers.is_online_only", lambda _st: False)
    stats = hydrate_online_only(settings)
    assert (stats.requested, stats.hydrated, stats.failed) == (1, 1, 0)

    with _session(settings) as s:
        row = s.get(ScanState, str(lib / "stub.jpg"))
        assert row.online_only is False
        assert row.size is None  # fast-path reset so the next scan re-decodes it

    scan_once(settings)
    with _session(settings) as s:
        assert len(s.exec(select(Photo)).all()) == 1


def test_hydrate_counts_a_read_failure_and_keeps_the_flag(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = _lib(settings)
    _jpeg(lib / "stub.jpg")
    monkeypatch.setattr("sortandpost.indexer.workers.is_online_only", lambda _st: True)
    scan_once(settings)

    real_open = Path.open

    def boom(self: Path, *a: object, **kw: object):  # type: ignore[no-untyped-def]
        if self.name == "stub.jpg":
            raise OSError("network drive offline")
        return real_open(self, *a, **kw)

    monkeypatch.setattr(Path, "open", boom)
    stats = hydrate_online_only(settings)
    assert (stats.requested, stats.hydrated, stats.failed) == (1, 0, 1)

    with _session(settings) as s:
        assert s.get(ScanState, str(lib / "stub.jpg")).online_only is True
