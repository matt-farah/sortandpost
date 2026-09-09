"""Indexer: discover -> decode -> pixel-hash -> thumbnails -> metadata -> CLIP.

Each stage is idempotent and separately resumable, keyed by real path in
``scan_state``. See CLAUDE.md -> "Indexer pipeline". Runs only when this machine
holds the write lock.
"""

from sortandpost.indexer.pipeline import IndexOutcome, Stage, index_file
from sortandpost.indexer.reconcile import ReconcileOutcome, reconcile_metadata
from sortandpost.indexer.scan import ClipStats, ScanStats, scan_clip_once, scan_once
from sortandpost.indexer.service import IndexerLoopConfig, run_indexer, watchdog_available

__all__ = [
    "ClipStats",
    "IndexOutcome",
    "IndexerLoopConfig",
    "ReconcileOutcome",
    "ScanStats",
    "Stage",
    "index_file",
    "reconcile_metadata",
    "run_indexer",
    "scan_clip_once",
    "scan_once",
    "watchdog_available",
]
