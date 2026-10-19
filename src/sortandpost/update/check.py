"""The update check, the download, and what this machine remembers about them.

Everything lives in ``<default_local_root>/updates/`` (per machine, never
synced, shared by every library on it):

* ``state.json`` -- ``{check, last_check_at, last_attempt_at, last_error,
  manifest, signature, skipped}``; the manifest is kept as its signed bytes and
  re-verified on every load.
* ``<version>/<file>`` -- a verified download.
* ``pending.json`` -- ``{path, version, sha256}``: the server's note to the tray
  that a verified update is ready to install (:mod:`sortandpost.update.apply`).
* ``result.txt`` -- the installer helper's outcome (``ok <version>`` /
  ``failed <reason>``), read once by the next boot.

The check is the app's one call to the internet that the user didn't ask for
(docs/architecture.md → "Updates"): one GET of a static file on github.com a
day, no identifiers sent, off with a Settings switch or
``SORTANDPOST_NO_UPDATE_CHECK=1``. It never runs on the event loop
(constraint #7).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import shutil
import sys
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from sortandpost import __version__
from sortandpost.paths import default_local_root
from sortandpost.update import pubkey
from sortandpost.update.manifest import (
    MANIFEST_NAME,
    SIGNATURE_NAME,
    Asset,
    Manifest,
    ManifestError,
    is_newer,
    load_verified,
    platform_key,
)

log = logging.getLogger(__name__)

REPO = "matt-farah/sortandpost"
RELEASES_URL = f"https://github.com/{REPO}/releases"
# ``releases/latest`` never points at a draft or a pre-release.
DEFAULT_BASE_URL = f"{RELEASES_URL}/latest/download/"
# Where github.com sends a release-asset download.
_GITHUB_HOSTS = frozenset(
    {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
)
BASE_URL_ENV = "SORTANDPOST_UPDATE_URL"  # testing: a folder serving latest.json(.sig) + assets
PUBKEY_ENV = "SORTANDPOST_UPDATE_PUBKEY"  # testing, source runs only: trust this key instead
DISABLE_ENV = "SORTANDPOST_NO_UPDATE_CHECK"

CHECK_EVERY = timedelta(hours=24)
RETRY_AFTER_FAILURE = timedelta(hours=1)
FIRST_CHECK_DELAY_SEC = 120.0
_LOOP_SEC = 600.0
_MANIFEST_MAX = 1 << 20  # 1 MB: a manifest is a few KB
_TIMEOUT = httpx.Timeout(15.0, read=60.0)


def updates_dir() -> Path:
    return default_local_root() / "updates"


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# where to look, and whom to trust
# --------------------------------------------------------------------------- #
def base_url() -> str:
    url = os.environ.get(BASE_URL_ENV, "").strip()
    if not url:
        return DEFAULT_BASE_URL
    return url if url.endswith("/") else url + "/"


def trusted_keys() -> tuple[str, ...]:
    """The embedded keys -- or, in a source run only, a test key from the
    environment (a frozen build always trusts just its own keys)."""
    override = os.environ.get(PUBKEY_ENV, "").strip()
    if override and not getattr(sys, "frozen", False):
        return (override,)
    return pubkey.PUBLIC_KEYS


def allowed_url(url: str, base: str | None = None) -> bool:
    """https to GitHub's release hosts, or to the overridden base's own host
    (plain http only for a loopback test server)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" and host in _GITHUB_HOSTS:
        return True
    b = urlsplit(base or base_url())
    if b.hostname and host == b.hostname.lower() and parts.scheme == b.scheme:
        return parts.scheme == "https" or host in ("127.0.0.1", "localhost", "::1")
    return False


def make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """The one HTTP client for updates: GitHub's hosts only (redirects too)."""
    base = base_url()

    def _guard(request: httpx.Request) -> None:
        # Runs for every request, redirects included.
        if not allowed_url(str(request.url), base):
            raise httpx.UnsupportedProtocol(f"refusing to fetch an update from {request.url.host}")

    return httpx.Client(
        timeout=_TIMEOUT,
        follow_redirects=True,
        event_hooks={"request": [_guard]},
        headers={"User-Agent": f"sortandpost/{__version__}"},
        transport=transport,
    )


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #
@dataclass
class UpdateState:
    check: bool = True
    last_check_at: str = ""  # last successful check (ISO, UTC)
    last_attempt_at: str = ""
    last_error: str = ""
    manifest: str = ""  # base64 of the signed latest.json bytes
    signature: str = ""
    skipped: str = ""  # "Skip this version"

    @classmethod
    def load(cls, path: Path) -> UpdateState:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        st = cls()
        for key, default in asdict(st).items():
            value = raw.get(key, default)
            if isinstance(value, type(default)):
                setattr(st, key, value)
        return st

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, path)


