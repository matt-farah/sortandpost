"""Detect cloud "online-only" placeholder files without opening them.

OneDrive, Google Drive, Dropbox and iCloud leave a stub on disk whose real bytes
download only when something opens the file. Our decode stage
(:func:`sortandpost.indexer.decode.load_image`) would trigger that download for
every file on a first scan of a dehydrated library -- potentially pulling
hundreds of GB the user never asked for.

:func:`is_online_only` is a cheap, no-open check on the ``os.stat_result`` the
scanner already has. When it is true and ``indexer.online_only == "skip"`` the
pipeline records the file as :attr:`~sortandpost.db.models.ScanState.online_only`
and moves on; the user hydrates it on demand from the ``/`` banner or
``/settings`` (see :mod:`sortandpost.indexer.hydrate`).

The check is deliberately conservative: any doubt returns ``False`` and the file
is decoded as before (which hydrates it -- the pre-feature behaviour).
"""

from __future__ import annotations

import os
import sys

# macOS: the st_flags bit set on a dataless (iCloud / File Provider) placeholder.
_SF_DATALESS = 0x40000000

# Windows FILE_ATTRIBUTE_* bits that mark a not-fully-present file.
_FILE_ATTRIBUTE_OFFLINE = 0x1000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_WIN_PLACEHOLDER = (
    _FILE_ATTRIBUTE_OFFLINE | _FILE_ATTRIBUTE_RECALL_ON_OPEN | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)

# A real image below this size never costs enough to bother deferring, and the
# floor guards the POSIX heuristic against genuinely tiny / sparse local files.
_MIN_PLACEHOLDER_BYTES = 4096


def is_online_only(st: os.stat_result) -> bool:
    """True when *st* looks like a cloud placeholder whose bytes are not local."""
    if sys.platform.startswith("win"):
        attrs = int(getattr(st, "st_file_attributes", 0))
        return bool(attrs & _WIN_PLACEHOLDER)

    # macOS dataless flag -- definitive when present.
    if int(getattr(st, "st_flags", 0)) & _SF_DATALESS:
        return True

    # POSIX heuristic: the directory entry claims a real size but no data blocks
    # are allocated on disk. st_blocks is in 512-byte units; it is absent on
    # some platforms (Windows), in which case we can say nothing.
    st_blocks = getattr(st, "st_blocks", None)
    if st_blocks is None:
        return False
    return st_blocks == 0 and st.st_size > _MIN_PLACEHOLDER_BYTES
