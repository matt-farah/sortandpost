"""Stack writes touch one file only (Issues.md, 2026-10-03).

Rating or tagging a stack still lands on every member in the DB (stack-level
ratings), but only the stack's *embed target* -- its newest member that can
carry metadata -- gets the EXIF/XMP write. Writing every member bumped the
original's mtime, and because the stack's top photo is its newest member, the
last member written became the new top: the stack's face swapped and the
photo page's prev/next (keyed on the old top) went dead.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from sortandpost.app import (
    _apply_bulk_rating,
    _apply_bulk_tag,
    _apply_make_top,
    _apply_photo_tag,
    _apply_rating,
)
from sortandpost.config import Settings, StackingConfig
from sortandpost.db import init_db, make_engine
from sortandpost.db.models import (
    PhotoPath,
    PhotoTag,
    Prediction,
    PredictionState,
    Rating,
    TagSource,
)
from sortandpost.db.tags import add_manual_tag, photo_keywords
from sortandpost.indexer.pipeline import _is_embed_target
from sortandpost.indexer.reconcile import reconcile_metadata
from sortandpost.metadata import EmbeddedMetadata, ExiftoolClient
from sortandpost.metadata.shared import EXIFTOOL
from sortandpost.stacks import build_stacks, embed_target
from tests.test_raw_heic_audit import assert_only_changed, index_file, snapshot_folder

pytestmark = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not on PATH")

ALL_ON = StackingConfig(enabled=True, raw_jpeg=True, similar_names=True)
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


@pytest.fixture
def library(tmp_path: Path) -> Path:
    lib = tmp_path / "library"
    lib.mkdir()
    return lib


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Engine]:
    engine = make_engine(tmp_path / "db.sqlite")
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    monkeypatch.setenv("APP_DATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("PORT", raising=False)
    s = Settings.resolve()
    s.config.stacking = ALL_ON.model_copy()
    yield s
    EXIFTOOL.close()


@pytest.fixture
def copy_stack(library: Path, db: Engine) -> tuple[str, str]:
    """``IMG.jpg`` (the original, older) + ``IMG copy.jpg`` (newer, so the top);
    returns their (original, copy) pixel_hashes."""
    orig = library / "IMG.jpg"
    copy = library / "IMG copy.jpg"
    Image.new("RGB", (64, 48), "red").save(orig, "JPEG")
    Image.new("RGB", (64, 48), "blue").save(copy, "JPEG")
    os.utime(orig, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
    os.utime(copy, ns=(1_100_000_000_000_000_000, 1_100_000_000_000_000_000))
    with Session(db) as session:
        return index_file(session, orig), index_file(session, copy)


def _rescan_mtimes(db: Engine) -> None:
    """What the watcher's rescan does to stacking: refresh each path's mtime."""
    with Session(db) as session:
        for pp in session.exec(select(PhotoPath)).all():
            pp.mtime_ns = Path(pp.path).stat().st_mtime_ns
            session.add(pp)
        session.commit()


def _top(db: Engine, pixel_hash: str) -> str:
    with Session(db) as session:
        return build_stacks(session, ALL_ON).representative(pixel_hash)


def test_embed_target_is_newest_member(db: Engine, copy_stack: tuple[str, str]) -> None:
    orig, copy = copy_stack
    with Session(db) as session:
        stacks = build_stacks(session, ALL_ON)
        assert embed_target(session, stacks, orig) == copy
        assert embed_target(session, stacks, copy) == copy


def test_rating_writes_only_the_top_file_and_keeps_the_top(
    library: Path, db: Engine, settings: Settings, copy_stack: tuple[str, str]
) -> None:
    orig, copy = copy_stack
    before = snapshot_folder(library)

    assert _apply_rating(settings, db, orig, 4)

    assert_only_changed(before, snapshot_folder(library), "IMG copy.jpg")
    with Session(db) as session:
        for h in (orig, copy):
            row = session.get(Rating, h)
            assert row is not None and row.stars == 4, "stack fan-out didn't reach the DB"
    _rescan_mtimes(db)
    assert _top(db, orig) == copy, "the stack's top photo swapped after a rating"


def test_bulk_rating_and_tag_write_only_the_top_file(
    library: Path, db: Engine, settings: Settings, copy_stack: tuple[str, str]
) -> None:
    orig, copy = copy_stack
    before = snapshot_folder(library)

    _apply_bulk_rating(settings, db, [copy], 3)
    _apply_bulk_tag(settings, db, [copy], "sunset")

    assert_only_changed(before, snapshot_folder(library), "IMG copy.jpg")
    with Session(db) as session:
        assert photo_keywords(session, copy) == ["sunset"]
        assert photo_keywords(session, orig) == [], "tags live on the top photo only"
    with ExiftoolClient() as exiftool:
        md = exiftool.read(library / "IMG copy.jpg")
    assert md.rating == 3
    assert "sunset" in md.keywords


def test_photo_page_tag_on_hidden_member_goes_on_the_top_only(
    library: Path, db: Engine, settings: Settings, copy_stack: tuple[str, str]
) -> None:
    orig, copy = copy_stack
    before = snapshot_folder(library)

    assert _apply_photo_tag(settings, db, orig, "beach", "add")

    assert_only_changed(before, snapshot_folder(library), "IMG copy.jpg")
    with Session(db) as session:
        assert photo_keywords(session, copy) == ["beach"]
        assert photo_keywords(session, orig) == []
    _rescan_mtimes(db)
    assert _top(db, orig) == copy, "tagging changed the stack's top photo"

    before = snapshot_folder(library)
    assert _apply_photo_tag(settings, db, orig, "beach", "remove")
    assert_only_changed(before, snapshot_folder(library), "IMG copy.jpg")
    with Session(db) as session:
        assert photo_keywords(session, copy) == []


def test_photo_page_remove_clears_legacy_tags_off_hidden_members(
    library: Path, db: Engine, settings: Settings, copy_stack: tuple[str, str]
) -> None:
    """Tags fanned out to every member before the top-only rule: removing one
    from the photo page takes it off all of them, writing only the top's file."""
    orig, copy = copy_stack
    with Session(db) as session:
        for h in (orig, copy):
            add_manual_tag(session, pixel_hash=h, tag_name="old", device_id="d", now=NOW)
        session.commit()
    before = snapshot_folder(library)

    assert _apply_photo_tag(settings, db, copy, "old", "remove")

    assert_only_changed(before, snapshot_folder(library), "IMG copy.jpg")
    with Session(db) as session:
        assert photo_keywords(session, orig) == []
        assert photo_keywords(session, copy) == []


