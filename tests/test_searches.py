"""``sortandpost.searches`` -- saved `/search` states in ``sync/searches.json``."""

from __future__ import annotations

import json

from sortandpost.paths import Paths
from sortandpost.searches import (
    SavedSearch,
    add_search,
    load_searches,
    remove_search,
    save_searches,
)


def test_empty_when_no_file(paths: Paths) -> None:
    assert not paths.saved_searches_file.exists()
    assert load_searches(paths) == []


def test_file_lives_under_sync(paths: Paths) -> None:
    add_search(paths, SavedSearch(name="x", q="cat"))
    assert paths.saved_searches_file.is_relative_to(paths.sync)


def test_add_then_load_round_trips(paths: Paths) -> None:
    s = SavedSearch(name="Beach 4★", q="beach at sunset", stars=4, gps=True)
    add_search(paths, s)
    got = load_searches(paths)
    assert got == [s]


def test_add_same_name_replaces_case_insensitively(paths: Paths) -> None:
    add_search(paths, SavedSearch(name="Favorites", stars=3))
    add_search(paths, SavedSearch(name="favorites", stars=5, mark=True))
    got = load_searches(paths)
    assert len(got) == 1
    assert got[0].stars == 5 and got[0].mark is True


def test_list_is_sorted_by_name(paths: Paths) -> None:
    add_search(paths, SavedSearch(name="zebra"))
    add_search(paths, SavedSearch(name="apple"))
    add_search(paths, SavedSearch(name="Mango"))
    assert [s.name for s in load_searches(paths)] == ["apple", "Mango", "zebra"]


def test_remove(paths: Paths) -> None:
    add_search(paths, SavedSearch(name="a"))
    add_search(paths, SavedSearch(name="b"))
    remove_search(paths, "A")  # case-insensitive
    assert [s.name for s in load_searches(paths)] == ["b"]


def test_query_string() -> None:
    assert SavedSearch(name="x").query_string() == "/search"
    s = SavedSearch(
        name="x", q="red car", stars=3, camera="X-S10", gps=True, dups=True, missing=True, mark=True
    )
    qs = s.query_string()
    assert qs.startswith("/search?")
    assert "q=red%20car" in qs
    assert "stars=3" in qs and "camera=X-S10" in qs
    assert "gps=true" in qs and "dups=true" in qs and "missing=true" in qs and "mark=true" in qs


def test_is_empty_and_summary() -> None:
    assert SavedSearch(name="x").is_empty()
    assert not SavedSearch(name="x", stars=2).is_empty()
    assert SavedSearch(name="x").summary() == "all photos"
    assert SavedSearch(name="x", q="dog", stars=2).summary() == '"dog" · ≥2★'


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
                    {"name": "keep", "q": "x", "bogus": 1},
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
    assert got[0].q == "x"


def test_bare_list_json_is_accepted(paths: Paths) -> None:
    paths.saved_searches_file.parent.mkdir(parents=True, exist_ok=True)
    paths.saved_searches_file.write_text(
        json.dumps([{"name": "b"}, {"name": "a"}]), encoding="utf-8"
    )
    assert [s.name for s in load_searches(paths)] == ["a", "b"]


def test_save_searches_overwrites(paths: Paths) -> None:
    add_search(paths, SavedSearch(name="old"))
    save_searches(paths, [SavedSearch(name="new")])
    assert [s.name for s in load_searches(paths)] == ["new"]
