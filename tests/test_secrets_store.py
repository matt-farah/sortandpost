from __future__ import annotations

import keyring
import keyring.backends.fail
import pytest

from sortandpost import secrets_store as ss
from tests.conftest import MemoryKeyring


def test_no_key_by_default() -> None:
    assert ss.get_robopost_key() == (None, None)


def test_keychain_round_trip(memory_keyring: MemoryKeyring) -> None:
    ss.set_robopost_key("  rp_secret_value_1234  ")
    assert ss.get_robopost_key() == ("rp_secret_value_1234", "keychain")
    assert memory_keyring.store[(ss.SERVICE, ss.ROBOPOST_ACCOUNT)] == "rp_secret_value_1234"
    ss.clear_robopost_key()
    assert ss.get_robopost_key() == (None, None)
    ss.clear_robopost_key()  # clearing twice is a no-op


def test_env_var_beats_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    ss.set_robopost_key("from-keychain-xxxx")
    monkeypatch.setenv("ROBOPOST_API_KEY", "from-env-yyyy")
    assert ss.get_robopost_key() == ("from-env-yyyy", "env")


def test_empty_key_rejected() -> None:
    with pytest.raises(ValueError):
        ss.set_robopost_key("   ")


def test_no_keychain_has_no_plaintext_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    keyring.set_keyring(keyring.backends.fail.Keyring())
    assert not ss.keychain_available()
    with pytest.raises(RuntimeError):
        ss.set_robopost_key("abc-def-ghi-jkl")
    assert ss.get_robopost_key() == (None, None)
    monkeypatch.setenv("ROBOPOST_API_KEY", "docker-key-1234")
    assert ss.get_robopost_key() == ("docker-key-1234", "env")


def test_mask_shows_last_four_only() -> None:
    assert ss.mask("abcdefghijkl1234") == "••••1234"
    assert ss.mask("short") == "••••"
    assert ss.mask(None) == ""
