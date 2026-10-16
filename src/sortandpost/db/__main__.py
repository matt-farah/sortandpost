"""``python -m sortandpost.db`` — DB maintenance commands (build-order step 4).

* ``snapshot``       — write a compressed DB snapshot to ``sync/snapshots/`` now
* ``restore <path>`` — restore the working DB from a snapshot (then run the
  indexer with ``--once`` to reconcile anything newer than the snapshot)
* ``merge <path>``   — fold a stray conflicted-copy DB into the working DB
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlmodel import Session

from sortandpost.config import LibraryUnavailable, NoAppDataFolder, Settings
from sortandpost.db import make_engine
from sortandpost.db.meta import get_generation
from sortandpost.logging_setup import configure
from sortandpost.sync.clock import utcnow
from sortandpost.sync.merge import merge_all
from sortandpost.sync.publish import read_generation
from sortandpost.sync.recovery import recover_working_db, sidecars
from sortandpost.sync.snapshots import prune_snapshots, restore_snapshot, write_snapshot


def _cmd_snapshot(settings: Settings) -> int:
    paths = settings.paths
    dest = write_snapshot(paths.working_db, paths.snapshots_dir, now=utcnow())
    removed = prune_snapshots(paths.snapshots_dir, settings.config.sync.snapshot_keep)
    print(f"wrote {dest}")
    if removed:
        print(f"pruned {len(removed)} old snapshot(s)")
    return 0


def _cmd_restore(settings: Settings, snapshot: Path) -> int:
    if not snapshot.exists():
        print(f"no such snapshot: {snapshot}", file=sys.stderr)
        return 2
    for side in sidecars(settings.paths.working_db):  # a stale journal must not meet it
        side.unlink(missing_ok=True)
    restore_snapshot(snapshot, settings.paths.working_db)
    print(f"restored {settings.paths.working_db} from {snapshot}")
    print("run `python -m sortandpost.indexer --once` to reconcile anything newer.")
    return 0


def _cmd_merge(settings: Settings, incoming: Path) -> int:
    if not incoming.exists():
        print(f"no such DB: {incoming}", file=sys.stderr)
        return 2
    paths = settings.paths
    target_engine = make_engine(paths.working_db)
    src_engine = make_engine(incoming)
    base_gen = read_generation(paths.working_db)
    src_gen = read_generation(incoming)
    try:
        with Session(target_engine) as target, Session(src_engine) as src:
            summary = merge_all(
                target,
                [(incoming.name, src)],
                now=utcnow(),
                base_generation=base_gen,
                source_generations=[src_gen],
            )
            target.commit()
            merged_gen = get_generation(target)
    finally:
        src_engine.dispose()
        target_engine.dispose()
    if summary.failed:
        print(f"could not merge {incoming.name} (see the log); left it in place", file=sys.stderr)
        return 1

    archive = paths.merged_snapshots_dir / incoming.name
    paths.merged_snapshots_dir.mkdir(parents=True, exist_ok=True)
    counter = 1
    while archive.exists():
        archive = paths.merged_snapshots_dir / f"{incoming.stem}-{counter}{incoming.suffix}"
        counter += 1
    incoming.replace(archive)

    print(
        f"merged {incoming.name}: +{summary.photos_added} photos, "
        f"+{summary.photo_paths_added} paths, {summary.ratings_updated} ratings, "
        f"{summary.changes_unioned} change rows; generation {base_gen} -> {merged_gen}"
    )
    if summary.skipped_rows:
        print(f"skipped {summary.skipped_rows} row(s) this version can't apply")
    print(f"archived the conflict copy to {archive}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sortandpost.db")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("snapshot", help="write a compressed DB snapshot to sync/snapshots/ now")

    p_restore = sub.add_parser(
        "restore", help="restore the working DB from a snapshot, then reconcile-scan"
    )
    p_restore.add_argument("path", help="path to a .db or .db.zst snapshot")

    p_merge = sub.add_parser("merge", help="fold a stray conflicted-copy DB into the current one")
    p_merge.add_argument("path", help="path to the conflicted-copy .db")

    args = parser.parse_args(argv)

    try:
        settings = Settings.resolve()
    except (NoAppDataFolder, LibraryUnavailable) as exc:
        print(exc, file=sys.stderr)
        return 1
    configure(settings.paths.logs_dir)
    if args.command != "restore":  # restore is the user's own explicit recovery
        recover_working_db(settings.paths, device_id=settings.device_id)

    if args.command == "snapshot":
        return _cmd_snapshot(settings)
    if args.command == "restore":
        return _cmd_restore(settings, Path(args.path))
    if args.command == "merge":
        return _cmd_merge(settings, Path(args.path))
    parser.error(f"unknown command {args.command!r}")  # pragma: no cover
    return 2


if __name__ == "__main__":
    sys.exit(main())
