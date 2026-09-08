"""``python -m sortandpost.indexer`` -- standalone indexer entrypoint.

Usable inside the container or natively (CLIP on CPU in Docker on macOS is slow).
The single-writer lock still applies: the native indexer and the container app
must not both write.

* no flag  -- **continuous mode**: acquire the write lock, then loop forever
  running a full reconcile scan + CLIP pass on a timer and on filesystem events
  (watchdog). Ctrl-C to stop; the lock is released on the way out.
* ``--once``   -- one full scan pass + one CLIP pass, then exit.
* ``--reembed`` -- clear every CLIP embedding and rebuild the matrix (run it
  after changing ``clip.model`` / ``pretrained``), then exit.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading

from sortandpost.clip.store import ModelMismatch
from sortandpost.config import Settings
from sortandpost.db import init_db, make_engine
from sortandpost.indexer.scan import scan_clip_once, scan_once
from sortandpost.indexer.service import IndexerLoopConfig, run_indexer
from sortandpost.logging_setup import configure
from sortandpost.sync import SyncManager, WriteMode


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

    settings = Settings.resolve()
    configure(settings.paths.logs_dir)

    if args.reembed:
        try:
            print(scan_clip_once(settings, reembed=True))
        except ModelMismatch as exc:  # pragma: no cover - defensive
            print(exc, file=sys.stderr)
            return 2
        return 0

    if args.once:
        print(scan_once(settings))
        try:
            print(scan_clip_once(settings))
        except ModelMismatch as exc:
            print(exc, file=sys.stderr)
            print("re-run with `--reembed` to rebuild the embedding matrix.", file=sys.stderr)
            return 2
        return 0

    return _run_continuous(settings, watch=not args.no_watch)


def _run_continuous(settings: Settings, *, watch: bool) -> int:
    # Make sure a working DB exists before the sync layer quick_checks it
    # (Alembic normally does this; the app does it in build_ui).
    init_db(make_engine(settings.paths.working_db))

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
    try:
        run_indexer(
            settings,
            is_writable=lambda: manager.refresh() is WriteMode.writable,
            stop=stop,
            loop_config=loop_cfg,
        )
    finally:
        manager.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
