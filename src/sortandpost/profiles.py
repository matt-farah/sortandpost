"""The user's profile: display name, achievements mode and profile picture.

One profile per library, in the shared ``sync/profile.json``, last-writer-wins
like collections (a deliberate exception to docs/architecture.md constraint #4:
it is not ``pixel_hash`` state and does not go in the ``changes`` log).

**The store seam.** Callers -- the Settings / wizard editor, the header chip,
the photo page -- go through a :class:`ProfileStore` from :func:`profile_store`,
never the files. :class:`FileProfileStore` is the only implementation today. A
hosted (multi-account) build implements the same protocol keyed by the
account: :attr:`Profile.id` is that key, and an avatar becomes a blob-store
object named by the same digest. So nothing outside this module knows where a
profile lives.

**Avatars** are content-addressed and write-once, like thumbnails:
``sync/profile/avatars/<sha256>.webp``; ``profile.json`` only names the digest,
so two machines picking the same picture write the same file and nothing
conflicts. The file is always a fresh 512x512 WebP rendered by
:func:`render_avatar` -- upright, sRGB, no EXIF / GPS / ICC -- never the
uploaded bytes or the library file (which is only read, constraints #1 / #2).
:func:`render_avatar` is CPU-bound and picklable, for the process pool (#7).
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol

from PIL import Image, UnidentifiedImageError

from sortandpost.indexer.decode import DecodeError, load_image
from sortandpost.paths import Paths
from sortandpost.post_export import upright_srgb
from sortandpost.sync.clock import Clock, utcnow

log = logging.getLogger(__name__)

Mode = Literal["tugs", "percival", "off"]
MODES: tuple[Mode, ...] = ("tugs", "percival", "off")
MODE_LABELS: dict[Mode, str] = {"tugs": "Tugs", "percival": "Percival", "off": "Off"}
DEFAULT_MODE: Mode = "tugs"
# The personas were Angel / Devil before 2026-10-19; a profile saved then keeps its pick.
_LEGACY_MODES: dict[str, Mode] = {"angel": "tugs", "devil": "percival"}
NAME_MAX_LEN = 40

AVATAR_SIZE = 512
AVATAR_QUALITY = 85
AVATAR_KEEP_OLD = 4  # older avatars kept beside the current one (a slow sync may still name one)
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_UPLOAD_PIXELS = 100_000_000  # an upload is a picture of a person, not a panorama
MAX_ZOOM = 5.0
AVATAR_URL = "/profile-avatar"  # served by app (behind the server guard)

_PROFILE = "profile.json"
_AVATARS = ("profile", "avatars")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class AvatarError(ValueError):
    """The picture couldn't be used (not an image, too big, unreadable)."""


@dataclass(frozen=True)
class Profile:
    id: str = ""  # stable uuid, set on the first save ("" = a profile never saved by this version)
    name: str = ""
    mode: Mode = DEFAULT_MODE
    avatar: str = ""  # sha256 of sync/profile/avatars/<digest>.webp, "" = none
    avatar_updated_at: str = ""
    updated_at: str = ""
    updated_by_device: str = ""

    @property
    def initials(self) -> str:
        """Up to two letters for the placeholder circle ("" when no name)."""
        words = [w for w in re.split(r"[\s._-]+", self.name) if w]
        return "".join(w[0] for w in words[:2]).upper()


@dataclass(frozen=True)
class Crop:
    """A square crop of an upright image, normalized: ``cx`` / ``cy`` (0..1)
    place the square in the slack along each axis (0 = left / top edge, like
    CSS ``object-position``), ``zoom`` >= 1 shrinks it from the largest square
    that fits."""

    cx: float = 0.5
    cy: float = 0.5
    zoom: float = 1.0

    def box(self, width: int, height: int) -> tuple[int, int, int, int]:
        zoom = min(max(self.zoom, 1.0), MAX_ZOOM)
        side = max(1, round(min(width, height) / zoom))
        cx, cy = (min(max(v, 0.0), 1.0) for v in (self.cx, self.cy))
        left = round((width - side) * cx)
        top = round((height - side) * cy)
        return left, top, left + side, top + side


