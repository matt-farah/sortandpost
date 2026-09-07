from __future__ import annotations

from pathlib import Path

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


def test_skips_hidden_and_symlinked_dirs(tmp_path: Path) -> None:
    _touch_image(tmp_path / "keep.jpg")
    _touch_image(tmp_path / ".hidden" / "skip.jpg")
    real = tmp_path / "real"
    _touch_image(real / "x.jpg")
    (tmp_path / "link").symlink_to(real, target_is_directory=True)

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
