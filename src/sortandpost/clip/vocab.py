"""Zero-shot vocabulary + its cached CLIP text embeddings (build-order step 5b).

The vocabulary is ``sync/tags.yaml`` -- user-editable, facet-grouped
(``{facet: [term, ...]}``), seeded from ``resources/tags.default.yaml`` on first
boot. Term order is preserved (facet order, then term order); a term that appears
under two facets keeps its first facet.

For each term the text embedding is the L2-normalized mean of the normalized
encodings of every ``prompt_templates`` entry (``"a photo of {}"`` etc.). The
resulting ``(n_terms, dim)`` matrix is cached to
``sync/embeddings/text_<model>_<vocabhash>.npy`` -- the ``<vocabhash>`` in the
filename means editing ``tags.yaml`` transparently invalidates the cache.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
import yaml

from sortandpost.tag_names import is_safe_tag_name, normalize_tag_name

log = logging.getLogger(__name__)


class TextEncoder(Protocol):
    def encode_text(self, texts: list[str]) -> np.ndarray: ...


@dataclass(frozen=True)
class VocabTerm:
    name: str
    facet: str


@dataclass(frozen=True)
class Vocab:
    terms: tuple[VocabTerm, ...]
    vocab_hash: str

    @property
    def names(self) -> list[str]:
        return [t.name for t in self.terms]

    def facet_of(self, name: str) -> str | None:
        for term in self.terms:
            if term.name == name:
                return term.facet
        return None


def load_vocab(tags_file: Path) -> Vocab:
    """Parse ``tags.yaml`` into an ordered, de-duplicated :class:`Vocab`."""
    try:
        raw = yaml.safe_load(Path(tags_file).read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raw = {}

    terms: list[VocabTerm] = []
    seen: set[str] = set()
    if isinstance(raw, dict):
        for facet, items in raw.items():
            if not isinstance(items, list):
                continue
            for item in items:
                name = normalize_tag_name(str(item))
                if not name or name in seen:
                    continue
                if not is_safe_tag_name(name):
                    log.warning("skipping tag %r in %s: control characters", name, tags_file)
                    continue
                seen.add(name)
                terms.append(VocabTerm(name=name, facet=str(facet)))

    digest = hashlib.sha256(
        json.dumps([[t.facet, t.name] for t in terms], ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:12]
    return Vocab(terms=tuple(terms), vocab_hash=digest)


def ensure_text_matrix(
    model: TextEncoder,
    vocab: Vocab,
    *,
    prompt_templates: list[str],
    cache_path: Path,
) -> np.ndarray:
    """Return the ``(n_terms, dim)`` text-embedding matrix, building + caching it
    on a miss. Rows align with ``vocab.terms``."""
    n = len(vocab.terms)
    if n == 0:
        return np.zeros((0, 0), dtype=np.float32)

    cache_path = Path(cache_path)
    if cache_path.exists():
        cached = np.load(cache_path)
        if cached.ndim == 2 and cached.shape[0] == n:
            return np.asarray(cached, dtype=np.float32)
        log.warning(
            "text-embedding cache %s is stale (%s rows, want %s); rebuilding",
            cache_path.name,
            cached.shape,
            n,
        )

    templates = list(prompt_templates) or ["{}"]
    prompts = [tmpl.format(term.name) for term in vocab.terms for tmpl in templates]
    flat = np.asarray(model.encode_text(prompts), dtype=np.float32)
    dim = flat.shape[1]
    per_term = flat.reshape(n, len(templates), dim).mean(axis=1)
    norms = np.linalg.norm(per_term, axis=1, keepdims=True)
    matrix = np.asarray(per_term / np.clip(norms, 1e-12, None), dtype=np.float32)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.parent / (cache_path.name + ".tmp")
    with open(tmp, "wb") as fh:
        np.save(fh, matrix)
    os.replace(tmp, cache_path)
    return matrix
