"""Boot-time recovery of a broken working DB -- before anything opens it.

:class:`~sortandpost.sync.manager.SyncManager` already restores a snapshot when
``PRAGMA quick_check`` fails, but it runs *after* the web app has opened the
working DB, and some failures stop that very first connection: on 2026-10-08 a
sync tool evicted ``local/sortandpost.db-journal`` to the cloud (macOS
``dataless``) and never uploaded it, so SQLite -- which must read a hot journal
to roll back on every open -- failed every query with ``disk I/O error`` and the
app could not boot at all.

:func:`recover_working_db` runs first, on every entrypoint (web app, indexer
CLI, db CLI):

1. **Check** the working DB the way the app will open it (read-write, so a
   readable hot journal is rolled back as usual). A missing DB is fine.
2. If it is broken, **move it aside** with its ``-journal`` / ``-wal`` / ``-shm``
   into ``local/recovered/broken-<ts>/`` -- never deleted -- and keep a copy of
   the DB file alone as a *salvage* candidate (``local/salvage/``).
3. **Rebuild** the working DB from the best intact source: the published
   ``sync/sortandpost.db`` or the newest snapshot that passes ``quick_check``
   (the higher ``db_generation`` wins; each is read fully first, so an
   online-only copy is downloaded -- or reported -- before use).
4. **Fold in** every salvage DB that passes ``quick_check`` with the
   conflict-copy merge (:func:`~sortandpost.sync.merge.merge_all`): its
   ``changes`` log is unioned and replayed last-writer-wins, so ratings / tags
   made after the last publish survive. If no intact source exists, the best
   salvage copy becomes the working DB.
5. Write ``local/recovery.json`` for ``/settings`` and log it.

A salvage file can also be left by :mod:`sortandpost.relocate` when a sidecar
couldn't be copied out of a cloud folder.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from sqlmodel import Session

from sortandpost.db import make_engine
from sortandpost.paths import Paths
from sortandpost.sync.clock import Clock, isoformat, utcnow
from sortandpost.sync.merge import merge_all
from sortandpost.sync.publish import atomic_replace, quick_check, read_generation
from sortandpost.sync.snapshots import list_snapshots, restore_snapshot

log = logging.getLogger(__name__)

SIDECARS = ("-journal", "-wal", "-shm")
_REPORT = "recovery.json"
_READ_CHUNK = 1 << 20


@dataclass
class RecoveryReport:
    at: str
    reason: str  # why the working DB was set aside ("" when only salvage was merged)
    set_aside: str | None  # local/recovered/broken-<ts>/, if anything was moved there
    base: str  # what the working DB was rebuilt from
    base_generation: int = 0
    salvaged: list[str] = field(default_factory=list)  # salvage DBs merged in
    unusable: list[str] = field(default_factory=list)  # salvage / sources that failed checks
    merge: str = ""  # the merge summary, if any

    def describe(self) -> str:
        text = f"rebuilt from {self.base}"
        if self.salvaged:
            text += ", with the edits made since the last sync"
        return f"{self.reason}. Database {text}" if self.reason else f"Database {text}"


def sidecars(db: Path) -> list[Path]:
    return [db.with_name(db.name + s) for s in SIDECARS]


def read_fully(path: Path) -> bool:
    """Read *path* end to end. For a cloud placeholder this makes the sync client
    download it; ``False`` when it can't (gone from the server, offline, ...)."""
    try:
        with path.open("rb") as fh:
            while fh.read(_READ_CHUNK):
                pass
    except OSError as exc:
        log.warning("cannot read %s: %s", path, exc)
        return False
    return True


def check_working_db(db: Path) -> str | None:
    """``None`` when *db* is absent or opens and passes ``quick_check`` the way the
    app opens it; otherwise a short reason."""
    if not db.exists():
        return None
    for side in sidecars(db):
        if side.exists() and not read_fully(side):
            return f"{side.name} can't be read (a sync tool may have moved it to the cloud)"
    if not read_fully(db):
        return f"{db.name} can't be read"
    try:
        conn = sqlite3.connect(db, timeout=5)
    except sqlite3.Error as exc:
        return f"{db.name} can't be opened: {exc}"
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc) or "busy" in str(exc):
            return None  # another of our processes is writing it: in use, not broken
        return f"{db.name}: {exc}"
    except sqlite3.Error as exc:
        return f"{db.name}: {exc}"
    finally:
        conn.close()
    if not row or row[0] != "ok":
        return f"{db.name} failed its integrity check ({row[0] if row else 'no result'})"
    return None


def salvage_dir(paths: Paths) -> Path:
    return paths.local / "salvage"


def recovered_dir(paths: Paths) -> Path:
    return paths.local / "recovered"


def report_file(paths: Paths) -> Path:
    return paths.local / _REPORT


def read_report(paths: Paths) -> RecoveryReport | None:
    try:
        data = json.loads(report_file(paths).read_text(encoding="utf-8"))
        return RecoveryReport(**data)
    except (OSError, ValueError, TypeError):
        return None


