"""Grid multi-select shared by the library and collection pages: Select all,
keeping a selection across a collection redraw, the ⌘/Ctrl+A key, and the
browser guard that stops ⌘A from also highlighting the page's text.

The selection is tested without a UI (no tile has drawn its checkbox, so the
repaint only calls ``on_change``). The guard test runs the real
``_SELECT_ALL_GUARD_JS`` in headless Chromium, skipped when Playwright /
Chromium aren't installed (like test_key_focus_guard).
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from sortandpost.app import _SELECT_ALL_GUARD_JS, _GridSelection, _is_select_all


def _sel(ids: list[str]) -> tuple[_GridSelection, list[int]]:
    calls: list[int] = []
    sel = _GridSelection(ids, lambda: calls.append(1))
    return sel, calls


def test_select_all_takes_every_photo_in_grid_order() -> None:
    sel, calls = _sel(["a", "b", "c", "d"])
    sel.click("c")
    sel.select_all()
    assert sel.ordered() == ["a", "b", "c", "d"]
    assert calls  # the toolbar re-renders


def test_select_all_on_empty_grid_is_empty() -> None:
    sel, _ = _sel([])
    sel.select_all()
    assert sel.ordered() == []


def test_set_order_keeps_photos_still_shown_and_follows_new_order() -> None:
    sel, _ = _sel(["a", "b", "c", "d"])
    sel.click("a")
    sel.click("c")
    sel.click("d")
    # "d" removed from the collection, "c" moved first
    sel.set_order(["c", "a", "b"])
    assert sel.selected == {"a", "c"}
    assert sel.ordered() == ["c", "a"]
    assert sel.order == ["c", "a", "b"]


def test_set_order_resets_the_range_anchor() -> None:
    sel, _ = _sel(["a", "b", "c"])
    sel.click("a")
    sel.set_order(["a", "b", "c"])
    sel.click("c", shift=True)  # no anchor any more: a plain toggle, not a range
    assert sel.ordered() == ["a", "c"]


def test_clicks_on_photos_not_in_the_grid_are_ignored() -> None:
    sel, _ = _sel(["a", "b"])
    sel.set_order(["b"])
    sel.click("a")
    assert sel.selected == set()


def _key(key: str, *, meta: bool = False, ctrl: bool = False, alt: bool = False,
         keydown: bool = True, repeat: bool = False) -> Any:  # fmt: skip
    return SimpleNamespace(
        key=key,
        action=SimpleNamespace(keydown=keydown, repeat=repeat),
        modifiers=SimpleNamespace(meta=meta, ctrl=ctrl, alt=alt, shift=False),
    )


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (_key("a", meta=True), True),
        (_key("a", ctrl=True), True),
        (_key("A", meta=True), True),
        (_key("a"), False),
        (_key("a", meta=True, alt=True), False),
        (_key("a", meta=True, keydown=False), False),
        (_key("a", meta=True, repeat=True), False),
        (_key("b", meta=True), False),
    ],
)
def test_is_select_all(event: Any, expected: bool) -> None:
    assert _is_select_all(event) is expected


# -- the browser guard ------------------------------------------------------ #

_PAGE = """
<p id="text">Some page text</p><input id="search" value="dog"><button id="btn">Day</button>
<script>
  window.prevented = [];
  document.addEventListener('keydown', e => window.prevented.push(e.defaultPrevented));
</script>
"""


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # browser binary not installed
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


@pytest.fixture
def page(browser: Any) -> Iterator[Any]:
    pg = browser.new_page()
    pg.set_content(_PAGE)
    pg.evaluate(_SELECT_ALL_GUARD_JS)
    pg.evaluate(_SELECT_ALL_GUARD_JS)  # a second page install is a no-op
    yield pg
    pg.close()


def _last_prevented(page: Any) -> bool:
    return bool(page.evaluate("window.prevented.at(-1)"))


def test_cmd_a_on_the_page_does_not_select_text(page: Any) -> None:
    page.keyboard.press("ControlOrMeta+a")
    assert _last_prevented(page)
    assert page.evaluate("String(getSelection())") == ""


def test_cmd_a_in_a_field_keeps_native_select_all(page: Any) -> None:
    page.focus("#search")
    page.keyboard.press("ControlOrMeta+a")
    assert not _last_prevented(page)


def test_cmd_a_on_a_focused_button_lets_go_of_it(page: Any) -> None:
    page.focus("#btn")
    page.keyboard.press("ControlOrMeta+a")
    assert _last_prevented(page)
    assert page.evaluate("document.activeElement.tagName") == "BODY"


def test_plain_a_is_left_alone(page: Any) -> None:
    page.keyboard.press("a")
    assert not _last_prevented(page)