def test_photo_page_clip_suggestion_on_the_top_and_removable(
    library: Path, db: Engine, settings: Settings, copy_stack: tuple[str, str]
) -> None:
    orig, copy = copy_stack
    model = settings.config.clip.model
    with Session(db) as session:
        session.add(
            Prediction(
                pixel_hash=copy,
                tag_name="red",
                score=0.5,
                model=model,
                run_at=NOW,
                state=PredictionState.pending,
            )
        )
        session.commit()
    before = snapshot_folder(library)

    # viewed from the hidden member, the suggestion shown is the top's
    assert _apply_photo_tag(settings, db, orig, "red", "approve")

    assert_only_changed(before, snapshot_folder(library), "IMG copy.jpg")
    with Session(db) as session:
        assert photo_keywords(session, orig) == []
        pt = session.exec(select(PhotoTag).where(PhotoTag.pixel_hash == copy)).one()
        assert pt.source == TagSource.clip
    with ExiftoolClient() as exiftool:
        assert "red" in exiftool.read(library / "IMG copy.jpg").keywords

    # ✕ on the approved CLIP pill: gone, and rejected so it isn't re-suggested
    assert _apply_photo_tag(settings, db, orig, "red", "remove")
    with Session(db) as session:
        assert photo_keywords(session, copy) == []
        pred = session.exec(select(Prediction).where(Prediction.pixel_hash == copy)).one()
        assert pred.state == PredictionState.rejected
    with ExiftoolClient() as exiftool:
        assert "red" not in exiftool.read(library / "IMG copy.jpg").keywords


def test_stacking_off_writes_the_photo_itself(
    library: Path, db: Engine, settings: Settings, copy_stack: tuple[str, str]
) -> None:
    orig, _copy = copy_stack
    settings.config.stacking.enabled = False
    before = snapshot_folder(library)

    assert _apply_rating(settings, db, orig, 2)

    assert_only_changed(before, snapshot_folder(library), "IMG.jpg")


