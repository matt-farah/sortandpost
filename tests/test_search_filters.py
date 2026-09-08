"""PhotoFilter -> allowed pixel_hash set, browse_photos ordering, and
search_by_vector intersecting a filter."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from sqlmodel import Session, select

from sortandpost.clip.store import EmbeddingStore
from sortandpost.config import Settings
from sortandpost.db import init_db, make_engine
from sortandpost.db.models import Photo, PhotoTag, Rating, Tag, TagSource
from sortandpost.search import PhotoFilter, browse_photos, search_by_vector
from sortandpost.search.filters import allowed_hashes

NOW = datetime(2026, 1, 1, tzinfo=UTC)
DIM = 4


def _h(tag: str) -> str:
    return (tag * 64)[:64]


def _unit(vals: list[float]) -> np.ndarray:
    v = np.asarray(vals, dtype=np.float32)
    return v / np.linalg.norm(v)


@pytest.fixture
def session(tmp_path: Path):
    engine = make_engine(tmp_path / "db.sqlite")
    init_db(engine)
    with Session(engine) as s:
        yield s


def _photo(
    s: Session,
    tag: str,
    *,
    make: str | None = None,
    model: str | None = None,
    gps: bool = False,
    taken: datetime | None = None,
    phash: str | None = None,
    missing: bool = False,
) -> None:
    s.add(
        Photo(
            pixel_hash=_h(tag),
            primary_path=f"/lib/{tag}.jpg",
            width=1,
            height=1,
            format="JPEG",
            file_size=1,
            file_mtime_ns=1,
            first_indexed_at=NOW,
            last_indexed_at=NOW,
            camera_make=make,
            camera_model=model,
            gps_lat=1.0 if gps else None,
            gps_lon=2.0 if gps else None,
            taken_at=taken,
            phash=phash,
            missing_since=NOW if missing else None,
        )
    )


def _rate(s: Session, tag: str, stars: int) -> None:
    s.add(Rating(pixel_hash=_h(tag), stars=stars, updated_at=NOW, updated_by_device="d"))


def _tag(s: Session, tag: str, name: str) -> None:
    existing = s.exec(select(Tag).where(Tag.name == name)).first()
    if existing is None:
        existing = Tag(name=name)
        s.add(existing)
        s.flush()
    s.add(
        PhotoTag(
            pixel_hash=_h(tag),
            tag_id=existing.id,
            source=TagSource.manual,
            added_at=NOW,
            added_by_device="d",
        )
    )


def _seed(s: Session) -> None:
    _photo(s, "a", make="Nikon", model="Z6", gps=True, taken=datetime(2024, 5, 1, tzinfo=UTC))
    _photo(s, "b", make="Canon", model="R5", taken=datetime(2025, 5, 1, tzinfo=UTC))
    _photo(s, "c", make="Nikon", model="D850", taken=datetime(2023, 5, 1, tzinfo=UTC))
    _photo(s, "d", missing=True)
    s.commit()
    _rate(s, "a", 5)
    _rate(s, "b", 2)
    _tag(s, "a", "dog")
    _tag(s, "a", "beach")
    _tag(s, "b", "dog")
    s.commit()


def test_default_filter_is_present_set(session: Session) -> None:
    _seed(session)
    assert allowed_hashes(session, PhotoFilter()) == {_h("a"), _h("b"), _h("c")}


def test_include_missing_means_no_constraint(session: Session) -> None:
    _seed(session)
    assert allowed_hashes(session, PhotoFilter(include_missing=True)) is None


def test_min_stars(session: Session) -> None:
    _seed(session)
    assert allowed_hashes(session, PhotoFilter(min_stars=3)) == {_h("a")}
    assert allowed_hashes(session, PhotoFilter(min_stars=2)) == {_h("a"), _h("b")}


def test_has_gps(session: Session) -> None:
    _seed(session)
    assert allowed_hashes(session, PhotoFilter(has_gps=True)) == {_h("a")}
    assert allowed_hashes(session, PhotoFilter(has_gps=False)) == {_h("b"), _h("c")}


def test_camera_substring_is_case_insensitive(session: Session) -> None:
    _seed(session)
    assert allowed_hashes(session, PhotoFilter(camera="nikon")) == {_h("a"), _h("c")}
    assert allowed_hashes(session, PhotoFilter(camera="r5")) == {_h("b")}


def test_tags_all_and_any(session: Session) -> None:
    _seed(session)
    assert allowed_hashes(session, PhotoFilter(tags_all=["dog", "beach"])) == {_h("a")}
    assert allowed_hashes(session, PhotoFilter(tags_any=["dog"])) == {_h("a"), _h("b")}


def test_only_duplicates(session: Session) -> None:
    _photo(session, "x", phash="00000000000000ff")
    _photo(session, "y", phash="00000000000000ff")
    _photo(session, "z", phash="ffffffffffffffff")
    session.commit()
    assert allowed_hashes(session, PhotoFilter(only_duplicates=True, duplicate_distance=0)) == {
        _h("x"),
        _h("y"),
    }


def test_browse_photos_orders_newest_first_and_filters(session: Session) -> None:
    _seed(session)
    got = [Path(p.primary_path).stem for p in browse_photos(session, PhotoFilter(camera="nikon"))]
    assert got == ["a", "c"]  # 2024 before 2023

    assert browse_photos(session, PhotoFilter(min_stars=5)) != []
    assert browse_photos(session, PhotoFilter(min_stars=1, camera="sony")) == []


def test_search_by_vector_intersects_filter(session: Session, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APP_DATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("PORT", raising=False)
    settings = Settings.resolve()
    model = settings.config.clip.model

    _photo(session, "a", make="Nikon", model="Z6")
    _photo(session, "b", make="Canon", model="R5")
    session.commit()
    with EmbeddingStore.open(
        settings.paths.image_embeddings_file(model),
        settings.paths.embeddings_rowmap_file,
        dim=DIM,
        model=model,
    ) as store:
        store.upsert(_h("a"), _unit([1, 0, 0, 0]))
        store.upsert(_h("b"), _unit([0.9, 0.1, 0, 0]))

    unfiltered = search_by_vector(session, settings, _unit([1, 0, 0, 0]), limit=10)
    assert {h.pixel_hash for h in unfiltered} == {_h("a"), _h("b")}

    filtered = search_by_vector(
        session, settings, _unit([1, 0, 0, 0]), limit=10, flt=PhotoFilter(camera="canon")
    )
    assert [h.pixel_hash for h in filtered] == [_h("b")]
