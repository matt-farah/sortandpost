from __future__ import annotations

import json
from pathlib import Path

import pytest

from sortandpost import launcher_pref
from sortandpost.config import AppConfig, Env, NoAppDataFolder, bootstrap, load_device_id
from sortandpost.paths import Paths


def test_bootstrap_seeds_first_run_files(paths: Paths) -> None:
    bootstrap(paths)
    assert paths.config_file.is_file()
    assert paths.tags_file.is_file()
    assert paths.device_file.is_file()

    device = json.loads(paths.device_file.read_text())
    assert len(device["device_id"]) == 32
    assert device["hostname"]


def test_bootstrap_is_idempotent_and_preserves_edits(paths: Paths) -> None:
    bootstrap(paths)
    paths.config_file.write_text(
        paths.config_file.read_text().replace("threshold: 0.22", "threshold: 0.5")
    )
    first_device = load_device_id(paths)

    bootstrap(paths)  # second run must not clobber

    assert "threshold: 0.5" in paths.config_file.read_text()
    assert load_device_id(paths) == first_device


def test_default_config_loads_and_matches_model_defaults(bootstrapped: Paths) -> None:
    cfg = AppConfig.load(bootstrapped.config_file)
    assert cfg.clip.model == "ViT-B-32"
    assert cfg.thumbnails.sizes == [320, 1280]
    assert cfg.sync.snapshot_keep == 10
    assert cfg.tagging.prompt_templates == ["a photo of {}", "{}"]


def test_config_round_trips(bootstrapped: Paths, tmp_path) -> None:
    cfg = AppConfig.load(bootstrapped.config_file)
    cfg.tagging.score_threshold = 0.31
    out = tmp_path / "config.yaml"
    cfg.dump(out)
    assert AppConfig.load(out).tagging.score_threshold == 0.31


def test_worker_count_auto_and_explicit(bootstrapped: Paths) -> None:
    cfg = AppConfig.load(bootstrapped.config_file)
    assert cfg.worker_count() >= 1
    cfg.indexer.workers = 3
    assert cfg.worker_count() == 3


# --------------------------------------------------------------------------- #
# Env.resolve_paths / paths_source -- a real process/shell APP_DATA env var
# (Docker's fixed mount, or `APP_DATA=... command`) > the shared folder picked in
# onboarding / settings > a legacy single folder saved from /settings > a bare
# value left in a .env file > nothing (first run -> the welcome wizard). Passing
# `APP_DATA=` as an `Env(...)` kwarg (rather than via monkeypatch.setenv)
# simulates a `.env`-file-only value, since it populates `self.app_data` without
# touching the real `os.environ` -- exactly what `paths_source` uses to tell the
# two apart.
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolated_pref_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Never touch the real ~/.sortandpost while testing this resolution logic.
    monkeypatch.setattr(launcher_pref, "_default_pref_dir", lambda: tmp_path / "prefs")
    monkeypatch.delenv("APP_DATA", raising=False)


def test_real_env_var_wins_over_a_remembered_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launcher_pref.remember_sync_dir(tmp_path / "shared")
    monkeypatch.setenv("APP_DATA", str(tmp_path / "explicit"))
    env = Env(_env_file=None)
    assert env.paths_source() == "env"
    paths = env.resolve_paths()
    assert paths is not None and not paths.is_split
    assert paths.sync == (tmp_path / "explicit").resolve() / "sync"


def test_shared_folder_gives_the_split_layout(tmp_path: Path) -> None:
    launcher_pref.remember_sync_dir(tmp_path / "shared")
    env = Env(_env_file=None)
    assert env.paths_source() == "shared"
    paths = env.resolve_paths()
    assert paths is not None and paths.is_split
    assert paths.sync == (tmp_path / "shared").resolve()
    machine = tmp_path / "_machine"  # SORTANDPOST_LOCAL_DIR from conftest
    assert paths.local.parent == machine / "libraries"
    assert paths.models_dir == machine / "models"


def test_legacy_remembered_folder_keeps_single_layout(tmp_path: Path) -> None:
    launcher_pref.remember_app_data(tmp_path / "remembered")
    env = Env(_env_file=None)
    assert env.paths_source() == "remembered"
    paths = env.resolve_paths()
    assert paths is not None
    assert paths.local == (tmp_path / "remembered").resolve() / "local"


def test_remembered_folder_wins_over_a_dotenv_only_value(tmp_path: Path) -> None:
    launcher_pref.remember_sync_dir(tmp_path / "shared")
    env = Env(_env_file=None, APP_DATA=str(tmp_path / "dotenv-value"))
    assert env.paths_source() == "shared"
    paths = env.resolve_paths()
    assert paths is not None and paths.sync == (tmp_path / "shared").resolve()


def test_dotenv_only_value_used_when_nothing_saved(tmp_path: Path) -> None:
    env = Env(_env_file=None, APP_DATA=str(tmp_path / "dotenv-value"))
    assert env.paths_source() == "dotenv"
    paths = env.resolve_paths()
    assert paths is not None and paths.app_data == (tmp_path / "dotenv-value").resolve()


def test_nothing_set_means_first_run(tmp_path: Path) -> None:
    env = Env(_env_file=None)
    assert env.paths_source() == "unset"
    assert env.resolve_paths() is None
    with pytest.raises(NoAppDataFolder):
        _ = env.paths
