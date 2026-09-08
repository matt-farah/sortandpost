"""Unit tests for the scoring primitives: pick_terms + upsert_prediction."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from sqlmodel import Session, select

from sortandpost.clip.tagging import pick_terms, upsert_prediction
from sortandpost.clip.vocab import load_vocab
from sortandpost.db import init_db, make_engine
from sortandpost.db.models import Photo, Prediction, PredictionState

NOW = datetime(2026, 1, 1, tzinfo=UTC)
MODEL = "ViT-B-32"


def _vocab(tmp_path: Path):
    f = tmp_path / "tags.yaml"
    f.write_text("scene:\n  - a\n  - b\n  - c\n  - d\n", encoding="utf-8")
    return load_vocab(f)


def test_pick_terms_threshold_topk_and_order(tmp_path: Path) -> None:
    vocab = _vocab(tmp_path)  # a, b, c, d
    scores = np.array([0.9, 0.1, 0.5, 0.3], dtype=np.float32)

    picked = pick_terms(scores, vocab, threshold=0.25, top_k=2)
    assert [t.name for t in picked] == ["a", "c"]  # >= 0.25, top 2 by score desc
    assert picked[0].facet == "scene"
    assert picked[0].score == pytest.approx(0.9, abs=1e-6)

    assert pick_terms(scores, vocab, threshold=0.95, top_k=8) == []
    assert pick_terms(np.zeros(0, dtype=np.float32), vocab, threshold=0.1, top_k=8) == []


def test_upsert_prediction_insert_refresh_and_sticky(tmp_path: Path) -> None:
    engine = make_engine(tmp_path / "db.sqlite")
    init_db(engine)
    ph = "a" * 64

    with Session(engine) as s:
        s.add(
            Photo(
                pixel_hash=ph,
                primary_path="/library/a.jpg",
                width=1,
                height=1,
                format="JPEG",
                file_size=1,
                file_mtime_ns=1,
                first_indexed_at=NOW,
                last_indexed_at=NOW,
            )
        )
        s.commit()

        assert upsert_prediction(s, pixel_hash=ph, tag_name="dog", model=MODEL, score=0.4, now=NOW)
        s.commit()
        row = s.exec(select(Prediction)).one()
        assert row.state == PredictionState.pending
        assert row.score == 0.4

        # refresh an existing pending row: score updates, stays pending, returns False
        assert not upsert_prediction(
            s, pixel_hash=ph, tag_name="dog", model=MODEL, score=0.7, now=NOW
        )
        s.commit()
        assert s.exec(select(Prediction)).one().score == 0.7
        assert s.exec(select(Prediction)).one().state == PredictionState.pending

        # a rejected row is sticky: refresh must not flip it back to pending
        s.exec(select(Prediction)).one().state = PredictionState.rejected
        s.commit()
        assert not upsert_prediction(
            s, pixel_hash=ph, tag_name="dog", model=MODEL, score=0.9, now=NOW
        )
        s.commit()
        row = s.exec(select(Prediction)).one()
        assert row.state == PredictionState.rejected
        assert row.score == 0.9
