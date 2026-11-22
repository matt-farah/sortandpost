"""Layer 3 -- container lifecycle: restart, `docker stop` (SIGTERM through tini), a new
container over the same data (what an image upgrade does), the one-shot indexer service,
and a library mount that comes up empty."""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable

from tests.docker import harness
from tests.docker.library import build_library


def test_stop_is_a_clean_shutdown(make_stack: Callable[..., harness.Stack]) -> None:
    stack = make_stack()
    lib = build_library(stack.library)
    stack.up()
    stack.wait_indexed(len(lib.files) - 1)
    harness.wait_for(lambda: stack.published_generation() > 0, what="a first publish")

    took = stack.stop(timeout=60)
    state = stack.inspect()["State"]
    assert took < 45, f"docker stop took {took:.0f}s (SIGTERM ignored? killed?)"
    # 143 = 128 + SIGTERM: uvicorn re-raises the signal after its graceful shutdown.
    assert state["ExitCode"] in (0, 143), stack.logs()[-3000:]
    assert not state["OOMKilled"]
    assert "shutdown complete" in stack.logs()

    lock = stack.lock()
    assert lock is not None and lock.get("handoff_to") == "", f"lock not released: {lock}"
    assert lock["db_generation"] == stack_generation_on_disk(stack)


def stack_generation_on_disk(stack: harness.Stack) -> int:
    import sqlite3

    con = sqlite3.connect(f"file:{stack.sync / 'sortandpost.db'}?mode=ro", uri=True)
    try:
        return int(con.execute("SELECT value FROM meta WHERE key='db_generation'").fetchone()[0])
    finally:
        con.close()


def test_restart_and_recreate_keep_everything(make_stack: Callable[..., harness.Stack]) -> None:
    stack = make_stack()
    lib = build_library(stack.library)
    stack.up()
    stack.wait_indexed(len(lib.files) - 1)
    device = stack.device_id()
    photos = stack.photo_count()

    stack.compose("restart", "app")
    harness.wait_for(lambda: stack.health() == "healthy", timeout=180, what="healthy")
    assert stack.device_id() == device
    assert stack.photo_count() == photos
    assert (stack.local / "onboarding.json").exists()

    # `down` + `up` = a brand-new container on the same folders (an image upgrade).
    stack.down()
    stack.up()
    assert stack.device_id() == device
    assert stack.photo_count() == photos
    assert len(list((stack.sync / "devices").glob("*/library.json"))) == 1, (
        "a recreated container must reuse its device folder"
    )


def test_one_shot_indexer_refuses_while_the_app_edits(
    make_stack: Callable[..., harness.Stack],
) -> None:
    """Constraint #9: only one writer. `docker compose run --rm indexer --once` while
    the app holds the lock must not write the same working DB."""
    stack = make_stack()
    build_library(stack.library)
    stack.up()
    stack.wait_indexed(1)
    proc = stack.compose("run", "--rm", "--no-deps", "indexer", "--once", check=False)
    assert proc.returncode != 0, f"indexer --once ran beside the app:\n{proc.stdout}"
    assert "lock" in (proc.stdout + proc.stderr).lower()


def test_one_shot_indexer_on_a_stopped_stack(make_stack: Callable[..., harness.Stack]) -> None:
    stack = make_stack()
    lib = build_library(stack.library)
    proc = stack.compose("run", "--rm", "--no-deps", "indexer", "--once", timeout=600)
    assert proc.returncode == 0, proc.stderr
    stack.up()  # the app picks up what the one-shot pass indexed
    assert stack.photo_count() == len(lib.files) - 1
    lock = stack.lock()
    assert lock is not None and lock["hostname"] == stack.device_name


def test_empty_library_mount_marks_missing_never_purges(
    make_stack: Callable[..., harness.Stack],
) -> None:
    """Constraint #8: a library that comes up empty (an unmounted NAS share behind the
    bind mount) marks photos missing; nothing is purged, and they come back."""
    stack = make_stack()
    lib = build_library(stack.library)
    n = len(lib.files) - 1
    stack.up()
    stack.wait_indexed(n)
    stack.stop()

    away = stack.root / "away"
    shutil.move(stack.library, away)
    stack.library.mkdir()
    stack.start()
    # Give it a few scan passes; an empty root may be judged offline or the files absent --
    # either is fine, as long as no photo row goes.
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        assert stack.photo_count() == n, "photos were purged"
        time.sleep(2)

    stack.stop()
    stack.library.rmdir()
    shutil.move(away, stack.library)
    stack.start()
    harness.wait_for(
        lambda: stack.scalar("SELECT COUNT(*) FROM photos WHERE missing_since IS NULL") == n,
        timeout=180,
        what="photos present again",
    )
