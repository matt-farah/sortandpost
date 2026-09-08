"""Vector search: the ``rank`` primitive on hand-made unit vectors, ``open_matrix``
slicing, and ``search_by_vector`` / ``similar_to`` including the missing-photo
filter. No torch needed."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from sqlmodel import Session

from sortandpost.clip.store import EmbeddingStore
from sortandpost.config import Settings
from sortandpost.db import init_db, make_engine
from sortandpost.db.models import Photo
from sortandpost.search.service import search_by_vector, semantic_search, similar_to
from sortandpost.search.vector import open_matrix, rank

DIM = 4


def _unit(values: list[float]) -> np.ndarray:
    v = np.asarray(values, dtype=np.float32)
    return v / np.linalg.norm(v)


def _hash(tag: str) -> str:
    return (tag * 64)[:64]


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("APP_DATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("PORT", raising=False)
    return Settings.resolve()


def _photo(pixel_hash: str, *, missing: bool = False) -> Photo:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Photo(
        pixel_hash=pixel_hash,
        primary_path=f"/library/{pixel_hash[:6]}.jpg",
        width=10,
        height=10,
        format="JPEG",
        file_size=1,
        file_mtime_ns=1,
        first_indexed_at=now,
        last_indexed_at=now,
        missing_since=now if missing else None,
    )


def _seed(settings: Settings, vecs: dict[str, np.ndarray]) -> None:
    model = settings.config.clip.model
    with EmbeddingStore.open(
        settings.paths.image_embeddings_file(model),
        settings.paths.embeddings_rowmap_file,
        dim=DIM,
        model=model,
    ) as store:
        for h, v in vecs.items():
            store.upsert(h, v)


# --------------------------------------------------------------------------- #
# rank / open_matrix
# --------------------------------------------------------------------------- #
def test_rank_orders_by_cosine_similarity() -> None:
    matrix = np.stack([_unit([1, 0, 0, 0]), _unit([0, 1, 0, 0]), _unit([1, 1, 0, 0])])
    hits = rank(matrix, _unit([1, 0, 0, 0]), limit=3)
    assert [i for i, _ in hits] == [0, 2, 1]
    assert hits[0][1] == pytest.approx(1.0, abs=1e-6)


def test_rank_limit_and_empty() -> None:
    matrix = np.stack([_unit([1, 0, 0, 0]), _unit([0, 1, 0, 0]), _unit([1, 1, 0, 0])])
    assert len(rank(matrix, _unit([1, 0, 0, 0]), limit=2)) == 2
    assert rank(np.zeros((0, DIM), dtype=np.float32), _unit([1, 0, 0, 0]), limit=5) == []


def test_open_matrix_slices_to_count(tmp_path: Path) -> None:
    mp, rp = tmp_path / "img.npy", tmp_path / "rowmap.json"
    with EmbeddingStore.open(mp, rp, dim=DIM, model="m") as store:
        store.upsert(_hash("a"), _unit([1, 0, 0, 0]))
        store.upsert(_hash("b"), _unit([0, 1, 0, 0]))
    m = open_matrix(mp, 2)
    assert m is not None and m.shape == (2, DIM)
    assert open_matrix(mp, 0) is None
    assert open_matrix(tmp_path / "missing.npy", 5) is None


# --------------------------------------------------------------------------- #
# search_by_vector / similar_to
# --------------------------------------------------------------------------- #
def test_search_ranks_and_drops_missing_photos(settings: Settings) -> None:
    vecs = {
        _hash("a"): _unit([1, 0, 0, 0]),
        _hash("b"): _unit([0.9, 0.1, 0, 0]),
        _hash("c"): _unit([0, 1, 0, 0]),
    }
    _seed(settings, vecs)

    engine = make_engine(settings.paths.working_db)
    init_db(engine)
    with Session(engine) as s:
        s.add(_photo(_hash("a")))
        s.add(_photo(_hash("b"), missing=True))
        s.add(_photo(_hash("c")))
        s.commit()

        hits = search_by_vector(s, settings, _unit([1, 0, 0, 0]), limit=10)
        got = [h.pixel_hash for h in hits]
        assert got[0] == _hash("a")
        assert _hash("b") not in got  # missing filtered out by default
        assert _hash("c") in got

        with_missing = search_by_vector(
            s, settings, _unit([1, 0, 0, 0]), limit=10, include_missing=True
        )
        assert _hash("b") in [h.pixel_hash for h in with_missing]


def test_similar_to_excludes_self(settings: Settings) -> None:
    vecs = {
        _hash("a"): _unit([1, 0, 0, 0]),
        _hash("b"): _unit([0.8, 0.2, 0, 0]),
        _hash("c"): _unit([0, 0, 1, 0]),
    }
    _seed(settings, vecs)
    engine = make_engine(settings.paths.working_db)
    init_db(engine)
    with Session(engine) as s:
        for h in vecs:
            s.add(_photo(h))
        s.commit()
        hits = similar_to(s, settings, _hash("a"), limit=5)
    got = [h.pixel_hash for h in hits]
    assert _hash("a") not in got
    assert got[0] == _hash("b")


def test_semantic_search_uses_text_encoder(settings: Settings) -> None:
    vecs = {_hash("a"): _unit([1, 0, 0, 0]), _hash("b"): _unit([0, 1, 0, 0])}
    _seed(settings, vecs)
    engine = make_engine(settings.paths.working_db)
    init_db(engine)

    class FakeEncoder:
        def encode_text(self, texts: list[str]) -> np.ndarray:
            assert texts == ["a red car"]
            return np.stack([_unit([0, 1, 0, 0])])

    with Session(engine) as s:
        for h in vecs:
            s.add(_photo(h))
        s.commit()
        hits = semantic_search(s, settings, FakeEncoder(), "  a red car  ")
    assert hits[0].pixel_hash == _hash("b")
    assert semantic_search(s, settings, FakeEncoder(), "   ") == []
