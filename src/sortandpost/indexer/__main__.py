"""``python -m sortandpost.indexer`` — standalone indexer entrypoint.

Usable inside the container or natively (CLIP on CPU in Docker on macOS is slow).
The single-writer lock still applies: the native indexer and the container app
must not both write. Implemented in build-order step 3.
"""

from __future__ import annotations

import argparse
import sys

from sortandpost.config import Settings
from sortandpost.logging_setup import configure


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sortandpost.indexer")
    parser.add_argument("--once", action="store_true", help="run one full scan pass, then exit")
    parser.add_argument(
        "--reembed", action="store_true", help="recompute all CLIP embeddings (after a model change)"
    )
    args = parser.parse_args(argv)

    settings = Settings.resolve()
    configure(settings.paths.logs_dir)

    raise NotImplementedError(
        f"indexer (once={args.once}, reembed={args.reembed}) is not implemented yet "
        "(build-order step 3). See CLAUDE.md → 'Indexer pipeline'."
    )


if __name__ == "__main__":
    sys.exit(main())
