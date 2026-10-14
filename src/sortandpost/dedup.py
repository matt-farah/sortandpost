"""Perceptual-hash near-duplicate detection (build-order step 7).

Each photo gets a 64-bit **dHash** (row-wise difference hash) stored hex in
``photos.phash`` by the indexer. Two photos are near-duplicates when the Hamming
distance between their dHashes is small -- resilient to re-compression, minor
crops, and resizes, unlike the exact ``pixel_hash``.

``find_duplicate_groups`` returns clusters (via union-find) of photos within
``max_distance`` bits of each other. The pairwise scan is a vectorised SWAR
popcount over a memory-mapped-friendly ``uint64`` array, blocked so peak memory
stays a few hundred MB even at the ~25k-photo design target.
"""

from __future__ import annotations

import logging
from collections import defaultdict

import numpy as np
from PIL import Image
from sqlmodel import Session, col, select

from sortandpost import missing
from sortandpost.db.models import Photo

log = logging.getLogger(__name__)

NEAR_DUPLICATE_DISTANCE = 8  # <= this many differing bits => "near-duplicate"
_BLOCK = 512  # rows compared against the whole set at once


def dhash(image: Image.Image, *, hash_size: int = 8) -> str:
    """64-bit row difference hash as 16 hex chars."""
    small = image.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
    pixels = small.tobytes()  # row-major, one byte per "L" pixel
    stride = hash_size + 1
    bits = 0
    for row in range(hash_size):
        base = row * stride
        for col_ in range(hash_size):
            left = pixels[base + col_]
            right = pixels[base + col_ + 1]
            bits = (bits << 1) | int(left > right)
    return f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def find_duplicate_groups(
    session: Session,
    *,
    max_distance: int = NEAR_DUPLICATE_DISTANCE,
    include_missing: bool = False,
) -> list[list[str]]:
    """Clusters of near-duplicate ``pixel_hash`` values, largest cluster first."""
    stmt = select(Photo.pixel_hash, Photo.phash).where(col(Photo.phash).is_not(None))
    if not include_missing:
        stmt = stmt.where(missing.shown_clause())
    rows = [(ph, int(ph_hex, 16)) for ph, ph_hex in session.exec(stmt).all() if ph_hex]
    if len(rows) < 2:
        return []

    keys = [ph for ph, _ in rows]
    values = np.array([v for _, v in rows], dtype=np.uint64)
    n = len(keys)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    threshold = np.uint64(max_distance)
    for start in range(0, n, _BLOCK):
        block = values[start : start + _BLOCK]
        dist = _popcount64(block[:, None] ^ values[None, :])
        hits = dist <= threshold
        for offset in range(block.shape[0]):
            gi = start + offset
            for j in np.nonzero(hits[offset])[0]:
                jj = int(j)
                if jj != gi:
                    union(gi, jj)

    clusters: dict[int, list[str]] = defaultdict(list)
    for idx, key in enumerate(keys):
        clusters[find(idx)].append(key)
    groups = [sorted(members) for members in clusters.values() if len(members) > 1]
    groups.sort(key=lambda g: (-len(g), g[0]))
    return groups


def _popcount64(x: np.ndarray) -> np.ndarray:
    """Per-element bit count of a ``uint64`` array (SWAR, fully vectorised)."""
    m1 = np.uint64(0x5555555555555555)
    m2 = np.uint64(0x3333333333333333)
    m4 = np.uint64(0x0F0F0F0F0F0F0F0F)
    h01 = np.uint64(0x0101010101010101)
    x = x - ((x >> np.uint64(1)) & m1)
    x = (x & m2) + ((x >> np.uint64(2)) & m2)
    x = (x + (x >> np.uint64(4))) & m4
    return (x * h01) >> np.uint64(56)
