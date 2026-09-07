"""``python -m sortandpost.db`` — DB maintenance commands.

Implemented in build-order step 4 (sync layer). Stubbed here so the CLI surface
and help text exist from the scaffold.
"""

from __future__ import annotations

import argparse
import sys


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
    raise NotImplementedError(
        f"`{args.command}` is not implemented yet (build-order step 4: sync layer). "
        "See CLAUDE.md → 'Sync model'."
    )


if __name__ == "__main__":
    sys.exit(main())
