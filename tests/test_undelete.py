"""Undo a delete end to end: scan -> rate/tag/reject -> delete -> put back.

Uses real JPEGs and the real pipeline, so the put-back re-indexes the file and
the saved rating / tags / CLIP decisions come back on top of it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image
from sqlmodel import Session, select

from sortandpost.clip.tagging import reject
from sortandpost.config import Settings
from sortandpost.db import make_engine
from sortandpost.db.deletion import delete_photos
from sortandpost.db.models import (
    Change,
    ChangeOp,
    Photo,
    Prediction,
    PredictionState,
    Rating,
    Tombstone,
)
from sortandpost.db.ratings import set_rating
from sortandpost.db.tags import add_manual_tag, photo_keywords
from sortandpost.indexer import scan_once
from sortandpost.library_roots import container_roots
from sortandpost.trash import list_batches
from sortandpost.undelete import put_back_batch

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
MODEL = "ViT-B-32"


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
    Image.new("RGB", (64, 48), color).save(path, "JPEG", quality=90)


def _delete(settings: Settings, ph: str) -> str:
    engine = make_engine(settings.paths.working_db)
    with Session(engine) as s:
        result = delete_photos(
            s,
            pixel_hashes=[ph],
            paths=settings.paths,
            roots=container_roots(settings.library_roots),
            clip_model=MODEL,
            thumb_sizes=settings.config.thumbnails.sizes,
            device_id=settings.device_id,
            now=NOW,
        )
        s.commit()
    assert result.trash is not None and result.trash.batch_id is not None
    return result.trash.batch_id


def _indexed_photo(settings: Settings) -> tuple[Path, str]:
    lib = Path(settings.library_roots[0].container)
    src = lib / "keep" / "a.jpg"
    _jpeg(src, (10, 120, 30))
    _jpeg(lib / "keep" / "other.jpg", (200, 10, 10))
    scan_once(settings, workers=1)
    engine = make_engine(settings.paths.working_db)
    with Session(engine) as s:
        photo = s.exec(select(Photo).where(Photo.primary_path == str(src))).one()
        ph = photo.pixel_hash
        set_rating(s, pixel_hash=ph, stars=4, device_id=settings.device_id, now=NOW)
        add_manual_tag(s, pixel_hash=ph, tag_name="dog", device_id=settings.device_id, now=NOW)
        reject(
            s,
            device_id=settings.device_id,
            model=MODEL,
            pixel_hash=ph,
            tag_name="beach",
            now=NOW,
        )
        s.commit()
    return src, ph


def test_put_back_restores_file_index_and_user_state(settings: Settings) -> None:
    src, ph = _indexed_photo(settings)
    batch_id = _delete(settings, ph)
    assert not src.exists()

    # a scan in between must not resurrect it (nothing on disk) nor break undo
    scan_once(settings, workers=1)

    engine = make_engine(settings.paths.working_db)
    result = put_back_batch(settings, engine, batch_id)
    assert result.error is None and not result.failed and not result.index_errors
    assert result.photos_restored == [ph]
    assert src.exists()
    assert list_batches(settings.paths.local) == []

    with Session(engine) as s:
        assert s.get(Tombstone, ph) is None
        photo = s.get(Photo, ph)
        assert photo is not None and photo.missing_since is None
        rating = s.get(Rating, ph)
        assert rating is not None and rating.stars == 4
        assert "dog" in photo_keywords(s, ph)
        pred = s.exec(
            select(Prediction).where(Prediction.pixel_hash == ph, Prediction.tag_name == "beach")
        ).one()
        assert pred.state == PredictionState.rejected
        ops = [c.op for c in s.exec(select(Change).where(Change.pixel_hash == ph)).all()]
        assert ops[-1] == ChangeOp.restore and ChangeOp.unindex in ops

    for size in settings.config.thumbnails.sizes:
        assert settings.paths.thumbnail_file(ph, size).is_file()

    # and a later scan leaves it indexed (no tombstone left to trash it again)
    scan_once(settings, workers=1)
    assert src.exists()
    with Session(engine) as s:
        assert s.get(Photo, ph) is not None


def test_put_back_refused_keeps_the_tombstone(settings: Settings) -> None:
    src, ph = _indexed_photo(settings)
    batch_id = _delete(settings, ph)
    src.write_bytes(b"someone put a different file here")

    engine = make_engine(settings.paths.working_db)
    result = put_back_batch(settings, engine, batch_id)
    assert result.photos_restored == []
    assert result.failed and "already exists" in (result.failed[0].error or "")
    with Session(engine) as s:
        assert s.get(Tombstone, ph) is not None
    assert len(list_batches(settings.paths.local)) == 1


def test_unknown_batch(settings: Settings) -> None:
    engine = make_engine(settings.paths.working_db)
    result = put_back_batch(settings, engine, "nope")
    assert result.error is not None
