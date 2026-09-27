"""The page shortcuts (arrows / 1-5 / f) must survive focus stranded in a field or
on a button -- ui.keyboard drops every key while one of those is focused, which
is how a filter dropdown or the search box closed with Esc used to leave the
grid's arrows and ratings silently dead.

Runs the real ``_KEY_FOCUS_GUARD_JS`` in headless Chromium against a stand-in for
NiceGUI's ``ui.keyboard`` (same document-level listener, same default ``ignore``
focus check). Skipped when Playwright / Chromium aren't installed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from sortandpost.app import _KEY_FOCUS_GUARD_JS

sync_api = pytest.importorskip("playwright.sync_api")

# mirrors nicegui/elements/keyboard.js: bail out if the focused element's tag is
# in `ignore` (default input/select/button/textarea), else the key is handled
_PAGE = """
<input id="search"><button id="btn">Day</button>
<script>
  window.handled = [];
  const ignore = ['input', 'select', 'button', 'textarea'];
  document.addEventListener('keydown', e => {
    const focus = document.activeElement;
    if (focus && ignore.includes(focus.tagName.toLowerCase())) return;
    window.handled.push(e.key);
  });
</script>
"""


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # browser binary not installed
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


@pytest.fixture
def page(browser: Any) -> Iterator[Any]:
    # a fresh page each test: the guard's one-time flag lives on `window`
    pg = browser.new_page()
    yield pg
    pg.close()


def _load(page: Any, *, guard: bool) -> None:
    page.set_content(_PAGE)
    if guard:
        page.evaluate(_KEY_FOCUS_GUARD_JS)
        page.evaluate(_KEY_FOCUS_GUARD_JS)  # a second page install is a no-op


def _handled(page: Any) -> list[str]:
    return list(page.evaluate("window.handled"))


def test_without_guard_esc_from_field_leaves_shortcuts_dead(page: Any) -> None:
    """The bug, pinned: without the guard the arrows go nowhere after Esc."""
    _load(page, guard=False)
    page.focus("#search")
    page.keyboard.press("Escape")
    page.keyboard.press("ArrowRight")
    assert _handled(page) == []


def test_esc_releases_a_field_and_shortcuts_resume(page: Any) -> None:
    _load(page, guard=True)
    page.focus("#search")
    page.keyboard.press("Escape")
    assert page.evaluate("document.activeElement.tagName") == "BODY"
    page.keyboard.press("ArrowRight")
    page.keyboard.press("3")
    # the Esc itself stayed the field's (not also a grid "clear focus")
    assert _handled(page) == ["ArrowRight", "3"]


def test_typing_in_a_field_is_not_hijacked(page: Any) -> None:
    _load(page, guard=True)
    page.focus("#search")
    page.keyboard.type("dog 3")
    page.keyboard.press("ArrowLeft")
    assert page.input_value("#search") == "dog 3"
    assert _handled(page) == []


@pytest.mark.parametrize("key", ["ArrowRight", "ArrowDown", "4", "0", "f"])
def test_shortcut_on_a_focused_button_goes_through(page: Any, key: str) -> None:
    _load(page, guard=True)
    page.focus("#btn")
    page.keyboard.press(key)
    assert _handled(page) == [key]


@pytest.mark.parametrize("key", [" ", "Enter", "Tab"])
def test_button_keeps_its_own_activate_and_tab_keys(page: Any, key: str) -> None:
    _load(page, guard=True)
    page.focus("#btn")
    page.keyboard.press(key)
    assert _handled(page) == []
