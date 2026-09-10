"""The deferred CLIP pass (`scan_clip_once`): embeddings + `embedding_rows` +
`embeddings_meta` + the `Stage.CLIP` bit, idempotency, append-on-new, `--reembed`,
and the model-mismatch guard. A fake model stands in for open_clip/torch."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from sqlmodel import Session, select

from sortandpost.clip.store import EmbeddingStore, ModelMismatch, ensure_meta
from sortandpost.config import Settings
from sortandpost.db import make_engine
from sortandpost.db.models import EmbeddingRow, EmbeddingsMeta, ScanState
from sortandpost.indexer import scan_clip_once, scan_once
from sortandpost.indexer.pipeline import Stage

DIM = 8


class FakeClipModel:
    """Deterministic embeddings from a 2x2 downscale of each image."""

    def __init__(self, dim: int = DIM) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def load(self) -> None:
        pass

    def encode_images(self, images: list[Image.Image]) -> np.ndarray:
        out = np.zeros((len(images), self._dim), dtype=np.float32)
        for i, im in enumerate(images):
            small = np.asarray(im.convert("RGB").resize((2, 2)), dtype=np.float32).reshape(-1)
            v = np.resize(small, self._dim).astype(np.float32)
            norm = np.linalg.norm(v)
            out[i] = v / norm if norm else v
        return out

    def encode_text(self, texts: list[str]) -> np.ndarray:  # pragma: no cover - unused here
        return np.zeros((len(texts), self._dim), dtype=np.float32)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setenv("APP_DATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("PORT", raising=False)
    resolved = Settings.resolve()
    resolved.set_library_roots([library])
    return resolved


def _jpeg(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 24), color).save(path, "JPEG", quality=90)


def _lib(settings: Settings) -> Path:
    return Path(settings.library_roots[0].container)


def _session(settings: Settings) -> Session:
    return Session(make_engine(settings.paths.working_db))


def test_clip_pass_embeds_browsable_photos(settings: Settings) -> None:
    _jpeg(_lib(settings) / "a.jpg", (200, 10, 10))
    _jpeg(_lib(settings) / "b.jpg", (10, 200, 10))
    scan_once(settings)

    stats = scan_clip_once(settings, model=FakeClipModel())
    assert stats.embedded == 2
    assert stats.total_rows == 2
    assert not stats.skipped_no_model

    model = settings.config.clip.model
    assert settings.paths.image_embeddings_file(model).is_file()
    assert settings.paths.embeddings_rowmap_file.is_file()

    with _session(settings) as s:
        rows = s.exec(select(EmbeddingRow)).all()
        assert {r.row_index for r in rows} == {0, 1}
        assert all(r.model == model for r in rows)
        meta = s.exec(select(EmbeddingsMeta)).one()
        assert meta.dim == DIM
        assert meta.count == 2
        assert meta.pretrained == settings.config.clip.pretrained
        for scan in s.exec(select(ScanState)).all():
            assert scan.stages_done & int(Stage.CLIP)

    store = EmbeddingStore.open(
        settings.paths.image_embeddings_file(model),
        settings.paths.embeddings_rowmap_file,
        dim=DIM,
        model=model,
    )
    assert len(store) == 2
    store.close()


def test_clip_pass_is_idempotent_and_appends_new(settings: Settings) -> None:
    _jpeg(_lib(settings) / "a.jpg", (200, 10, 10))
    scan_once(settings)
    assert scan_clip_once(settings, model=FakeClipModel()).embedded == 1

    # second run: nothing new
    again = scan_clip_once(settings, model=FakeClipModel())
    assert again.embedded == 0
    assert again.total_rows == 1

    # add a photo -> only that one is embedded, appended at row 1
    _jpeg(_lib(settings) / "b.jpg", (10, 10, 200))
    scan_once(settings)
    third = scan_clip_once(settings, model=FakeClipModel())
    assert third.embedded == 1
    assert third.total_rows == 2

    with _session(settings) as s:
        by_hash = {r.pixel_hash: r.row_index for r in s.exec(select(EmbeddingRow)).all()}
        assert sorted(by_hash.values()) == [0, 1]


def test_missing_photo_is_not_embedded(settings: Settings) -> None:
    _jpeg(_lib(settings) / "keep.jpg", (1, 2, 3))
    _jpeg(_lib(settings) / "drop.jpg", (4, 5, 6))
    scan_once(settings)
    (_lib(settings) / "drop.jpg").unlink()
    scan_once(settings)  # marks drop.jpg missing

    stats = scan_clip_once(settings, model=FakeClipModel())
    assert stats.embedded == 1
    assert stats.total_rows == 1


def test_reembed_rebuilds_from_scratch(settings: Settings) -> None:
    _jpeg(_lib(settings) / "a.jpg", (200, 10, 10))
    _jpeg(_lib(settings) / "b.jpg", (10, 200, 10))
    scan_once(settings)
    scan_clip_once(settings, model=FakeClipModel())

    reembed = scan_clip_once(settings, reembed=True, model=FakeClipModel())
    assert reembed.embedded == 2
    assert reembed.total_rows == 2

    with _session(settings) as s:
        assert len(s.exec(select(EmbeddingRow)).all()) == 2
        assert len(s.exec(select(EmbeddingsMeta)).all()) == 1


def test_model_mismatch_blocks_pass(settings: Settings) -> None:
    _jpeg(_lib(settings) / "a.jpg", (200, 10, 10))
    scan_once(settings)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with _session(settings) as s:
        ensure_meta(
            s,
            model=settings.config.clip.model,
            pretrained="some-other-weights",
            dim=DIM,
            count=0,
            now=now,
        )
        s.commit()

    with pytest.raises(ModelMismatch):
        scan_clip_once(settings, model=FakeClipModel())

    # --reembed clears the stale meta and proceeds
    stats = scan_clip_once(settings, reembed=True, model=FakeClipModel())
    assert stats.embedded == 1


def test_clip_pass_skips_cleanly_without_model(settings: Settings) -> None:
    _jpeg(_lib(settings) / "a.jpg", (200, 10, 10))
    scan_once(settings)

    class Unavailable(FakeClipModel):
        def load(self) -> None:
            from sortandpost.clip.model import ClipModelUnavailable

            raise ClipModelUnavailable("no torch here")

    stats = scan_clip_once(settings, model=Unavailable())
    assert stats.skipped_no_model
    assert stats.embedded == 0
