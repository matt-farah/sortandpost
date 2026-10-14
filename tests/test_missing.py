"""Missing photos are judged per computer (V3 P9, ``sortandpost.missing``)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image
from sqlmodel import Session, select

from sortandpost import missing
from sortandpost.config import Settings
from sortandpost.db import make_engine
from sortandpost.db.browse import list_photo_ids, photo_count
from sortandpost.db.models import Photo, PhotoPath
from sortandpost.indexer import scan_once
from sortandpost.indexer.pipeline import sweep_presence
from sortandpost.library_roots import LibraryRoot, other_device_roots, save_roots
from sortandpost.missing import MissingReason, PathScope
from sortandpost.search.filters import PhotoFilter, allowed_hashes

A_ID = "a" * 32
B_ID = "b" * 32


@pytest.fixture(autouse=True)
def _unset_scope() -> Iterator[None]:
    yield
    missing.configure(None)


@pytest.fixture
def two_machines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Settings, Path, Path]:
    """One shared app-data tree (what B has after pulling A's published DB)
    and two library folders: A's and B's."""
    lib_a, lib_b = tmp_path / "mac-a" / "Pictures", tmp_path / "mac-b" / "Photos"
    lib_a.mkdir(parents=True)
    lib_b.mkdir(parents=True)
    monkeypatch.setenv("APP_DATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("PORT", raising=False)
    settings = Settings.resolve()
    save_roots(settings.paths, A_ID, "mac-a", [LibraryRoot(container=str(lib_a), host=str(lib_a))])
    save_roots(settings.paths, B_ID, "mac-b", [LibraryRoot(container=str(lib_b), host=str(lib_b))])
    return settings, lib_a, lib_b


def _as(settings: Settings, device_id: str, root: Path) -> Settings:
    settings.device_id = device_id
    settings.set_library_roots([root])
    missing.configure(settings)
    return settings


def _jpeg(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), color).save(path, "JPEG", quality=90)


def _session(settings: Settings) -> Session:
    return Session(make_engine(settings.paths.working_db))


def _by_name(s: Session) -> dict[str, Photo]:
    return {Path(p.primary_path).name: p for p in s.exec(select(Photo)).all()}


# --------------------------------------------------------------------------- #
# the two-machine check
# --------------------------------------------------------------------------- #
def test_unscoped_sweep_marks_the_other_computers_photos_missing(
    two_machines: tuple[Settings, Path, Path],
) -> None:
    """What every full scan did before P9: B judged A's paths too."""
    settings, lib_a, lib_b = two_machines
    _jpeg(lib_a / "a.jpg", (10, 20, 30))
    scan_once(_as(settings, A_ID, lib_a))
    _jpeg(lib_b / "b.jpg", (200, 50, 50))
    scan_once(_as(settings, B_ID, lib_b))

    with _session(settings) as s:
        sweep_presence(s, {str(lib_b / "b.jpg")}, now=datetime.now(UTC))  # no judged=
        assert _by_name(s)["a.jpg"].missing_since is not None
        a_path = s.exec(select(PhotoPath).where(PhotoPath.path == str(lib_a / "a.jpg"))).one()
        assert not a_path.present


def test_full_scan_leaves_the_other_computers_paths_alone(
    two_machines: tuple[Settings, Path, Path],
) -> None:
    settings, lib_a, lib_b = two_machines
    _jpeg(lib_a / "a.jpg", (10, 20, 30))
    scan_once(_as(settings, A_ID, lib_a))

    _jpeg(lib_b / "b.jpg", (200, 50, 50))
    stats = scan_once(_as(settings, B_ID, lib_b))
    assert stats.marked_absent == 0 and stats.marked_missing == 0

    with _session(settings) as s:
        photos = _by_name(s)
        assert photos["a.jpg"].missing_since is None
        assert all(pp.present for pp in s.exec(select(PhotoPath)).all())
        # B's grid shows A's photo; nothing is missing on B
        assert set(list_photo_ids(s)) == {p.pixel_hash for p in photos.values()}
        assert missing.missing_count(s) == 0

    # B loses its own file: missing on B, not on A
    (lib_b / "b.jpg").unlink()
    assert scan_once(settings).marked_missing == 1
    with _session(settings) as s:
        b_hash = _by_name(s)["b.jpg"].pixel_hash
        assert list_photo_ids(s, only_missing=True) == [b_hash]
        assert missing.missing_reasons(s, [b_hash]) == {b_hash: MissingReason.gone}
        assert _by_name(s)["a.jpg"].missing_since is None

    _as(settings, A_ID, lib_a)
    with _session(settings) as s:
        assert missing.missing_count(s) == 0  # A never had b.jpg


def test_copy_on_both_lost_here_is_missing_here_only(
    two_machines: tuple[Settings, Path, Path],
) -> None:
    settings, lib_a, lib_b = two_machines
    _jpeg(lib_a / "same.jpg", (1, 2, 3))
    _jpeg(lib_b / "same.jpg", (1, 2, 3))
    scan_once(_as(settings, A_ID, lib_a))
    scan_once(_as(settings, B_ID, lib_b))
    (lib_b / "same.jpg").unlink()
    scan_once(settings)

    with _session(settings) as s:
        photo = s.exec(select(Photo)).one()
        assert photo.missing_since is None  # A's copy is still there
        assert photo.primary_path == str(lib_a / "same.jpg")
        assert missing.missing_here_set(s, [photo.pixel_hash]) == {photo.pixel_hash}
        assert photo_count(s) == 0
    _as(settings, A_ID, lib_a)
    with _session(settings) as s:
        assert missing.missing_count(s) == 0


def test_offline_folder_reason_and_count(two_machines: tuple[Settings, Path, Path]) -> None:
    settings, _lib_a, lib_b = two_machines
    _jpeg(lib_b / "b.jpg", (9, 9, 9))
    scan_once(_as(settings, B_ID, lib_b))
    lib_b.rename(lib_b.with_name("unplugged"))
    scan_once(settings)
    missing.invalidate()

    with _session(settings) as s:
        h = s.exec(select(Photo)).one().pixel_hash
        assert missing.missing_reasons(s, [h]) == {h: MissingReason.offline}
        assert missing.offline_missing_count(s) == 1

    lib_b.with_name("unplugged").rename(lib_b)
    scan_once(settings)
    missing.invalidate()
    with _session(settings) as s:
        assert missing.missing_count(s) == 0
        assert missing.offline_missing_count(s) == 0


def test_removed_folder_is_still_judged(two_machines: tuple[Settings, Path, Path]) -> None:
    """A folder no device lists any more: whoever scans marks its paths gone."""
    settings, _lib_a, lib_b = two_machines
    old = lib_b.parent / "Old"
    _jpeg(old / "old.jpg", (4, 5, 6))
    scan_once(_as(settings, B_ID, old))
    scan_once(_as(settings, B_ID, lib_b))  # B's library.json still says lib_b only

    with _session(settings) as s:
        photo = s.exec(select(Photo)).one()
        assert photo.missing_since is not None
        reasons = missing.missing_reasons(s, [photo.pixel_hash])
        assert reasons == {photo.pixel_hash: MissingReason.removed}


def test_filter_only_missing(two_machines: tuple[Settings, Path, Path]) -> None:
    settings, _lib_a, lib_b = two_machines
    _jpeg(lib_b / "keep.jpg", (1, 1, 1))
    _jpeg(lib_b / "drop.jpg", (2, 2, 2))
    scan_once(_as(settings, B_ID, lib_b))
    (lib_b / "drop.jpg").unlink()
    scan_once(settings)

    with _session(settings) as s:
        photos = _by_name(s)
        assert allowed_hashes(s, PhotoFilter(only_missing=True)) == {photos["drop.jpg"].pixel_hash}
        assert allowed_hashes(s, PhotoFilter()) == {photos["keep.jpg"].pixel_hash}


def test_other_device_roots_skips_own(two_machines: tuple[Settings, Path, Path]) -> None:
    settings, lib_a, _lib_b = two_machines
    assert other_device_roots(settings.paths, B_ID) == [str(lib_a)]


# --------------------------------------------------------------------------- #
# PathScope rules
# --------------------------------------------------------------------------- #
def test_scope_judges_mine_and_orphans_not_others() -> None:
    scope = PathScope(mine=("/Users/me/Pictures",), others=("/Users/me", "/library"))
    assert scope.judged("/Users/me/Pictures/a.jpg")  # mine wins over a wider other
    assert not scope.judged("/Users/me/Desktop/a.jpg")
    assert not scope.judged("/library/a.jpg")
    assert scope.judged("/Volumes/Old/a.jpg")  # nobody's folder
    assert not scope.judged("/Users/me/Pictures2/a.jpg")  # a sibling, not under mine


def test_scope_prefix_is_a_folder_boundary() -> None:
    scope = PathScope(mine=("/p",), others=("/photos",))
    assert scope.root_of("/photos/x.jpg") is None
    assert not scope.judged("/photos/x.jpg")


def test_scope_windows_other_device() -> None:
    scope = PathScope.build(["/Users/me/Pictures"], ["C:\\Users\\me\\Pictures\\"])
    assert scope.others == ("C:\\Users\\me\\Pictures",)
    assert not scope.judged("C:\\Users\\me\\Pictures\\a.jpg")


def test_scope_same_spelling_counts_as_mine() -> None:
    scope = PathScope.build(["/library"], ["/library"])
    assert scope.others == ()
    assert scope.judged("/library/a.jpg")


def test_reason_offline_beats_gone() -> None:
    scope = PathScope(mine=("/a", "/b"), offline=frozenset({"/b"}))
    assert scope.reason(["/a/x.jpg", "/b/x.jpg"]) is MissingReason.offline
    assert scope.reason(["/a/x.jpg"]) is MissingReason.gone
    assert scope.reason(["/c/x.jpg"]) is MissingReason.removed
