"""sortandpost.launcher_pref -- remembered APP_DATA folder pointer file."""

from __future__ import annotations

from pathlib import Path

import pytest

from sortandpost import launcher_pref


@pytest.fixture(autouse=True)
def _isolated_pref_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Never touch the real ~/.sortandpost while testing this module."""
    pref_dir = tmp_path / "prefs"
    monkeypatch.setattr(launcher_pref, "_default_pref_dir", lambda: pref_dir)
    return pref_dir


def test_read_remembered_app_data_returns_none_when_nothing_saved() -> None:
    assert launcher_pref.read_remembered_app_data() is None


def test_remember_and_read_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "my-appdata"
    launcher_pref.remember_app_data(target)
    assert launcher_pref.read_remembered_app_data() == target


def test_remember_overwrites_previous_value(tmp_path: Path) -> None:
    launcher_pref.remember_app_data(tmp_path / "first")
    launcher_pref.remember_app_data(tmp_path / "second")
    assert launcher_pref.read_remembered_app_data() == tmp_path / "second"


def test_read_remembered_app_data_tolerates_corrupt_file(_isolated_pref_dir: Path) -> None:
    _isolated_pref_dir.mkdir(parents=True, exist_ok=True)
    (_isolated_pref_dir / "app_data_path.json").write_text("not json", encoding="utf-8")
    assert launcher_pref.read_remembered_app_data() is None
