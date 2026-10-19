"""The Ed25519 public key(s) that release manifests are signed with.

The private key never enters the repo: it lives with whoever cuts releases
(``tools/release/make_manifest.py keygen`` writes it outside the repo). A list,
so a key can be rotated: ship a release that trusts both, then sign with the
new one.
"""

from __future__ import annotations

# base64 of the raw 32-byte public key
PUBLIC_KEYS: tuple[str, ...] = (
    "/Coz5wCQHz6c9H7OXOgrRs8FIqLf7tEBy9AynLbq+K0=",  # 2026-10-19, first release key
)
