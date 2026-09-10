"""``sortandpost.library_roots`` -- per-device library folders."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sortandpost.library_roots import (
    LibraryRoot,
    clean_path,
    compose_volume_line,
    container_roots,
    device_dir,
    load_roots,
    save_roots,
    to_host_path,
)
from sortandpost.paths import Paths


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = Paths(app_data=tmp_path / "appdata")
    p.ensure_dirs()
    return p


DEV = "3f9c1a2bdeadbeef" * 4  # a plausible device_id
HOST = "Matthews-MacBook-Air.local"


# --- device folder --------------------------------------------------------


def test_device_dir_is_named_by_hostname_and_id_suffix(paths: Paths) -> None:
    d = device_dir(paths, DEV, HOST)
    assert d.parent == paths.devices_dir
    assert d.name == "matthews-macbook-air-local-3f9c1a2b"
    assert d.is_dir()


def test_device_dir_adopts_a_folder_with_a_stale_hostname_prefix(paths: Paths) -> None:
    first = device_dir(paths, DEV, HOST)
    (first / "library.json").write_text('{"roots": []}', encoding="utf-8")
    # same machine, hostname changed
    again = device_dir(paths, DEV, "matthews-new-name")
    assert again == first  # matched on the -3f9c1a2b suffix, not the prefix
    assert list(paths.devices_dir.iterdir()) == [first]


def test_two_device_ids_get_two_folders(paths: Paths) -> None:
    a = device_dir(paths, "aaaaaaaa1111", "box")
    b = device_dir(paths, "bbbbbbbb2222", "box")
    assert a != b
    assert {p.name for p in paths.devices_dir.iterdir()} == {"box-aaaaaaaa", "box-bbbbbbbb"}


# --- load / save --------------------------------------------------------


def test_save_then_load_round_trip(paths: Paths) -> None:
    roots = [
        LibraryRoot(container="/library", host="/Users/matt/Photos", label="Main"),
        LibraryRoot(container="/library/1", host="/Volumes/Scans", label=""),
    ]
    save_roots(paths, DEV, HOST, roots)
    assert load_roots(paths, DEV, HOST) == roots


def test_load_missing_file_is_empty(paths: Paths) -> None:
    assert load_roots(paths, DEV, HOST) == []


def test_load_tolerates_corrupt_json(paths: Paths) -> None:
    (device_dir(paths, DEV, HOST) / "library.json").write_text("{not json", encoding="utf-8")
    assert load_roots(paths, DEV, HOST) == []


def test_load_accepts_a_bare_list_and_legacy_string_entries(paths: Paths) -> None:
    (device_dir(paths, DEV, HOST) / "library.json").write_text(
        json.dumps(["/library", {"container": "/library/1", "host": "D:\\Pics"}]),
        encoding="utf-8",
    )
    got = load_roots(paths, DEV, HOST)
    assert got == [
        LibraryRoot(container="/library", host="/library", label=""),
        LibraryRoot(container="/library/1", host="D:\\Pics", label=""),
    ]


def test_load_dedupes_on_container(paths: Paths) -> None:
    rows = [{"container": "/library", "host": "/a"}, {"container": "/library", "host": "/b"}]
    (device_dir(paths, DEV, HOST) / "library.json").write_text(
        json.dumps({"roots": rows}), encoding="utf-8"
    )
    got = load_roots(paths, DEV, HOST)
    assert [r.host for r in got] == ["/a"]


def test_save_is_atomic_no_tmp_left(paths: Paths) -> None:
    save_roots(paths, DEV, HOST, [LibraryRoot("/library", "/library")])
    d = device_dir(paths, DEV, HOST)
    assert (d / "library.json").exists()
    assert not (d / "library.json.tmp").exists()


# --- helpers -----------------------------------------------------------


def test_container_roots_extracts_paths() -> None:
    roots = [LibraryRoot("/library", "/x"), LibraryRoot("/library/1", "/y")]
    assert container_roots(roots) == [Path("/library"), Path("/library/1")]


def test_to_host_path_swaps_the_matching_prefix() -> None:
    roots = [LibraryRoot(container="/library", host="/Users/matt/Photos")]
    assert (
        to_host_path("/library/2024/summer/a.jpg", roots) == "/Users/matt/Photos/2024/summer/a.jpg"
    )


def test_to_host_path_uses_the_longest_matching_root() -> None:
    roots = [
        LibraryRoot(container="/library", host="/a"),
        LibraryRoot(container="/library/nested", host="/deep"),
    ]
    assert to_host_path("/library/nested/x.jpg", roots) == "/deep/x.jpg"


def test_to_host_path_windows_host_gets_backslashes() -> None:
    roots = [LibraryRoot(container="/library", host="C:\\Users\\matt\\OneDrive\\Photos")]
    assert to_host_path("/library/a/b.jpg", roots) == "C:\\Users\\matt\\OneDrive\\Photos\\a\\b.jpg"


def test_to_host_path_passthrough_when_nothing_matches() -> None:
    assert to_host_path("/other/x.jpg", [LibraryRoot("/library", "/h")]) == "/other/x.jpg"
    assert to_host_path("/anything", []) == "/anything"


def test_to_host_path_root_itself_returns_the_host_root() -> None:
    roots = [LibraryRoot(container="/library", host="/Users/matt/Photos")]
    assert to_host_path("/library", roots) == "/Users/matt/Photos"


def test_compose_volume_line() -> None:
    line = compose_volume_line(LibraryRoot(container="/library", host="/Users/matt/Photos"))
    assert line == '- "/Users/matt/Photos:/library:rw"'


def test_clean_path_strips_wrapping_quotes_and_space() -> None:
    assert clean_path("  '/Users/matt/Matt Portfolio'  ") == "/Users/matt/Matt Portfolio"
    assert clean_path('"/x/y"') == "/x/y"
    assert clean_path("/no/quotes") == "/no/quotes"
    assert clean_path("''") == ""
    assert clean_path("O'Brien/pics") == "O'Brien/pics"  # only a *matched* pair


def test_load_cleans_quoted_paths(paths: Paths) -> None:
    rows = [{"container": "/library", "host": "'/Users/matt/Matt Portfolio'"}]
    (device_dir(paths, DEV, HOST) / "library.json").write_text(
        json.dumps({"roots": rows}), encoding="utf-8"
    )
    assert load_roots(paths, DEV, HOST)[0].host == "/Users/matt/Matt Portfolio"
