"""Layer 2 -- the normal test suite, run inside the image: Debian's exiftool, the Linux
wheels (Pillow, pillow-heif, rawpy, CPU torch), a non-root user, and tini as PID 1.

Twice: with pytest's temp folders on the container's own filesystem, and on a bind
mount (virtiofs on Docker Desktop for Mac, a plain bind mount on Linux), where the
library and the shared folder really live -- mtime precision, ``os.replace``, exiftool's
in-place writes and permission bits all go through it. The repo is mounted read-only;
the sample RAW / HEIC fixtures are mounted when this computer has them."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.docker import harness
from tests.docker.library import SAMPLES


@pytest.mark.parametrize("where", ["container", "bind-mount"])
def test_suite_passes_in_the_container(images: str, tmp_path: Path, where: str) -> None:
    uid, gid = harness.host_ids()
    basetemp = "/tmp/pytest" if where == "container" else "/work/pytest"
    cmd = [
        "docker", "run", "--rm", "--user", f"{uid}:{gid}",
        "-e", "HOME=/tmp", "-e", "HF_HUB_OFFLINE=1",
        "-v", f"{harness.REPO}:/repo:ro", "-v", f"{tmp_path}:/work", "-w", "/repo",
    ]  # fmt: skip
    if SAMPLES.is_dir():
        cmd += ["-v", f"{SAMPLES}:/tmp/Downloads/sampleImages:ro"]
    cmd += [
        harness.SUITE_IMAGE, "pytest", "tests", "-p", "no:cacheprovider", "-o", "addopts=",
        "-q", "-rfE", f"--basetemp={basetemp}",
    ]  # fmt: skip
    proc = harness.run(cmd, check=False, timeout=1800)
    (tmp_path / "pytest.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
    assert proc.returncode == 0, "in-container suite failed:\n" + "\n".join(
        (proc.stdout + proc.stderr).splitlines()[-60:]
    )
