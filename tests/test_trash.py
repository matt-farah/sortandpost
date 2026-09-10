"""``sortandpost.trash`` -- move-to-trash with a managed, reversible fallback.

The OS-Trash backend is platform-specific and would touch the real Recycle Bin,
so these tests pin the managed backend.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from sortandpost import trash

NOW = datetime(2026, 2, 3, 4, 5, 6)


@pytest.fixture(autouse=True)
def _managed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trash, "_use_os_trash", lambda: False)


def test_moves_files_into_a_timestamped_folder_with_a_manifest(tmp_path: Path) -> None:
    local = tmp_path / "local"
    local.mkdir()
    lib = tmp_path / "lib"
    lib.mkdir()
    a = lib / "a.jpg"
    b = lib / "sub" / "b.jpg"
    b.parent.mkdir()
    a.write_bytes(b"aaa")
    b.write_bytes(b"bbb")

    result = trash.move_to_trash([a, b], local_dir=local, now=NOW)

    assert result.backend == "managed"
    assert sorted(result.moved) == sorted([str(a), str(b)])
    assert not a.exists() and not b.exists()

    trash_dir = Path(result.trash_dir)
    assert trash_dir.parent == local / "trash"
    manifest = json.loads((trash_dir / "manifest.json").read_text())
    originals = {item["original"] for item in manifest["items"]}
    assert originals == {str(a), str(b)}
    for item in manifest["items"]:
        assert (trash_dir / item["stored"]).exists()


def test_missing_file_is_reported_not_raised(tmp_path: Path) -> None:
    local = tmp_path / "local"
    local.mkdir()
    result = trash.move_to_trash([tmp_path / "ghost.jpg"], local_dir=local, now=NOW)
    assert result.moved == []
    assert len(result.failed) == 1
    assert result.failed[0].error == "file not found"


def test_same_second_call_appends_to_the_existing_manifest(tmp_path: Path) -> None:
    local = tmp_path / "local"
    local.mkdir()
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "a.jpg").write_bytes(b"a")
    (lib / "b.jpg").write_bytes(b"b")

    r1 = trash.move_to_trash([lib / "a.jpg"], local_dir=local, now=NOW)
    r2 = trash.move_to_trash([lib / "b.jpg"], local_dir=local, now=NOW)

    assert r1.trash_dir == r2.trash_dir
    manifest = json.loads((Path(r2.trash_dir) / "manifest.json").read_text())
    assert len(manifest["items"]) == 2
    stored = {item["stored"] for item in manifest["items"]}
    assert stored == {"0000_a.jpg", "0001_b.jpg"}  # sequence continues


def test_restore_managed_round_trips(tmp_path: Path) -> None:
    local = tmp_path / "local"
    local.mkdir()
    lib = tmp_path / "lib"
    lib.mkdir()
    src = lib / "a.jpg"
    src.write_bytes(b"payload")

    result = trash.move_to_trash([src], local_dir=local, now=NOW)
    assert not src.exists()

    restored = trash.restore_managed(Path(result.trash_dir))
    assert [i.ok for i in restored] == [True]
    assert src.read_bytes() == b"payload"
