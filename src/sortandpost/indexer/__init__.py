"""Indexer: discover -> decode -> pixel-hash -> thumbnails -> metadata -> CLIP.

Each stage is idempotent and separately resumable, keyed by real path in
``scan_state``. See CLAUDE.md → "Indexer pipeline". Runs only when this machine
holds the write lock.
"""

from sortandpost.indexer.pipeline import Stage

__all__ = ["Stage"]
