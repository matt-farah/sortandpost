from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from PIL import Image

from sortandpost.indexer.scanner import iter_image_files


def _touch_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(path, "JPEG")


def test_finds_images_recursively_sorted(tmp_path: Path) -> None:
    _touch_image(tmp_path / "b.jpg")
    _touch_image(tmp_path / "a.jpg")
    _touch_image(tmp_path / "sub" / "c.png")
    (tmp_path / "notes.txt").write_text("nope")
    (tmp_path / "sub" / "readme.md").write_text("nope")

    found = list(iter_image_files([tmp_path]))
    assert [p.name for p in found] == ["a.jpg", "b.jpg", "c.png"]


def test_skips_hidden_and_symlinked_dirs(
    tmp_path: Path, dir_link: Callable[[Path, Path], None]
) -> None:
    _touch_image(tmp_path / "keep.jpg")
    _touch_image(tmp_path / ".hidden" / "skip.jpg")
    real = tmp_path / "real"
    _touch_image(real / "x.jpg")
    dir_link(tmp_path / "link", real)  # a junction on Windows
    dir_link(real / "loop", tmp_path)  # pointing back up: must not recurse forever

    names = sorted(p.name for p in iter_image_files([tmp_path]))
    assert names == ["keep.jpg", "x.jpg"]  # link/ traversal skipped, not doubled


def test_recognises_raw_and_ignores_unknown_extensions(tmp_path: Path) -> None:
    (tmp_path / "DSC1.NEF").write_bytes(b"not really raw")
    (tmp_path / "clip.mov").write_bytes(b"video")
    _touch_image(tmp_path / "pic.jpeg")

    names = sorted(p.name for p in iter_image_files([tmp_path]))
    assert names == ["DSC1.NEF", "pic.jpeg"]


def test_exclude_prunes_subtree(tmp_path: Path) -> None:
    _touch_image(tmp_path / "lib" / "a.jpg")
    _touch_image(tmp_path / "appdata" / "sync" / "thumbnails" / "aa" / "x_320.jpg")

    found = list(iter_image_files([tmp_path], exclude=[tmp_path / "appdata"]))
    assert [p.name for p in found] == ["a.jpg"]


def test_missing_root_is_ignored(tmp_path: Path) -> None:
    assert list(iter_image_files([tmp_path / "does-not-exist"])) == []


def test_exclude_names_prunes_folders_case_insensitively_at_any_depth(tmp_path: Path) -> None:
    _touch_image(tmp_path / "keep.jpg")
    _touch_image(tmp_path / "RAW" / "r.jpg")
    _touch_image(tmp_path / "trip" / "raw" / "r2.jpg")
    _touch_image(tmp_path / "trip" / "af" / "a.jpg")
    _touch_image(tmp_path / "trip" / "good.jpg")

    names = sorted(p.name for p in iter_image_files([tmp_path], exclude_names=["raw", " AF "]))
    assert names == ["good.jpg", "keep.jpg"]


def test_exclude_names_empty_and_blank_entries_are_noops(tmp_path: Path) -> None:
    _touch_image(tmp_path / "a.jpg")
    _touch_image(tmp_path / "sub" / "b.jpg")

    names = sorted(p.name for p in iter_image_files([tmp_path], exclude_names=["", "   "]))
    assert names == ["a.jpg", "b.jpg"]


def test_an_unstatable_entry_is_skipped_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entry that raises on ``is_symlink`` / ``is_dir`` (a macOS data-vault
    where even ``lstat`` is "Operation not permitted") must not abort the walk --
    the sibling images still come back."""
    _touch_image(tmp_path / "a.jpg")
    _touch_image(tmp_path / "sub" / "b.jpg")
    (tmp_path / "vault").mkdir()

    real_is_symlink = Path.is_symlink

    def guard(self: Path) -> bool:
        if self.name == "vault":
            raise PermissionError(1, "Operation not permitted")
        return real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", guard)
    names = sorted(p.name for p in iter_image_files([tmp_path]))
    assert names == ["a.jpg", "b.jpg"]


def test_collapse_scope(tmp_path: Path) -> None:
    from sortandpost.indexer.scanner import collapse_scope

    lib = tmp_path / "lib"
    got = collapse_scope(
        [lib / "a" / "x.jpg", lib / "a", lib / "a-b", tmp_path / "outside", lib / "a" / "c"],
        [lib],
    )
    assert got == [lib / "a", lib / "a-b"]
    assert collapse_scope([lib / "a", lib], [lib]) is None
    assert collapse_scope([tmp_path], [lib]) is None  # contains a root
    assert collapse_scope([], [lib]) == []
