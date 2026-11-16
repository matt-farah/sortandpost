"""Drag to reorder a post's photos (V3.1 #3).

``posts.reorder`` is the server-side check on what a drag sends back; the
vendored SortableJS is pinned by hash (CONTRIBUTING.md "Vendored JS"); and the
browser bridge ``posts_ui._SORT_JS`` runs for real in headless Chromium (skipped
when Playwright / Chromium aren't installed, like test_grid_selection): a drop
must report the new order **and** leave the DOM as Vue drew it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from importlib import resources
from typing import Any

import pytest

from sortandpost.posts import reorder
from sortandpost.posts_ui import _SORT_JS, _SORTABLE_FILE

# SortableJS 1.15.6 from the npm tarball (integrity sha512 checked against the
# registry when it was vendored). Updating it = replace the file + this hash.
SORTABLE_SHA256 = "6d0a831fc19b4bae851797ad3393157e861afb7862459c11226359b27e2c4337"


def _js_dir() -> Any:
    return resources.files("sortandpost.resources").joinpath("js")


# -- the server-side check ------------------------------------------------- #


def test_reorder_accepts_a_permutation() -> None:
    assert reorder(["a", "b", "c"], ["c", "a", "b"]) == ["c", "a", "b"]
    assert reorder(["a", "b"], ["a", "b"]) == ["a", "b"]
    assert reorder([], []) == []


@pytest.mark.parametrize(
    "proposed",
    [
        ["a", "b"],  # dropped a photo
        ["a", "b", "c", "d"],  # added one
        ["a", "a", "b"],  # duplicated one
        ["a", "b", "x"],  # swapped one in
        ["a", "b", 3],  # not a hash
        "abc",  # not a list
        None,
        {"0": "a"},
    ],
)
def test_reorder_refuses_anything_but_a_rearrangement(proposed: object) -> None:
    assert reorder(["a", "b", "c"], proposed) is None


# -- the vendored file ----------------------------------------------------- #


def test_vendored_sortable_is_pinned() -> None:
    data = _js_dir().joinpath(_SORTABLE_FILE).read_bytes()
    assert hashlib.sha256(data).hexdigest() == SORTABLE_SHA256
    assert _js_dir().joinpath("Sortable.LICENSE").read_text().startswith("MIT License")


# -- the browser bridge ---------------------------------------------------- #

_PAGE = """
<style>
  #tiles { display: flex; gap: 8px; }
  #tiles > div { width: 80px; height: 80px; background: #ccc; }
</style>
<div id="tiles" data-sp-sortable>
  <div data-hash="a">a</div><div data-hash="b">b</div><div data-hash="c">c</div>
</div>
<script>
  window.orders = [];
  document.getElementById('tiles')
    .addEventListener('sp-reorder', (e) => window.orders.push(e.detail));
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
    pg.add_script_tag(content=_js_dir().joinpath(_SORTABLE_FILE).read_text())
    bridge = _SORT_JS.strip().removeprefix("<script>").removesuffix("</script>")
    pg.add_script_tag(content=bridge)
    pg.add_script_tag(content=bridge)  # a second install is a no-op
    yield pg
    pg.close()


def _drag(page: Any, src: str, dst: str) -> None:
    """Press on ``src``, move in small steps to just past ``dst``'s centre, release."""
    a = page.locator(f'[data-hash="{src}"]').bounding_box()
    b = page.locator(f'[data-hash="{dst}"]').bounding_box()
    x0, y = a["x"] + 40, a["y"] + 40
    x1 = b["x"] + 40 + (30 if b["x"] > a["x"] else -30)
    page.mouse.move(x0, y)
    page.mouse.down()
    for step in range(1, 21):
        page.mouse.move(x0 + (x1 - x0) * step / 20, y)
        page.wait_for_timeout(15)
    page.mouse.up()
    page.wait_for_timeout(250)  # past Sortable's animation


