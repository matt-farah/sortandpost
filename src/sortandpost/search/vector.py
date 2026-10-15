"""Brute-force cosine ranking over the memory-mapped CLIP matrix.

Rows in ``img_<model>.npy`` are already L2-normalized, so cosine similarity is a
plain dot product. The query vector is normalized here defensively. Target
<= ~25k photos; if the library outgrows that, an ANN index slots in behind this
same ``rank`` signature. See docs/architecture.md.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from numpy.lib.format import open_memmap


def load_rowmap(rowmap_path: Path) -> list[str]:
    """``row_index -> pixel_hash`` from ``sync/embeddings/rowmap.json``."""
    p = Path(rowmap_path)
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    return [str(h) for h in data.get("rows", [])]


def open_matrix(matrix_path: Path, count: int) -> np.ndarray | None:
    """Read-only view of the first *count* rows of the matrix, or ``None``."""
    p = Path(matrix_path)
    if count <= 0 or not p.exists():
        return None
    mm = open_memmap(p, mode="r")
    rows = min(count, mm.shape[0])
    if rows <= 0:
        return None
    return np.asarray(mm[:rows])


def rank(matrix: np.ndarray, query: np.ndarray, *, limit: int) -> list[tuple[int, float]]:
    """Top-*limit* ``(row_index, score)`` by descending dot product."""
    if matrix.size == 0:
        return []
    q = np.asarray(query, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(q))
    if norm > 0:
        q = q / norm
    scores = matrix @ q
    limit = max(1, min(limit, scores.shape[0]))
    top = np.argpartition(-scores, limit - 1)[:limit]
    top = top[np.argsort(-scores[top])]
    return [(int(i), float(scores[i])) for i in top]
