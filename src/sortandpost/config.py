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

import hashlib
import json
import logging
import os
import socket
import uuid
from importlib import resources
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from sortandpost.cpu import ClipSpeed, default_workers
from sortandpost.launcher_pref import read_pointer
from sortandpost.library_roots import LibraryRoot, load_roots, running_in_docker, save_roots
from sortandpost.onboarding import settle_on_startup
from sortandpost.paths import Paths
from sortandpost.relocate import maybe_relocate

log = logging.getLogger(__name__)

PathsSource = Literal["env", "shared", "remembered", "dotenv", "unset"]


class Env(BaseSettings):
    """Per-machine settings from the environment. Never synced."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Which folders to use -- see :meth:`resolve_paths`. Precedence is
    # deliberately *not* just "whichever source pydantic-settings would
    # normally prefer": a genuine process/shell environment variable (Docker's
    # `environment: APP_DATA: /data`, or `APP_DATA=... command`) is a firm
    # instruction from whoever launched the process and always wins. But a bare
    # value sitting in a `.env` file is a soft default -- often a leftover from
    # a previous setup -- so it must rank *below* a folder the user has chosen
    # in the wizard or /settings (`os.environ` is checked directly to tell the
    # two apart; `self.app_data` alone can't, since pydantic-settings merges
    # both sources into it).
    app_data: Path | None = Field(default=None, alias="APP_DATA")
    port: int = Field(default=8080, alias="PORT")
    # Loopback by default (docs/security-review.md H1): a source run is only reachable from this
    # computer. The Docker image sets HOST=0.0.0.0 inside the container (compose still
    # publishes it on 127.0.0.1); other devices need a login -- see sortandpost.auth.
    host: str = Field(default="127.0.0.1", alias="HOST")
    clip_model_override: str | None = Field(default=None, alias="CLIP_MODEL")

    def resolve_paths(self) -> Paths | None:
        """The folders this launch uses, or ``None`` when nothing has been
        chosen yet (first run -> the onboarding wizard asks).

        1. a real ``APP_DATA`` env var -- single folder (``local/`` + ``sync/``)
        2. the shared folder picked in onboarding / /settings -- split layout,
           machine-local data in :func:`paths.local_dir_for` (one per library)
        3. a legacy single folder saved from /settings
        4. ``APP_DATA`` from a ``.env`` file -- single folder
        """
        if os.environ.get("APP_DATA"):
            assert self.app_data is not None
            return Paths(app_data=self.app_data.expanduser().resolve())
        ptr = read_pointer()
        if ptr is not None and ptr.kind == "sync_dir":
            return Paths.for_sync_dir(ptr.path.resolve())
        if ptr is not None:
            return Paths(app_data=ptr.path.resolve())
        if self.app_data is not None:  # from a .env file only
            return Paths(app_data=self.app_data.expanduser().resolve())
        return None

    def paths_source(self) -> PathsSource:
        """``"env"`` (a real process/shell ``APP_DATA`` -- always wins, the UI
        can't change it), ``"shared"`` (a shared folder picked in onboarding /
        /settings), ``"remembered"`` (a legacy single folder saved from
        /settings), ``"dotenv"`` (only a `.env` file has it), or ``"unset"``."""
        if os.environ.get("APP_DATA"):
            return "env"
        ptr = read_pointer()
        if ptr is not None:
            return "shared" if ptr.kind == "sync_dir" else "remembered"
        if self.app_data is not None:
            return "dotenv"
        return "unset"

    @property
    def paths(self) -> Paths:
        paths = self.resolve_paths()
        if paths is None:
            raise NoAppDataFolder
        return paths


class NoAppDataFolder(RuntimeError):
    """No data folder chosen yet on this machine (first run)."""

    def __str__(self) -> str:
        return (
            "sortandpost.photos hasn't been set up on this computer yet -- start the web "
            "app (python -m sortandpost) and finish the setup wizard, or set APP_DATA."
        )


class LibraryUnavailable(RuntimeError):
    """The saved data folder can't be opened (unmounted drive / NAS, deleted,
    no permission). The web app starts in setup mode and explains; the CLIs exit."""

    def __init__(self, folder: Path, reason: str, *, source: PathsSource, missing: bool) -> None:
        super().__init__(f"{folder}: {reason}")
        self.folder = folder
        self.reason = reason
        self.source = source
        self.missing = missing  # it may come back by itself (a drive / NAS reconnecting)

    def __str__(self) -> str:
        return f"The library folder {self.folder} isn't available: {self.reason}"


# --------------------------------------------------------------------------- #
# Synced config tree (config.yaml)
# --------------------------------------------------------------------------- #
class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080


class SyncConfig(BaseModel):
    publish_interval_sec: int = 60
    lock_heartbeat_sec: int = 20
    lock_stale_after_sec: int = 300
    # Editing checkout: how long the writer's screen shows "<machine> wants to
    # edit" (with Keep editing) before it hands over by itself.
    handoff_grace_sec: int = 30
    snapshot_every_publishes: int = 20
    # Also snapshot when the newest one is older than this (0 = off), so a quiet
    # library still gets a fresh backup in sync/snapshots/ every day it changes.
    snapshot_every_hours: int = 24
    snapshot_keep: int = 10
    sync_thumbnails: bool = True


class ClipConfig(BaseModel):
    # Library-wide switch (rides in the shared config.yaml). Off = no embedding /
    # tagging pass, no AI text search, no /review; the embeddings already in
    # sync/ are kept so turning it back on costs nothing.
    enabled: bool = True
    model: str = "ViT-B-32"
    pretrained: str = "laion2b_s34b_b79k"
    device: str = "auto"  # "auto" -> cuda / mps / cpu; or force "cpu" / "mps" / "cuda"
    batch_size: int = 16
    # "slow": ~30% of cores, batch <= 8, lowered OS priority -- a background scan
    # that leaves the machine usable. "fast": every core but one, full batch.
    speed: ClipSpeed = "slow"


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
    # What to do with cloud "online-only" placeholder files (OneDrive / Drive /
    # Dropbox / iCloud stubs whose bytes aren't on disk). "skip" records them and
    # leaves them alone; "download" hydrates every file it meets while scanning.
    online_only: Literal["skip", "download"] = "skip"
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


class DeletionConfig(BaseModel):
    """The explicit "Delete photos" action (docs/architecture.md constraint #1, exception 2)."""

    # "selected"    -> delete only the photo(s) the user clicked
    # "whole_stack" -> delete every member of a selected photo's stack (like ratings fan out)
    stack_scope: Literal["selected", "whole_stack"] = "selected"


class PostingConfig(BaseModel):
    """Which posting back-end is used, plus the clean JPEG made from each photo
    before it is uploaded. Never the source file: orientation applied, sRGB,
    all EXIF/GPS stripped. API keys are never here (OS keychain / env var)."""

    provider: str = "robopost"  # a key of sortandpost.posting.PROVIDERS
    base_urls: dict[str, str] = Field(default_factory=dict)  # provider -> its API address
    # provider -> {operation: calls per the spec's window}; 0 turns a limit off
    rate_limit_overrides: dict[str, dict[str, int]] = Field(default_factory=dict)
    export_long_edge: int = 2048  # 0 = full resolution
    export_quality: int = 90


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
    deletion: DeletionConfig = Field(default_factory=DeletionConfig)
    posting: PostingConfig = Field(default_factory=PostingConfig)

    @classmethod
    def load(cls, path: Path) -> AppConfig:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)

    @classmethod
    def load_lenient(cls, path: Path) -> tuple[AppConfig, str | None]:
        """Like :meth:`load`, but a broken file never stops the app: ``config.yaml``
        syncs, so one bad edit would brick every machine at once. Unparseable
        YAML -> all defaults; a wrong-typed value -> that setting's default (the
        rest is kept). Returns the config and what was wrong (``None`` if fine)."""
        text = path.read_text(encoding="utf-8")  # OSError: a folder problem, not ours
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            where = getattr(exc, "problem_mark", None)
            at = f" (line {where.line + 1})" if where is not None else ""
            return cls(), f"it isn't valid YAML{at}, so every setting is at its default"
        if data is None:
            return cls(), None
        if not isinstance(data, dict):
            return cls(), "it doesn't hold settings, so every setting is at its default"
        dropped: list[str] = []
        for _ in range(100):
            try:
                config = cls.model_validate(data)
            except ValidationError as exc:
                progressed = False
                for err in exc.errors():
                    key = _drop_key(data, err["loc"])
                    if key is not None:
                        dropped.append(key)
                        progressed = True
                if not progressed:
                    return cls(), f"{exc.error_count()} invalid value(s); using all defaults"
                continue
            if not dropped:
                return config, None
            names = ", ".join(dict.fromkeys(dropped))
            return config, f"invalid value(s) for {names}; using the default for those"
        return cls(), "too many invalid values; using all defaults"

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
        return default_workers(self.clip.speed)


