"""``sortandpost.prefs`` -- per-machine UI preferences (theme)."""

from __future__ import annotations

from pathlib import Path

from sortandpost.paths import Paths
from sortandpost.prefs import (
    get_open_with_app,
    get_theme,
    load_prefs,
    save_prefs,
    set_open_with_app,
    set_theme,
)


def test_default_theme_is_system_when_no_file(paths: Paths) -> None:
    assert not paths.ui_prefs_file.exists()
    assert get_theme(paths) == "system"


def test_set_theme_round_trips(paths: Paths) -> None:
    set_theme(paths, "dark")
    assert get_theme(paths) == "dark"
    set_theme(paths, "light")
    assert get_theme(paths) == "light"
    assert paths.ui_prefs_file.is_relative_to(paths.local)  # never synced


def test_set_theme_preserves_other_prefs(paths: Paths) -> None:
    save_prefs(paths, {"something_else": 42})
    set_theme(paths, "dark")
    prefs = load_prefs(paths)
    assert prefs == {"something_else": 42, "theme": "dark"}


def test_unknown_theme_value_falls_back_to_system(paths: Paths) -> None:
    save_prefs(paths, {"theme": "solarized"})
    assert get_theme(paths) == "system"


def test_corrupt_prefs_file_falls_back(paths: Paths) -> None:
    paths.ui_prefs_file.write_text("{not json", encoding="utf-8")
    assert load_prefs(paths) == {}
    assert get_theme(paths) == "system"


def test_set_theme_rejects_garbage(paths: Paths) -> None:
    set_theme(paths, "bogus")  # type: ignore[arg-type]
    assert get_theme(paths) == "system"


def test_bootstrapped_tree_has_no_prefs_file(bootstrapped: Paths) -> None:
    # bootstrap seeds config/tags/device but not ui_prefs -- it's written lazily
    assert not (Path(bootstrapped.local) / "ui_prefs.json").exists()
    assert get_theme(bootstrapped) == "system"


def test_default_open_with_app_is_none(paths: Paths) -> None:
    assert get_open_with_app(paths) is None


def test_set_open_with_app_round_trips(paths: Paths) -> None:
    set_open_with_app(paths, "/Applications/Preview.app")
    assert get_open_with_app(paths) == "/Applications/Preview.app"


def test_set_open_with_app_none_clears_it(paths: Paths) -> None:
    set_open_with_app(paths, "/Applications/Preview.app")
    set_open_with_app(paths, None)
    assert get_open_with_app(paths) is None


def test_set_open_with_app_blank_string_clears_it(paths: Paths) -> None:
    set_open_with_app(paths, "/Applications/Preview.app")
    set_open_with_app(paths, "   ")
    assert get_open_with_app(paths) is None


def test_set_open_with_app_strips_whitespace(paths: Paths) -> None:
    set_open_with_app(paths, "  /Applications/Preview.app  ")
    assert get_open_with_app(paths) == "/Applications/Preview.app"


def test_set_open_with_app_preserves_other_prefs(paths: Paths) -> None:
    save_prefs(paths, {"theme": "dark"})
    set_open_with_app(paths, "/Applications/Preview.app")
    prefs = load_prefs(paths)
    assert prefs == {"theme": "dark", "open_with_app": "/Applications/Preview.app"}
