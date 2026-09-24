"""``sortandpost.searches`` -- saved library views in ``sync/searches.json``."""

from __future__ import annotations

import json

from sortandpost.library_view import UNRATED, LibraryQuery
from sortandpost.paths import Paths
from sortandpost.searches import (
    SavedSearch,
    add_search,
    legacy_query,
    load_searches,
    remove_search,
    save_searches,
)


def _s(name: str, **kw: object) -> SavedSearch:
    return SavedSearch.of(name, LibraryQuery(**kw))  # type: ignore[arg-type]


def test_empty_when_no_file(paths: Paths) -> None:
    assert not paths.saved_searches_file.exists()
    assert load_searches(paths) == []


def test_file_lives_under_sync(paths: Paths) -> None:
    add_search(paths, _s("x", q="cat"))
    assert paths.saved_searches_file.is_relative_to(paths.sync)


def test_add_then_load_round_trips(paths: Paths) -> None:
    s = _s("Beach 4★", q="beach at sunset", stars=frozenset({4, 5}), gps=True)
    add_search(paths, s)
    got = load_searches(paths)
    assert got == [s]


def test_add_same_name_replaces_case_insensitively(paths: Paths) -> None:
    add_search(paths, _s("Favorites", stars=frozenset({3})))
    add_search(paths, _s("favorites", stars=frozenset({5}), mark=True))
    got = load_searches(paths)
    assert len(got) == 1
    assert got[0].library_query.stars == {5} and got[0].library_query.mark is True


def test_list_is_sorted_by_name(paths: Paths) -> None:
    add_search(paths, _s("zebra"))
    add_search(paths, _s("apple"))
    add_search(paths, _s("Mango"))
    assert [s.name for s in load_searches(paths)] == ["apple", "Mango", "zebra"]


def test_remove(paths: Paths) -> None:
    add_search(paths, _s("a"))
    add_search(paths, _s("b"))
    remove_search(paths, "A")  # case-insensitive
    assert [s.name for s in load_searches(paths)] == ["b"]


def test_url() -> None:
    assert _s("x").url() == "/"
    s = _s("x", q="red car", stars=frozenset({UNRATED, 3}), cameras=("X-S10",), gps=True)
    url = s.url()
    assert url.startswith("/?")
    assert "q=red+car" in url and "stars=u,3" in url and "camera=X-S10" in url
    assert "gps=true" in url


def test_view_params_are_not_saved() -> None:
    s = SavedSearch.of("x", LibraryQuery(q="dog", group="month", folder_sort="az"))
    assert s.query == "q=dog"


def test_is_empty_and_summary() -> None:
    assert _s("x").is_empty()
    assert not _s("x", stars=frozenset({2})).is_empty()
    assert _s("x").summary() == "all photos"
    assert _s("x", q="dog", stars=frozenset({UNRATED, 2})).summary() == "“dog” · unrated 2★"


def test_legacy_fields_convert(paths: Paths) -> None:
    """Pre-merge files stored /search fields; ``stars`` was a minimum."""
    paths.saved_searches_file.parent.mkdir(parents=True, exist_ok=True)
    paths.saved_searches_file.write_text(
        json.dumps(
            {
                "searches": [
                    {
                        "name": "old",
                        "q": "beach",
                        "stars": 3,
                        "camera": "X-S10",
                        "gps": True,
                        "dups": False,
                        "missing": False,
                        "mark": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (got,) = load_searches(paths)
    lq = got.library_query
    assert lq.q == "beach" and lq.stars == {3, 4, 5}
    assert lq.cameras == ("X-S10",) and lq.gps and lq.mark and not lq.dups


def test_legacy_query_from_url_params() -> None:
    """``/search?...`` redirects use the same conversion (string values)."""
    lq = legacy_query({"q": "cat", "stars": "5", "missing": "true", "mark": "false"})
    assert lq == LibraryQuery(q="cat", stars=frozenset({5}), missing=True)
    assert legacy_query({"stars": "0"}) == LibraryQuery()


def test_corrupt_file_yields_empty(paths: Paths) -> None:
    paths.saved_searches_file.parent.mkdir(parents=True, exist_ok=True)
    paths.saved_searches_file.write_text("}{not json", encoding="utf-8")
    assert load_searches(paths) == []


def test_coerce_drops_nameless_and_ignores_unknown_keys(paths: Paths) -> None:
    paths.saved_searches_file.parent.mkdir(parents=True, exist_ok=True)
    paths.saved_searches_file.write_text(
        json.dumps(
            {
                "searches": [
                    {"name": "keep", "query": "q=x", "bogus": 1},
                    {"q": "no name here"},
                    {"name": "  ", "q": "blank name"},
                    "not a dict",
                ]
            }
        ),
        encoding="utf-8",
    )
    got = load_searches(paths)
    assert [s.name for s in got] == ["keep"]
    assert got[0].library_query.q == "x"


def test_bare_list_json_is_accepted(paths: Paths) -> None:
    paths.saved_searches_file.parent.mkdir(parents=True, exist_ok=True)
    paths.saved_searches_file.write_text(
        json.dumps([{"name": "b"}, {"name": "a"}]), encoding="utf-8"
    )
    assert [s.name for s in load_searches(paths)] == ["a", "b"]


def test_save_searches_overwrites(paths: Paths) -> None:
    add_search(paths, _s("old"))
    save_searches(paths, [_s("new")])
    assert [s.name for s in load_searches(paths)] == ["new"]