def _parse_ts(text: str) -> datetime | None:
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- #
# fetch + download
# --------------------------------------------------------------------------- #
def _get_small(client: httpx.Client, url: str) -> bytes:
    with client.stream("GET", url) as resp:
        resp.raise_for_status()
        out = bytearray()
        for chunk in resp.iter_bytes():
            out += chunk
            if len(out) > _MANIFEST_MAX:
                raise ManifestError("the update manifest is too large")
        return bytes(out)


def fetch_latest(client: httpx.Client | None = None) -> tuple[Manifest, bytes, bytes]:
    """Download and verify ``latest.json``; ``(manifest, bytes, signature)``.
    Raises :class:`ManifestError` / ``httpx.HTTPError``."""
    own = client is None
    c = client or make_client()
    try:
        base = base_url()
        data = _get_small(c, base + MANIFEST_NAME)
        sig = _get_small(c, base + SIGNATURE_NAME)
    finally:
        if own:
            c.close()
    return load_verified(data, sig, trusted_keys()), data, sig


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download_asset(
    asset: Asset,
    dest_dir: Path,
    *,
    client: httpx.Client | None = None,
    progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Path:
    """Stream *asset* into *dest_dir*, checking its size and sha256 as it
    goes; the file appears under its real name only once it's verified."""
    name = Path(urlsplit(asset.url).path).name or "update"
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / name
    if (
        final.is_file()
        and final.stat().st_size == asset.size
        and sha256_file(final) == asset.sha256
    ):
        return final  # already downloaded (an earlier try that wasn't installed)
    part = dest_dir / (name + ".part")
    own = client is None
    c = client or make_client()
    h = hashlib.sha256()
    done = 0
    try:
        with c.stream("GET", asset.url) as resp, part.open("wb") as f:
            resp.raise_for_status()
            for chunk in resp.iter_bytes(1 << 16):
                if cancelled is not None and cancelled():
                    raise InterruptedError("download cancelled")
                done += len(chunk)
                if done > asset.size:
                    raise ManifestError("the download is larger than the release says")
                h.update(chunk)
                f.write(chunk)
                if progress is not None:
                    progress(done, asset.size)
            f.flush()
            os.fsync(f.fileno())
        if done != asset.size:
            raise ManifestError(f"the download was cut short ({done:,} of {asset.size:,} bytes)")
        if h.hexdigest() != asset.sha256:
            raise ManifestError("the download doesn't match the release's checksum")
        os.replace(part, final)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        if own:
            c.close()
    return final


# --------------------------------------------------------------------------- #
# the pending note (server -> tray) and the helper's result (helper -> next boot)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PendingUpdate:
    path: str
    version: str
    sha256: str


def write_pending(pending: PendingUpdate, folder: Path | None = None) -> None:
    d = folder or updates_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "pending.json.tmp"
    tmp.write_text(json.dumps(asdict(pending)), encoding="utf-8")
    os.replace(tmp, d / "pending.json")


def take_pending(folder: Path | None = None) -> PendingUpdate | None:
    """Read and remove the pending note; ``None`` if there's none, it's
    unreadable, or its file no longer matches its checksum."""
    p = (folder or updates_dir()) / "pending.json"
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    finally:
        p.unlink(missing_ok=True)
    try:
        pending = PendingUpdate(
            path=str(raw["path"]), version=str(raw["version"]), sha256=str(raw["sha256"])
        )
        if sha256_file(Path(pending.path)) != pending.sha256:
            log.error("update %s: the downloaded file changed; not installing", pending.version)
            return None
    except (KeyError, TypeError, OSError):
        return None
    return pending


RESULT_NAME = "result.txt"


def write_result(ok: bool, text: str, folder: Path | None = None) -> None:
    """What the helpers write themselves, for when the tray never got that far."""
    d = folder or updates_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / RESULT_NAME).write_text(f"{'ok' if ok else 'failed'} {text}\n", encoding="utf-8")


def take_result(folder: Path | None = None) -> tuple[bool, str] | None:
    """The installer helper's last outcome, once: ``(True, version)`` or
    ``(False, reason)``."""
    p = (folder or updates_dir()) / RESULT_NAME
    try:
        text = p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    with contextlib.suppress(OSError):
        p.unlink()
    word, _, rest = text.partition(" ")
    if word == "ok":
        return True, rest.strip()
    if word == "failed":
        return False, rest.strip() or "unknown error"
    return None


# --------------------------------------------------------------------------- #
# the checker
# --------------------------------------------------------------------------- #
@dataclass
class Download:
    """A download in progress (read by the page's progress timer)."""

    version: str
    done: int = 0
    total: int = 0
    error: str = ""
    path: Path | None = None
    sha256: str = ""  # of path, verified
    finished: bool = False
    cancel: bool = False


