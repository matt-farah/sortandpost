"""``sync/`` online-only check (Docker can't read cloud placeholders)."""

from __future__ import annotations

import os
from collections.abc import Collection
from pathlib import Path

import pytest

from sortandpost.sync import online_only
from sortandpost.sync.online_only import find_online_only

_FILES = [
    "sortandpost.db",
    ".sortandpost.lock",
    "config.yaml",
    "embeddings/img_ViT-B-32.npy",
    "thumbnails/ab/ab1_320.webp",
    "thumbnails/ab/ab2_320.webp",
    "thumbnails/cd/cd1_320.webp",
    "posts/p1.json",
]
_STUB = b"stub!!!"  # a real placeholder needs a cloud client; fake one by its size


def _tree(root: Path, stubs: Collection[str] = ()) -> None:
    for rel in _FILES:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_STUB if rel in stubs else b"x")


@pytest.fixture(autouse=True)
def _fake_placeholders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        online_only, "is_online_only", lambda st: os.stat_result(st).st_size == len(_STUB)
    )


def test_nothing_online_only(tmp_path: Path) -> None:
    _tree(tmp_path)
    report = find_online_only(tmp_path)
    assert not report.any and report.complete
    assert report.checked == len(_FILES)


def test_reports_important_files_and_examples(tmp_path: Path) -> None:
    _tree(tmp_path, {"sortandpost.db", "thumbnails/ab/ab1_320.webp", "thumbnails/cd/cd1_320.webp"})
    report = find_online_only(tmp_path)
    assert report.found == 3
    assert report.important == ["sortandpost.db"]
    assert report.examples[0] == "sortandpost.db"  # important files are checked first
    assert "thumbnails/ab/ab1_320.webp" in report.examples


def test_stops_early_on_a_big_folder(tmp_path: Path) -> None:
    _tree(tmp_path, {"thumbnails/ab/ab1_320.webp", "thumbnails/ab/ab2_320.webp", "posts/p1.json"})
    report = find_online_only(tmp_path, stop_after=2)
    assert report.found == 2 and not report.complete
    report = find_online_only(tmp_path, max_files=3)
    assert report.checked == 3 and not report.complete