def _drop_key(data: dict[str, object], loc: tuple[int | str, ...]) -> str | None:
    """Delete the deepest mapping key on a validation error's path (a list or a
    scalar is dropped whole). Returns its dotted name, or ``None`` if nothing
    could be dropped."""
    cur: object = data
    parent: dict[str, object] | None = None
    last = ""
    names: list[str] = []
    for key in loc:
        if not (isinstance(cur, dict) and isinstance(key, str) and key in cur):
            break
        parent, last = cur, key
        names.append(key)
        cur = cur[key]
    if parent is None:
        return None
    del parent[last]
    return ".".join(names)


def _keep_broken_config(paths: Paths, problem: str) -> Path | None:
    """Copy a ``config.yaml`` that didn't load into ``snapshots/`` (once per
    distinct content) before the app's next settings save rewrites it."""
    try:
        raw = paths.config_file.read_bytes()
        dest = paths.snapshots_dir / f"config-broken-{hashlib.sha256(raw).hexdigest()[:8]}.yaml"
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
    except OSError as exc:
        log.warning("could not keep a copy of the broken config.yaml: %s", exc)
        return None
    log.warning("config.yaml: %s (a copy is in %s)", problem, dest)
    return dest


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
    return load_device(paths)[0]


def load_device(paths: Paths) -> tuple[str, str]:
    """``(device_id, hostname)`` from ``local/device.json`` (written by
    :func:`bootstrap`). A pre-existing file without ``hostname`` falls back to
    the current machine's name."""
    data = json.loads(paths.device_file.read_text(encoding="utf-8"))
    return str(data["device_id"]), str(data.get("hostname") or socket.gethostname())


