"""CLIP engine: image embeddings + (step 5b) zero-shot tagging.

Embeddings are L2-normalized and stored as rows in a memory-mapped ``float32``
matrix under ``sync/embeddings/``. See CLAUDE.md -> "CLIP tagging & search".

* :mod:`sortandpost.clip.model` -- guarded ``open_clip`` wrapper (CPU).
* :mod:`sortandpost.clip.store` -- the append-mostly embedding matrix + rowmap.
"""

from __future__ import annotations

from sortandpost.clip.model import (
    ClipModel,
    ClipModelUnavailable,
    clip_available,
    known_dim,
)
from sortandpost.clip.store import (
    EmbeddingDimMismatch,
    EmbeddingStore,
    ModelMismatch,
    check_compatible,
    ensure_meta,
    load_meta,
)

__all__ = [
    "ClipModel",
    "ClipModelUnavailable",
    "EmbeddingDimMismatch",
    "EmbeddingStore",
    "ModelMismatch",
    "check_compatible",
    "clip_available",
    "ensure_meta",
    "known_dim",
    "load_meta",
]
