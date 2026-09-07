"""CLIP engine: image embeddings + zero-shot tagging against the vocabulary.

Embeddings are L2-normalized and stored as rows in a memory-mapped float32 matrix
under ``sync/embeddings/``. See CLAUDE.md → "CLIP tagging & search".
Implemented in build-order step 5.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from sortandpost.config import ClipConfig, TaggingConfig


@dataclass
class Prediction:
    tag: str
    facet: str | None
    score: float


class ClipEngine:
    def __init__(self, config: ClipConfig, cache_dir: Path) -> None:
        self.config = config
        self.cache_dir = cache_dir

    def load(self) -> None:
        raise NotImplementedError("build-order step 5: CLIP engine")

    def embed_images(self, images: list[Path]) -> np.ndarray:
        raise NotImplementedError("build-order step 5: CLIP engine")

    def embed_text(self, prompts: list[str]) -> np.ndarray:
        raise NotImplementedError("build-order step 5: CLIP engine")

    def zero_shot(
        self, image_embedding: np.ndarray, vocab: dict[str, list[str]], tagging: TaggingConfig
    ) -> list[Prediction]:
        raise NotImplementedError("build-order step 5: CLIP engine")