def _dom(page: Any) -> list[str]:
    order: list[str] = page.eval_on_selector_all(
        "#tiles > div", "els => els.map(e => e.dataset.hash)"
    )
    return order


def test_drop_reports_order_and_restores_the_dom(page: Any) -> None:
    _drag(page, "a", "c")
    assert page.evaluate("window.orders") == [["b", "c", "a"]]
    assert _dom(page) == ["a", "b", "c"]  # Vue's DOM untouched; Python re-renders


def test_drag_backwards(page: Any) -> None:
    _drag(page, "c", "a")
    assert page.evaluate("window.orders") == [["c", "a", "b"]]
    assert _dom(page) == ["a", "b", "c"]


def test_a_click_reports_nothing(page: Any) -> None:
    page.locator('[data-hash="b"]').click()
    page.wait_for_timeout(250)
    assert page.evaluate("window.orders") == []


# -- link tiles (the collection page, V3.1 #8) ------------------------------ #

_LINK_PAGE = """
<style>
  #tiles { display: flex; gap: 8px; }
  #tiles > div { width: 80px; height: 80px; background: #ccc; position: relative; }
  #tiles a { display: block; width: 80px; height: 80px; }
  .box { position: absolute; left: 4px; top: 4px; width: 20px; height: 20px; background: #333; }
</style>
<div id="tiles" data-sp-sortable>
  <div data-hash="a"><a href="#a" draggable="false">a</a>
    <div class="box" data-sp-nodrag></div></div>
  <div data-hash="b"><a href="#b" draggable="false">b</a>
    <div class="box" data-sp-nodrag></div></div>
  <div data-hash="c"><a href="#c" draggable="false">c</a>
    <div class="box" data-sp-nodrag></div></div>
</div>
<script>
  window.orders = [];
  window.clicks = [];
  const tiles = document.getElementById('tiles');
  tiles.addEventListener('sp-reorder', (e) => window.orders.push(e.detail));
  tiles.querySelectorAll('a').forEach((a) =>
    a.addEventListener('click', () => window.clicks.push(a.textContent)));
</script>
"""


@pytest.fixture
def link_page(browser: Any) -> Iterator[Any]:
    pg = browser.new_page()
    pg.set_content(_LINK_PAGE)
    pg.add_script_tag(content=_js_dir().joinpath(_SORTABLE_FILE).read_text())
    pg.add_script_tag(content=_SORT_JS.strip().removeprefix("<script>").removesuffix("</script>"))
    yield pg
    pg.close()


def test_drag_over_link_tiles_does_not_follow_the_link(link_page: Any) -> None:
    _drag(link_page, "a", "c")
    assert link_page.evaluate("window.orders") == [["b", "c", "a"]]
    assert _dom(link_page) == ["a", "b", "c"]
    assert link_page.evaluate("location.hash") == ""
    assert link_page.evaluate("window.clicks") == []


def test_a_plain_click_on_a_link_tile_still_works(link_page: Any) -> None:
    _drag(link_page, "a", "c")
    link_page.wait_for_timeout(350)  # past the post-drop click guard
    link_page.locator('[data-hash="b"] a').click()
    assert link_page.evaluate("window.clicks") == ["b"]
    assert link_page.evaluate("location.hash") == "#b"


def test_a_drag_from_the_selection_box_moves_nothing(link_page: Any) -> None:
    box = link_page.locator('[data-hash="a"] .box').bounding_box()
    c = link_page.locator('[data-hash="c"]').bounding_box()
    x0, y = box["x"] + 10, box["y"] + 10
    link_page.mouse.move(x0, y)
    link_page.mouse.down()
    for step in range(1, 21):
        link_page.mouse.move(x0 + (c["x"] + 70 - x0) * step / 20, y)
        link_page.wait_for_timeout(15)
    link_page.mouse.up()
    link_page.wait_for_timeout(250)
    assert link_page.evaluate("window.orders") == []
