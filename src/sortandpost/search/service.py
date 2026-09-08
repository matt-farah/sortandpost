"""Semantic search over the CLIP image-embedding matrix (build-order step 5a).

``semantic_search`` : query text -> CLIP text embedding -> dot product vs. the
memmap matrix -> ranked ``pixel_hash`` list. ``similar_to`` does the same from a
stored image row ("more like this"). Filters are minimal here (missing photos are
dropped by default); the full filter set is build-order step 7.

The text-embed step is CLIP inference and must not run on the web event loop --
callers on the UI side offload it (``nicegui.run.io_bound``). This module itself
is synchronous and pure given a query vector.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np
from sqlmodel import Session, col, select

from sortandpost.config import Settings
from sortandpost.db.models import Photo
from sortandpost.search.vector import load_rowmap, open_matrix, rank


@dataclass
class SearchHit:
    pixel_hash: str
    score: float


class TextEncoder(Protocol):
    def encode_text(self, texts: list[str]) -> np.ndarray: ...


def search_by_vector(
    session: Session,
    settings: Settings,
    query_vec: np.ndarray,
    *,
    limit: int = 200,
    include_missing: bool = False,
) -> list[SearchHit]:
    model = settings.config.clip.model
    rows = load_rowmap(settings.paths.embeddings_rowmap_file)
    if not rows:
        return []
    matrix = open_matrix(settings.paths.image_embeddings_file(model), len(rows))
    if matrix is None or matrix.size == 0:
        return []

    # Over-fetch so post-filtering (missing photos) can still fill `limit`.
    over = limit if include_missing else min(len(rows), max(limit * 4, limit + 50))
    ranked = rank(matrix, query_vec, limit=over)

    present: set[str] | None = None
    if not include_missing:
        present = set(
            session.exec(select(Photo.pixel_hash).where(col(Photo.missing_since).is_(None))).all()
        )

    hits: list[SearchHit] = []
    for row_index, score in ranked:
        if row_index >= len(rows):
            continue
        pixel_hash = rows[row_index]
        if present is not None and pixel_hash not in present:
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
) -> list[SearchHit]:
    text = query.strip()
    if not text:
        return []
    qvec = model.encode_text([text])
    if qvec.shape[0] == 0:
        return []
    return search_by_vector(
        session, settings, qvec[0], limit=limit, include_missing=include_missing
    )


def similar_to(
    session: Session,
    settings: Settings,
    pixel_hash: str,
    *,
    limit: int = 50,
    include_missing: bool = False,
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
        session, settings, matrix[idx], limit=limit + 1, include_missing=include_missing
    )
    return [h for h in hits if h.pixel_hash != pixel_hash][:limit]
