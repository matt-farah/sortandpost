"""``sortandpost.clip.model`` -- device resolution + cache check / download."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sortandpost.clip import model as clip_model
from sortandpost.clip.model import _resolve_device
from sortandpost.config import ClipConfig


@pytest.mark.parametrize("explicit", ["cpu", "mps", "cuda", "CPU", "  mps  "])
def test_explicit_device_is_passed_through_lowercased(explicit: str) -> None:
    assert _resolve_device(explicit) == explicit.strip().lower()


def test_auto_resolves_to_a_concrete_backend() -> None:
    got = _resolve_device("auto")
    assert got in {"cpu", "mps", "cuda"}


def test_auto_is_cpu_when_torch_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clip_model, "torch", None)
    assert _resolve_device("auto") == "cpu"


def test_empty_or_none_is_treated_as_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clip_model, "torch", None)
    assert _resolve_device("") == "cpu"


# --------------------------------------------------------------------------- #
# check_cached / ensure_downloaded -- the /settings "is CLIP downloaded?" +
# "download it" actions. ``open_clip`` itself is monkeypatched so these run
# without touching the network or a real (huge) model.
# --------------------------------------------------------------------------- #


class _FakeOpenClip:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.seen_offline: list[str | None] = []

    def create_model_and_transforms(self, model: str, pretrained: str, cache_dir: str, device: str):
        self.seen_offline.append(os.environ.get("HF_HUB_OFFLINE"))
        if self.fail:
            raise RuntimeError("boom")
        return object(), None, object()

    def get_tokenizer(self, model: str):
        return object()


def test_check_cached_reports_missing_torch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(clip_model, "_IMPORT_ERROR", ModuleNotFoundError("no torch"))
    ok, detail = clip_model.check_cached(ClipConfig(), tmp_path)
    assert ok is False
    assert "not installed" in detail


def test_check_cached_forces_hf_hub_offline_and_restores_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    fake = _FakeOpenClip()
    monkeypatch.setattr(clip_model, "open_clip", fake)
    monkeypatch.setattr(clip_model, "_IMPORT_ERROR", None)

    ok, detail = clip_model.check_cached(ClipConfig(), tmp_path)

    assert (ok, detail) == (True, "")
    assert fake.seen_offline == ["1"]  # offline during the call
    assert "HF_HUB_OFFLINE" not in os.environ  # restored afterwards


def test_ensure_downloaded_clears_hf_hub_offline_even_if_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")  # e.g. the documented offline dev run
    fake = _FakeOpenClip()
    monkeypatch.setattr(clip_model, "open_clip", fake)
    monkeypatch.setattr(clip_model, "_IMPORT_ERROR", None)

    ok, detail = clip_model.ensure_downloaded(ClipConfig(), tmp_path)

    assert (ok, detail) == (True, "")
    assert fake.seen_offline == [None]  # network allowed during the call
    assert os.environ.get("HF_HUB_OFFLINE") == "1"  # restored to the prior value


def test_check_cached_surfaces_the_failure_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(clip_model, "open_clip", _FakeOpenClip(fail=True))
    monkeypatch.setattr(clip_model, "_IMPORT_ERROR", None)

    ok, detail = clip_model.check_cached(ClipConfig(), tmp_path)

    assert ok is False
    assert "boom" in detail
