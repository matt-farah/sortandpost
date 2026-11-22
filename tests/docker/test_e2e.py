"""Layer 3 -- the app in a browser (headless Chromium), against the container, checking
the result where it lands: the host's photo files (read with the host's exiftool), the
container's ``local/`` and the shared folder."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.docker import harness
from tests.docker.library import add_photo, build_library

needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None, reason="no host exiftool")


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # Chromium not downloaded
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


@pytest.fixture
def page(browser: Any) -> Iterator[Any]:
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.set_default_timeout(20_000)
    yield pg
    ctx.close()


def _meta(path: Path) -> dict[str, Any]:
    out = subprocess.run(
        ["exiftool", "-j", "-XMP-xmp:Rating", "-XMP-dc:Subject", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    data: dict[str, Any] = json.loads(out)[0]
    return data


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ready_stack(make_stack: Callable[..., harness.Stack]) -> tuple[harness.Stack, Any]:
    stack = make_stack()
    lib = build_library(stack.library)
    stack.up()
    stack.wait_indexed(len(lib.files) - 1)
    return stack, lib


def test_wizard_on_a_fresh_container(make_stack: Callable[..., harness.Stack], page: Any) -> None:
    stack = make_stack(ready=False)
    lib = build_library(stack.library)
    stack.up()
    page.goto(stack.url + "/")
    page.wait_for_url("**/welcome**")
    assert stack.photo_count() == 0, "the indexer ran before the wizard finished"

    start = page.get_by_role("button", name="Start using sortandpost.photos")
    for _ in range(15):
        if start.is_visible():
            break
        page.get_by_role("button", name="Next").locator("visible=true").last.click()
        page.wait_for_timeout(500)
    start.click()
    harness.wait_for(lambda: (stack.local / "onboarding.json").exists(), what="wizard marker")
    stack.wait_indexed(len(lib.files) - 1)
    page.goto(stack.url + "/")
    page.locator("img[src*='/thumbs/']").first.wait_for()


@needs_exiftool
def test_rating_and_tag_land_in_the_host_file(
    make_stack: Callable[..., harness.Stack], page: Any
) -> None:
    stack, lib = _ready_stack(make_stack)
    beach = lib["forest"]
    page.goto(f"{stack.url}/photo/{stack.hash_for(beach)}")
    page.get_by_placeholder("Add a tag…").wait_for()
    page.locator("body").click(position={"x": 5, "y": 5})
    page.keyboard.press("4")
    harness.wait_for(lambda: _meta(beach).get("Rating") == 4, what="XMP:Rating=4 in the file")
    stars = stack.scalar(f"SELECT stars FROM ratings WHERE pixel_hash='{stack.hash_for(beach)}'")
    assert stars == 4

    box = page.get_by_placeholder("Add a tag…")
    box.fill("seaside")
    box.press("Enter")
    harness.wait_for(
        lambda: "seaside" in str(_meta(beach).get("Subject", "")), what="XMP:Subject has the tag"
    )

    harness.wait_for(
        lambda: stack.scalar(
            "SELECT COUNT(*) FROM changes WHERE op='rate'", "/data/sync/sortandpost.db"
        ),
        timeout=30,
        what="the rating published to sync/",
    )


@needs_exiftool
def test_raw_is_never_written(make_stack: Callable[..., harness.Stack], page: Any) -> None:
    stack, lib = _ready_stack(make_stack)
    if "raw" not in lib.files:
        pytest.skip("no sample RAW on this computer")
    raw = lib["raw"]
    before = (_sha(raw), raw.stat().st_mtime_ns)
    siblings = sorted(p.name for p in raw.parent.iterdir())
    page.goto(f"{stack.url}/photo/{stack.hash_for(raw)}")
    page.get_by_role("button", name="Delete photo").wait_for()
    page.locator("body").click(position={"x": 5, "y": 5})
    page.keyboard.press("3")
    ph = stack.hash_for(raw)
    harness.wait_for(
        lambda: stack.scalar(f"SELECT stars FROM ratings WHERE pixel_hash='{ph}'") == 3,
        what="the RAW rating in the DB",
    )
    assert (_sha(raw), raw.stat().st_mtime_ns) == before
    assert sorted(p.name for p in raw.parent.iterdir()) == siblings, "a sidecar appeared"


def test_delete_then_put_back(make_stack: Callable[..., harness.Stack], page: Any) -> None:
    stack, lib = _ready_stack(make_stack)
    city = lib["city"]
    digest = _sha(city)
    page.goto(f"{stack.url}/photo/{stack.hash_for(city)}")
    page.get_by_role("button", name="Delete photo").click()
    page.get_by_role("button", name="Delete", exact=True).click()
    harness.wait_for(lambda: not city.exists(), what="the file to leave the library")
    trashed = list((stack.local / "trash").glob("*/files/*/city.jpg"))
    assert len(trashed) == 1 and _sha(trashed[0]) == digest
    assert stack.scalar("SELECT COUNT(*) FROM tombstones") == 1

    page.goto(stack.url + "/settings")
    page.get_by_role("button", name="Put back").first.click()
    # local/ and /library are separate mounts, so shutil.move copies then deletes: the
    # file appears before all its bytes are there (DockerTestFindings.md F11).
    harness.wait_for(
        lambda: city.exists() and _sha(city) == digest,
        what="the file back at its original path, all of it",
    )
    assert stack.scalar("SELECT COUNT(*) FROM tombstones") == 0


def test_a_new_file_is_picked_up_by_the_watcher(
    make_stack: Callable[..., harness.Stack],
) -> None:
    stack, _lib = _ready_stack(make_stack)
    n = stack.photo_count()
    add_photo(stack.library, "2025/new.jpg", 99)
    # Well under the 15-minute full scan: this is the watchdog event through the mount.
    harness.wait_for(lambda: stack.photo_count() == n + 1, timeout=60, what="the new photo")


def test_docker_only_ui(make_stack: Callable[..., harness.Stack], page: Any) -> None:
    stack, lib = _ready_stack(make_stack)
    page.goto(f"{stack.url}/photo/{stack.hash_for(lib['beach'])}")
    page.get_by_role("button", name="Delete photo").wait_for()
    body = page.content()
    assert "Reveal in file manager" not in body, "a container can't open Finder / Explorer"
    assert "content_copy" in body, "the copy-path button replaces Reveal under Docker"

    page.goto(stack.url + "/settings")
    page.get_by_text("Sync & storage").first.wait_for()
    assert page.get_by_text("Open with", exact=True).count() == 0
    assert (
        stack.python(
            "from sortandpost.update.source import install_source; print(install_source())"
        ).strip()
        == "docker"
    )
