"""Per-machine secrets -- today only the robopost.app API key.

A secret is **never** written to a file under ``APP_DATA`` (not even ``local/``):
that tree can end up in a sync folder or a backup by mistake. Sources, in order:

1. a real ``ROBOPOST_API_KEY`` process/shell env var (Docker's ``.env`` ->
   ``environment:``, or ``ROBOPOST_API_KEY=... command``) -- always wins;
2. the OS keychain via :mod:`keyring` (macOS Keychain, Windows Credential
   Manager, Linux Secret Service), written from ``/settings``.

There is deliberately no plaintext fallback: with no usable keychain backend
(e.g. inside a Docker container) the only way in is the env var.

A keychain call can **block for as long as an OS prompt stays open**: an
ad-hoc-signed app gets a new signature on every rebuild, so macOS asks again
whether it may read the item. Every keychain call here is therefore blocking --
callers on the web event loop go through ``run.io_bound`` (hard constraint #7)
-- and the key is read once per process and cached in memory
(:func:`cached_robopost_key` is the non-blocking view of that cache).
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from typing import Literal

import keyring
import keyring.errors

log = logging.getLogger(__name__)

SERVICE = "sortandpost"
ROBOPOST_ACCOUNT = "robopost_api_key"
ROBOPOST_ENV = "ROBOPOST_API_KEY"

KeySource = Literal["env", "keychain"]
KeyResult = tuple[str | None, KeySource | None]

# the keychain read, once per process (set / clear keep it current); the lock
# makes concurrent first readers wait on one keychain prompt, not raise several
_lock = threading.Lock()
_cached: KeyResult | None = None


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


def get_robopost_key() -> KeyResult:
    """``(key, source)``; ``(None, None)`` when no key is configured. **Blocking**
    the first time (it may wait on a keychain prompt): call it off the event loop.
    A failed or denied read is cached too, so it doesn't prompt again until the
    app restarts or a key is saved."""
    global _cached
    env = _env_key()
    if env:
        return env
    with _lock:
        if _cached is None:
            _cached = _read_keychain()
        return _cached


def cached_robopost_key() -> KeyResult | None:
    """Non-blocking: the env var key, or what :func:`get_robopost_key` already
    read. ``None`` = the keychain hasn't been read yet in this process."""
    return _env_key() or _cached


def forget_cached_key() -> None:
    """Drop the cached keychain read (tests; the next read goes to the keychain)."""
    global _cached
    with _lock:
        _cached = None


def _env_key() -> KeyResult | None:
    env = os.environ.get(ROBOPOST_ENV, "").strip()
    return (env, "env") if env else None


def _read_keychain() -> KeyResult:
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
    keychain (the caller tells the user to use the env var instead). Blocking."""
    global _cached
    key = key.strip()
    if not key:
        raise ValueError("empty API key")
    if not keychain_available():
        raise RuntimeError(f"no OS keychain available; set {ROBOPOST_ENV} instead")
    with _lock:
        keyring.set_password(SERVICE, ROBOPOST_ACCOUNT, key)
        _cached = (key, "keychain")


def clear_robopost_key() -> None:
    """Remove the keychain entry (a no-op when there is none). The env var, if
    set, is outside our control and keeps working. Blocking."""
    global _cached
    if not keychain_available():
        return
    with _lock:
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(SERVICE, ROBOPOST_ACCOUNT)
        _cached = (None, None)


def mask(key: str | None) -> str:
    """``••••abcd`` -- the only form of the key the UI ever shows."""
    if not key:
        return ""
    return "••••" + key[-4:] if len(key) > 8 else "••••"
