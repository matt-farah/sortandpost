"""Move a single-folder install's ``local/`` out of a cloud-sync folder.

A legacy single-folder install (``Paths(app_data=X)``: ``X/local`` + ``X/sync``)
whose ``X`` sits inside a sync tool's folder has its machine-only data synced
too -- the working SQLite DB, its journal, the managed trash, CLIP weights.
That is exactly what the sync model forbids (docs/architecture.md -> "Sync model"), and it
broke a real library on 2026-10-08 (see :mod:`sortandpost.cloud_folders`).

:func:`maybe_relocate` runs at startup, before anything opens ``local/``, and
turns such an install into the split layout the first-run wizard sets up:

* ``X/sync`` stays where it is and keeps syncing -- settings (``config.yaml``),
  profile, tags, per-device library folders, collections, posts, the published
  DB, snapshots, thumbnails and embeddings all still travel between computers;
* ``X/local`` is copied to this machine's per-library folder
  (:func:`sortandpost.paths.local_dir_for`), keeping ``device.json`` -- the same
  ``device_id``, so the write lock and ``sync/devices/<host>-<id8>/`` stay this
  machine's -- and CLIP weights go to the shared ``models/`` folder;
* the launcher pointer becomes ``{"sync_dir": "X/sync"}``.

It copies into a staging folder and renames it into place, so a kill
mid-copy leaves the old install untouched and the next launch starts over.
The old ``X/local`` is removed only when every file that matters copied;
otherwise it is renamed ``X/local.moved-<ts>`` and the leftovers are listed in
``local/relocated.json`` (shown in ``/settings``). A DB whose journal can't be
read out of the cloud is handed to :mod:`sortandpost.sync.recovery` as a
salvage copy instead of being used as-is.

Only ``remembered`` / ``dotenv`` sources are moved. A real ``APP_DATA``
environment variable (Docker, dev) is the launcher's explicit choice -- the app
warns instead (:func:`cloud_provider` on ``paths.local``).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sortandpost.cloud_folders import cloud_provider
from sortandpost.launcher_pref import remember_sync_dir
from sortandpost.paths import Paths

log = logging.getLogger(__name__)

_RECORD = "relocated.json"
_DB = "sortandpost.db"
_SIDECARS = ("-journal", "-wal", "-shm")
_MODELS = "models"
# Regenerable / disposable: a failure to copy these never keeps the old folder.
_DISPOSABLE_TOP = {_MODELS, "logs", "post_exports", ".DS_Store", "desktop.ini", "Thumbs.db"}
_LOCK_FRESH_SEC = 300  # SyncConfig.lock_stale_after_sec default; config isn't loaded yet


@dataclass
class Relocation:
    provider: str
    old_local: str
    new_local: str
    moved_at: str
    old_kept_at: str | None = None  # set when the old folder couldn't be removed
    leftovers: list[str] = field(default_factory=list)  # files that couldn't be copied
    db_salvaged: bool = False  # the DB went to recovery (an unreadable journal)


def record_file(paths: Paths) -> Path:
    return paths.local / _RECORD


def read_record(paths: Paths) -> Relocation | None:
    try:
        return Relocation(**json.loads(record_file(paths).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined,unused-ignore]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _in_use(old: Paths) -> bool:
    """Another process of this machine is running against *old* right now: its
    fresh write lock names our device and a live pid that isn't us."""
    try:
        device = json.loads(old.device_file.read_text(encoding="utf-8"))["device_id"]
        lock = json.loads(old.lock_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if lock.get("device_id") != device:
        return False
    try:
        beat = datetime.fromisoformat(str(lock["heartbeat_at"]))
        pid = int(lock.get("pid") or 0)
    except (KeyError, ValueError, TypeError):
        return False
    if beat.tzinfo is None:
        beat = beat.replace(tzinfo=UTC)
    fresh = (datetime.now(UTC) - beat).total_seconds() < _LOCK_FRESH_SEC
    return fresh and pid != os.getpid() and pid_alive(pid)


def _copy_tree(src: Path, dst: Path, failures: list[str], *, label: str) -> None:
    """``copytree`` that records unreadable files instead of stopping."""
    dst.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir()):
        target = dst / entry.name
        rel = f"{label}/{entry.name}"
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                _copy_tree(entry, target, failures, label=rel)
            else:
                shutil.copy2(entry, target)
        except OSError as exc:
            log.warning("relocate: could not copy %s: %s", entry, exc)
            failures.append(rel)


