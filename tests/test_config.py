from __future__ import annotations

import json

from sortandpost.config import AppConfig, bootstrap, load_device_id
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
