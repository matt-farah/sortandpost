"""``sortandpost.trash`` -- the managed, per-machine "oops" trash.

Every delete lands in ``local/trash/<batch>/``; :func:`put_back` reverses it and
:func:`flush_to_system_trash` hands it to the OS Trash (mocked here -- never the
real Recycle Bin).
"""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest

from sortandpost import trash

NOW = datetime(2026, 2, 3, 4, 5, 6)


def _lib(tmp_path: Path) -> tuple[Path, Path]:
    local = tmp_path / "local"
    local.mkdir()
    lib = tmp_path / "lib"
    lib.mkdir()
    return local, lib


def test_moves_files_into_a_batch_folder_with_a_manifest(tmp_path: Path) -> None:
    local, lib = _lib(tmp_path)
    a = lib / "a.jpg"
    b = lib / "sub" / "b.jpg"
    b.parent.mkdir()
    a.write_bytes(b"aaa")
    b.write_bytes(b"bbb")

    result = trash.move_to_trash(
        [a, b],
        local_dir=local,
        now=NOW,
        pixel_hash_of={str(a): "ha", str(b): "hb"},
        photo_state={"ha": {"rating": {"stars": 4}}, "zz": {"rating": None}},
    )

    assert result.backend == "managed"
    assert sorted(result.moved) == sorted([str(a), str(b)])
    assert not a.exists() and not b.exists()

    assert result.trash_dir is not None
    trash_dir = Path(result.trash_dir)
    assert trash_dir.parent == local / "trash"
    manifest = json.loads((trash_dir / "manifest.json").read_text())
    assert manifest["reason"] == "delete"
    assert {i["original"] for i in manifest["items"]} == {str(a), str(b)}
    assert {i["pixel_hash"] for i in manifest["items"]} == {"ha", "hb"}
    for item in manifest["items"]:
        stored = trash_dir / item["stored"]
        assert stored.exists()
        assert stored.name == Path(item["original"]).name  # original name kept
    assert set(manifest["photos"]) == {"ha"}  # state only for hashes that moved


def test_missing_file_is_reported_and_no_batch_is_created(tmp_path: Path) -> None:
    local, _ = _lib(tmp_path)
    result = trash.move_to_trash([tmp_path / "ghost.jpg"], local_dir=local, now=NOW)
    assert result.moved == []
    assert len(result.failed) == 1
    assert result.failed[0].error == "file not found"
    assert result.trash_dir is None
    assert trash.list_batches(local) == []


def test_same_timestamp_gets_a_separate_batch(tmp_path: Path) -> None:
    local, lib = _lib(tmp_path)
    (lib / "a.jpg").write_bytes(b"a")
    (lib / "b.jpg").write_bytes(b"b")

    r1 = trash.move_to_trash([lib / "a.jpg"], local_dir=local, now=NOW)
    r2 = trash.move_to_trash([lib / "b.jpg"], local_dir=local, now=NOW)

    assert r1.trash_dir != r2.trash_dir
    assert len(trash.list_batches(local)) == 2


def test_list_batches_newest_first_and_thumbs(tmp_path: Path) -> None:
    local, lib = _lib(tmp_path)
    (lib / "a.jpg").write_bytes(b"a")
    (lib / "b.jpg").write_bytes(b"b")
    thumb = tmp_path / "t.webp"
    thumb.write_bytes(b"webp")

    trash.move_to_trash(
        [lib / "a.jpg"],
        local_dir=local,
        now=NOW,
        pixel_hash_of={str(lib / "a.jpg"): "ha"},
        thumbs={"ha": thumb},
    )
    later = trash.move_to_trash([lib / "b.jpg"], local_dir=local, now=datetime(2026, 2, 3, 4, 5, 7))

    batches = trash.list_batches(local)
    assert [b.id for b in batches] == [later.batch_id, batches[1].id]
    assert batches[1].hashes == ["ha"]
    t = batches[1].thumb("ha")
    assert t is not None and t.read_bytes() == b"webp"


def test_find_batch_rejects_path_tricks(tmp_path: Path) -> None:
    local, _ = _lib(tmp_path)
    assert trash.find_batch(local, "../local") is None
    assert trash.find_batch(local, "") is None
    assert trash.find_batch(local, "nope") is None


def test_put_back_round_trips_and_removes_the_batch(tmp_path: Path) -> None:
    local, lib = _lib(tmp_path)
    src = lib / "a.jpg"
    src.write_bytes(b"payload")

    result = trash.move_to_trash(
        [src],
        local_dir=local,
        now=NOW,
        pixel_hash_of={str(src): "ha"},
        photo_state={"ha": {"rating": {"stars": 5}}},
    )
    assert not src.exists()
    assert result.trash_dir is not None

    pb = trash.put_back(Path(result.trash_dir))
    assert [i.ok for i in pb.items] == [True]
    assert pb.hashes == ["ha"]
    assert pb.photos == {"ha": {"rating": {"stars": 5}}}
    assert src.read_bytes() == b"payload"
    assert not Path(result.trash_dir).exists()


def test_put_back_refuses_an_occupied_path(tmp_path: Path) -> None:
    local, lib = _lib(tmp_path)
    src = lib / "a.jpg"
    src.write_bytes(b"old")
    result = trash.move_to_trash([src], local_dir=local, now=NOW)
    assert result.trash_dir is not None
    src.write_bytes(b"new file at the same path")

    pb = trash.put_back(Path(result.trash_dir))
    assert pb.restored == []
    assert pb.failed[0].error == "a file already exists at the original path"
    assert src.read_bytes() == b"new file at the same path"  # untouched
    assert len(trash.list_batches(local)) == 1  # still recoverable


def test_put_back_never_recreates_a_missing_folder(tmp_path: Path) -> None:
    local, lib = _lib(tmp_path)
    sub = lib / "sub"
    sub.mkdir()
    src = sub / "a.jpg"
    src.write_bytes(b"x")
    result = trash.move_to_trash([src], local_dir=local, now=NOW)
    assert result.trash_dir is not None
    sub.rmdir()

    pb = trash.put_back(Path(result.trash_dir))
    assert pb.failed[0].error == "the original folder no longer exists"
    assert not sub.exists()


def test_put_back_only_some_hashes(tmp_path: Path) -> None:
    local, lib = _lib(tmp_path)
    a, b = lib / "a.jpg", lib / "b.jpg"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    result = trash.move_to_trash(
        [a, b], local_dir=local, now=NOW, pixel_hash_of={str(a): "ha", str(b): "hb"}
    )
    assert result.trash_dir is not None

    pb = trash.put_back(Path(result.trash_dir), only_hashes=["hb"])
    assert pb.hashes == ["hb"]
    assert b.exists() and not a.exists()
    [left] = trash.list_batches(local)
    assert left.hashes == ["ha"]


def test_flush_to_system_trash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local, lib = _lib(tmp_path)
    (lib / "a.jpg").write_bytes(b"a")
    trash.move_to_trash([lib / "a.jpg"], local_dir=local, now=NOW)

    sent: list[str] = []
    fake = types.ModuleType("send2trash")
    fake.send2trash = sent.append  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "send2trash", fake)
    monkeypatch.setattr(trash, "system_trash_available", lambda: True)

    res = trash.flush_to_system_trash(local)
    assert res.moved == 1 and res.batches_cleared == 1
    assert [Path(p).name for p in sent] == ["a.jpg"]
    assert trash.list_batches(local) == []


def test_flush_refuses_without_a_system_trash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(trash, "system_trash_available", lambda: False)
    with pytest.raises(RuntimeError):
        trash.flush_to_system_trash(tmp_path)
