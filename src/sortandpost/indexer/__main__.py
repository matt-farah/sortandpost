"""``python -m sortandpost.indexer`` -- standalone indexer entrypoint.

Usable inside the container or natively (CLIP on CPU in Docker on macOS is slow).
The single-writer lock still applies: the native indexer and the container app
must not both write.

``--once`` runs one full scan pass (stages 1-6) followed by the deferred CLIP
embedding pass (stage 7), then exits. ``--reembed`` clears every CLIP embedding
and rebuilds the matrix (run it after changing ``clip.model`` / ``pretrained``).
Continuous operation (watchers + periodic reconcile) is build-order step 6.
"""

from __future__ import annotations

import argparse
import sys

from sortandpost.clip.store import ModelMismatch
from sortandpost.config import Settings
from sortandpost.indexer.scan import scan_clip_once, scan_once
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
        try:
            print(scan_clip_once(settings, reembed=True))
        except ModelMismatch as exc:  # pragma: no cover - defensive
            print(exc, file=sys.stderr)
            return 2
        return 0

    if not args.once:
        raise NotImplementedError(
            "the continuous indexer is build-order step 6 (watchers + reconcile). "
            "Use `--once` for a single scan pass."
        )

    print(scan_once(settings))
    try:
        print(scan_clip_once(settings))
    except ModelMismatch as exc:
        print(exc, file=sys.stderr)
        print("re-run with `--reembed` to rebuild the embedding matrix.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
