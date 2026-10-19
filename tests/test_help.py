"""The /help keyboard-shortcut page: its data, and the global ``?`` key.

The key test runs the real ``HELP_KEY_JS`` in headless Chromium (skipped when
Playwright / Chromium aren't installed), like test_key_focus_guard.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from sortandpost.help_ui import HELP_KEY_JS, PAGE_URL, SECTIONS


def test_sections_are_filled_in() -> None:
    titles = [s.title for s in SECTIONS]
    assert len(titles) == len(set(titles))
    for section in SECTIONS:
        assert section.shortcuts, section.title
        for shortcut in section.shortcuts:
            assert shortcut.keys and all(k.strip() for k in shortcut.keys)
            assert shortcut.does.strip()


def test_help_key_is_listed() -> None:
    everywhere = SECTIONS[0]
    assert any("?" in s.keys for s in everywhere.shortcuts)


@pytest.fixture(scope="module")
def page() -> Iterator[Any]:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # Chromium not downloaded
            pytest.skip(f"chromium unavailable: {exc}")
        pg = browser.new_page()
        yield pg
        browser.close()


_ORIGIN = "http://sp.test"


def _load(page: Any) -> None:
    """Serve a stand-in page (and /help) from a fake origin, so the key's real
    navigation can be watched."""
    body = '<input id="search"><button id="btn">Day</button>' + HELP_KEY_JS

    def _serve(route: Any) -> None:
        html = "help" if route.request.url.endswith(PAGE_URL) else body
        route.fulfill(status=200, content_type="text/html", body=html)

    page.unroute(f"{_ORIGIN}/**")
    page.route(f"{_ORIGIN}/**", _serve)
    page.goto(f"{_ORIGIN}/")


def _went_to_help(page: Any) -> bool:
    page.wait_for_timeout(200)
    return bool(page.url == _ORIGIN + PAGE_URL)


def test_question_mark_opens_help(page: Any) -> None:
    _load(page)
    page.keyboard.press("Shift+Slash")
    assert _went_to_help(page)


def test_question_mark_while_typing_is_text(page: Any) -> None:
    _load(page)
    page.focus("#search")
    page.keyboard.press("Shift+Slash")
    assert not _went_to_help(page)
    assert page.input_value("#search") == "?"


def test_question_mark_on_a_button_still_opens_help(page: Any) -> None:
    _load(page)
    page.focus("#btn")
    page.keyboard.press("Shift+Slash")
    assert _went_to_help(page)
