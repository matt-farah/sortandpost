"""EmbeddingStore: append / replace-in-place / grow / rowmap round-trip, plus the
``embeddings_meta`` compatibility check. No torch needed -- hand-made float32
vectors exercise the memmap layer directly."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from sqlmodel import Session, select

from sortandpost.clip.store import (
    EmbeddingDimMismatch,
    EmbeddingStore,
    ModelMismatch,
    check_compatible,
    ensure_meta,
)
from sortandpost.db import init_db, make_engine
from sortandpost.db.models import EmbeddingsMeta

DIM = 6
MODEL = "ViT-B-32"


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "img.npy", tmp_path / "rowmap.json"


def _vec(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(DIM).astype(np.float32)
    return v / np.linalg.norm(v)


def _hash(tag: str) -> str:
    return (tag * 64)[:64]


def test_append_then_reopen_round_trips(tmp_path: Path) -> None:
    mp, rp = _paths(tmp_path)
    with EmbeddingStore.open(mp, rp, dim=DIM, model=MODEL) as store:
        assert store.upsert(_hash("a"), _vec(1)) == 0
        assert store.upsert(_hash("b"), _vec(2)) == 1
        assert len(store) == 2

    reopened = EmbeddingStore.open(mp, rp, dim=DIM, model=MODEL)
    assert len(reopened) == 2
    assert reopened.row_for(_hash("b")) == 1
    np.testing.assert_allclose(reopened.vector_for(_hash("a")), _vec(1), rtol=1e-6)
    assert reopened.matrix().shape == (2, DIM)
    reopened.close()


def test_upsert_existing_hash_replaces_row_in_place(tmp_path: Path) -> None:
    mp, rp = _paths(tmp_path)
    with EmbeddingStore.open(mp, rp, dim=DIM, model=MODEL) as store:
        store.upsert(_hash("a"), _vec(1))
        store.upsert(_hash("b"), _vec(2))
        idx = store.upsert(_hash("a"), _vec(99))  # same hash -> same row
        assert idx == 0
        assert len(store) == 2
        np.testing.assert_allclose(store.vector_for(_hash("a")), _vec(99), rtol=1e-6)


def test_grow_preserves_existing_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sortandpost.clip.store._GROWTH_ROWS", 2)  # grow every 2 rows
    mp, rp = _paths(tmp_path)
    with EmbeddingStore.open(mp, rp, dim=DIM, model=MODEL) as store:
        for i in range(5):
            store.upsert(_hash(chr(ord("a") + i)), _vec(i))
        assert len(store) == 5
        assert store._capacity >= 5

    reopened = EmbeddingStore.open(mp, rp, dim=DIM, model=MODEL)
    assert len(reopened) == 5
    for i in range(5):
        np.testing.assert_allclose(
            reopened.vector_for(_hash(chr(ord("a") + i))), _vec(i), rtol=1e-6
        )
    reopened.close()


def test_reopen_with_wrong_dim_raises(tmp_path: Path) -> None:
    mp, rp = _paths(tmp_path)
    with EmbeddingStore.open(mp, rp, dim=DIM, model=MODEL) as store:
        store.upsert(_hash("a"), _vec(1))
    with pytest.raises(EmbeddingDimMismatch):
        EmbeddingStore.open(mp, rp, dim=DIM + 2, model=MODEL)


def test_wrong_length_vector_rejected(tmp_path: Path) -> None:
    mp, rp = _paths(tmp_path)
    with (
        EmbeddingStore.open(mp, rp, dim=DIM, model=MODEL) as store,
        pytest.raises(ValueError),
    ):
        store.upsert(_hash("a"), np.zeros(DIM + 1, dtype=np.float32))


def test_check_compatible_and_ensure_meta(tmp_path: Path) -> None:
    engine = make_engine(tmp_path / "db.sqlite")
    init_db(engine)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with Session(engine) as s:
        check_compatible(s, model=MODEL, pretrained="p1", dim=DIM)  # no row yet -> ok
        ensure_meta(s, model=MODEL, pretrained="p1", dim=DIM, count=3, now=now)
        s.commit()

        check_compatible(s, model=MODEL, pretrained="p1", dim=DIM)  # matches -> ok
        with pytest.raises(ModelMismatch):
            check_compatible(s, model=MODEL, pretrained="p2", dim=DIM)
        with pytest.raises(ModelMismatch):
            check_compatible(s, model=MODEL, pretrained="p1", dim=DIM + 1)

        ensure_meta(s, model=MODEL, pretrained="p1", dim=DIM, count=9, now=now)
        s.commit()
        row = s.exec(select(EmbeddingsMeta)).one()
        assert row.count == 9


def test_remove_many_compacts_and_returns_new_rowmap(tmp_path: Path) -> None:
    mp, rp = _paths(tmp_path)
    with EmbeddingStore.open(mp, rp, dim=DIM, model=MODEL) as store:
        for i, tag in enumerate("abcde"):
            store.upsert(_hash(tag), _vec(i + 1))

        new_index = store.remove_many([_hash("b"), _hash("d"), _hash("zz")])

        assert new_index == {_hash("a"): 0, _hash("c"): 1, _hash("e"): 2}
        assert len(store) == 3
        assert store.row_for(_hash("b")) is None
        # surviving rows keep their vectors, just at compacted indices
        np.testing.assert_allclose(store.vector_for(_hash("c")), _vec(3), rtol=1e-6)
        np.testing.assert_allclose(store.vector_for(_hash("e")), _vec(5), rtol=1e-6)

    reopened = EmbeddingStore.open(mp, rp, dim=DIM, model=MODEL)
    assert reopened.row_for(_hash("a")) == 0
    assert reopened.row_for(_hash("e")) == 2
    assert reopened.matrix().shape == (3, DIM)
    reopened.close()


def test_remove_many_noop_when_nothing_matches(tmp_path: Path) -> None:
    mp, rp = _paths(tmp_path)
    with EmbeddingStore.open(mp, rp, dim=DIM, model=MODEL) as store:
        store.upsert(_hash("a"), _vec(1))
        store.upsert(_hash("b"), _vec(2))
        assert store.remove_many([_hash("zz")]) == {_hash("a"): 0, _hash("b"): 1}
        assert len(store) == 2
