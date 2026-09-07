from __future__ import annotations

from pathlib import Path

from sortandpost.paths import Paths


def test_all_paths_under_app_data(tmp_path: Path) -> None:
    root = tmp_path / "appdata"
    p = Paths(app_data=root)
    candidates = [
        p.working_db,
        p.device_file,
        p.publish_state_file,
        p.models_dir,
        p.logs_dir,
        p.published_db,
        p.lock_file,
        p.config_file,
        p.tags_file,
        p.snapshots_dir,
        p.merged_snapshots_dir,
        p.thumbnails_dir,
        p.embeddings_dir,
        p.embeddings_rowmap_file,
        p.image_embeddings_file("ViT-B-32"),
        p.text_embeddings_file("ViT-B-32", "abc123"),
        p.thumbnail_file("deadbeef" * 8, 320),
    ]
    for c in candidates:
        assert root in c.parents


def test_local_and_sync_are_separate_subtrees(tmp_path: Path) -> None:
    p = Paths(app_data=tmp_path / "appdata")
    assert p.local != p.sync
    assert p.working_db.parent == p.local
    assert p.published_db.parent == p.sync
    # The working DB must never live in the synced subtree.
    assert p.sync not in p.working_db.parents


def test_ensure_dirs_creates_layout(tmp_path: Path) -> None:
    p = Paths(app_data=tmp_path / "appdata")
    p.ensure_dirs()
    for d in p.all_dirs():
        assert d.is_dir()


def test_thumbnail_is_sharded_by_hash_prefix(tmp_path: Path) -> None:
    p = Paths(app_data=tmp_path / "appdata")
    h = "abcd" + "0" * 60
    tf = p.thumbnail_file(h, 1280)
    assert tf.parent.name == "ab"
    assert tf.name == f"{h}_1280.webp"
