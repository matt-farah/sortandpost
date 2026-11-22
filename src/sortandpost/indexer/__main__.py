"""``python -m sortandpost.indexer`` -- standalone indexer entrypoint.

Usable inside the container or natively (CLIP on CPU in Docker on macOS is slow).
The single-writer lock still applies: the native indexer and the container app
must not both write.

* no flag  -- **continuous mode**: acquire the write lock, then loop forever
  running a full reconcile scan + CLIP pass on a timer and on filesystem events
  (watchdog). Ctrl-C to stop; the lock is released on the way out.
* ``--once``   -- one full scan pass + one CLIP pass, then exit. Takes the write
  lock for the pass (exit 3 while another app / machine holds it).
* ``--reembed`` -- clear every CLIP embedding and rebuild the matrix (run it
  after changing ``clip.model`` / ``pretrained``), then exit.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from datetime import UTC, datetime, timedelta

from sortandpost.clip.store import ModelMismatch
from sortandpost.config import LibraryUnavailable, NoAppDataFolder, Settings
from sortandpost.db import init_db, make_engine
from sortandpost.indexer.scan import scan_clip_once, scan_once
from sortandpost.indexer.service import IndexerLoopConfig, run_indexer
from sortandpost.logging_setup import configure
from sortandpost.sync import SyncManager, WriteMode
from sortandpost.sync.lock import LockInfo, read_lock
from sortandpost.sync.recovery import recover_working_db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sortandpost.indexer")
    parser.add_argument("--once", action="store_true", help="run one full scan pass, then exit")
    parser.add_argument(
        "--reembed",
        action="store_true",
        help="recompute all CLIP embeddings (after a model change)",
    )
    parser.add_argument(
        "--no-watch",
        action="store_true",
        help="continuous mode: rely on the timer only, ignore filesystem events",
    )
    args = parser.parse_args(argv)

    try:
        settings = Settings.resolve()
    except (NoAppDataFolder, LibraryUnavailable) as exc:
        print(exc, file=sys.stderr)
        return 1
    configure(settings.paths.logs_dir)
    recover_working_db(settings.paths, device_id=settings.device_id)  # before any open
    if settings.onboarding_pending:
        # The wizard's promise: nothing is scanned until setup is finished.
        print(
            "setup isn't finished on this computer yet -- open the web app "
            "(python -m sortandpost) and complete the welcome wizard first.",
            file=sys.stderr,
        )
        return 1

    if args.reembed or args.once:
        return _run_one_shot(settings, reembed=args.reembed)

    return _run_continuous(settings, watch=not args.no_watch)


def _run_one_shot(settings: Settings, *, reembed: bool) -> int:
    """``--once`` / ``--reembed`` under the write lock (constraint #9): refuse while
    another process or machine edits -- the web app, a container, a continuous
    indexer -- since both would write the same working DB. Publishes and releases
    the lock on the way out, so the next app start picks the pass up."""
    init_db(make_engine(settings.paths.working_db))
    if (sibling := _live_sibling(settings)) is not None:
        print(
            f"another process on this computer (pid {sibling.pid} -- the web app?) is "
            "editing this library and holds the write lock; stop it first.",
            file=sys.stderr,
        )
        return 3
    manager = SyncManager(settings)
    if manager.start(background=False) is not WriteMode.writable:
        print(
            f"{manager._holder_name()} holds the write lock, so this pass would race it; "
            "stop it (or run this from that app's Settings page) and try again.",
            file=sys.stderr,
        )
        manager.stop()
        return 3
    try:
        if reembed:
            print(scan_clip_once(settings, reembed=True))
            return 0
        print(scan_once(settings))
        print(scan_clip_once(settings))
        return 0
    except ModelMismatch as exc:
        print(exc, file=sys.stderr)
        print("re-run with `--reembed` to rebuild the embedding matrix.", file=sys.stderr)
        return 2
    finally:
        manager.stop()


def _live_sibling(settings: Settings) -> LockInfo | None:
    """The lock, when another live process of *this* device holds it: the web app on the
    same data folder (natively, or a second container sharing its ``local/``). The sync
    layer treats our own device's lock as ours, so the CLI checks the heartbeat itself:
    a running holder re-stamps it every ``lock_heartbeat_sec``."""
    info = read_lock(settings.paths.lock_file)
    # Called before this process takes the lock, so a live lock of our device is never
    # ours. (Not a pid check: two containers on one local/ can share a pid.)
    if info is None or info.device_id != settings.device_id:
        return None
    if info.handoff_to is not None:  # a release / grant, not a live holder
        return None
    fresh = timedelta(seconds=3 * settings.config.sync.lock_heartbeat_sec)
    return info if info.heartbeat_age(datetime.now(UTC)) < fresh else None


def _run_continuous(settings: Settings, *, watch: bool) -> int:
    # Make sure a working DB exists before the sync layer quick_checks it
    # (Alembic normally does this; the app does it in build_ui).
    init_db(make_engine(settings.paths.working_db))
    if (sibling := _live_sibling(settings)) is not None:
        print(
            f"another process on this computer (pid {sibling.pid}) holds the write lock; "
            "stop it first.",
            file=sys.stderr,
        )
        return 3

    manager = SyncManager(settings)
    manager.start()
    if manager.mode is not WriteMode.writable:
        print(
            f"another machine ({manager._holder_name()}) holds the write lock; "
            "waiting for it to free up",
            file=sys.stderr,
        )

    stop = threading.Event()

    def _handle(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    loop_cfg = IndexerLoopConfig.from_settings(settings)
    loop_cfg.watch = watch

    def _is_writable() -> bool:
        # Asked between files, so a handover (another computer pressed "Take
        # over editing") has nothing in flight to stop first.
        if manager.maybe_hand_over():
            print(f"handed editing to {manager.handed_to}; waiting", file=sys.stderr)
        return manager.refresh() is WriteMode.writable

    try:
        run_indexer(
            settings,
            is_writable=_is_writable,
            stop=stop,
            loop_config=loop_cfg,
        )
    finally:
        manager.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
