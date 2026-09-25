"""``sortandpost.library_view`` -- the merged library/search view: URL state,
date ranges, the new filter clauses, and ``resolve_view`` ordering (which the
photo page's prev/next reuses)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from sqlmodel import Session, select

from sortandpost.clip.model import ClipModelUnavailable
from sortandpost.clip.store import EmbeddingStore
from sortandpost.config import Settings
from sortandpost.db import init_db, make_engine
from sortandpost.db.flags import EDIT_FLAG
from sortandpost.db.models import (
    Photo,
    PhotoPath,
    PhotoTag,
    Prediction,
    PredictionState,
    Rating,
    Tag,
    TagSource,
)
from sortandpost.library_roots import LibraryRoot
from sortandpost.library_view import (
    UNRATED,
    LibraryQuery,
    camera_choices,
    dates_label,
    dates_to_picker,
    folder_choices,
    neighbors,
    parse_date_range,
    picker_to_dates,
    resolve_view,
    tag_choices,
)
from sortandpost.search.filters import PhotoFilter, allowed_hashes

NOW = datetime(2026, 1, 1, tzinfo=UTC)
DIM = 4


def _h(tag: str) -> str:
    return (tag * 64)[:64]


def _unit(vals: list[float]) -> np.ndarray:
    v = np.asarray(vals, dtype=np.float32)
    return v / np.linalg.norm(v)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("APP_DATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("PORT", raising=False)
    return Settings.resolve()


@pytest.fixture
def session(tmp_path: Path):
    engine = make_engine(tmp_path / "db.sqlite")
    init_db(engine)
    with Session(engine) as s:
        yield s


def _add(
    s: Session,
    tag: str,
    path: str,
    *,
    taken: datetime | None = None,
    make: str | None = None,
    model: str | None = None,
    mtime: int = 1,
    is_raw: bool = False,
) -> None:
    s.add(
        Photo(
            pixel_hash=_h(tag),
            primary_path=path,
            width=1,
            height=1,
            format="JPEG",
            is_raw=is_raw,
            file_size=1,
            file_mtime_ns=mtime,
            first_indexed_at=NOW,
            last_indexed_at=NOW,
            camera_make=make,
            camera_model=model,
            taken_at=taken,
        )
    )
    s.add(PhotoPath(pixel_hash=_h(tag), path=path, size=1, mtime_ns=mtime))


def _rate(s: Session, tag: str, stars: int) -> None:
    s.add(Rating(pixel_hash=_h(tag), stars=stars, updated_at=NOW, updated_by_device="d"))


def _tag(s: Session, tag: str, name: str, *, facet: str | None = None) -> None:
    t = s.exec(select(Tag).where(Tag.name == name)).first()
    if t is None:
        t = Tag(name=name, facet=facet)
        s.add(t)
        s.flush()
    assert t.id is not None
    s.add(
        PhotoTag(
            pixel_hash=_h(tag),
            tag_id=t.id,
            source=TagSource.manual,
            added_at=NOW,
            added_by_device="d",
        )
    )


def _predict(s: Session, tag: str, name: str, state: PredictionState) -> None:
    s.add(
        Prediction(pixel_hash=_h(tag), tag_name=name, score=0.3, model="m", run_at=NOW, state=state)
    )


def _seed(s: Session) -> None:
    """a (newest) .. d (oldest), e undated."""
    _add(s, "a", "/lib/trip/a.jpg", taken=datetime(2026, 2, 1), make="Canon", model="Canon R5")
    _add(
        s, "b", "/lib/trip/day2/b.jpg", taken=datetime(2025, 6, 14), make="FUJIFILM", model="X-S10"
    )
    _add(s, "c", "/lib/home/c.jpg", taken=datetime(2025, 1, 3), make="Canon", model="Canon R5")
    _add(s, "d", "/lib/tripx/d.jpg", taken=datetime(2024, 12, 31))
    _add(s, "e", "/lib/e.jpg")
    s.commit()
    _rate(s, "a", 5)
    _rate(s, "b", 0)  # a zeroed rating counts as unrated
    _rate(s, "c", 3)
    _tag(s, "a", "dog", facet="subject")
    _predict(s, "b", "beach", PredictionState.pending)
    _predict(s, "c", "beach", PredictionState.rejected)
    _predict(s, "d", "dog", PredictionState.pending)
    s.commit()


def _ids(s: Session, settings: Settings, qs: str, **kw: object) -> list[str]:
    view = resolve_view(s, settings, LibraryQuery.from_query_string(qs), **kw)  # type: ignore[arg-type]
    tags = {_h(t): t for t in "abcdef"}
    return [tags[h] for h in view.ids]


# --------------------------------------------------------------------------- #
# dates
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "lo", "hi"),
    [
        ("2025", datetime(2025, 1, 1), datetime(2025, 12, 31, 23, 59, 59, 999999)),
        ("2025-2026", datetime(2025, 1, 1), datetime(2026, 12, 31, 23, 59, 59, 999999)),
        (
            "20260103-20260210",
            datetime(2026, 1, 3),
            datetime(2026, 2, 10, 23, 59, 59, 999999),
        ),
        ("2025-06", datetime(2025, 6, 1), datetime(2025, 6, 30, 23, 59, 59, 999999)),
        ("202512", datetime(2025, 12, 1), datetime(2025, 12, 31, 23, 59, 59, 999999)),
        ("2025-06-14", datetime(2025, 6, 14), datetime(2025, 6, 14, 23, 59, 59, 999999)),
        ("2025-06 .. 2025-08", datetime(2025, 6, 1), datetime(2025, 8, 31, 23, 59, 59, 999999)),
        ("2025 to 2026", datetime(2025, 1, 1), datetime(2026, 12, 31, 23, 59, 59, 999999)),
        ("2026–2025", datetime(2025, 1, 1), datetime(2026, 12, 31, 23, 59, 59, 999999)),  # noqa: RUF001
        ("2025..", datetime(2025, 1, 1), None),
        ("..2024", None, datetime(2024, 12, 31, 23, 59, 59, 999999)),
        ("", None, None),
    ],
)
def test_parse_date_range(text: str, lo: datetime | None, hi: datetime | None) -> None:
    assert parse_date_range(text) == (lo, hi)


@pytest.mark.parametrize("bad", ["junk", "2025-6", "20251301", "..", "2025-06-2026"])
def test_parse_date_range_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_date_range(bad)


def test_bad_dates_constrain_nothing_but_report() -> None:
    lq = LibraryQuery(dates="nonsense")
    assert lq.date_bounds() == (None, None)
    assert lq.date_error() is not None
    assert LibraryQuery(dates="2025").date_error() is None


def test_picker_to_dates() -> None:
    assert picker_to_dates({"from": "2026-01-03", "to": "2026-02-10"}) == "20260103-20260210"
    assert picker_to_dates({"from": "2026/01/03", "to": "2026/01/03"}) == "20260103"
    assert picker_to_dates("2025-06-14") == "20250614"
    assert picker_to_dates(None) == "" and picker_to_dates({"from": None}) == ""
    lo, hi = parse_date_range(picker_to_dates({"from": "2026-01-03", "to": "2026-02-10"}))
    assert lo == datetime(2026, 1, 3) and hi is not None and hi.date().day == 10


def test_dates_to_picker() -> None:
    assert dates_to_picker(LibraryQuery(dates="2025-06")) == {
        "from": "2025-06-01",
        "to": "2025-06-30",
    }
    assert dates_to_picker(LibraryQuery()) is None
    assert dates_to_picker(LibraryQuery(dates="2025..")) is None
    assert dates_to_picker(LibraryQuery(dates="junk")) is None


@pytest.mark.parametrize(
    ("dates", "label"),
    [
        ("", ""),
        ("20250614", "Jun 14, 2025"),
        ("20260103-20260210", "Jan 3 \u2013 Feb 10, 2026"),
        ("20251220-20260105", "Dec 20, 2025 \u2013 Jan 5, 2026"),
        ("2025", "2025"),
        ("2024-2026", "2024 \u2013 2026"),
        ("2025..", "from Jan 1, 2025"),
        ("..2024", "until Dec 31, 2024"),
        ("junk", "junk"),
    ],
)
def test_dates_label(dates: str, label: str) -> None:
    assert dates_label(LibraryQuery(dates=dates)) == label


# --------------------------------------------------------------------------- #
# URL state
# --------------------------------------------------------------------------- #
def test_query_string_round_trips() -> None:
    lq = LibraryQuery(
        q="dog on beach",
        stars=frozenset({UNRATED, 4, 5}),
        cameras=("Canon R5", ""),
        dates="2025-2026",
        folder="/lib/trip",
        tags=("dog", "beach"),
        tagmode="all",
        gps=True,
        mark=True,
        group="month",
    )
    qs = lq.query_string()
    assert "stars=u,4,5" in qs
    assert LibraryQuery.from_query_string(qs) == lq
    assert LibraryQuery.from_query_string("?" + qs) == lq


def test_view_params_and_empty() -> None:
    assert LibraryQuery().query_string() == ""
    assert LibraryQuery().url() == "/"
    lq = LibraryQuery(group="folder", folder_sort="az")
    assert not lq.has_filters()
    assert lq.query_string(view=False) == ""
    assert lq.url() == "/?group=folder&folder_sort=az"


def test_junk_params_are_ignored() -> None:
    lq = LibraryQuery.from_query_string("stars=9,x,u&group=bogus&tagmode=weird&tag=&gps=nah")
    assert lq == LibraryQuery(stars=frozenset({UNRATED}))


def test_text_query_disables_group_by() -> None:
    assert LibraryQuery(group="month").mode == "month"
    assert LibraryQuery(q="x", group="month").mode is None


def test_to_filter_maps_fields() -> None:
    flt = LibraryQuery(tags=("a", "b"), mark=True, stars=frozenset({2})).to_filter()
    assert flt.tags_any == ["a", "b"] and flt.tags_all == [EDIT_FLAG]
    assert flt.tag_suggestions and flt.stars_in == {2}
    flt = LibraryQuery(tags=("a", "b"), tagmode="all").to_filter()
    assert flt.tags_all == ["a", "b"] and flt.tags_any == []


# --------------------------------------------------------------------------- #
# new PhotoFilter clauses
# --------------------------------------------------------------------------- #
def test_stars_in_with_unrated(session: Session) -> None:
    _seed(session)
    unrated = allowed_hashes(session, PhotoFilter(stars_in=frozenset({UNRATED})))
    assert unrated == {_h("b"), _h("d"), _h("e")}
    mixed = allowed_hashes(session, PhotoFilter(stars_in=frozenset({UNRATED, 5})))
    assert mixed == {_h("a"), _h("b"), _h("d"), _h("e")}


def test_cameras_exact_labels(session: Session) -> None:
    _seed(session)
    got = allowed_hashes(session, PhotoFilter(cameras=["Canon R5"]))
    assert got == {_h("a"), _h("c")}
    got = allowed_hashes(session, PhotoFilter(cameras=["FUJIFILM X-S10", ""]))
    assert got == {_h("b"), _h("d"), _h("e")}


def test_folder_includes_subfolders_but_not_siblings(session: Session) -> None:
    _seed(session)
    assert allowed_hashes(session, PhotoFilter(folder="/lib/trip")) == {_h("a"), _h("b")}
    assert allowed_hashes(session, PhotoFilter(folder="/lib/trip/")) == {_h("a"), _h("b")}
    assert len(allowed_hashes(session, PhotoFilter(folder="/lib")) or ()) == 5


def test_tag_suggestions_match_unrejected_predictions(session: Session) -> None:
    _seed(session)
    assert allowed_hashes(session, PhotoFilter(tags_any=["dog"])) == {_h("a")}
    with_sugg = PhotoFilter(tags_any=["dog"], tag_suggestions=True)
    assert allowed_hashes(session, with_sugg) == {_h("a"), _h("d")}
    beach = PhotoFilter(tags_any=["beach"], tag_suggestions=True)
    assert allowed_hashes(session, beach) == {_h("b")}  # c's was rejected


# --------------------------------------------------------------------------- #
# resolve_view
# --------------------------------------------------------------------------- #
def test_unfiltered_is_newest_first(session: Session, settings: Settings) -> None:
    _seed(session)
    assert _ids(session, settings, "") == ["a", "b", "c", "d", "e"]


def test_filters_keep_date_order(session: Session, settings: Settings) -> None:
    _seed(session)
    assert _ids(session, settings, "stars=u") == ["b", "d", "e"]
    assert _ids(session, settings, "dates=2025") == ["b", "c"]
    assert _ids(session, settings, "camera=Canon+R5&dates=2025..") == ["a", "c"]
    view = resolve_view(session, settings, LibraryQuery(stars=frozenset({3})))
    assert view.total == 5


def test_group_by_reorders_filtered_set(session: Session, settings: Settings) -> None:
    _seed(session)
    view = resolve_view(session, settings, LibraryQuery(group="camera", dates="2025.."))
    assert [g.label for g in view.groups] == ["Canon R5", "FUJIFILM X-S10"]
    assert view.ids == [_h("a"), _h("c"), _h("b")]


def test_filtered_view_shows_stack_representative(session: Session, settings: Settings) -> None:
    """A RAW+JPEG pair stacks; filtering on the hidden member still shows the
    stack (via its representative), at the representative's position."""
    settings.config.stacking.enabled = True
    _add(session, "j", "/lib/s/IMG_1.jpg", taken=datetime(2025, 3, 1), mtime=5)
    _add(session, "r", "/lib/s/IMG_1.CR2", taken=datetime(2025, 3, 1), mtime=1, is_raw=True)
    _add(session, "x", "/lib/s/other.jpg", taken=datetime(2025, 4, 1))
    session.commit()
    _rate(session, "r", 4)  # only the hidden member matches
    session.commit()
    view = resolve_view(session, settings, LibraryQuery(stars=frozenset({4})))
    assert view.ids == [_h("j")]
    assert view.stacks.size(_h("j")) == 2


