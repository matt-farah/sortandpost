"""Vector search over the CLIP image-embedding matrix + structured filters.

Brute-force cosine (dot product on normalized rows) via NumPy, memory-mapped.
Target <= ~25k photos. If the library outgrows that, add an on-disk ANN index
behind this same interface without changing it. See docs/architecture.md.

* :mod:`sortandpost.search.vector` -- matrix open + ``rank`` primitive.
* :mod:`sortandpost.search.filters` -- ``PhotoFilter`` + ``browse_photos``.
* :mod:`sortandpost.search.service` -- ``semantic_search`` / ``similar_to``.
"""

from __future__ import annotations

from sortandpost.search.filters import PhotoFilter, allowed_hashes, browse_photos
from sortandpost.search.service import (
    SearchHit,
    TextEncoder,
    search_by_vector,
    semantic_search,
    similar_to,
)
from sortandpost.search.vector import load_rowmap, open_matrix, rank

__all__ = [
    "PhotoFilter",
    "SearchHit",
    "TextEncoder",
    "allowed_hashes",
    "browse_photos",
    "load_rowmap",
    "open_matrix",
    "rank",
    "search_by_vector",
    "semantic_search",
    "similar_to",
]