def _check_available(paths: Paths, source: PathsSource) -> None:
    """A folder the user picked earlier that isn't there now (a drive or NAS
    not mounted, a sync folder deleted) must not be silently recreated as an
    empty library -- or, on an unmounted volume, crash the boot."""
    if source not in ("shared", "remembered"):
        return  # APP_DATA / .env: creating it on first run is the expected setup
    folder = paths.sync if source == "shared" else paths.app_data
    assert folder is not None
    try:
        there = folder.is_dir()
    except OSError:
        there = False
    if not there:
        raise LibraryUnavailable(
            folder,
            "the folder isn't there. If it's on an external drive or a network share, connect it",
            source=source,
            missing=True,
        )


def _os_reason(exc: OSError) -> str:
    text = exc.strerror or str(exc)
    return f"{text} ({exc.filename})" if exc.filename else text


class Settings(BaseModel):
    """Everything resolved: env + synced config + identity."""

    model_config = {"arbitrary_types_allowed": True}

    env: Env
    paths: Paths
    config: AppConfig
    device_id: str
    hostname: str = ""
    # Per-device library folders (``sync/devices/<name>-<id8>/library.json``).
    # NOT ``config.library_roots`` -- see sortandpost.library_roots.
    library_roots: list[LibraryRoot] = Field(default_factory=list)
    # First-run wizard state for this machine (sortandpost.onboarding).
    onboarding_pending: bool = False
    joined_existing: bool = False
    # This launch moved local/ out of a cloud-synced folder (sortandpost.relocate).
    relocated: bool = False
    # config.yaml didn't load cleanly: what was wrong + where a copy was kept.
    config_problem: str | None = None
    config_backup: Path | None = None

    @classmethod
    def resolve(cls) -> Settings:
        """Raises :class:`NoAppDataFolder` (nothing chosen yet) or
        :class:`LibraryUnavailable` (the chosen folder can't be opened)."""
        env = Env()
        paths = env.paths
        source = env.paths_source()
        _check_available(paths, source)
        relocated = False
        try:
            if source in ("remembered", "dotenv"):
                # A legacy single folder inside OneDrive / Synology Drive / ...: move
                # this machine's working files out before anything opens them.
                moved = maybe_relocate(paths)
                if moved is not None:
                    paths, relocated = moved, True
            paths.local.mkdir(parents=True, exist_ok=True)
            onboarding = settle_on_startup(paths)  # before bootstrap creates device.json
            bootstrap(paths)
            config, config_problem = AppConfig.load_lenient(paths.config_file)
        except OSError as exc:
            folder = paths.app_data or paths.sync
            raise LibraryUnavailable(folder, _os_reason(exc), source=source, missing=False) from exc
        config_backup = (
            _keep_broken_config(paths, config_problem) if config_problem is not None else None
        )
        if env.clip_model_override:
            config.clip.model = env.clip_model_override
        device_id, hostname = load_device(paths)
        roots = load_roots(paths, device_id, hostname)
        if not roots and onboarding.fresh_device and running_in_docker():
            # A new container: compose always mounts the photo library at /library.
            # The shared config's roots are whichever machine made the library
            # (a Mac / Windows path when joining one), so they'd scan nothing here.
            host = os.environ.get("SORTANDPOST_HOST_LIBRARY_DIR", "").strip() or "/library"
            roots = [LibraryRoot(container="/library", host=host)]
            save_roots(paths, device_id, hostname, roots)
        elif not roots and config.library_roots and not onboarding.fresh_device:
            # First run after upgrading: seed the per-device file once from the
            # old shared config, then treat the per-device file as canonical.
            # A brand-new native machine gets its folders from onboarding
            # instead (the template's "/library" is a Docker mount point).
            roots = [LibraryRoot(container=str(p), host=str(p)) for p in config.library_roots]
            save_roots(paths, device_id, hostname, roots)
        return cls(
            env=env,
            paths=paths,
            config=config,
            device_id=device_id,
            hostname=hostname,
            library_roots=roots,
            onboarding_pending=onboarding.pending,
            joined_existing=onboarding.joined_existing,
            relocated=relocated,
            config_problem=config_problem,
            config_backup=config_backup,
        )

    def set_library_roots(self, roots: list[str | Path]) -> None:
        """Replace the in-memory root list (``host`` == ``container``). Used by
        tests and the first-run seed; the Settings UI persists its edits via
        :mod:`sortandpost.library_roots`."""
        self.library_roots = [LibraryRoot(container=str(r), host=str(r)) for r in roots]
