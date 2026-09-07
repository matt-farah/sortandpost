"""``python -m sortandpost.indexer`` -- standalone indexer entrypoint.

Usable inside the container or natively (CLIP on CPU in Docker on macOS is slow).
The single-writer lock still applies: the native indexer and the container app
must not both write.

``--once`` runs one full scan pass (stages 1-6) and exits. Continuous operation
(watchers + periodic reconcile) is build-order step 6; ``--reembed`` is step 5.
"""

from __future__ import annotations

import argparse
import sys

from sortandpost.config import Settings
from sortandpost.indexer.scan import scan_once
from sortandpost.logging_setup import configure


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sortandpost.indexer")
    parser.add_argument("--once", action="store_true", help="run one full scan pass, then exit")
    parser.add_argument(
        "--reembed",
        action="store_true",
        help="recompute all CLIP embeddings (after a model change)",
    )
    args = parser.parse_args(argv)

    settings = Settings.resolve()
    configure(settings.paths.logs_dir)

    if args.reembed:
        raise NotImplementedError(
            "--reembed is build-order step 5 (CLIP engine). See CLAUDE.md -> 'CLIP tagging'."
        )

    if not args.once:
        raise NotImplementedError(
            "the continuous indexer is build-order step 6 (watchers + reconcile). "
            "Use `--once` for a single scan pass."
        )

    stats = scan_once(settings)
    print(stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
