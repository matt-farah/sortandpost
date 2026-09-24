"""``sortandpost.cpu`` -- the ``clip.speed`` slow / fast budget."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

from sortandpost import cpu
from sortandpost.clip import model as clip_model
from sortandpost.config import AppConfig, ClipConfig
from sortandpost.cpu import clip_profile, default_workers
from sortandpost.paths import Paths


@pytest.mark.parametrize(
    ("cores", "slow", "fast"),
    [(1, 1, 1), (2, 1, 1), (4, 1, 3), (8, 2, 7), (10, 3, 9), (16, 5, 15)],
)
def test_default_workers_slow_is_about_30_percent(cores: int, slow: int, fast: int) -> None:
    assert default_workers("slow", cores) == slow
    assert default_workers("fast", cores) == fast


def test_slow_profile_caps_batch_and_lowers_priority() -> None:
    p = clip_profile("slow", 16, cores=10)
    assert (p.threads, p.batch_size, p.lower_priority) == (3, 8, True)


def test_slow_profile_respects_a_smaller_configured_batch() -> None:
    assert clip_profile("slow", 4, cores=10).batch_size == 4


def test_fast_profile_is_the_old_behaviour() -> None:
    p = clip_profile("fast", 16, cores=10)
    assert (p.threads, p.batch_size, p.lower_priority) == (9, 16, False)


def test_speed_defaults_to_slow_and_rejects_junk() -> None:
    assert ClipConfig().speed == "slow"
    with pytest.raises(ValueError):
        ClipConfig(speed="turbo")  # type: ignore[arg-type]


def test_worker_count_follows_speed_unless_explicit(
    bootstrapped: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: 10)
    cfg = AppConfig.load(bootstrapped.config_file)
    assert cfg.clip.speed == "slow"
    assert cfg.worker_count() == 3
    cfg.clip.speed = "fast"
    assert cfg.worker_count() == 9
    cfg.indexer.workers = 2
    assert cfg.worker_count() == 2


def test_speed_round_trips_through_config_yaml(bootstrapped: Paths, tmp_path: Path) -> None:
    cfg = AppConfig.load(bootstrapped.config_file)
    cfg.clip.speed = "fast"
    out = tmp_path / "config.yaml"
    cfg.dump(out)
    assert AppConfig.load(out).clip.speed == "fast"


def test_clip_model_profile_tracks_live_config() -> None:
    m = clip_model.ClipModel(ClipConfig(speed="slow", batch_size=16), Path("."))
    assert m.profile().batch_size == 8
    m.config.speed = "fast"
    assert m.profile().batch_size == 16


def test_apply_cpu_profile_sets_torch_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []

    class FakeTorch:
        @staticmethod
        def set_num_threads(n: int) -> None:
            seen.append(n)

    monkeypatch.setattr(clip_model, "torch", FakeTorch)
    monkeypatch.setattr(clip_model, "set_thread_priority", lambda _lowered: None)
    clip_model.apply_cpu_profile(clip_profile("slow", 16, cores=10))
    assert seen == [3]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX nice")
def test_lower_process_priority_calls_nice(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(os, "nice", lambda inc: calls.append(inc) or inc)
    cpu.lower_process_priority()
    assert calls == [cpu.NICE_INCREMENT]


def test_priority_helpers_swallow_os_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object) -> None:
        raise PermissionError("nope")

    monkeypatch.setattr(os, "nice", boom, raising=False)
    monkeypatch.setattr(os, "setpriority", boom, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    cpu.lower_process_priority()
    cpu.set_thread_priority(False)


def test_set_thread_priority_is_per_thread_and_linux_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "setpriority", lambda _w, tid, n: calls.append((tid, n)), raising=False)
    monkeypatch.setattr(os, "PRIO_PROCESS", 0, raising=False)

    monkeypatch.setattr(sys, "platform", "darwin")
    cpu.set_thread_priority(True)
    assert calls == []  # nice is process-wide off Linux: never touch the web process

    monkeypatch.setattr(sys, "platform", "linux")
    cpu.set_thread_priority(True)
    assert calls == [(threading.get_native_id(), cpu.NICE_INCREMENT)]
