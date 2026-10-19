"""The release manifest: ``latest.json`` + its detached Ed25519 signature
``latest.json.sig`` (base64), both assets of every GitHub release.

A download is trusted only if the manifest's signature checks out against a key
in :mod:`sortandpost.update.pubkey` **and** the file's sha256 matches the
manifest. HTTPS alone isn't enough: anyone who can publish a release (a leaked
GitHub token) could otherwise push code to every install.

``tools/release/make_manifest.py`` writes and signs it.
"""

from __future__ import annotations

import base64
import binascii
import json
import platform
import re
import sys
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MANIFEST_NAME = "latest.json"
SIGNATURE_NAME = "latest.json.sig"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


class ManifestError(ValueError):
    """A manifest that can't be trusted or understood."""


@dataclass(frozen=True)
class Asset:
    url: str
    sha256: str
    size: int


@dataclass(frozen=True)
class Manifest:
    version: str
    published_at: str = ""
    notes: str = ""  # markdown
    notes_url: str = ""
    # Asset key (see platform_key) -> the file to install.
    assets: dict[str, Asset] = field(default_factory=dict)

    def asset_for(self, key: str | None) -> Asset | None:
        return self.assets.get(key) if key else None


def parse_version(text: str) -> tuple[int, int, int] | None:
    """``"3.1.0"`` / ``"v3.1.0"`` -> ``(3, 1, 0)``; anything else (a
    pre-release, a typo) -> ``None``, which is never offered as an update."""
    m = _VERSION.match(text.strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def is_newer(candidate: str, current: str) -> bool:
    new, cur = parse_version(candidate), parse_version(current)
    return new is not None and cur is not None and new > cur


def platform_key() -> str | None:
    """Which manifest asset this build installs: ``macos-arm64``,
    ``windows-x64``, ``linux-x86_64`` or ``linux-aarch64`` (the platforms the
    packaging scripts build)."""
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        return "macos-arm64" if machine == "arm64" else None
    if sys.platform == "win32":
        return "windows-x64" if machine in ("amd64", "x86_64") else None
    if sys.platform.startswith("linux"):
        if machine in ("x86_64", "amd64"):
            return "linux-x86_64"
        if machine in ("aarch64", "arm64"):
            return "linux-aarch64"
    return None


def verify_signature(data: bytes, signature: bytes, public_keys: tuple[str, ...]) -> None:
    """Raise :class:`ManifestError` unless *signature* (base64 text, as in
    ``latest.json.sig``) is a valid Ed25519 signature of *data* by one of
    *public_keys*."""
    if not public_keys:
        raise ManifestError("this build has no update signing key")
    try:
        sig = base64.b64decode(signature.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ManifestError("the update signature isn't valid base64") from exc
    for key in public_keys:
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(key)).verify(sig, data)
        except (InvalidSignature, ValueError):
            continue
        return
    raise ManifestError("the update's signature doesn't match")


def load_verified(data: bytes, signature: bytes, public_keys: tuple[str, ...]) -> Manifest:
    """Verify, then parse: the only way the app reads a manifest, whether just
    downloaded or saved in ``update_state.json``."""
    verify_signature(data, signature, public_keys)
    return parse_manifest(data)


def parse_manifest(data: bytes) -> Manifest:
    """Parse ``latest.json`` (unverified: use :func:`load_verified`)."""
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"the update manifest isn't JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestError("the update manifest isn't an object")
    version = raw.get("version")
    if not isinstance(version, str) or parse_version(version) is None:
        raise ManifestError(f"the update manifest has a bad version: {version!r}")
    assets: dict[str, Asset] = {}
    raw_assets = raw.get("assets") or {}
    if not isinstance(raw_assets, dict):
        raise ManifestError("the update manifest's assets aren't an object")
    for key, a in raw_assets.items():
        if not isinstance(a, dict):
            raise ManifestError(f"asset {key!r} isn't an object")
        url, sha, size = a.get("url"), a.get("sha256"), a.get("size")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ManifestError(f"asset {key!r} has a bad url")
        if not isinstance(sha, str) or not _SHA256.match(sha):
            raise ManifestError(f"asset {key!r} has a bad sha256")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ManifestError(f"asset {key!r} has a bad size")
        assets[str(key)] = Asset(url=url, sha256=sha, size=size)

    def _text(name: str) -> str:
        value = raw.get(name, "")
        return value if isinstance(value, str) else ""

    return Manifest(
        version=version.strip().lstrip("v"),
        published_at=_text("published_at"),
        notes=_text("notes"),
        notes_url=_text("notes_url"),
        assets=assets,
    )