class Updater:
    """This machine's update state + the background check. Thread-safe: the
    page reads it from the event loop, the check and download run on threads."""

    def __init__(self, folder: Path | None = None, *, current: str = __version__) -> None:
        self.folder = folder or updates_dir()
        self.current = current
        self._lock = threading.Lock()
        self._state = UpdateState.load(self.state_path)
        self._manifest = self._load_manifest(self._state)
        self.download: Download | None = None
        # The installer's outcome from the last update, once per boot.
        self.last_result = take_result(self.folder)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def state_path(self) -> Path:
        return self.folder / "state.json"

    @staticmethod
    def _load_manifest(st: UpdateState) -> Manifest | None:
        if not st.manifest:
            return None
        try:
            return load_verified(
                base64.b64decode(st.manifest), st.signature.encode(), trusted_keys()
            )
        except (ManifestError, ValueError):
            return None

    # -- what the page shows ------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        return self._state.check and not os.environ.get(DISABLE_ENV)

    @property
    def disabled_by_env(self) -> bool:
        return bool(os.environ.get(DISABLE_ENV))

    @property
    def last_check_at(self) -> datetime | None:
        return _parse_ts(self._state.last_check_at)

    @property
    def last_error(self) -> str:
        return self._state.last_error

    def latest(self) -> Manifest | None:
        """The newest release seen, if it's newer than this build."""
        m = self._manifest
        return m if m is not None and is_newer(m.version, self.current) else None

    def available(self) -> Manifest | None:
        """:meth:`latest`, unless the user skipped that version."""
        m = self.latest()
        return None if m is None or m.version == self._state.skipped else m

    # -- user actions ------------------------------------------------------- #
    def set_enabled(self, on: bool) -> None:
        with self._lock:
            self._state.check = on
            self._state.save(self.state_path)

    def skip(self, version: str) -> None:
        with self._lock:
            self._state.skipped = version
            self._state.save(self.state_path)

    def check_now(self, client: httpx.Client | None = None) -> Manifest | None:
        """Blocking: fetch + verify the manifest, remember it, return
        :meth:`latest`. Errors are recorded (``last_error``), never raised."""
        now = _now().isoformat(timespec="seconds")
        try:
            manifest, data, sig = fetch_latest(client)
        except (httpx.HTTPError, ManifestError, OSError) as exc:
            log.info("update check failed: %s", exc)
            with self._lock:
                self._state.last_attempt_at = now
                self._state.last_error = str(exc) or type(exc).__name__
                self._state.save(self.state_path)
            return self.latest()
        with self._lock:
            self._manifest = manifest
            self._state.manifest = base64.b64encode(data).decode()
            self._state.signature = sig.decode("ascii", "replace").strip()
            self._state.last_check_at = self._state.last_attempt_at = now
            self._state.last_error = ""
            self._state.save(self.state_path)
        log.info("update check: latest %s (running %s)", manifest.version, self.current)
        return self.latest()

    def due(self, now: datetime | None = None) -> bool:
        if not self.enabled:
            return False
        now = now or _now()
        last_ok = self.last_check_at
        if last_ok is not None and now - last_ok < CHECK_EVERY:
            return False
        tried = _parse_ts(self._state.last_attempt_at)
        return not (
            tried is not None and self._state.last_error and now - tried < RETRY_AFTER_FAILURE
        )

    # -- background --------------------------------------------------------- #
    def start(self, first_delay: float = FIRST_CHECK_DELAY_SEC) -> None:
        if self._thread is not None:
            return

        def _run() -> None:
            if self._stop.wait(first_delay):
                return
            while True:
                try:
                    if self.due():
                        self.check_now()
                except Exception:  # never let the check end the thread
                    log.exception("update check crashed")
                if self._stop.wait(_LOOP_SEC):
                    return

        self._thread = threading.Thread(target=_run, name="sortandpost-update-check", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- download ----------------------------------------------------------- #
    def start_download(self, manifest: Manifest) -> Download:
        """Download this platform's asset on a thread; poll the returned
        :class:`Download`. A second call while one runs returns that one."""
        with self._lock:
            if self.download is not None and not self.download.finished:
                return self.download
            dl = self.download = Download(version=manifest.version)
        asset = manifest.asset_for(platform_key())

        def _run() -> None:
            try:
                if asset is None:
                    raise ManifestError("this release has no download for this computer")
                dl.total = asset.size

                def _progress(done: int, total: int) -> None:
                    dl.done, dl.total = done, total

                path = download_asset(
                    asset,
                    self.folder / manifest.version,
                    progress=_progress,
                    cancelled=lambda: dl.cancel,
                )
                dl.sha256, dl.path = asset.sha256, path
                self._prune(keep=manifest.version)
            except Exception as exc:
                log.warning("update download failed: %s", exc)
                dl.error = str(exc) or type(exc).__name__
            finally:
                dl.finished = True

        threading.Thread(target=_run, name="sortandpost-update-download", daemon=True).start()
        return dl

    def _prune(self, *, keep: str) -> None:
        """Drop older downloads (a version folder that isn't *keep*)."""
        for child in self.folder.iterdir():
            if child.is_dir() and child.name != keep:
                shutil.rmtree(child, ignore_errors=True)
