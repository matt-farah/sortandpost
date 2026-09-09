"""Cross-machine sync: local working copy + atomic publish + single-writer lock.

See CLAUDE.md → "Sync model". Build-order step 4.

Layout:

* :mod:`sortandpost.sync.clock` — injectable UTC clock
* :mod:`sortandpost.sync.lock` — ``sync/.sortandpost.lock`` read/write + staleness
* :mod:`sortandpost.sync.publish` — atomic DB publish + raw-SQLite generation/quick_check
* :mod:`sortandpost.sync.snapshots` — zstd snapshots + prune + restore
* :mod:`sortandpost.sync.changelog` — append to the ``changes`` log (hard constraint #4)
* :mod:`sortandpost.sync.merge` — conflict-copy detection + deterministic merge
* :mod:`sortandpost.sync.manager` — :class:`SyncManager` orchestration
"""

from __future__ import annotations

from sortandpost.sync.changelog import record_change
from sortandpost.sync.lock import LockInfo
from sortandpost.sync.manager import SyncManager, WriteMode
from sortandpost.sync.merge import MergeSummary, find_conflict_copies, merge_all, merge_session

__all__ = [
    "LockInfo",
    "MergeSummary",
    "SyncManager",
    "WriteMode",
    "find_conflict_copies",
    "merge_all",
    "merge_session",
    "record_change",
]