def _stamp(now: datetime) -> str:
    return now.strftime("%Y%m%d-%H%M%S")


def _unique(path: Path) -> Path:
    out, n = path, 1
    while out.exists():
        out = path.with_name(f"{path.name}-{n}")
        n += 1
    return out


def _set_aside(db: Path, dest: Path) -> list[Path]:
    """Move *db* and its sidecars into *dest* (a rename: works on cloud
    placeholders too). Returns what moved."""
    dest.mkdir(parents=True, exist_ok=True)
    moved = []
    for p in (db, *sidecars(db)):
        if p.exists():
            p.replace(dest / p.name)
            moved.append(dest / p.name)
    return moved


def _intact(path: Path) -> bool:
    return read_fully(path) and quick_check(path)


def _pick_base(paths: Paths, scratch: Path) -> tuple[Path, str] | None:
    """The intact source with the highest generation: the published DB or the
    newest snapshot that decompresses and passes ``quick_check``."""
    found: list[tuple[int, Path, str]] = []
    pub = paths.published_db
    if pub.exists():
        if _intact(pub):
            found.append((read_generation(pub), pub, "the published copy in the shared folder"))
        else:
            log.warning("recovery: published %s is not usable", pub)
    for snap in reversed(list_snapshots(paths.snapshots_dir)):
        if not read_fully(snap):
            continue
        out = scratch / f"snapshot-{snap.name}.db"
        try:
            restore_snapshot(snap, out)
        except Exception as exc:  # zstd error, truncated file, ...
            log.warning("recovery: snapshot %s unusable: %s", snap.name, exc)
            continue
        if quick_check(out):
            found.append((read_generation(out), out, f"snapshot {snap.name}"))
            break  # newest intact snapshot only
    if not found:
        return None
    _, path, label = max(found, key=lambda t: t[0])
    return path, label


def recover_working_db(
    paths: Paths, *, device_id: str, clock: Clock = utcnow
) -> RecoveryReport | None:
    """Make ``paths.working_db`` safe to open. ``None`` when nothing was wrong."""
    db = paths.working_db
    reason = check_working_db(db) or ""
    sal = salvage_dir(paths)
    pending = sorted(sal.glob("*.db")) if sal.is_dir() else []
    if not reason and not pending:
        return None

    now = clock()
    stamp = _stamp(now)
    set_aside: Path | None = None
    if reason:
        log.error("working DB is unusable (%s); recovering", reason)
        set_aside = _unique(recovered_dir(paths) / f"broken-{stamp}")
        moved = _set_aside(db, set_aside)
        main = set_aside / db.name
        if main in moved:
            # The DB file alone (no journal) is often intact and holds edits
            # made since the last publish -- try to salvage them.
            sal.mkdir(parents=True, exist_ok=True)
            copy = _unique(sal / f"working-{stamp}.db")
            with contextlib.suppress(OSError):
                shutil.copyfile(main, copy)
                pending.append(copy)

    scratch = _unique(recovered_dir(paths) / f"scratch-{stamp}")
    scratch.mkdir(parents=True, exist_ok=True)
    report = RecoveryReport(
        at=isoformat(now),
        reason=reason,
        set_aside=str(set_aside) if set_aside else None,
        base="",
    )
    try:
        usable = []
        for p in pending:
            if _intact(p):
                usable.append(p)
            else:
                report.unusable.append(p.name)

        if reason or not db.exists():
            base = _pick_base(paths, scratch)
            if base is None and usable:
                best = max(usable, key=read_generation)
                base = (best, f"the damaged working copy ({best.name})")
                usable.remove(best)
            if base is None:
                report.base = "nothing (no intact copy) -- the library will be rescanned"
            else:
                src, report.base = base
                for p in sidecars(db):  # a stale journal must never meet the new file
                    p.unlink(missing_ok=True)
                atomic_replace(src, db, tag=device_id)
                report.base_generation = read_generation(db)
        else:
            report.base = "the existing working copy"
            report.base_generation = read_generation(db)

        if usable and db.exists():
            target = make_engine(db)
            engines = [make_engine(p) for p in usable]
            sessions = [(p.name, Session(e)) for p, e in zip(usable, engines, strict=True)]
            try:
                with Session(target) as t:
                    summary = merge_all(
                        t,
                        sessions,
                        now=now,
                        base_generation=read_generation(db),
                        source_generations=[read_generation(p) for p in usable],
                    )
                    t.commit()
                report.merge = str(summary)
                report.salvaged = [p.name for p in usable]
            finally:
                for _, s in sessions:
                    s.close()
                for e in engines:
                    e.dispose()
                target.dispose()

        # Done with every salvage file: keep them, out of the way.
        if pending:
            keep = set_aside or _unique(recovered_dir(paths) / f"salvage-{stamp}")
            keep.mkdir(parents=True, exist_ok=True)
            for p in pending:
                if p.exists():
                    p.replace(_unique(keep / p.name))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    report_file(paths).write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")
    log.warning("working DB recovered: %s", report.describe())
    return report
