"""``sortandpost.collections`` -- photo collections in ``sync/collections.json``."""

from __future__ import annotations

import json

import pytest

from sortandpost.collections import (
    NAME_MAX_LEN,
    Collection,
    DuplicateNameError,
    add_to_collection,
    clean_name,
    collections_containing,
    create_collection,
    delete_collection,
    get_collection,
    load_collections,
    remove_from_collection,
    rename_collection,
    reorder_collection,
    save_collections,
    set_collection_notes,
)
from sortandpost.paths import Paths


def test_empty_when_no_file(paths: Paths) -> None:
    assert not paths.collections_file.exists()
    assert load_collections(paths) == []


def test_file_lives_under_sync(paths: Paths) -> None:
    create_collection(paths, "Trip")
    assert paths.collections_file.is_relative_to(paths.sync)


def test_create_stamps_id_and_timestamps(paths: Paths) -> None:
    c = create_collection(paths, "  Beach  ", notes="  sunset set ")
    assert c.name == "Beach"
    assert c.notes == "sunset set"
    assert len(c.id) == 16
    assert c.created_at and c.updated_at == c.created_at
    (loaded,) = load_collections(paths)
    assert loaded.id == c.id and loaded.name == "Beach"


def test_add_appends_dedupes_and_counts(paths: Paths) -> None:
    c = create_collection(paths, "c")
    assert add_to_collection(paths, c.id, ["a", "b", "c"]) == 3
    assert add_to_collection(paths, c.id, ["c", "d"]) == 1  # only d is new
    assert get_collection(paths, c.id).photos == ["a", "b", "c", "d"]


def test_add_preserves_insertion_order(paths: Paths) -> None:
    c = create_collection(paths, "c")
    add_to_collection(paths, c.id, ["z", "m", "a"])
    assert get_collection(paths, c.id).photos == ["z", "m", "a"]


def test_remove(paths: Paths) -> None:
    c = create_collection(paths, "c")
    add_to_collection(paths, c.id, ["a", "b", "c", "d"])
    assert remove_from_collection(paths, c.id, ["b", "d", "nope"]) == 2
    assert get_collection(paths, c.id).photos == ["a", "c"]


def test_reorder_is_a_permutation_with_leftovers_kept(paths: Paths) -> None:
    c = create_collection(paths, "c")
    add_to_collection(paths, c.id, ["a", "b", "c", "d"])
    reorder_collection(paths, c.id, ["d", "a", "zzz"])  # zzz unknown; b,c omitted
    assert get_collection(paths, c.id).photos == ["d", "a", "b", "c"]


def test_rename_and_resort(paths: Paths) -> None:
    a = create_collection(paths, "alpha")
    create_collection(paths, "beta")
    rename_collection(paths, a.id, "zeta")
    assert [c.name for c in load_collections(paths)] == ["beta", "zeta"]


def test_set_notes(paths: Paths) -> None:
    c = create_collection(paths, "c")
    set_collection_notes(paths, c.id, "post copy here")
    assert get_collection(paths, c.id).notes == "post copy here"


def test_delete(paths: Paths) -> None:
    a = create_collection(paths, "a")
    create_collection(paths, "b")
    delete_collection(paths, a.id)
    assert [c.name for c in load_collections(paths)] == ["b"]


def test_collections_containing(paths: Paths) -> None:
    a = create_collection(paths, "a")
    b = create_collection(paths, "b")
    add_to_collection(paths, a.id, ["h1", "h2"])
    add_to_collection(paths, b.id, ["h2"])
    assert sorted(c.name for c in collections_containing(paths, "h2")) == ["a", "b"]
    assert [c.name for c in collections_containing(paths, "h1")] == ["a"]


def test_list_sorted_by_name_case_insensitively(paths: Paths) -> None:
    create_collection(paths, "zebra")
    create_collection(paths, "apple")
    create_collection(paths, "Mango")
    assert [c.name for c in load_collections(paths)] == ["apple", "Mango", "zebra"]


def test_post_init_dedupes_photos() -> None:
    c = Collection(name="x", photos=["a", "b", "a", "", "c", "b"])
    assert c.photos == ["a", "b", "c"]


def test_corrupt_file_yields_empty(paths: Paths) -> None:
    paths.collections_file.parent.mkdir(parents=True, exist_ok=True)
    paths.collections_file.write_text("}{not json", encoding="utf-8")
    assert load_collections(paths) == []


