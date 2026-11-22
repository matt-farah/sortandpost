"""Docker tests: opt-in (``pytest --docker tests/docker`` or ``SORTANDPOST_DOCKER_TESTS=1``).

Without the flag nothing under this folder is even imported, so a plain ``uv run pytest``
is unchanged. With it, the images are built once per session (layer cache makes a
rebuild after a code change take seconds), and each test gets throwaway compose stacks.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.docker import harness


def _enabled(config: pytest.Config) -> bool:
    return bool(config.getoption("--docker", default=False)) or (
        os.environ.get("SORTANDPOST_DOCKER_TESTS") == "1"
    )


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    if collection_path.suffix == ".py" and collection_path.name.startswith("test_"):
        return None if _enabled(config) else True
    return None


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    here = Path(__file__).parent
    for item in items:
        if here in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.docker)


@pytest.fixture(scope="session")
def images() -> str:
    why = harness.docker_available()
    if why is not None:
        pytest.skip(f"Docker isn't available: {why}")
    harness.build_images()
    return harness.IMAGE


@pytest.fixture
def make_stack(
    images: str, tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[Callable[..., harness.Stack]]:
    """``make_stack(name="a", **Stack kwargs)`` -> a configured (not started) stack.
    Every stack is torn down after the test; its log is kept beside the temp folders,
    and copied to ``$SORTANDPOST_DOCKER_LOGS`` (CI uploads it) when set."""
    stacks: list[harness.Stack] = []

    def _make(name: str = "a", **kwargs: Any) -> harness.Stack:
        stack = harness.Stack(tmp_path / name, **kwargs)
        stacks.append(stack)
        return stack

    yield _make
    keep = os.environ.get("SORTANDPOST_DOCKER_LOGS")
    for stack in stacks:
        log = stack.logs() if stack.container_id_or_none() else ""
        (stack.root / "container.log").write_text(log, encoding="utf-8")
        if keep and log:
            dest = Path(keep) / f"{request.node.name}-{stack.root.name}.log"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(stack.root / "container.log", dest)
        stack.down()