class _Encoder:
    def encode_text(self, texts: list[str]) -> np.ndarray:
        return np.stack([_unit([1, 0, 0, 0])])


def test_text_query_ranks_by_relevance(session: Session, settings: Settings) -> None:
    _seed(session)
    model = settings.config.clip.model
    with EmbeddingStore.open(
        settings.paths.image_embeddings_file(model),
        settings.paths.embeddings_rowmap_file,
        dim=DIM,
        model=model,
    ) as store:
        store.upsert(_h("c"), _unit([1, 0, 0, 0]))
        store.upsert(_h("a"), _unit([0.8, 0.2, 0, 0]))
        store.upsert(_h("b"), _unit([0, 1, 0, 0]))
    got = _ids(session, settings, "q=anything&group=month", encoder=_Encoder)
    assert got == ["c", "a", "b"]  # relevance, not date; group ignored
    got = _ids(session, settings, "q=anything&stars=5", encoder=_Encoder)
    assert got == ["a"]


def test_text_query_without_clip_gives_notice(session: Session, settings: Settings) -> None:
    _seed(session)
    view = resolve_view(session, settings, LibraryQuery(q="dog"), encoder=None)
    assert view.ids == [] and view.notice

    def _broken() -> _Encoder:
        raise ClipModelUnavailable("no weights")

    view = resolve_view(session, settings, LibraryQuery(q="dog"), encoder=_broken)
    assert view.ids == [] and view.notice


