"""Layer 1 -- the image and its runtime: tools, users, PID 1, healthcheck, and that the
entrypoint never touches anything but ``/data/local`` (constraint #1)."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from tests.docker import harness
from tests.docker.library import build_library

_TOOLS = r"""
import json, shutil, subprocess, torch, rawpy, pillow_heif
from sortandpost.binaries import exiftool_path
from sortandpost.library_roots import running_in_docker
print(json.dumps({
    "exiftool": subprocess.run([exiftool_path(), "-ver"], capture_output=True,
                               text=True).stdout.strip(),
    "exiftool_path": shutil.which(exiftool_path()),
    "torch": torch.__version__,
    "in_docker": running_in_docker(),
    "heif": pillow_heif.__version__,
    "libraw": rawpy.libraw_version,
}, default=str))
"""

# pid -> (uid, gid, argv) for every process in the container
_PROCS = r"""
import json, os
out = {}
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        argv = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
        status = open(f"/proc/{pid}/status").read().splitlines()
    except OSError:
        continue
    ids = {l.split(":")[0]: int(l.split()[1]) for l in status if l.startswith(("Uid", "Gid"))}
    out[pid] = [ids["Uid"], ids["Gid"], [a.decode() for a in argv if a]]
print(json.dumps(out))
"""


def _owners(root: Path) -> dict[str, tuple[int, int]]:
    return {str(p): (p.stat().st_uid, p.stat().st_gid) for p in root.rglob("*")}


def _processes(stack: harness.Stack) -> dict[str, list[object]]:
    out = harness.run(["docker", "exec", stack.container_id(), "python", "-c", _PROCS]).stdout
    procs: dict[str, list[object]] = json.loads(out)
    return procs


def test_compose_file_validates(make_stack: Callable[..., harness.Stack]) -> None:
    stack = make_stack()
    config = stack.compose("config", "--format", "json").stdout
    app = json.loads(config)["services"]["app"]
    published = app["ports"][0]
    assert published["host_ip"] == "127.0.0.1", "the port must be localhost-only by default"
    assert app["hostname"] == stack.device_name
    targets = {v["target"] for v in app["volumes"]}
    assert targets == {"/library", "/data/sync", "/data/local"}


def test_tools_in_the_image(images: str) -> None:
    out = harness.run(["docker", "run", "--rm", "--entrypoint", "python", images, "-c", _TOOLS])
    tools = json.loads(out.stdout)
    assert tools["in_docker"] is True
    assert tools["exiftool_path"] == "/usr/bin/exiftool"
    assert float(tools["exiftool"]) >= 12.0
    assert tools["torch"].endswith("+cpu"), "Linux torch must be the CPU build"


def test_default_stack_runs_and_leaves_the_library_alone(
    make_stack: Callable[..., harness.Stack],
) -> None:
    stack = make_stack()
    lib = build_library(stack.library)
    before = lib.snapshot()
    owners = _owners(stack.library) | _owners(stack.sync)
    stack.up()

    assert stack.health() == "healthy"
    assert httpx.get(stack.url + "/healthz").text.strip() == "ok"

    procs = _processes(stack)
    uid, gid = harness.host_ids()
    assert "tini" in procs["1"][2][0], "PID 1 must be the init (reaps zombies, forwards SIGTERM)"
    app = [p for p in procs.values() if p[2][:3] == ["python", "-m", "sortandpost"]]
    assert len(app) == 1, procs
    assert app[0][:2] == [uid, gid], "the app must run as PUID:PGID, not root"

    stack.wait_indexed(len(lib.files) - 1)  # the duplicate is one photo with two paths
    assert lib.snapshot() == before, "indexing changed a file under the library"
    after = _owners(stack.library) | _owners(stack.sync)
    assert {k: after[k] for k in owners} == owners, "the entrypoint re-owned a shared file"

    # A new container registers the /library mount, labelled with the host folder.
    devices = list((stack.sync / "devices").glob(f"{stack.device_name}-*/library.json"))
    assert len(devices) == 1
    roots = json.loads(devices[0].read_text())["roots"]
    assert [(r["container"], r["host"]) for r in roots] == [("/library", str(stack.library))]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Docker Desktop maps owners")
def test_custom_puid_owns_only_local(make_stack: Callable[..., harness.Stack]) -> None:
    stack = make_stack(puid=(4321, 4321))
    build_library(stack.library)
    for d in (stack.library, stack.sync):
        for p in [d, *d.rglob("*")]:
            if p.is_dir():
                p.chmod(0o777)
    owners = _owners(stack.library)
    stack.up()
    try:
        app = [p for p in _processes(stack).values() if p[2][:3] == ["python", "-m", "sortandpost"]]
        assert app[0][:2] == [4321, 4321]
        assert (stack.local / "device.json").stat().st_uid == 4321
        assert _owners(stack.library) == owners
        stack.wait_indexed(1)
    finally:
        stack.down()
        uid, gid = os.getuid(), os.getgid()  # hand the temp folders back for cleanup
        harness.run(
            ["docker", "run", "--rm", "--entrypoint", "chown", "-v", f"{stack.root}:/x",
             harness.IMAGE, "-R", f"{uid}:{gid}", "/x"]
        )  # fmt: skip
