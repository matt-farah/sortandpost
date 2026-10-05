from __future__ import annotations

import keyring
import keyring.backends.fail
import pytest

from sortandpost import secrets_store as ss
from sortandpost.posting.postiz import SPEC as PZ
from sortandpost.posting.robopost import SPEC as RP
from tests.conftest import MemoryKeyring


def test_no_key_by_default() -> None:
    assert ss.get_key(RP) == (None, None)


def test_keychain_round_trip(memory_keyring: MemoryKeyring) -> None:
    ss.set_key(RP, "  rp_secret_value_1234  ")
    assert ss.get_key(RP) == ("rp_secret_value_1234", "keychain")
    assert memory_keyring.store[(ss.SERVICE, RP.keychain_account)] == "rp_secret_value_1234"
    ss.clear_key(RP)
    assert ss.get_key(RP) == (None, None)
    ss.clear_key(RP)  # clearing twice is a no-op


def test_env_var_beats_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    ss.set_key(RP, "from-keychain-xxxx")
    monkeypatch.setenv("ROBOPOST_API_KEY", "from-env-yyyy")
    assert ss.get_key(RP) == ("from-env-yyyy", "env")


def test_empty_key_rejected() -> None:
    with pytest.raises(ValueError):
        ss.set_key(RP, "   ")


def test_no_keychain_has_no_plaintext_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    keyring.set_keyring(keyring.backends.fail.Keyring())
    assert not ss.keychain_available()
    with pytest.raises(RuntimeError):
        ss.set_key(RP, "abc-def-ghi-jkl")
    assert ss.get_key(RP) == (None, None)
    monkeypatch.setenv("ROBOPOST_API_KEY", "docker-key-1234")
    assert ss.get_key(RP) == ("docker-key-1234", "env")


def test_mask_shows_last_four_only() -> None:
    assert ss.mask("abcdefghijkl1234") == "••••1234"
    assert ss.mask("short") == "••••"
    assert ss.mask(None) == ""


def test_each_provider_has_its_own_key(monkeypatch: pytest.MonkeyPatch) -> None:
    ss.set_key(RP, "robopost-key-1111")
    assert ss.get_key(PZ) == (None, None)
    ss.set_key(PZ, "postiz-key-2222")
    assert ss.get_key(RP) == ("robopost-key-1111", "keychain")
    assert ss.get_key(PZ) == ("postiz-key-2222", "keychain")
    monkeypatch.setenv("POSTIZ_API_KEY", "postiz-env-3333")
    assert ss.get_key(PZ) == ("postiz-env-3333", "env")
    assert ss.get_key(RP) == ("robopost-key-1111", "keychain")
    ss.clear_key(RP)
    assert ss.get_key(RP) == (None, None) and ss.cached_key(PZ) == ("postiz-env-3333", "env")
