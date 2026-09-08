"""Vector search over the CLIP image-embedding matrix.

Brute-force cosine (dot product on normalized rows) via NumPy, memory-mapped.
Target <= ~25k photos. If the library outgrows that, add an on-disk ANN index
behind this same interface without changing it. See CLAUDE.md.

* :mod:`sortandpost.search.vector` -- matrix open + ``rank`` primitive.
* :mod:`sortandpost.search.service` -- ``semantic_search`` / ``similar_to``.
"""

from __future__ import annotations

from sortandpost.search.service import (
    SearchHit,
    TextEncoder,
    search_by_vector,
    semantic_search,
    similar_to,
)
from sortandpost.search.vector import load_rowmap, open_matrix, rank

__all__ = [
    "SearchHit",
    "TextEncoder",
    "load_rowmap",
    "open_matrix",
    "rank",
    "search_by_vector",
    "semantic_search",
    "similar_to",
]
