"""Per-file indexing pipeline stages.

``stages_done`` on :class:`sortandpost.db.models.ScanState` is a bitmask of the
:class:`Stage` values below, so a file killed mid-pipeline resumes where it left
off. Implemented in build-order step 3 (stages 1-6) and step 5 (CLIP).
"""

from __future__ import annotations

from enum import IntFlag


class Stage(IntFlag):
    STAT = 1 << 0        # stat + fast-path (size, mtime_ns) check
    DECODE = 1 << 1      # decode to RGB (Pillow / pillow-heif / rawpy preview)
    HASH = 1 << 2        # pixel_hash = sha256(rgb_bytes); upsert Photo/PhotoPath
    THUMBNAILS = 1 << 3  # write configured WebP sizes, keyed by hash
    METADATA = 1 << 4    # exiftool batch read into Photo columns
    RECONCILE = 1 << 5   # rating: file vs DB (mtime wins tie-break)
    CLIP = 1 << 6        # image embedding + zero-shot predictions

    BROWSABLE = STAT | DECODE | HASH | THUMBNAILS | METADATA | RECONCILE
    COMPLETE = BROWSABLE | CLIP


def run_file(path: str) -> None:
    raise NotImplementedError("build-order step 3: indexer pipeline")
