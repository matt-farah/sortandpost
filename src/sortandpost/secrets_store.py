"""Per-machine secrets: one API key per posting provider.

A secret is **never** written to a file under ``APP_DATA`` (not even ``local/``):
that tree can end up in a sync folder or a backup by mistake. Sources, in order:

1. a real process/shell env var named by the provider's spec (``ROBOPOST_API_KEY``,
   ``POSTIZ_API_KEY``, …; Docker's ``.env`` -> ``environment:``) -- always wins;
2. the OS keychain via :mod:`keyring` (macOS Keychain, Windows Credential
   Manager, Linux Secret Service), written from ``/settings``, under the
   service ``sortandpost`` and the spec's ``keychain_account``.

There is deliberately no plaintext fallback: with no usable keychain backend
(e.g. inside a Docker container) the only way in is the env var.

A keychain call can **block for as long as an OS prompt stays open**: an
ad-hoc-signed app gets a new signature on every rebuild, so macOS asks again
whether it may read the item. Every keychain call here is therefore blocking --
callers on the web event loop go through ``run.io_bound`` (hard constraint #7)
-- and each key is read once per process and cached in memory
(:func:`cached_key` is the non-blocking view of that cache).
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from typing import Literal, Protocol

import keyring
import keyring.errors

log = logging.getLogger(__name__)

SERVICE = "sortandpost"

KeySource = Literal["env", "keychain"]
KeyResult = tuple[str | None, KeySource | None]


class KeyRef(Protocol):
    """Where a provider's key lives (a ``ProviderSpec`` is one)."""

    @property
    def env_var(self) -> str: ...

    @property
    def keychain_account(self) -> str: ...


# keychain reads, once per process per account (set / clear keep them current);
# the lock makes concurrent first readers wait on one keychain prompt
_lock = threading.Lock()
_cached: dict[str, KeyResult] = {}


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


def get_key(ref: KeyRef) -> KeyResult:
    """``(key, source)``; ``(None, None)`` when no key is configured. **Blocking**
    the first time (it may wait on a keychain prompt): call it off the event loop.
    A failed or denied read is cached too, so it doesn't prompt again until the
    app restarts or a key is saved."""
    env = _env_key(ref)
    if env:
        return env
    with _lock:
        if ref.keychain_account not in _cached:
            _cached[ref.keychain_account] = _read_keychain(ref)
        return _cached[ref.keychain_account]


def cached_key(ref: KeyRef) -> KeyResult | None:
    """Non-blocking: the env var key, or what :func:`get_key` already read.
    ``None`` = the keychain hasn't been read yet in this process."""
    return _env_key(ref) or _cached.get(ref.keychain_account)


def forget_cached_key() -> None:
    """Drop every cached keychain read (tests; the next read goes to the keychain)."""
    with _lock:
        _cached.clear()


def _env_key(ref: KeyRef) -> KeyResult | None:
    env = os.environ.get(ref.env_var, "").strip() if ref.env_var else ""
    return (env, "env") if env else None


def _read_keychain(ref: KeyRef) -> KeyResult:
    if not keychain_available() or not ref.keychain_account:
        return None, None
    try:
        stored = keyring.get_password(SERVICE, ref.keychain_account)
    except keyring.errors.KeyringError as exc:
        log.warning(
            "could not read %s from the keychain: %s", ref.keychain_account, type(exc).__name__
        )
        return None, None
    stored = (stored or "").strip()
    return (stored, "keychain") if stored else (None, None)


def set_key(ref: KeyRef, key: str) -> None:
    """Store *key* in the OS keychain. Raises ``RuntimeError`` when there is no
    keychain (the caller tells the user to use the env var instead). Blocking."""
    key = key.strip()
    if not key:
        raise ValueError("empty API key")
    if not keychain_available():
        raise RuntimeError(f"no OS keychain available; set {ref.env_var} instead")
    with _lock:
        keyring.set_password(SERVICE, ref.keychain_account, key)
        _cached[ref.keychain_account] = (key, "keychain")


def clear_key(ref: KeyRef) -> None:
    """Remove the keychain entry (a no-op when there is none). The env var, if
    set, is outside our control and keeps working. Blocking."""
    if not keychain_available():
        return
    with _lock:
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(SERVICE, ref.keychain_account)
        _cached[ref.keychain_account] = (None, None)


def mask(key: str | None) -> str:
    """``••••abcd`` -- the only form of the key the UI ever shows."""
    if not key:
        return ""
    return "••••" + key[-4:] if len(key) > 8 else "••••"
