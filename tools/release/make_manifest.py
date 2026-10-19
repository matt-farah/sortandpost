"""Write and sign a release's update manifest (docs/architecture.md → "Updates").

    uv run python tools/release/make_manifest.py keygen
        One time: a new Ed25519 key. The private key goes OUTSIDE the repo
        (default ~/.sortandpost-release/update-signing-key.pem, mode 0600) and the
        public key is printed for src/sortandpost/update/pubkey.py. Back the
        private key up: without it, installed copies can't be sent updates.

    uv run python tools/release/make_manifest.py build --notes notes.md [--dist dist]
        Every release: hashes the built files in --dist (all platforms gathered
        in one folder), writes <dist>/latest.json + latest.json.sig, and prints
        the values for the Homebrew cask and the winget manifest. Upload all of it
        to the GitHub release tagged v<version>.

    uv run python tools/release/make_manifest.py verify dist/latest.json
        Check a manifest + signature against the keys the app trusts.

The private key is read from --key, else $SORTANDPOST_UPDATE_KEY, else the
default path above.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sortandpost import __version__
from sortandpost.update import pubkey
from sortandpost.update.check import RELEASES_URL
from sortandpost.update.manifest import MANIFEST_NAME, SIGNATURE_NAME, load_verified

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KEY = Path.home() / ".sortandpost-release" / "update-signing-key.pem"

# manifest asset key -> the file name each packaging script produces
ASSET_FILES = {
    "macos-arm64": "sortandpost-{v}-arm64.dmg",
    "windows-x64": "sortandpost-{v}-setup.exe",
    "linux-x86_64": "sortandpost-{v}-x86_64.AppImage",
    "linux-aarch64": "sortandpost-{v}-aarch64.AppImage",
}


def _key_path(arg: str | None) -> Path:
    return Path(arg or os.environ.get("SORTANDPOST_UPDATE_KEY") or DEFAULT_KEY).expanduser()


def _public_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return base64.b64encode(raw).decode()


def keygen(out: Path) -> int:
    if out.exists():
        print(f"{out} already exists; not overwriting it.", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    print(f"private key: {out}  (back it up; never commit it)")
    print(f"public key:  {_public_b64(key)}")
    print("Add the public key to PUBLIC_KEYS in src/sortandpost/update/pubkey.py.")
    return 0


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def build(dist: Path, notes: Path | None, version: str, key_file: Path) -> int:
    key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        print(f"{key_file} isn't an Ed25519 key", file=sys.stderr)
        return 1
    if _public_b64(key) not in pubkey.PUBLIC_KEYS:
        print(
            "warning: this key isn't in update/pubkey.py -- installed copies won't trust it",
            file=sys.stderr,
        )
    tag = f"v{version}"
    assets: dict[str, dict[str, object]] = {}
    for akey, pattern in ASSET_FILES.items():
        path = dist / pattern.format(v=version)
        if not path.is_file():
            print(f"  (no {path.name}: {akey} gets no update)")
            continue
        assets[akey] = {
            "url": f"{RELEASES_URL}/download/{tag}/{path.name}",
            "sha256": _sha256(path),
            "size": path.stat().st_size,
        }
        print(f"  {akey}: {path.name}")
    if not assets:
        print(f"no release files for {version} in {dist}", file=sys.stderr)
        return 1
    manifest = {
        "version": version,
        "published_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "notes": notes.read_text(encoding="utf-8") if notes else "",
        "notes_url": f"{RELEASES_URL}/tag/{tag}",
        "assets": assets,
    }
    data = (json.dumps(manifest, indent=2) + "\n").encode()
    sig = base64.b64encode(key.sign(data)) + b"\n"
    (dist / MANIFEST_NAME).write_bytes(data)
    (dist / SIGNATURE_NAME).write_bytes(sig)
    load_verified(data, sig, (_public_b64(key),))  # round-trip check
    print(f"wrote {dist / MANIFEST_NAME} + {SIGNATURE_NAME}")
    _print_package_values(version, assets)
    return 0


def _print_package_values(version: str, assets: dict[str, dict[str, object]]) -> None:
    mac, win = assets.get("macos-arm64"), assets.get("windows-x64")
    if mac:
        print(
            "\nHomebrew cask (Casks/sortandpost.rb in matt-farah/homebrew-tap):\n"
            f'  version "{version}"\n'
            f'  sha256 "{mac["sha256"]}"\n'
            '  url "https://github.com/matt-farah/sortandpost/releases/download/'
            'v#{version}/sortandpost-#{version}-arm64.dmg"\n'
            "  auto_updates true"
        )
    if win:
        print(
            "\nwinget (InstallerType inno, Scope user):\n"
            f"  PackageVersion: {version}\n"
            f"  InstallerUrl: {win['url']}\n"
            f"  InstallerSha256: {str(win['sha256']).upper()}"
        )


def verify(path: Path) -> int:
    data = path.read_bytes()
    sig = path.with_name(SIGNATURE_NAME).read_bytes()
    m = load_verified(data, sig, pubkey.PUBLIC_KEYS)
    print(f"ok: {m.version}, assets: {', '.join(sorted(m.assets))}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("--out", default=None)
    b = sub.add_parser("build")
    b.add_argument("--dist", default=str(ROOT / "dist"))
    b.add_argument("--notes", default=None, help="markdown release notes")
    b.add_argument("--version", default=__version__)
    b.add_argument("--key", default=None)
    v = sub.add_parser("verify")
    v.add_argument("manifest")
    args = ap.parse_args(argv)
    if args.cmd == "keygen":
        return keygen(_key_path(args.out))
    if args.cmd == "build":
        notes = Path(args.notes) if args.notes else None
        return build(Path(args.dist), notes, args.version, _key_path(args.key))
    return verify(Path(args.manifest))


if __name__ == "__main__":
    raise SystemExit(main())
