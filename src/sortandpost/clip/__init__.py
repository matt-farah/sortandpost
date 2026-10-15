"""CLIP engine: image embeddings + zero-shot tagging.

Embeddings are L2-normalized and stored as rows in a memory-mapped ``float32``
matrix under ``sync/embeddings/``. See docs/architecture.md -> "CLIP tagging & search".

* :mod:`sortandpost.clip.model` -- guarded ``open_clip`` wrapper (CPU).
* :mod:`sortandpost.clip.store` -- the append-mostly embedding matrix + rowmap.
* :mod:`sortandpost.clip.vocab` -- ``tags.yaml`` + its cached text embeddings.
* :mod:`sortandpost.clip.tagging` -- zero-shot scoring + the ``/review`` flow.
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
from sortandpost.clip.tagging import (
    ReviewGroup,
    ScoredTerm,
    approve,
    pending_groups,
    pick_terms,
    reject,
    upsert_prediction,
)
from sortandpost.clip.vocab import Vocab, VocabTerm, ensure_text_matrix, load_vocab

__all__ = [
    "ClipModel",
    "ClipModelUnavailable",
    "EmbeddingDimMismatch",
    "EmbeddingStore",
    "ModelMismatch",
    "ReviewGroup",
    "ScoredTerm",
    "Vocab",
    "VocabTerm",
    "approve",
    "check_compatible",
    "clip_available",
    "ensure_meta",
    "ensure_text_matrix",
    "known_dim",
    "load_meta",
    "load_vocab",
    "pending_groups",
    "pick_terms",
    "reject",
    "upsert_prediction",
]
