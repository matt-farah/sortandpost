"""Per-machine secrets -- today only the robopost.app API key.

A secret is **never** written to a file under ``APP_DATA`` (not even ``local/``):
that tree can end up in a sync folder or a backup by mistake. Sources, in order:

1. a real ``ROBOPOST_API_KEY`` process/shell env var (Docker's ``.env`` ->
   ``environment:``, or ``ROBOPOST_API_KEY=... command``) -- always wins;
2. the OS keychain via :mod:`keyring` (macOS Keychain, Windows Credential
   Manager, Linux Secret Service), written from ``/settings``.

There is deliberately no plaintext fallback: with no usable keychain backend
(e.g. inside a Docker container) the only way in is the env var.
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Literal

import keyring
import keyring.errors

log = logging.getLogger(__name__)

SERVICE = "sortandpost"
ROBOPOST_ACCOUNT = "robopost_api_key"
ROBOPOST_ENV = "ROBOPOST_API_KEY"

KeySource = Literal["env", "keychain"]


def keychain_available() -> bool:
    """True when :mod:`keyring` resolved a real backend (not the ``fail`` /
    ``null`` placeholders it picks when no keychain exists)."""
    try:
        backend = keyring.get_keyring()
    except Exception:  # pragma: no cover - backend discovery blew up
        return False
    module = type(backend).__module__
    return not (module.startswith("keyring.backends.fail") or module.endswith(".null"))


def keychain_name() -> str:
    """Human label for the active backend, for the settings page."""
    module = type(keyring.get_keyring()).__module__
    if "macOS" in module:
        return "macOS Keychain"
    if "Windows" in module:
        return "Windows Credential Manager"
    if "SecretService" in module or "kwallet" in module.lower():
        return "system keyring"
    return "keychain"


def get_robopost_key() -> tuple[str | None, KeySource | None]:
    """``(key, source)``; ``(None, None)`` when no key is configured."""
    env = os.environ.get(ROBOPOST_ENV, "").strip()
    if env:
        return env, "env"
    if not keychain_available():
        return None, None
    try:
        stored = keyring.get_password(SERVICE, ROBOPOST_ACCOUNT)
    except keyring.errors.KeyringError as exc:
        log.warning("could not read the robopost key from the keychain: %s", type(exc).__name__)
        return None, None
    stored = (stored or "").strip()
    return (stored, "keychain") if stored else (None, None)


def set_robopost_key(key: str) -> None:
    """Store *key* in the OS keychain. Raises ``RuntimeError`` when there is no
    keychain (the caller tells the user to use the env var instead)."""
    key = key.strip()
    if not key:
        raise ValueError("empty API key")
    if not keychain_available():
        raise RuntimeError(f"no OS keychain available; set {ROBOPOST_ENV} instead")
    keyring.set_password(SERVICE, ROBOPOST_ACCOUNT, key)


def clear_robopost_key() -> None:
    """Remove the keychain entry (a no-op when there is none). The env var, if
    set, is outside our control and keeps working."""
    if not keychain_available():
        return
    with contextlib.suppress(keyring.errors.PasswordDeleteError):
        keyring.delete_password(SERVICE, ROBOPOST_ACCOUNT)


def mask(key: str | None) -> str:
    """``••••abcd`` -- the only form of the key the UI ever shows."""
    if not key:
        return ""
    return "••••" + key[-4:] if len(key) > 8 else "••••"
