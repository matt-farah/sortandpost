"""App-data folder layout.

    APP_DATA/
      local/          machine-specific, never synced
        sortandpost.db (+ -wal/-shm/-journal while running)
        device.json
        publish_state.json
        models/        downloaded CLIP weights
        logs/
      sync/           user points their file-sync tool HERE
        sortandpost.db          published snapshot (cross-machine source of truth)
        .sortandpost.lock
        config.yaml
        tags.yaml
        snapshots/  (+ snapshots/merged/)
        thumbnails/<hh>/<pixel_hash>_<size>.webp
        embeddings/img_<model>.npy, text_<model>_<vocabhash>.npy, rowmap.json

See CLAUDE.md → "App-data layout" and "Sync model".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    """Resolved locations under a single APP_DATA root. Nothing here touches disk
    until :meth:`ensure_dirs` is called."""

    app_data: Path

    # --- local subtree (never synced) ---------------------------------------
    @property
    def local(self) -> Path:
        return self.app_data / "local"

    @property
    def working_db(self) -> Path:
        """The SQLite file the app actually runs against."""
        return self.local / "sortandpost.db"

    @property
    def device_file(self) -> Path:
        return self.local / "device.json"

    @property
    def publish_state_file(self) -> Path:
        return self.local / "publish_state.json"

    @property
    def ui_prefs_file(self) -> Path:
        """Per-machine UI preferences (theme, ...). Never synced."""
        return self.local / "ui_prefs.json"

    @property
    def models_dir(self) -> Path:
        return self.local / "models"

    @property
    def logs_dir(self) -> Path:
        return self.local / "logs"

    # --- sync subtree (user points their sync tool here) -------------------
    @property
    def sync(self) -> Path:
        return self.app_data / "sync"

    @property
    def published_db(self) -> Path:
        return self.sync / "sortandpost.db"

    @property
    def lock_file(self) -> Path:
        return self.sync / ".sortandpost.lock"

    @property
    def config_file(self) -> Path:
        return self.sync / "config.yaml"

    @property
    def tags_file(self) -> Path:
        return self.sync / "tags.yaml"

    @property
    def snapshots_dir(self) -> Path:
        return self.sync / "snapshots"

    @property
    def merged_snapshots_dir(self) -> Path:
        return self.snapshots_dir / "merged"

    @property
    def thumbnails_dir(self) -> Path:
        return self.sync / "thumbnails"

    @property
    def embeddings_dir(self) -> Path:
        return self.sync / "embeddings"

    # --- helpers ----------------------------------------------------------
    def image_embeddings_file(self, model: str) -> Path:
        return self.embeddings_dir / f"img_{_slug(model)}.npy"

    def text_embeddings_file(self, model: str, vocab_hash: str) -> Path:
        return self.embeddings_dir / f"text_{_slug(model)}_{vocab_hash}.npy"

    @property
    def embeddings_rowmap_file(self) -> Path:
        return self.embeddings_dir / "rowmap.json"

    def thumbnail_file(self, pixel_hash: str, size: int, fmt: str = "webp") -> Path:
        return self.thumbnails_dir / pixel_hash[:2] / f"{pixel_hash}_{size}.{fmt}"

    def all_dirs(self) -> list[Path]:
        return [
            self.local,
            self.models_dir,
            self.logs_dir,
            self.sync,
            self.snapshots_dir,
            self.merged_snapshots_dir,
            self.thumbnails_dir,
            self.embeddings_dir,
        ]

    def ensure_dirs(self) -> None:
        for d in self.all_dirs():
            d.mkdir(parents=True, exist_ok=True)


def _slug(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-." else "-" for c in value)