def test_coerce_drops_nameless_and_ignores_unknown_keys(paths: Paths) -> None:
    paths.collections_file.parent.mkdir(parents=True, exist_ok=True)
    paths.collections_file.write_text(
        json.dumps(
            {
                "collections": [
                    {"name": "keep", "id": "abc", "photos": ["h1", 7, "h1"], "bogus": 1},
                    {"photos": ["x"]},  # no name
                    {"name": "   "},  # blank name
                    "not a dict",
                ]
            }
        ),
        encoding="utf-8",
    )
    got = load_collections(paths)
    assert [c.name for c in got] == ["keep"]
    assert got[0].id == "abc"
    assert got[0].photos == ["h1"]  # int dropped, dupe collapsed


def test_bare_list_json_is_accepted(paths: Paths) -> None:
    paths.collections_file.parent.mkdir(parents=True, exist_ok=True)
    paths.collections_file.write_text(json.dumps([{"name": "b"}, {"name": "a"}]), encoding="utf-8")
    assert [c.name for c in load_collections(paths)] == ["a", "b"]


def test_save_round_trips_all_fields(paths: Paths) -> None:
    c = Collection(name="Trip", notes="n", photos=["p1", "p2"])
    save_collections(paths, [c])
    (got,) = load_collections(paths)
    assert got == c


def test_add_to_missing_collection_is_noop(paths: Paths) -> None:
    assert add_to_collection(paths, "nonexistent", ["a"]) == 0
    assert load_collections(paths) == []


def test_clean_name_strips_control_chars_and_collapses_whitespace() -> None:
    assert clean_name("  My   Trip\t2024 ") == "My Trip 2024"
    assert clean_name("line\nbreak\r\nhere") == "line break here"
    assert clean_name("bell\x07null\x00del\x7f") == "bell null del"
    assert clean_name("") == ""
    assert clean_name("   \n\t ") == ""


def test_clean_name_keeps_ordinary_punctuation() -> None:
    # a semicolon (and friends) are fine -- json.dumps escapes everything; the
    # point of clean_name is only control chars / whitespace / length.
    s = "Trip; day 2 & 3 (best!) @ home #summer"
    assert clean_name(s) == s


def test_clean_name_caps_length() -> None:
    assert len(clean_name("x" * 500)) == NAME_MAX_LEN


def test_create_and_rename_sanitize_the_name(paths: Paths) -> None:
    c = create_collection(paths, "  weird\x00\nname\t ")
    assert c.name == "weird name"
    (loaded,) = load_collections(paths)
    assert loaded.name == "weird name"
    rename_collection(paths, c.id, "clean\r\nagain")
    assert get_collection(paths, c.id).name == "clean again"


def test_name_with_semicolon_round_trips_through_the_json_file(paths: Paths) -> None:
    c = create_collection(paths, 'a; b "c" \\d/ e', notes='has "quotes"; and ; semis')
    raw = json.loads(paths.collections_file.read_text(encoding="utf-8"))
    assert raw["collections"][0]["name"] == 'a; b "c" \\d/ e'
    (loaded,) = load_collections(paths)
    assert loaded.name == c.name and loaded.notes == c.notes


def test_corrupt_file_is_backed_up_not_lost(paths: Paths) -> None:
    paths.collections_file.parent.mkdir(parents=True, exist_ok=True)
    paths.collections_file.write_text("}{ not json", encoding="utf-8")
    assert load_collections(paths) == []
    assert not paths.collections_file.exists()
    backups = list(paths.collections_file.parent.glob("collections.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "}{ not json"
    # and the store is usable again afterwards
    create_collection(paths, "fresh")
    assert [c.name for c in load_collections(paths)] == ["fresh"]


def test_create_refuses_a_name_already_taken(paths: Paths) -> None:
    first = create_collection(paths, "Double Exposure")
    for again in ("Double Exposure", "double exposure", "  Double   Exposure "):
        with pytest.raises(DuplicateNameError) as err:
            create_collection(paths, again)
        assert err.value.existing.id == first.id
    assert [c.name for c in load_collections(paths)] == ["Double Exposure"]


def test_rename_refuses_another_collections_name(paths: Paths) -> None:
    a = create_collection(paths, "alpha")
    create_collection(paths, "beta")
    with pytest.raises(DuplicateNameError):
        rename_collection(paths, a.id, "BETA")
    assert sorted(c.name for c in load_collections(paths)) == ["alpha", "beta"]
    # re-casing its own name is fine
    rename_collection(paths, a.id, "Alpha")
    assert sorted(c.name for c in load_collections(paths)) == ["Alpha", "beta"]
