"""App-data folder layout.

Two roots: a machine-local one and a shared (synced) one. Two ways to place them:

* **legacy / single folder** -- ``Paths(app_data=X)``: ``X/local`` + ``X/sync``.
  What a real ``APP_DATA`` env var (Docker, dev) and older saved pointers give.
* **split** -- ``Paths.split(local=L, sync=S)``: the machine-local data lives in a
  per-machine OS folder (:func:`default_local_root`) and ``S`` is the folder the
  user shares with their other machines. What first-run onboarding sets up, so a
  second machine that points at the same shared folder never shares the first
  one's ``device.json`` or working DB.

    <local>/          machine-specific, never synced
        sortandpost.db (+ -wal/-shm/-journal while running)
        device.json
        publish_state.json
        <provider>.json  posting back-end cache: channels, rate-limit log (no secrets)
        post_exports/  temporary clean JPEGs made for an upload
        models/        downloaded CLIP weights
        logs/
    <sync>/           user points their file-sync tool HERE
        sortandpost.db          published snapshot (cross-machine source of truth)
        .sortandpost.lock
        config.yaml
        tags.yaml
        posts/<post_id>.json    social posts (one file each)
        snapshots/  (+ snapshots/merged/)
        thumbnails/<hh>/<pixel_hash>_<size>.webp
        embeddings/img_<model>.npy, text_<model>_<vocabhash>.npy, rowmap.json

See CLAUDE.md → "App-data layout" and "Sync model".
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def default_local_root() -> Path:
    """The per-machine folder for machine-only data in the split layout
    (``~/Library/Application Support/sortandpost``, ``%LOCALAPPDATA%\\sortandpost``,
    ``$XDG_DATA_HOME/sortandpost``). ``SORTANDPOST_LOCAL_DIR`` overrides it (tests,
    simulating a fresh machine)."""
    override = os.environ.get("SORTANDPOST_LOCAL_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "sortandpost"
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "sortandpost"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "sortandpost"


def local_dir_for(sync_dir: Path, base: Path | None = None) -> Path:
    """This machine's private folder for one shared library in the split layout:
    ``<default_local_root>/libraries/<name>-<hash8>``. One per shared folder, so
    switching libraries never carries a working DB (or device identity) from one
    library into another."""
    import hashlib

    key = str(sync_dir.expanduser().resolve())
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    name = _slug(sync_dir.name or "library").strip("-.") or "library"
    return (base or default_local_root()) / "libraries" / f"{name}-{digest}"


@dataclass(frozen=True)
class Paths:
    """Resolved locations. Nothing here touches disk until :meth:`ensure_dirs`
    is called. Build with ``Paths(app_data=X)`` (legacy single folder) or
    :meth:`split`."""

    app_data: Path | None = None
    local_root: Path | None = None
    sync_root: Path | None = None
    models_root: Path | None = None  # split layout: CLIP weights shared by every library

    def __post_init__(self) -> None:
        if self.app_data is None and (self.local_root is None or self.sync_root is None):
            raise ValueError("Paths needs app_data, or both local_root and sync_root")

    @classmethod
    def split(cls, *, local: Path, sync: Path, models: Path | None = None) -> Paths:
        return cls(local_root=local, sync_root=sync, models_root=models)

    @classmethod
    def for_sync_dir(cls, sync_dir: Path) -> Paths:
        """The split layout for a shared folder picked in onboarding / settings."""
        base = default_local_root()
        return cls.split(local=local_dir_for(sync_dir, base), sync=sync_dir, models=base / "models")

    @property
    def is_split(self) -> bool:
        return self.app_data is None

    def roots(self) -> list[Path]:
        """Every folder this app owns -- pruned from library scans."""
        return [self.app_data] if self.app_data is not None else [self.local, self.sync]

    # --- local subtree (never synced) ---------------------------------------
    @property
    def local(self) -> Path:
        if self.local_root is not None:
            return self.local_root
        assert self.app_data is not None
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

    def provider_file(self, provider: str) -> Path:
        """Non-secret per-posting-provider state (``robopost.json``,
        ``postiz.json``): channel list + rate-limit log. API keys are NEVER
        stored here -- they live in the OS keychain or an env var (see
        :mod:`sortandpost.secrets_store`). Never synced."""
        return self.local / f"{provider}.json"

    @property
    def post_exports_dir(self) -> Path:
        """Temporary clean-JPEG exports made for an upload, one folder per post.
        Removed after a successful send. Never synced."""
        return self.local / "post_exports"

    @property
    def models_dir(self) -> Path:
        return self.models_root if self.models_root is not None else self.local / "models"

    @property
    def logs_dir(self) -> Path:
        return self.local / "logs"

    # --- sync subtree (user points their sync tool here) -------------------
    @property
    def sync(self) -> Path:
        if self.sync_root is not None:
            return self.sync_root
        assert self.app_data is not None
        return self.app_data / "sync"

    @property
    def published_db(self) -> Path:
        return self.sync / "sortandpost.db"

    @property
    def lock_file(self) -> Path:
        return self.sync / ".sortandpost.lock"

    @property
    def locks_dir(self) -> Path:
        """Edit requests for the editing checkout, one file per requesting
        device (:mod:`sortandpost.sync.handoff`)."""
        return self.sync / "locks"

    @property
    def config_file(self) -> Path:
        return self.sync / "config.yaml"

    @property
    def tags_file(self) -> Path:
        return self.sync / "tags.yaml"

    @property
    def saved_searches_file(self) -> Path:
        """Named `/search` states. Synced (like ``tags.yaml``), not merged."""
        return self.sync / "searches.json"

    @property
    def collections_file(self) -> Path:
        """User photo collections. Synced (like ``tags.yaml``), not merged."""
        return self.sync / "collections.json"

    @property
    def posts_dir(self) -> Path:
        """Social posts, one ``<post_id>.json`` per post. Synced, not merged --
        one file per post so two machines editing different posts never
        collide. See :mod:`sortandpost.posts`."""
        return self.sync / "posts"

    @property
    def devices_dir(self) -> Path:
        """Per-device config, keyed by ``<slug(hostname)>-<device_id[:8]>``.

        Rides in ``sync/`` so a device's library-folder list is backed up by the
        file-sync tool, but each device only ever writes its own subfolder, so
        two devices never collide (unlike ``config.yaml``). See
        :mod:`sortandpost.library_roots`.
        """
        return self.sync / "devices"

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
            self.devices_dir,
            self.posts_dir,
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
