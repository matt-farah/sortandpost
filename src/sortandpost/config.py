"""Configuration: process environment + the synced ``config.yaml``.

Two layers:

* :class:`Env` — read from process environment / ``.env`` (paths and port only).
  These differ per machine and must never be synced.
* :class:`AppConfig` — the tree mirrored from ``APP_DATA/sync/config.yaml``.
  This syncs across machines.

:func:`bootstrap` creates the app-data folder structure and seeds
``config.yaml``, ``tags.yaml`` and ``device.json`` on first run.
"""

from __future__ import annotations

import json
import socket
import uuid
from importlib import resources
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from sortandpost.paths import Paths

_DEFAULT_APP_DATA = Path("appdata").resolve()


class Env(BaseSettings):
    """Per-machine settings from the environment. Never synced."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_data: Path = Field(default=_DEFAULT_APP_DATA, alias="APP_DATA")
    port: int = Field(default=8080, alias="PORT")
    host: str = Field(default="0.0.0.0", alias="HOST")
    clip_model_override: str | None = Field(default=None, alias="CLIP_MODEL")

    @property
    def paths(self) -> Paths:
        return Paths(app_data=self.app_data.resolve())


# --------------------------------------------------------------------------- #
# Synced config tree (config.yaml)
# --------------------------------------------------------------------------- #
class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080


class SyncConfig(BaseModel):
    publish_interval_sec: int = 60
    lock_heartbeat_sec: int = 20
    lock_stale_after_sec: int = 300
    snapshot_every_publishes: int = 20
    snapshot_keep: int = 10
    sync_thumbnails: bool = True


class ClipConfig(BaseModel):
    model: str = "ViT-B-32"
    pretrained: str = "laion2b_s34b_b79k"
    device: str = "cpu"
    batch_size: int = 16


class TaggingConfig(BaseModel):
    vocab_file: str = "tags.yaml"
    score_threshold: float = 0.22
    top_k: int = 8
    prompt_templates: list[str] = Field(default_factory=lambda: ["a photo of {}", "{}"])


class ThumbnailsConfig(BaseModel):
    sizes: list[int] = Field(default_factory=lambda: [320, 1280])
    format: str = "webp"
    quality: int = 80


class IndexerConfig(BaseModel):
    full_scan_interval_min: int = 15
    workers: str | int = "auto"
    clip_deferred: bool = True
    run_in_app: bool = True  # run the continuous indexer inside the web app process
    watch: bool = True  # react to filesystem events (needs `watchdog`), not just the timer
    debounce_sec: float = 2.0  # quiet period after an FS event before a rescan
    # Directory names to skip entirely while scanning (case-insensitive, matched
    # at any depth), e.g. ["RAW", "af", "Lightroom"]. Photos already indexed from
    # a newly-excluded folder fall out of the library on the next scan.
    exclude_dir_names: list[str] = Field(default_factory=list)


class RatingConfig(BaseModel):
    write_windows_compat_tags: bool = True


class StackingConfig(BaseModel):
    """Group near-duplicate photos in the same folder into one grid entry.
    Computed at read time -- toggling this never triggers a re-index."""

    enabled: bool = False
    raw_jpeg: bool = True  # pair a RAW with its same-named JPEG
    similar_names: bool = True  # group "img.jpg" / "img copy.jpg" / "img (1).jpg" / "copy img.jpg"


class AppConfig(BaseModel):
    """Mirror of ``APP_DATA/sync/config.yaml``."""

    library_roots: list[Path] = Field(default_factory=lambda: [Path("/library")])
    server: ServerConfig = Field(default_factory=ServerConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    clip: ClipConfig = Field(default_factory=ClipConfig)
    tagging: TaggingConfig = Field(default_factory=TaggingConfig)
    thumbnails: ThumbnailsConfig = Field(default_factory=ThumbnailsConfig)
    indexer: IndexerConfig = Field(default_factory=IndexerConfig)
    rating: RatingConfig = Field(default_factory=RatingConfig)
    stacking: StackingConfig = Field(default_factory=StackingConfig)

    @classmethod
    def load(cls, path: Path) -> AppConfig:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)

    def dump(self, path: Path) -> None:
        path.write_text(
            yaml.safe_dump(
                json.loads(self.model_dump_json()), sort_keys=False, default_flow_style=False
            ),
            encoding="utf-8",
        )

    def worker_count(self) -> int:
        workers = self.indexer.workers
        if isinstance(workers, int):
            return max(1, workers)
        import os

        return max(1, (os.cpu_count() or 2) - 1)


def _resource_text(name: str) -> str:
    return resources.files("sortandpost.resources").joinpath(name).read_text(encoding="utf-8")


def bootstrap(paths: Paths) -> None:
    """Create the app-data layout and seed first-run files. Idempotent."""
    paths.ensure_dirs()

    if not paths.config_file.exists():
        paths.config_file.write_text(_resource_text("config.default.yaml"), encoding="utf-8")

    if not paths.tags_file.exists():
        paths.tags_file.write_text(_resource_text("tags.default.yaml"), encoding="utf-8")

    if not paths.device_file.exists():
        paths.device_file.write_text(
            json.dumps({"device_id": uuid.uuid4().hex, "hostname": socket.gethostname()}, indent=2),
            encoding="utf-8",
        )


def load_device_id(paths: Paths) -> str:
    return str(json.loads(paths.device_file.read_text(encoding="utf-8"))["device_id"])


class Settings(BaseModel):
    """Everything resolved: env + synced config + identity."""

    model_config = {"arbitrary_types_allowed": True}

    env: Env
    paths: Paths
    config: AppConfig
    device_id: str

    @classmethod
    def resolve(cls) -> Settings:
        env = Env()
        paths = env.paths
        bootstrap(paths)
        config = AppConfig.load(paths.config_file)
        if env.clip_model_override:
            config.clip.model = env.clip_model_override
        return cls(env=env, paths=paths, config=config, device_id=load_device_id(paths))
