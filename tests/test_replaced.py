"""A file saved over in place (``sortandpost.indexer.replaced``): the old photo
goes missing ("file replaced") and the new version keeps the user's work."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image
from sqlmodel import Session, select

from sortandpost import binaries, missing
from sortandpost.collections import add_to_collection, create_collection, get_collection
from sortandpost.config import Settings
from sortandpost.db import make_engine
from sortandpost.db.browse import list_photo_ids
from sortandpost.db.flags import EDIT_FLAG, set_edit_flag
from sortandpost.db.meta import get_meta
from sortandpost.db.models import Change, ChangeOp, Meta, Photo, PhotoTag, Rating, Tag
from sortandpost.db.ratings import set_rating
from sortandpost.db.tags import add_manual_tag
from sortandpost.indexer import scan_once
from sortandpost.indexer.replaced import BACKFILL_KEY, backfill
from sortandpost.metadata import ExiftoolClient
from sortandpost.missing import MissingReason

DEVICE = "d" * 32
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _unset_scope() -> Iterator[None]:
    yield
    missing.configure(None)


@pytest.fixture
def lib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Settings, Path]:
    root = tmp_path / "Pictures"
    root.mkdir()
    monkeypatch.setenv("APP_DATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("PORT", raising=False)
    settings = Settings.resolve()
    settings.device_id = DEVICE
    settings.set_library_roots([root])
    missing.configure(settings)
    return settings, root


def _jpeg(path: Path, color: tuple[int, int, int]) -> None:
    Image.new("RGB", (64, 48), color).save(path, "JPEG", quality=90)
    st = path.stat()  # a new mtime, as an editor's save would give
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


def _session(settings: Settings) -> Session:
    return Session(make_engine(settings.paths.working_db))


def _tags(s: Session, pixel_hash: str) -> set[str]:
    rows = s.exec(select(Tag.name).join(PhotoTag).where(PhotoTag.pixel_hash == pixel_hash)).all()
    return set(rows)


def _stars(s: Session, pixel_hash: str) -> int | None:
    row = s.get(Rating, pixel_hash)
    return row.stars if row else None


def _rate_tag_collect(settings: Settings, path: Path) -> tuple[str, str]:
    """Index *path*, then rate / tag / flag / collect it. Returns (hash, coll id)."""
    scan_once(settings, workers=1)
    with _session(settings) as s:
        old = s.exec(select(Photo)).one().pixel_hash
        set_rating(s, pixel_hash=old, stars=5, device_id=DEVICE, now=NOW)
        add_manual_tag(s, pixel_hash=old, tag_name="beach", device_id=DEVICE, now=NOW)
        set_edit_flag(s, pixel_hash=old, marked=True, device_id=DEVICE, now=NOW)
        s.commit()
    coll = create_collection(settings.paths, "Best")
    add_to_collection(settings.paths, coll.id, [old])
    return old, coll.id


def _the_other(s: Session, old: str) -> str:
    return next(p.pixel_hash for p in s.exec(select(Photo)).all() if p.pixel_hash != old)


def test_saved_over_file_carries_state_and_hides_the_old_photo(
    lib: tuple[Settings, Path],
) -> None:
    settings, root = lib
    path = root / "DSCF5104.jpg"
    _jpeg(path, (10, 20, 30))
    old, coll_id = _rate_tag_collect(settings, path)

    _jpeg(path, (200, 40, 40))  # edited and saved over: new pixels, no XMP
    scan_once(settings, workers=1)

    with _session(settings) as s:
        new = _the_other(s, old)
        # the new version took the rating, tags and collection...
        assert _stars(s, new) == 5
        assert _tags(s, new) == {"beach"}  # the edit flag stays behind
        ops = {(c.op, c.field) for c in s.exec(select(Change).where(Change.pixel_hash == new))}
        assert (ChangeOp.rate, "stars") in ops and (ChangeOp.tag_add, "beach") in ops
        # ...and the old one kept everything
        assert _stars(s, old) == 5
        assert _tags(s, old) == {"beach", EDIT_FLAG}
        # the old photo has no path: missing here, not a pathless duplicate
        assert s.get(Photo, old).missing_since is not None  # type: ignore[union-attr]
        assert list_photo_ids(s) == [new]
        assert list_photo_ids(s, only_missing=True) == [old]
        assert missing.missing_reasons(s, [old, new]) == {old: MissingReason.replaced}
        assert missing.replaced_by(s, old, str(path)) == new
    coll = get_collection(settings.paths, coll_id)
    assert coll is not None and coll.photos == [old, new]


def test_rating_in_the_new_file_wins(lib: tuple[Settings, Path]) -> None:
    if not binaries.exiftool_available():
        pytest.skip("needs exiftool")
    settings, root = lib
    path = root / "a.jpg"
    _jpeg(path, (10, 20, 30))
    old, _ = _rate_tag_collect(settings, path)

    _jpeg(path, (200, 40, 40))
    with ExiftoolClient() as et:
        et.write_rating(path, 3)
    scan_once(settings, workers=1)

    with _session(settings) as s:
        new = _the_other(s, old)
        assert _stars(s, new) == 3
        assert _tags(s, new) == {"beach"}


def test_path_back_to_a_known_photo_carries_nothing(lib: tuple[Settings, Path]) -> None:
    """Not a brand-new photo (a copy of it is already indexed): no carry-over."""
    settings, root = lib
    _jpeg(root / "other.jpg", (200, 40, 40))
    path = root / "a.jpg"
    _jpeg(path, (10, 20, 30))
    scan_once(settings, workers=1)
    with _session(settings) as s:
        old = next(p.pixel_hash for p in s.exec(select(Photo)) if p.primary_path == str(path))
        set_rating(s, pixel_hash=old, stars=4, device_id=DEVICE, now=NOW)
        s.commit()

    _jpeg(path, (200, 40, 40))  # now the same pixels as other.jpg
    scan_once(settings, workers=1)
    with _session(settings) as s:
        new = _the_other(s, old)
        assert _stars(s, new) is None


def test_backfill_runs_once_and_skips_a_path_that_went_back(
    lib: tuple[Settings, Path],
) -> None:
    settings, root = lib
    path = root / "a.jpg"
    _jpeg(path, (10, 20, 30))
    old, coll_id = _rate_tag_collect(settings, path)
    _jpeg(path, (200, 40, 40))
    scan_once(settings, workers=1)

    with _session(settings) as s:
        # the backfill already ran on that pass, finding nothing to do
        assert get_meta(s, BACKFILL_KEY) is not None
        # make it look like a pair from before carry-over existed
        new = _the_other(s, old)
        s.delete(s.get(Rating, new))
        for pt in s.exec(select(PhotoTag).where(PhotoTag.pixel_hash == new)):
            s.delete(pt)
        s.commit()
        assert backfill(s, paths=settings.paths, device_id=DEVICE, now=NOW) == 0  # flag set

        s.delete(s.get(Meta, BACKFILL_KEY))
        newer = s.get(Photo, new)
        assert newer is not None
        orig_first = newer.first_indexed_at
        newer.first_indexed_at = orig_first - timedelta(days=30)  # path went back
        s.commit()
        assert backfill(s, paths=settings.paths, device_id=DEVICE, now=NOW) == 0
        assert _stars(s, new) is None

        s.delete(s.get(Meta, BACKFILL_KEY))
        newer.first_indexed_at = orig_first
        s.commit()
        assert backfill(s, paths=settings.paths, device_id=DEVICE, now=NOW) == 1
        s.commit()
        assert _stars(s, new) == 5
        assert _tags(s, new) == {"beach"}
        assert backfill(s, paths=settings.paths, device_id=DEVICE, now=NOW) == 0
    coll = get_collection(settings.paths, coll_id)
    assert coll is not None and coll.photos == [old, new]
