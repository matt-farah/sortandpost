"""``sortandpost.db.deletion`` -- the explicit "Delete photos" action and the
lazy cross-machine tombstone sweep (CLAUDE.md constraint #1, exception 2)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from sortandpost.clip.store import EmbeddingStore
from sortandpost.db.deletion import (
    apply_pending_tombstones,
    delete_photos,
    is_tombstoned,
)
from sortandpost.db.models import (
    Change,
    ChangeOp,
    EmbeddingRow,
    EmbeddingsMeta,
    Photo,
    PhotoPath,
    PhotoTag,
    Prediction,
    PredictionState,
    Rating,
    RatingSource,
    ScanState,
    Tag,
    Tombstone,
)
from sortandpost.indexer.pipeline import persist_decoded
from sortandpost.indexer.workers import DecodedFile
from sortandpost.paths import Paths

NOW = datetime(2026, 2, 3, tzinfo=UTC)
MODEL = "ViT-B-32"
DIM = 4
SIZES = (320, 1280)


@pytest.fixture(autouse=True)
def _force_managed_trash(monkeypatch: pytest.MonkeyPatch) -> None:
    # Deterministic across platforms: never touch the real OS Trash from a test.
    monkeypatch.setattr("sortandpost.trash._use_os_trash", lambda: False)


def _hash(tag: str) -> str:
    return (tag * 64)[:64]


def _make_photo(
    session: Session,
    paths: Paths,
    tag: str,
    *,
    library: Path,
    present: bool = True,
    with_thumbs: bool = True,
) -> str:
    """A photo with a file on disk, both thumbnail sizes, and one of every
    derived row so a purge has something to remove."""
    ph = _hash(tag)
    src = library / f"{tag}.jpg"
    src.write_bytes(b"jpeg-bytes-" + tag.encode())

    session.add(
        Photo(
            pixel_hash=ph,
            primary_path=str(src),
            width=4,
            height=4,
            format="JPEG",
            file_size=src.stat().st_size,
            file_mtime_ns=1_000,
            first_indexed_at=NOW,
            last_indexed_at=NOW,
        )
    )
    session.add(
        PhotoPath(
            pixel_hash=ph,
            path=str(src),
            size=src.stat().st_size,
            mtime_ns=1_000,
            present=present,
        )
    )
    session.add(
        Rating(
            pixel_hash=ph,
            stars=4,
            updated_at=NOW,
            updated_by_device="d1",
            source=RatingSource.app,
        )
    )
    t = Tag(name=f"tag-{tag}")
    session.add(t)
    session.flush()
    session.add(PhotoTag(pixel_hash=ph, tag_id=t.id, added_at=NOW, added_by_device="d1"))
    session.add(
        Prediction(
            pixel_hash=ph,
            tag_name="beach",
            score=0.5,
            model=MODEL,
            run_at=NOW,
            state=PredictionState.pending,
        )
    )
    session.add(ScanState(path=str(src), pixel_hash=ph, stages_done=63, indexed_at=NOW))

    if with_thumbs:
        for size in SIZES:
            fp = paths.thumbnail_file(ph, size)
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_bytes(b"webp")
    return ph


def _seed_embeddings(session: Session, paths: Paths, hashes: list[str]) -> None:
    mp = paths.image_embeddings_file(MODEL)
    rp = paths.embeddings_rowmap_file
    with EmbeddingStore.open(mp, rp, dim=DIM, model=MODEL) as store:
        for i, h in enumerate(hashes):
            v = np.zeros(DIM, dtype=np.float32)
            v[i % DIM] = 1.0
            store.upsert(h, v)
    for i, h in enumerate(hashes):
        session.add(EmbeddingRow(pixel_hash=h, row_index=i, model=MODEL))
    session.add(
        EmbeddingsMeta(model=MODEL, pretrained="p", dim=DIM, count=len(hashes), updated_at=NOW)
    )
    session.commit()


def test_delete_purges_every_derived_row_and_trashes_the_file(
    engine: Engine, paths: Paths, tmp_path: Path
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    with Session(engine) as s:
        ph = _make_photo(s, paths, "a", library=library)
        _make_photo(s, paths, "b", library=library)  # bystander
        s.commit()

        result = delete_photos(
            s,
            pixel_hashes=[ph],
            paths=paths,
            clip_model=MODEL,
            thumb_sizes=SIZES,
            device_id="d1",
            now=NOW,
        )
        s.commit()

        assert result.purged == [ph]
        assert result.files_trashed == 1 and result.files_failed == 0
        assert not (library / "a.jpg").exists()
        assert (library / "b.jpg").exists()

        assert s.get(Photo, ph) is None
        assert s.exec(select(PhotoPath).where(PhotoPath.pixel_hash == ph)).all() == []
        assert s.get(Rating, ph) is None
        assert s.exec(select(PhotoTag).where(PhotoTag.pixel_hash == ph)).all() == []
        assert s.exec(select(Prediction).where(Prediction.pixel_hash == ph)).all() == []
        assert s.exec(select(ScanState).where(ScanState.pixel_hash == ph)).all() == []
        for size in SIZES:
            assert not paths.thumbnail_file(ph, size).exists()

        # bystander untouched
        assert s.get(Photo, _hash("b")) is not None
        assert paths.thumbnail_file(_hash("b"), 320).exists()


def test_delete_writes_unindex_change_and_tombstone(
    engine: Engine, paths: Paths, tmp_path: Path
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    with Session(engine) as s:
        ph = _make_photo(s, paths, "a", library=library)
        s.commit()
        delete_photos(
            s,
            pixel_hashes=[ph],
            paths=paths,
            clip_model=MODEL,
            thumb_sizes=SIZES,
            device_id="dev-1",
            now=NOW,
        )
        s.commit()

        change = s.exec(select(Change).where(Change.op == ChangeOp.unindex)).one()
        assert change.pixel_hash == ph
        assert change.device_id == "dev-1"

        tomb = s.get(Tombstone, ph)
        assert tomb is not None and tomb.applied is True
        assert is_tombstoned(s, ph)


def test_delete_compacts_the_embedding_matrix_and_repoints_rows(
    engine: Engine, paths: Paths, tmp_path: Path
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    with Session(engine) as s:
        a = _make_photo(s, paths, "a", library=library)
        b = _make_photo(s, paths, "b", library=library)
        c = _make_photo(s, paths, "c", library=library)
        s.commit()
        _seed_embeddings(s, paths, [a, b, c])

        result = delete_photos(
            s,
            pixel_hashes=[b],
            paths=paths,
            clip_model=MODEL,
            thumb_sizes=SIZES,
            device_id="d1",
            now=NOW,
        )
        s.commit()
        assert result.embeddings_removed == 1

        rows = {r.pixel_hash: r.row_index for r in s.exec(select(EmbeddingRow)).all()}
        assert rows == {a: 0, c: 1}
        assert s.get(EmbeddingsMeta, MODEL).count == 2

        store = EmbeddingStore.open(
            paths.image_embeddings_file(MODEL), paths.embeddings_rowmap_file, dim=DIM, model=MODEL
        )
        assert len(store) == 2
        assert store.row_for(b) is None
        assert store.row_for(a) == 0 and store.row_for(c) == 1
        store.close()


def test_delete_of_already_missing_photo_still_tombstones(
    engine: Engine, paths: Paths, tmp_path: Path
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    with Session(engine) as s:
        ph = _make_photo(s, paths, "a", library=library, present=False)
        (library / "a.jpg").unlink()
        s.commit()

        result = delete_photos(
            s,
            pixel_hashes=[ph],
            paths=paths,
            clip_model=MODEL,
            thumb_sizes=SIZES,
            device_id="d1",
            now=NOW,
        )
        s.commit()
        assert result.files_trashed == 0
        assert s.get(Photo, ph) is None
        assert s.get(Tombstone, ph) is not None


def test_apply_pending_tombstones_lazily_purges_and_trashes(
    engine: Engine, paths: Paths, tmp_path: Path
) -> None:
    """The machine-B path: a tombstone arrives via merge (applied=False) while the
    photo is still fully indexed and its file still present."""
    library = tmp_path / "library"
    library.mkdir()
    with Session(engine) as s:
        ph = _make_photo(s, paths, "a", library=library)
        s.add(Tombstone(pixel_hash=ph, deleted_at=NOW, deleted_by_device="other", applied=False))
        s.commit()

        result = apply_pending_tombstones(
            s,
            paths=paths,
            clip_model=MODEL,
            thumb_sizes=SIZES,
            now=NOW,
            trash_local_files=True,
        )
        s.commit()

        assert result.purged == [ph]
        assert result.files_trashed == 1
        assert not (library / "a.jpg").exists()
        assert s.get(Photo, ph) is None
        assert s.get(Tombstone, ph).applied is True

        # idempotent: a second sweep does nothing
        again = apply_pending_tombstones(
            s, paths=paths, clip_model=MODEL, thumb_sizes=SIZES, now=NOW, trash_local_files=True
        )
        assert again.purged == []


def test_scanner_refuses_to_reindex_a_tombstoned_hash(engine: Engine, paths: Paths) -> None:
    ph = _hash("z")
    with Session(engine) as s:
        s.add(Tombstone(pixel_hash=ph, deleted_at=NOW, deleted_by_device="d1", applied=False))
        s.commit()

        decoded = DecodedFile(
            path="/library/z.jpg",
            size=10,
            mtime_ns=1,
            pixel_hash=ph,
            width=4,
            height=4,
            fmt="JPEG",
            phash="0" * 16,
            thumbnails_written=(320, 1280),
        )
        outcome = persist_decoded(s, decoded, exiftool=None, device_id="d1", now=NOW)
        s.commit()

        assert outcome.tombstoned is True
        assert s.get(Photo, ph) is None
        assert s.exec(select(PhotoPath).where(PhotoPath.pixel_hash == ph)).all() == []


def test_managed_trash_is_recoverable(engine: Engine, paths: Paths, tmp_path: Path) -> None:
    from sortandpost.trash import restore_managed

    library = tmp_path / "library"
    library.mkdir()
    with Session(engine) as s:
        ph = _make_photo(s, paths, "a", library=library)
        s.commit()
        result = delete_photos(
            s,
            pixel_hashes=[ph],
            paths=paths,
            clip_model=MODEL,
            thumb_sizes=SIZES,
            device_id="d1",
            now=NOW,
        )
        s.commit()

    assert result.trash is not None and result.trash.backend == "managed"
    trash_dir = Path(result.trash.trash_dir)
    assert trash_dir.is_relative_to(paths.local)
    assert (trash_dir / "manifest.json").exists()

    restored = restore_managed(trash_dir)
    assert all(i.ok for i in restored)
    assert (library / "a.jpg").read_bytes().startswith(b"jpeg-bytes-a")
