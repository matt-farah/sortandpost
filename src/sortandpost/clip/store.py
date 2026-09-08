"""The CLIP image-embedding matrix (build-order step 5a).

Rows are L2-normalized ``float32`` CLIP image embeddings, one per ``pixel_hash``,
stored in a memory-mapped ``.npy`` file at ``sync/embeddings/img_<model>.npy``.
The file is **append-mostly**: new photos append a row, a re-embed of an existing
photo overwrites its row in place, and the file grows in chunks so appends don't
rewrite it every time.

Two derived facts travel alongside the matrix:

* ``sync/embeddings/rowmap.json`` -- ``row_index -> pixel_hash``, paired with the
  matrix file so the matrix can be interpreted without the DB.
* the ``embedding_rows`` / ``embeddings_meta`` tables -- the DB's mirror, kept in
  step by the indexer's CLIP pass.

The on-disk matrix is allocated with spare capacity, so ``matrix()`` slices it to
the number of populated rows (``len(store)``). A model / dimension change vs.
``embeddings_meta`` is a hard error that only ``--reembed`` clears.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from types import TracebackType

import numpy as np
from numpy.lib.format import open_memmap
from sqlmodel import Session

from sortandpost.db.models import EmbeddingsMeta

log = logging.getLogger(__name__)

# Spare rows added whenever the matrix fills up. Keeps a full index of ~25k
# photos to a couple of dozen grows rather than 25k file rewrites.
_GROWTH_ROWS = 1024
_ROWMAP_VERSION = 1


class EmbeddingDimMismatch(RuntimeError):
    """The on-disk matrix has a different dimensionality than the loaded model.

    Recover with ``python -m sortandpost.indexer --reembed``.
    """


class ModelMismatch(RuntimeError):
    """``embeddings_meta`` was written by a different model/pretrained/dim.

    The stored embeddings are not comparable with the ones the current config
    would produce; recover with ``python -m sortandpost.indexer --reembed``.
    """


class EmbeddingStore:
    """Read/write access to one ``img_<model>.npy`` matrix + its ``rowmap.json``."""

    def __init__(self, matrix_path: Path, rowmap_path: Path, *, dim: int, model: str) -> None:
        self.matrix_path = Path(matrix_path)
        self.rowmap_path = Path(rowmap_path)
        self.dim = int(dim)
        self.model = model
        self._mm: np.memmap | None = None
        self._capacity = 0
        self._rows: list[str] = []  # row_index -> pixel_hash
        self._index: dict[str, int] = {}  # pixel_hash -> row_index
        self._dirty_rowmap = False

    # -- lifecycle --------------------------------------------------------- #
    @classmethod
    def open(cls, matrix_path: Path, rowmap_path: Path, *, dim: int, model: str) -> EmbeddingStore:
        store = cls(matrix_path, rowmap_path, dim=dim, model=model)
        store._load()
        return store

    def _load(self) -> None:
        self.matrix_path.parent.mkdir(parents=True, exist_ok=True)
        if self.matrix_path.exists():
            mm = open_memmap(self.matrix_path, mode="r+")
            if mm.ndim != 2 or mm.shape[1] != self.dim:
                shape = mm.shape
                del mm
                raise EmbeddingDimMismatch(
                    f"{self.matrix_path.name} is {shape}, expected (*, {self.dim})"
                )
            self._mm = mm
            self._capacity = mm.shape[0]
        else:
            self._mm = open_memmap(
                self.matrix_path,
                mode="w+",
                dtype=np.float32,
                shape=(_GROWTH_ROWS, self.dim),
            )
            self._capacity = _GROWTH_ROWS
        self._load_rowmap()

    def _load_rowmap(self) -> None:
        if self.rowmap_path.exists():
            data = json.loads(self.rowmap_path.read_text(encoding="utf-8"))
            self._rows = [str(h) for h in data.get("rows", [])]
        else:
            self._rows = []
        self._index = {h: i for i, h in enumerate(self._rows)}

    def __enter__(self) -> EmbeddingStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.flush()
        self.close()

    def close(self) -> None:
        if self._mm is not None:
            self._mm.flush()
            self._mm = None

    # -- queries --------------------------------------------------------- #
    def __len__(self) -> int:
        return len(self._rows)

    def row_for(self, pixel_hash: str) -> int | None:
        return self._index.get(pixel_hash)

    def vector_for(self, pixel_hash: str) -> np.ndarray | None:
        idx = self._index.get(pixel_hash)
        if idx is None or self._mm is None:
            return None
        return np.asarray(self._mm[idx], dtype=np.float32).copy()

    def matrix(self) -> np.ndarray:
        """A read-only view of the populated rows, shape ``(len(self), dim)``."""
        if self._mm is None or not self._rows:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.asarray(self._mm[: len(self._rows)])

    # -- mutation ------------------------------------------------------- #
    def upsert(self, pixel_hash: str, vec: np.ndarray) -> int:
        """Append *vec* for *pixel_hash*, or overwrite its row if already present.

        Returns the row index. *vec* must already be L2-normalized; it is stored
        verbatim as ``float32``.
        """
        v = np.asarray(vec, dtype=np.float32).reshape(-1)
        if v.shape[0] != self.dim:
            raise ValueError(f"embedding dim {v.shape[0]} != store dim {self.dim}")

        idx = self._index.get(pixel_hash)
        if idx is None:
            idx = len(self._rows)
            if idx >= self._capacity:
                self._grow(idx + 1)
            self._rows.append(pixel_hash)
            self._index[pixel_hash] = idx
            self._dirty_rowmap = True

        assert self._mm is not None
        self._mm[idx] = v
        return idx

    def upsert_many(self, items: list[tuple[str, np.ndarray]]) -> list[int]:
        return [self.upsert(h, v) for h, v in items]

    def _grow(self, min_rows: int) -> None:
        new_cap = max(min_rows, self._capacity + _GROWTH_ROWS)
        tmp = self.matrix_path.parent / (self.matrix_path.name + ".tmp")
        dst = open_memmap(tmp, mode="w+", dtype=np.float32, shape=(new_cap, self.dim))
        if self._mm is not None:
            dst[: self._capacity] = self._mm[:]
            self._mm.flush()
            self._mm = None
        dst.flush()
        del dst
        os.replace(tmp, self.matrix_path)
        self._mm = open_memmap(self.matrix_path, mode="r+")
        self._capacity = new_cap

    def flush(self) -> None:
        if self._mm is not None:
            self._mm.flush()
        if self._dirty_rowmap or not self.rowmap_path.exists():
            self._write_rowmap()

    def _write_rowmap(self) -> None:
        payload = {
            "version": _ROWMAP_VERSION,
            "model": self.model,
            "dim": self.dim,
            "rows": self._rows,
        }
        tmp = self.rowmap_path.parent / (self.rowmap_path.name + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, self.rowmap_path)
        self._dirty_rowmap = False


# --------------------------------------------------------------------------- #
# embeddings_meta bookkeeping
# --------------------------------------------------------------------------- #
def load_meta(session: Session, model: str) -> EmbeddingsMeta | None:
    return session.get(EmbeddingsMeta, model)


def check_compatible(session: Session, *, model: str, pretrained: str, dim: int) -> None:
    """Raise :class:`ModelMismatch` if ``embeddings_meta`` disagrees with the config.

    A missing row is fine -- the first CLIP pass writes it.
    """
    row = session.get(EmbeddingsMeta, model)
    if row is None:
        return
    if row.pretrained != pretrained or row.dim != dim:
        raise ModelMismatch(
            f"embeddings_meta has {model}/{row.pretrained} dim={row.dim}, "
            f"config wants {pretrained} dim={dim}. "
            f"Run `python -m sortandpost.indexer --reembed`."
        )


def ensure_meta(
    session: Session,
    *,
    model: str,
    pretrained: str,
    dim: int,
    count: int,
    now: datetime,
) -> EmbeddingsMeta:
    row = session.get(EmbeddingsMeta, model)
    if row is None:
        row = EmbeddingsMeta(
            model=model, pretrained=pretrained, dim=dim, count=count, updated_at=now
        )
        session.add(row)
    else:
        row.pretrained = pretrained
        row.dim = dim
        row.count = count
        row.updated_at = now
    return row
