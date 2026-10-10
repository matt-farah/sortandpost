"""``sortandpost.db.deletion`` -- the explicit "Delete photos" action and the
lazy cross-machine tombstone sweep (CLAUDE.md constraint #1, exception 2)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from sortandpost.clip.store import EmbeddingStore
from sortandpost.db.deletion import (
    DeleteResult,
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
from sortandpost.hashing import pixel_hash_of_image
from sortandpost.indexer.pipeline import persist_decoded
from sortandpost.indexer.workers import DecodedFile
from sortandpost.paths import Paths

NOW = datetime(2026, 2, 3, tzinfo=UTC)
MODEL = "ViT-B-32"
DIM = 4
SIZES = (320, 1280)


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
            mtime_ns=src.stat().st_mtime_ns,
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
            roots=[library],
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
            roots=[library],
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
            roots=[library],
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
            roots=[library],
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
            roots=[library],
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
            s,
            paths=paths,
            roots=[library],
            clip_model=MODEL,
            thumb_sizes=SIZES,
            now=NOW,
            trash_local_files=True,
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
    from sortandpost.trash import load_batch, put_back

    library = tmp_path / "library"
    library.mkdir()
    with Session(engine) as s:
        ph = _make_photo(s, paths, "a", library=library)
        s.commit()
        result = delete_photos(
            s,
            pixel_hashes=[ph],
            paths=paths,
            roots=[library],
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

    batch = load_batch(trash_dir)
    assert batch.hashes == [ph]
    saved = batch.photos[ph]
    assert saved["rating"]["stars"] == 4
    assert [t["name"] for t in saved["tags"]] == ["tag-a"]
    assert [p["tag_name"] for p in saved["predictions"]] == ["beach"]
    assert batch.thumb(ph) is not None  # smallest thumbnail kept for the trash view

    restored = put_back(trash_dir)
    assert all(i.ok for i in restored.items)
    assert (library / "a.jpg").read_bytes().startswith(b"jpeg-bytes-a")


# --------------------------------------------------------------------------- #
# Only this machine's library files that still hold the deleted pixels move.
# ``photo_paths`` rides the shared DB, so a path may name another machine's
# file or a file replaced since the last scan.
# --------------------------------------------------------------------------- #
def _delete_one(s: Session, paths: Paths, ph: str, roots: list[Path]) -> DeleteResult:
    result = delete_photos(
        s,
        pixel_hashes=[ph],
        paths=paths,
        roots=roots,
        clip_model=MODEL,
        thumb_sizes=SIZES,
        device_id="d1",
        now=NOW,
    )
    s.commit()
    return result


def test_delete_never_moves_a_file_outside_the_library_roots(
    engine: Engine, paths: Paths, tmp_path: Path
) -> None:
    library = tmp_path / "library"
    elsewhere = tmp_path / "Documents"
    library.mkdir()
    elsewhere.mkdir()
    with Session(engine) as s:
        ph = _make_photo(s, paths, "a", library=elsewhere)  # e.g. a hostile synced DB row
        s.commit()
        result = _delete_one(s, paths, ph, [library])

        assert (elsewhere / "a.jpg").exists()
        assert result.files_trashed == 0 and result.files_failed == 1
        assert result.trash is not None
        assert result.trash.failed[0].error == "outside the library folders"
        assert s.get(Tombstone, ph) is not None  # the delete itself still happened


def test_delete_never_follows_a_symlink_out_of_the_library(
    engine: Engine, paths: Paths, tmp_path: Path, dir_link: Callable[[Path, Path], None]
) -> None:
    library = tmp_path / "library"
    elsewhere = tmp_path / "Documents"
    library.mkdir()
    elsewhere.mkdir()
    dir_link(library / "escape", elsewhere)
    with Session(engine) as s:
        ph = _make_photo(s, paths, "a", library=library / "escape")
        s.commit()
        result = _delete_one(s, paths, ph, [library])

    assert (elsewhere / "a.jpg").exists()
    assert result.files_trashed == 0


def test_delete_skips_a_file_replaced_since_it_was_indexed(
    engine: Engine, paths: Paths, tmp_path: Path
) -> None:
    """Same path, different photo (machine B's IMG_0001.JPG, or a re-shoot that
    overwrote the file): the pixel hash no longer matches, so it stays."""
    library = tmp_path / "library"
    library.mkdir()
    with Session(engine) as s:
        ph = _make_photo(s, paths, "a", library=library)
        s.commit()
        Image.new("RGB", (8, 8), (200, 10, 10)).save(library / "a.jpg", "JPEG")
        result = _delete_one(s, paths, ph, [library])

    assert (library / "a.jpg").exists()
    assert result.files_trashed == 0 and result.files_failed == 1
    assert result.trash is not None
    assert result.trash.failed[0].error == "the file has changed since it was indexed"


def test_delete_rehashes_a_touched_file_and_moves_it_if_the_pixels_match(
    engine: Engine, paths: Paths, tmp_path: Path
) -> None:
    """A metadata-only change (our own EXIF write, a sync tool resetting mtime)
    misses the size+mtime fast path but keeps the pixel hash."""
    library = tmp_path / "library"
    library.mkdir()
    src = library / "real.jpg"
    Image.new("RGB", (8, 8), (10, 120, 30)).save(src, "JPEG")
    with Image.open(src) as img:
        ph = pixel_hash_of_image(img)
    with Session(engine) as s:
        s.add(
            Photo(
                pixel_hash=ph,
                primary_path=str(src),
                width=8,
                height=8,
                format="JPEG",
                file_size=1,
                file_mtime_ns=1_000,
                first_indexed_at=NOW,
                last_indexed_at=NOW,
            )
        )
        s.add(PhotoPath(pixel_hash=ph, path=str(src), size=1, mtime_ns=1_000, present=True))
        s.commit()
        result = _delete_one(s, paths, ph, [library])

    assert not src.exists()
    assert result.files_trashed == 1 and result.files_failed == 0


def test_tombstone_sweep_leaves_another_machines_path_alone(
    engine: Engine, paths: Paths, tmp_path: Path
) -> None:
    """Machine B applying A's delete: A's recorded path happens to exist on B but
    outside B's library folders -- it is not touched."""
    library = tmp_path / "library"
    other = tmp_path / "other-machine-root"
    library.mkdir()
    other.mkdir()
    with Session(engine) as s:
        ph = _make_photo(s, paths, "a", library=other)
        s.add(Tombstone(pixel_hash=ph, deleted_at=NOW, deleted_by_device="A", applied=False))
        s.commit()
        result = apply_pending_tombstones(
            s,
            paths=paths,
            roots=[library],
            clip_model=MODEL,
            thumb_sizes=SIZES,
            now=NOW,
            trash_local_files=True,
        )
        s.commit()

        assert (other / "a.jpg").exists()
        assert result.purged == [ph]
        assert s.get(Tombstone, ph).applied is True
