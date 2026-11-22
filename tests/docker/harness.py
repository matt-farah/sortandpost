"""Drive the real image through the real ``docker-compose.yml`` for the Docker tests.

A :class:`Stack` is one compose project (one container = one "machine") on temporary host
folders: ``library/`` (``PHOTO_LIBRARY``), ``sync/`` (``SYNC_DIR``) and ``local/``
(``LOCAL_DIR``, a host folder instead of the named volume so a test can look inside).
``tests/docker/compose.test.yml`` is layered on top. Everything compose reads comes from
the process environment, never from a ``.env`` (the repo's own ``.env`` may name a real
library: the project directory is the stack's temp folder).
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE_FILES = (REPO / "docker-compose.yml", Path(__file__).with_name("compose.test.yml"))
IMAGE = os.environ.get("SORTANDPOST_DOCKER_IMAGE", "sortandpost:test")
SUITE_IMAGE = IMAGE + "-suite"

# Seeded into sync/config.yaml: CLIP off (no weights in the tests) and short sync timings
# so publish / heartbeat / handoff happen within seconds.
TEST_CONFIG: dict[str, Any] = {
    "library_roots": ["/library"],
    "sync": {
        "publish_interval_sec": 3,
        "lock_heartbeat_sec": 2,
        "lock_stale_after_sec": 30,
        "handoff_grace_sec": 6,
    },
    "clip": {"enabled": False},
    "indexer": {"debounce_sec": 0.5, "workers": 2},
}


def run(
    args: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
    timeout: float = 600,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        list(args),
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"`{' '.join(args)}` exited {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout[-4000:]}\n--- stderr ---\n{proc.stderr[-4000:]}"
        )
    return proc


def docker_available() -> str | None:
    """``None`` when the daemon answers, else why not."""
    try:
        proc = run(["docker", "info", "--format", "{{.ServerVersion}}"], check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    return None if proc.returncode == 0 else proc.stderr.strip() or "docker info failed"


def build_images() -> None:
    """The app image, then the test-suite image on top of it. Skipped when CI built
    them already (``SORTANDPOST_DOCKER_SKIP_BUILD=1``)."""
    if os.environ.get("SORTANDPOST_DOCKER_SKIP_BUILD") == "1":
        return
    run(["docker", "build", "-t", IMAGE, str(REPO)], timeout=3600)
    run(
        [
            "docker", "build", "-f", str(REPO / "tests/docker/Dockerfile.test"),
            "--build-arg", f"BASE={IMAGE}", "-t", SUITE_IMAGE, str(REPO),
        ],
        timeout=1800,
    )  # fmt: skip


def host_ids() -> tuple[int, int]:
    """The uid / gid the container should run as. On Linux, the test's own user, so it
    can write the temp folders (they're 0700) -- exactly what docs/docker.md tells a NAS
    owner to do. Docker Desktop maps ownership itself, so the image default works."""
    if sys.platform.startswith("linux"):
        return os.getuid(), os.getgid()
    return 1000, 1000


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_for[T](
    probe: Callable[[], T | None], *, timeout: float = 60, interval: float = 0.5, what: str = ""
) -> T:
    """Poll *probe* until it returns something truthy; return that."""
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = probe()
        except Exception as exc:  # the thing isn't there yet
            last = exc
            value = None
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what or probe}: {last}")


class Stack:
    """One compose project = one sortandpost "machine" in a container."""

    def __init__(
        self,
        root: Path,
        *,
        sync: Path | None = None,
        library: Path | None = None,
        device_name: str = "sp-docker-test",
        ready: bool = True,
        config: dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        puid: tuple[int, int] | None = None,
    ) -> None:
        self.root = root
        self.project = f"sptest-{secrets.token_hex(4)}"
        self.library = library or root / "library"
        self.sync = sync or root / "sync"
        self.local = root / "local"
        for d in (self.library, self.sync, self.local):
            d.mkdir(parents=True, exist_ok=True)
        self.port = free_port()
        self.device_name = device_name
        uid, gid = puid or host_ids()
        self.env: dict[str, str] = {
            "PHOTO_LIBRARY": str(self.library),
            "SYNC_DIR": str(self.sync),
            "LOCAL_DIR": str(self.local),
            "DEVICE_NAME": device_name,
            "PORT": str(self.port),
            "PUID": str(uid),
            "PGID": str(gid),
            "SP_TEST_IMAGE": IMAGE,
            **(env or {}),
        }
        config_file = self.sync / "config.yaml"
        if not config_file.exists():  # a second machine joins the first one's config
            merged = json.loads(json.dumps(TEST_CONFIG))
            for key, value in (config or {}).items():
                if isinstance(value, dict):
                    merged.setdefault(key, {}).update(value)
                else:
                    merged[key] = value
            config_file.write_text(yaml.safe_dump(merged), encoding="utf-8")
        if ready:  # skip the welcome wizard (test_e2e.py runs it for real)
            (self.local / "onboarding.json").write_text(
                json.dumps({"completed_at": "2026-01-01T00:00:00+00:00", "how": "new"}),
                encoding="utf-8",
            )

    # -- compose ------------------------------------------------------------ #
    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}"

    def _proc_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("SORTANDPOST_")}
        for key in ("APP_DATA", "PORT", "BIND", "COMPOSE_FILE", "COMPOSE_PROJECT_NAME"):
            env.pop(key, None)
        env.update(self.env)
        return env

    def compose(
        self, *args: str, check: bool = True, timeout: float = 300
    ) -> subprocess.CompletedProcess[str]:
        cmd = ["docker", "compose", "-p", self.project, "--project-directory", str(self.root)]
        for f in COMPOSE_FILES:
            cmd += ["-f", str(f)]
        return run([*cmd, *args], env=self._proc_env(), check=check, timeout=timeout)

    def up(self, *, wait: bool = True) -> Stack:
        args = ["up", "-d", "--no-build"]
        if wait:
            args += ["--wait", "--wait-timeout", "180"]
        proc = self.compose(*args, "app", check=False, timeout=240)
        if proc.returncode != 0:
            raise AssertionError(f"compose up failed:\n{proc.stderr[-3000:]}\n{self.logs()}")
        return self

    def logs(self) -> str:
        return self.compose("logs", "--no-color", "app", check=False).stdout

    def stop(self, timeout: int = 60) -> float:
        """``docker compose stop`` (SIGTERM, then SIGKILL after *timeout*); seconds taken."""
        t0 = time.monotonic()
        self.compose("stop", "-t", str(timeout), "app", timeout=timeout + 60)
        return time.monotonic() - t0

    def start(self) -> Stack:
        self.compose("start", "app")
        wait_for(lambda: self.health() == "healthy", timeout=180, what="healthy")
        return self

    def down(self) -> None:
        self.compose("down", "--remove-orphans", "--timeout", "30", check=False, timeout=180)

    def container_id(self) -> str:
        cid = self.container_id_or_none()
        assert cid, f"{self.project} has no app container"
        return cid

    def container_id_or_none(self) -> str | None:
        return self.compose("ps", "-a", "-q", "app", check=False).stdout.strip() or None

    def inspect(self) -> dict[str, Any]:
        out = run(["docker", "inspect", self.container_id()]).stdout
        data: list[dict[str, Any]] = json.loads(out)
        return data[0]

    def health(self) -> str:
        state = self.inspect()["State"]
        return str(state.get("Health", {}).get("Status") or state.get("Status"))

    def exec(
        self, *cmd: str, user: str | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        args = ["exec", "-T"]
        if user is not None:
            args += ["--user", user]
        return self.compose(*args, "app", *cmd, check=check)

    def python(self, code: str, *args: str) -> str:
        """Run Python in the container (as the app's user); return stdout."""
        uid, gid = self.env["PUID"], self.env["PGID"]
        return self.exec("python", "-c", code, *args, user=f"{uid}:{gid}").stdout

    # -- the app's state ------------------------------------------------------ #
    def query(self, sql: str, db: str = "/data/local/sortandpost.db") -> list[list[Any]]:
        """Rows from a DB, read inside the container (no cross-VM SQLite locking)."""
        code = (
            "import json, sqlite3, sys\n"
            "con = sqlite3.connect(f'file:{sys.argv[1]}?mode=ro', uri=True, timeout=10)\n"
            "print(json.dumps(con.execute(sys.argv[2]).fetchall()))"
        )
        rows: list[list[Any]] = json.loads(self.python(code, db, sql))
        return rows

    def scalar(self, sql: str, db: str = "/data/local/sortandpost.db") -> Any:
        rows = self.query(sql, db)
        return rows[0][0] if rows else None

    def lock(self) -> dict[str, Any] | None:
        path = self.sync / ".sortandpost.lock"
        try:
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data

    def device_id(self) -> str:
        data: dict[str, str] = json.loads((self.local / "device.json").read_text())
        return data["device_id"]

    def published_generation(self) -> int:
        """``meta.db_generation`` of ``sync/sortandpost.db`` (0 before the first publish)."""
        if not (self.sync / "sortandpost.db").exists():
            return 0
        value = self.scalar(
            "SELECT value FROM meta WHERE key = 'db_generation'", "/data/sync/sortandpost.db"
        )
        return int(value or 0)

    def photo_count(self) -> int:
        return int(self.scalar("SELECT COUNT(*) FROM photos") or 0)

    def wait_indexed(self, n: int, *, timeout: float = 180) -> None:
        wait_for(lambda: self.photo_count() >= n, timeout=timeout, what=f"{n} photos indexed")

    def hash_for(self, path: Path) -> str:
        """The pixel_hash the container recorded for a host file under the library."""
        container_path = "/library/" + path.relative_to(self.library).as_posix()
        value = self.scalar(f"SELECT pixel_hash FROM photo_paths WHERE path = '{container_path}'")
        assert value, f"{container_path} isn't indexed"
        return str(value)