def _same_size(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_size == b.stat().st_size
    except OSError:
        return False


def _now() -> datetime:
    return datetime.now(UTC)


def maybe_relocate(paths: Paths, *, home: Path | None = None) -> Paths | None:
    """Move a cloud-synced single-folder ``local/`` out. Returns the new split
    :class:`Paths` (and has saved the pointer), or ``None`` when nothing moved."""
    if paths.is_split:
        return None
    old_local = paths.local
    provider = cloud_provider(old_local, home=home)
    if provider is None:
        return None
    new = Paths.for_sync_dir(paths.sync)
    if cloud_provider(new.local, home=home) is not None:
        log.warning("relocate: the per-machine folder %s is itself synced; not moving", new.local)
        return None
    if _in_use(paths):
        log.warning("relocate: %s is in use by another sortandpost process; not moving", old_local)
        return None

    now = _now()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    rec = Relocation(
        provider=provider,
        old_local=str(old_local),
        new_local=str(new.local),
        moved_at=now.isoformat(timespec="seconds"),
    )

    if old_local.is_dir():
        log.warning(
            "moving this computer's working files out of %s: %s -> %s",
            provider,
            old_local,
            new.local,
        )
        staging = new.local.with_name(new.local.name + ".moving")
        shutil.rmtree(staging, ignore_errors=True)  # a previous, interrupted attempt
        staging.mkdir(parents=True)
        failures: list[str] = []

        for entry in sorted(old_local.iterdir()):
            name = entry.name
            if name == _DB or any(name == _DB + s for s in _SIDECARS):
                continue  # below
            if name == _MODELS:
                if entry.is_dir() and not any(new.models_dir.glob("models--*")):
                    _copy_tree(entry, new.models_dir, [], label=_MODELS)  # re-downloadable
                continue
            try:
                if entry.is_dir():
                    _copy_tree(entry, staging / name, failures, label=name)
                elif not entry.is_symlink():
                    shutil.copy2(entry, staging / name)
            except OSError as exc:
                log.warning("relocate: could not copy %s: %s", entry, exc)
                failures.append(name)

        db = old_local / _DB
        if db.exists():
            try:
                shutil.copy2(db, staging / _DB)
                if not _same_size(db, staging / _DB):
                    raise OSError("size mismatch after copy")
            except OSError as exc:
                log.error("relocate: could not copy the working DB %s: %s", db, exc)
                failures.append(_DB)
            else:
                for suffix in _SIDECARS:
                    side = old_local / (_DB + suffix)
                    if not side.exists():
                        continue
                    try:
                        shutil.copy2(side, staging / side.name)
                    except OSError as exc:
                        # An unreadable journal: the DB can't be trusted as-is.
                        # Let recovery rebuild from sync/ and merge this in.
                        log.error("relocate: %s can't be read (%s); salvaging the DB", side, exc)
                        salvage = staging / "salvage"
                        salvage.mkdir(exist_ok=True)
                        for s in _SIDECARS:
                            (staging / (_DB + s)).unlink(missing_ok=True)
                        (staging / _DB).replace(salvage / f"relocated-{stamp}.db")
                        rec.db_salvaged = True
                        break

        # Disposable things (logs, caches) never hold the old folder back.
        rec.leftovers = [f for f in failures if f.split("/", 1)[0] not in _DISPOSABLE_TOP]
        if new.local.exists() and any(new.local.iterdir()):
            aside = new.local.with_name(f"{new.local.name}.replaced-{stamp}")
            log.warning("relocate: %s already existed; kept as %s", new.local, aside)
            new.local.replace(aside)
        elif new.local.exists():
            new.local.rmdir()
        new.local.parent.mkdir(parents=True, exist_ok=True)
        staging.replace(new.local)

    remember_sync_dir(paths.sync)

    if old_local.is_dir():
        if rec.leftovers:
            kept = old_local.with_name(f"local.moved-{stamp}")
            try:
                old_local.replace(kept)
                rec.old_kept_at = str(kept)
            except OSError as exc:
                log.warning("relocate: could not rename %s: %s", old_local, exc)
                rec.old_kept_at = str(old_local)
            log.warning(
                "relocate: %d file(s) couldn't be copied; old folder kept at %s",
                len(rec.leftovers),
                rec.old_kept_at,
            )
        else:
            shutil.rmtree(old_local, ignore_errors=True)
            if old_local.exists():  # e.g. a placeholder the provider won't let go of
                rec.old_kept_at = str(old_local)

    new.local.mkdir(parents=True, exist_ok=True)
    record_file(new).write_text(json.dumps(asdict(rec), indent=2), encoding="utf-8")
    log.warning("moved working files out of %s to %s", provider, new.local)
    return new
