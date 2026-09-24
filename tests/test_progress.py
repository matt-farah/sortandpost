"""Live scan / CLIP progress (`sortandpost.indexer.progress`) and its wiring
into `scan_once` / `scan_clip_once`."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from sortandpost.config import Settings
from sortandpost.indexer import scan as scan_mod
from sortandpost.indexer import scan_clip_once, scan_once
from sortandpost.indexer.progress import PROGRESS, ProgressSnapshot, ProgressTracker
from tests.test_clip_scan import FakeClipModel


def test_tracker_starts_idle_and_labels_stages() -> None:
    t = ProgressTracker()
    assert not t.snapshot().active
    assert t.snapshot().label() == ""

    t.begin("indexing", total=4300)
    t.update(done=120, current="a.jpg")
    snap = t.snapshot()
    assert (snap.done, snap.total, snap.current) == (120, 4300, "a.jpg")
    assert snap.label() == "Indexing 120/4,300"

    t.begin("embedding", total=10)
    assert t.snapshot().label() == "Embedding 0/10"  # begin() resets the counters

    t.begin("discovering")
    t.update(done=1234)
    assert t.snapshot().label() == "Scanning folders… 1,234 found"
    assert t.snapshot().fraction is None  # total unknown while walking

    t.finish()
    assert not t.snapshot().active


def test_update_while_idle_is_ignored() -> None:
    t = ProgressTracker()
    t.update(done=5, total=9)
    assert t.snapshot() == ProgressSnapshot()


def test_fraction_and_eta() -> None:
    snap = ProgressSnapshot(stage="indexing", done=25, total=100, started_at=100.0)
    assert snap.fraction == 0.25
    assert snap.eta_seconds(now=150.0) == pytest.approx(150.0)  # 50 s for 25 -> 150 s for 75
    assert (
        ProgressSnapshot(stage="indexing", done=1, total=100, started_at=0).eta_seconds(5) is None
    )
    assert ProgressSnapshot(stage="indexing", done=100, total=100).eta_seconds(5) is None
    assert ProgressSnapshot(stage="indexing", done=5, total=0).fraction is None


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    library = tmp_path / "library"
    library.mkdir()
    for i in range(5):
        Image.new("RGB", (32, 24), (40 * i, 10, 10)).save(library / f"p{i}.jpg", "JPEG")
    monkeypatch.setenv("APP_DATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("PORT", raising=False)
    resolved = Settings.resolve()
    resolved.set_library_roots([library])
    return resolved


def test_scan_reports_per_file_progress_then_goes_idle(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[ProgressSnapshot] = []
    real = scan_mod.persist_decoded

    def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(PROGRESS.snapshot())
        return real(*args, **kwargs)

    monkeypatch.setattr(scan_mod, "persist_decoded", spy)
    scan_once(settings, workers=1)

    assert [(s.stage, s.done, s.total) for s in seen] == [("indexing", n, 5) for n in range(1, 6)]
    assert all(s.current.endswith(".jpg") for s in seen)
    assert not PROGRESS.snapshot().active


def test_clip_pass_reports_batches_then_goes_idle(settings: Settings) -> None:
    scan_once(settings, workers=1)
    settings.config.clip.batch_size = 2
    seen: list[ProgressSnapshot] = []

    class Spy(FakeClipModel):
        def encode_images(self, images: list[Image.Image]) -> np.ndarray:
            seen.append(PROGRESS.snapshot())
            return super().encode_images(images)

    assert scan_clip_once(settings, model=Spy()).embedded == 5
    assert [(s.stage, s.done, s.total) for s in seen] == [
        ("embedding", 0, 5),
        ("embedding", 2, 5),
        ("embedding", 4, 5),
    ]
    assert not PROGRESS.snapshot().active


def test_progress_resets_when_a_pass_raises(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(scan_mod, "sweep_presence", boom)
    with pytest.raises(RuntimeError):
        scan_once(settings, workers=1)
    assert not PROGRESS.snapshot().active