# -- the indexer's reconcile never writes a hidden member's file ------------ #
class _RecordingExiftool:
    def __init__(self) -> None:
        self.rating_calls: list[tuple[str, int]] = []

    def write_rating(self, path: Path, stars: int) -> None:
        self.rating_calls.append((path.name, stars))


def test_reconcile_skips_write_back_when_told(db: Engine, copy_stack: tuple[str, str]) -> None:
    orig, _copy = copy_stack
    et = _RecordingExiftool()
    with Session(db) as session:
        session.add(
            Rating(pixel_hash=orig, stars=4, updated_at=NOW, updated_by_device="d", source="app")
        )
        session.commit()
        reconcile_metadata(
            session,
            pixel_hash=orig,
            path=Path("/lib/IMG.jpg"),
            md=EmbeddedMetadata(rating=None),
            exiftool=et,  # type: ignore[arg-type]
            device_id="d",
            file_mtime_ns=1,
            now=NOW,
            write_back=False,
        )
    assert et.rating_calls == []


def test_pipeline_write_back_only_for_the_embed_target(
    library: Path, db: Engine, copy_stack: tuple[str, str]
) -> None:
    orig, copy = copy_stack
    with Session(db) as session:
        assert not _is_embed_target(session, ALL_ON, orig, library / "IMG.jpg")
        assert _is_embed_target(session, ALL_ON, copy, library / "IMG copy.jpg")
        assert _is_embed_target(session, None, orig, library / "IMG.jpg")


def test_build_stacks_folder_ignores_subfolders(
    library: Path, db: Engine, copy_stack: tuple[str, str]
) -> None:
    orig, copy = copy_stack
    sub = library / "sub"
    sub.mkdir()
    Image.new("RGB", (64, 48), "green").save(sub / "IMG (1).jpg", "JPEG")
    with Session(db) as session:
        nested = index_file(session, sub / "IMG (1).jpg")
        stacks = build_stacks(session, ALL_ON, folder=str(library))
    assert sorted(stacks.members(orig)) == sorted([orig, copy])
    assert stacks.members(nested) == [nested]


# -- "Make this the top photo" ---------------------------------------------- #
def test_make_top_restamps_only_that_file_and_regroups(
    library: Path, db: Engine, settings: Settings, copy_stack: tuple[str, str]
) -> None:
    orig, copy = copy_stack
    assert _apply_rating(settings, db, copy, 4)  # stack rating; written to the copy only
    _apply_bulk_tag(settings, db, [copy], "sunset")
    before = snapshot_folder(library)

    assert _apply_make_top(settings, db, orig) is None

    assert_only_changed(before, snapshot_folder(library), "IMG.jpg")
    assert _top(db, copy) == orig, "photo_paths wasn't refreshed / the top didn't move"
    with ExiftoolClient() as exiftool:
        md = exiftool.read(library / "IMG.jpg")
    assert md.rating == 4
    assert "sunset" in md.keywords
    with Session(db) as session:  # the tags moved with the top
        assert photo_keywords(session, orig) == ["sunset"]
        assert photo_keywords(session, copy) == []


def test_make_top_on_an_unrated_untagged_photo_still_moves_it(
    library: Path, db: Engine, settings: Settings, copy_stack: tuple[str, str]
) -> None:
    orig, copy = copy_stack
    before = snapshot_folder(library)

    assert _apply_make_top(settings, db, orig) is None

    assert_only_changed(before, snapshot_folder(library), "IMG.jpg")
    assert _top(db, copy) == orig
    with ExiftoolClient() as exiftool:
        assert not exiftool.read(library / "IMG.jpg").rating  # explicit 0 = unrated


def test_make_top_refuses_the_current_top_and_unstacked_photos(
    library: Path, db: Engine, settings: Settings, copy_stack: tuple[str, str]
) -> None:
    _orig, copy = copy_stack
    before = snapshot_folder(library)
    assert _apply_make_top(settings, db, copy) is not None
    settings.config.stacking.enabled = False
    assert _apply_make_top(settings, db, copy) is not None
    assert_only_changed(before, snapshot_folder(library))
