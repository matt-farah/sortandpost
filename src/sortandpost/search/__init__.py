"""Vector search over the CLIP image-embedding matrix.

Brute-force cosine (dot product on normalized rows) via NumPy, memory-mapped.
Target <= ~25k photos. If the library outgrows that, add an on-disk ANN index
behind this same interface without changing it. See CLAUDE.md.
Implemented in build-order step 5.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class SearchHit:
    pixel_hash: str
    score: float


class VectorIndex:
    def __init__(self, matrix_path: Path, rowmap_path: Path) -> None:
        self.matrix_path = matrix_path
        self.rowmap_path = rowmap_path

    def open(self) -> None:
        raise NotImplementedError("build-order step 5: vector search")

    def append(self, pixel_hash: str, embedding: np.ndarray) -> int:
        raise NotImplementedError("build-order step 5: vector search")

    def query(self, embedding: np.ndarray, *, limit: int = 200) -> list[SearchHit]:
        raise NotImplementedError("build-order step 5: vector search")