def clean_profile_name(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    return re.sub(r"\s+", " ", _CONTROL_CHARS.sub(" ", raw)).strip()[:NAME_MAX_LEN].strip()


def profile_file(paths: Paths) -> Path:
    return paths.sync / _PROFILE


def avatars_dir(paths: Paths) -> Path:
    return paths.sync.joinpath(*_AVATARS)


def is_digest(value: str) -> bool:
    return bool(_DIGEST.fullmatch(value))


def load_profile(paths: Paths) -> Profile:
    path = profile_file(paths)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Profile()
    except (OSError, ValueError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return Profile()
    if not isinstance(data, dict):
        return Profile()
    mode = data.get("mode")
    mode = _LEGACY_MODES.get(mode, mode) if isinstance(mode, str) else mode
    avatar = str(data.get("avatar") or "")
    pid = str(data.get("id") or "")
    return Profile(
        id=pid if _valid_id(pid) else "",
        name=clean_profile_name(data.get("name", "")),
        mode=mode if mode in MODES else DEFAULT_MODE,
        avatar=avatar if is_digest(avatar) else "",
        avatar_updated_at=str(data.get("avatar_updated_at") or ""),
        updated_at=str(data.get("updated_at") or ""),
        updated_by_device=str(data.get("updated_by_device") or ""),
    )


def save_profile(
    paths: Paths, *, name: str, mode: Mode, device_id: str, clock: Clock = utcnow
) -> Profile:
    """Name + mode; the avatar (and id) already in the file are kept."""
    current = load_profile(paths)
    return _write_profile(
        paths,
        replace(
            current, name=clean_profile_name(name), mode=mode if mode in MODES else DEFAULT_MODE
        ),
        device_id,
        clock,
    )


def _valid_id(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _write_profile(paths: Paths, profile: Profile, device_id: str, clock: Clock) -> Profile:
    profile = replace(
        profile,
        id=profile.id or str(uuid.uuid4()),
        updated_at=clock().isoformat(),
        updated_by_device=device_id,
    )
    write_json(
        profile_file(paths),
        {
            "id": profile.id,
            "name": profile.name,
            "mode": profile.mode,
            "avatar": profile.avatar,
            "avatar_updated_at": profile.avatar_updated_at,
            "updated_at": profile.updated_at,
            "updated_by_device": profile.updated_by_device,
        },
    )
    return profile


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# the store seam
# --------------------------------------------------------------------------- #
class ProfileStore(Protocol):
    def get(self) -> Profile: ...

    def save(self, *, name: str, mode: Mode) -> Profile: ...

    def set_avatar(self, webp: bytes) -> Profile: ...

    def clear_avatar(self) -> Profile: ...

    def avatar_url(self, profile: Profile) -> str | None:
        """Where the browser fetches the picture, or None (show the placeholder)."""
        ...


class FileProfileStore:
    """``sync/profile.json`` + ``sync/profile/avatars/``. Every write re-reads
    the file first, so setting a picture never undoes a name another machine
    saved a moment before (and vice versa)."""

    def __init__(self, paths: Paths, device_id: str, clock: Clock = utcnow) -> None:
        self.paths = paths
        self.device_id = device_id
        self.clock = clock

    def get(self) -> Profile:
        return load_profile(self.paths)

    def save(self, *, name: str, mode: Mode) -> Profile:
        return save_profile(
            self.paths, name=name, mode=mode, device_id=self.device_id, clock=self.clock
        )

    def set_avatar(self, webp: bytes) -> Profile:
        digest = hashlib.sha256(webp).hexdigest()
        folder = avatars_dir(self.paths)
        dest = folder / f"{digest}.webp"
        if not dest.exists():  # content-addressed: an existing file is these bytes
            folder.mkdir(parents=True, exist_ok=True)
            tmp = folder / f".{digest}.tmp-{os.getpid()}"
            try:
                tmp.write_bytes(webp)
                os.replace(tmp, dest)
            finally:
                tmp.unlink(missing_ok=True)
        profile = _write_profile(
            self.paths,
            replace(self.get(), avatar=digest, avatar_updated_at=self.clock().isoformat()),
            self.device_id,
            self.clock,
        )
        self._prune(keep=digest)
        return profile

    def clear_avatar(self) -> Profile:
        current = self.get()
        if not current.avatar:
            return current
        return _write_profile(
            self.paths,
            replace(current, avatar="", avatar_updated_at=self.clock().isoformat()),
            self.device_id,
            self.clock,
        )

    def avatar_file(self, digest: str) -> Path | None:
        if not is_digest(digest):
            return None
        path = avatars_dir(self.paths) / f"{digest}.webp"
        return path if path.is_file() else None

    def avatar_url(self, profile: Profile) -> str | None:
        if not profile.avatar or self.avatar_file(profile.avatar) is None:
            return None  # not set, or not synced here yet
        return f"{AVATAR_URL}/{profile.avatar}.webp"

    def _prune(self, *, keep: str) -> None:
        """Drop all but the current avatar and the few newest others."""
        try:
            others = [
                p
                for p in avatars_dir(self.paths).glob("*.webp")
                if is_digest(p.stem) and p.stem != keep
            ]
            others.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            for old in others[AVATAR_KEEP_OLD:]:
                old.unlink(missing_ok=True)
        except OSError as exc:
            log.info("avatar prune skipped: %s", exc)


def profile_store(paths: Paths, device_id: str) -> ProfileStore:
    """The store this build uses (the one place a hosted build swaps in its own)."""
    return FileProfileStore(paths, device_id)


# --------------------------------------------------------------------------- #
# rendering (worker-safe)
# --------------------------------------------------------------------------- #
def open_upload(data: bytes) -> Image.Image:
    """Decode uploaded bytes (JPEG / PNG / WebP / HEIC / …) with the size and
    pixel caps. Raises :class:`AvatarError`."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise AvatarError(f"that file is over {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    try:
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > MAX_UPLOAD_PIXELS:  # checked before the pixels are decoded
            raise AvatarError("that image is too large")
        img.load()
    except AvatarError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise AvatarError("that file isn't an image this app can read") from exc
    return img


def render_avatar(source: str | bytes, crop: Crop) -> bytes:
    """The avatar WebP for *source*: a library file path (decoded like the
    indexer -- RAW preview, HEIC) or uploaded bytes."""
    if isinstance(source, bytes):
        img = open_upload(source)
    else:
        try:
            img = load_image(Path(source))
        except DecodeError as exc:
            raise AvatarError(f"couldn't read that photo ({exc})") from exc
    img = upright_srgb(img)
    img = img.crop(crop.box(*img.size))
    if img.size != (AVATAR_SIZE, AVATAR_SIZE):
        img = img.resize((AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    # a brand-new image: no exif= / icc_profile=, so nothing from the source rides along
    img.save(out, "WEBP", quality=AVATAR_QUALITY, method=4)
    return out.getvalue()


def preview_jpeg(data: bytes, long_edge: int = 1024) -> bytes:
    """A small upright JPEG of an upload, for the crop dialog's preview."""
    img = upright_srgb(open_upload(data))
    img.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=82)
    return out.getvalue()
