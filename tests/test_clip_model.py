"""``sortandpost.clip.model._resolve_device`` -- "auto" -> best backend."""

from __future__ import annotations

import pytest

from sortandpost.clip import model as clip_model
from sortandpost.clip.model import _resolve_device


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