def test_neighbors() -> None:
    ids = ["a", "b", "c"]
    assert neighbors(ids, "a") == (None, "b")
    assert neighbors(ids, "b") == ("a", "c")
    assert neighbors(ids, "c") == ("b", None)
    assert neighbors(ids, "zz") == (None, None)


# --------------------------------------------------------------------------- #
# filter-bar choices
# --------------------------------------------------------------------------- #
def test_camera_choices_most_used_first_unknown_last(session: Session) -> None:
    _seed(session)
    assert camera_choices(session) == [("Canon R5", 2), ("FUJIFILM X-S10", 1), ("", 2)]


def test_tag_choices_group_by_facet_and_skip_rejected_and_flag(session: Session) -> None:
    _seed(session)
    _tag(session, "b", EDIT_FLAG)
    _predict(session, "e", "sunset", PredictionState.rejected)
    session.commit()
    got = tag_choices(session)
    assert got == {"subject": ["dog"], "tags": ["beach"]}


def test_folder_choices_include_ancestors_relative_to_root(session: Session) -> None:
    _seed(session)
    roots = [LibraryRoot(container="/lib", host="/Users/me/Pictures", label="Pictures")]
    got = folder_choices(session, roots)
    assert got == [
        ("/lib", "Pictures"),
        ("/lib/home", "Pictures/home"),
        ("/lib/trip", "Pictures/trip"),
        ("/lib/trip/day2", "Pictures/trip/day2"),
        ("/lib/tripx", "Pictures/tripx"),
    ]


# --------------------------------------------------------------------------- #
# posted (sent to robopost)
# --------------------------------------------------------------------------- #
def test_posted_param_round_trips() -> None:
    for raw, want in (("1", "1"), ("true", "1"), ("0", "0"), ("no", "0"), ("", "")):
        lq = LibraryQuery.from_query_string(f"posted={raw}")
        assert lq.posted == want
        assert LibraryQuery.from_query_string(lq.query_string()) == lq
    assert LibraryQuery(posted="0").summary() == "not posted"
    assert LibraryQuery(posted="1").has_filters()


def test_posted_filter(session: Session, settings: Settings) -> None:
    from sortandpost.posts import create_post, save_post

    _seed(session)
    sent = create_post(settings.paths, title="x", photos=[_h("a"), _h("c")])
    sent.status = "scheduled"
    save_post(settings.paths, sent)
    create_post(settings.paths, title="draft only", photos=[_h("b")])  # never sent
    assert _ids(session, settings, "posted=1") == ["a", "c"]
    assert _ids(session, settings, "posted=0") == ["b", "d", "e"]
    assert _ids(session, settings, "posted=0&stars=5") == []
