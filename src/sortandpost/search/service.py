"""Semantic search over the CLIP image-embedding matrix.

``semantic_search`` : query text -> CLIP text embedding -> dot product vs. the
memmap matrix -> ranked ``pixel_hash`` list, intersected with a
:class:`~sortandpost.search.filters.PhotoFilter`. ``similar_to`` does the same
from a stored image row ("more like this").

The text-embed step is CLIP inference and must not run on the web event loop --
callers on the UI side offload it (``nicegui.run.io_bound``). This module itself
is synchronous and pure given a query vector.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np
from sqlmodel import Session

from sortandpost.config import Settings
from sortandpost.search.filters import PhotoFilter, allowed_hashes
from sortandpost.search.vector import load_rowmap, open_matrix, rank


@dataclass
class SearchHit:
    pixel_hash: str
    score: float


class TextEncoder(Protocol):
    def encode_text(self, texts: list[str]) -> np.ndarray: ...


def _resolve_filter(flt: PhotoFilter | None, include_missing: bool) -> PhotoFilter:
    if flt is not None:
        return flt
    return PhotoFilter(include_missing=include_missing)


def search_by_vector(
    session: Session,
    settings: Settings,
    query_vec: np.ndarray,
    *,
    limit: int = 200,
    include_missing: bool = False,
    flt: PhotoFilter | None = None,
) -> list[SearchHit]:
    model = settings.config.clip.model
    rows = load_rowmap(settings.paths.embeddings_rowmap_file)
    if not rows:
        return []
    matrix = open_matrix(settings.paths.image_embeddings_file(model), len(rows))
    if matrix is None or matrix.size == 0:
        return []

    allowed = allowed_hashes(session, _resolve_filter(flt, include_missing))
    # Over-fetch so post-filtering can still fill `limit`.
    over = len(rows) if allowed is not None else max(limit * 2, limit + 50)
    ranked = rank(matrix, query_vec, limit=min(len(rows), over))

    hits: list[SearchHit] = []
    for row_index, score in ranked:
        if row_index >= len(rows):
            continue
        pixel_hash = rows[row_index]
        if allowed is not None and pixel_hash not in allowed:
            continue
        hits.append(SearchHit(pixel_hash=pixel_hash, score=score))
        if len(hits) >= limit:
            break
    return hits


def semantic_search(
    session: Session,
    settings: Settings,
    model: TextEncoder,
    query: str,
    *,
    limit: int = 200,
    include_missing: bool = False,
    flt: PhotoFilter | None = None,
) -> list[SearchHit]:
    text = query.strip()
    if not text:
        return []
    qvec = model.encode_text([text])
    if qvec.shape[0] == 0:
        return []
    return search_by_vector(
        session, settings, qvec[0], limit=limit, include_missing=include_missing, flt=flt
    )


def similar_to(
    session: Session,
    settings: Settings,
    pixel_hash: str,
    *,
    limit: int = 50,
    include_missing: bool = False,
    flt: PhotoFilter | None = None,
) -> list[SearchHit]:
    model = settings.config.clip.model
    rows = load_rowmap(settings.paths.embeddings_rowmap_file)
    try:
        idx = rows.index(pixel_hash)
    except ValueError:
        return []
    matrix = open_matrix(settings.paths.image_embeddings_file(model), len(rows))
    if matrix is None or idx >= matrix.shape[0]:
        return []
    hits = search_by_vector(
        session,
        settings,
        matrix[idx],
        limit=limit + 1,
        include_missing=include_missing,
        flt=flt,
    )
    return [h for h in hits if h.pixel_hash != pixel_hash][:limit]
