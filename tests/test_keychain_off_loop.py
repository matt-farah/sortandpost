"""A keychain read must never run on the web event loop (dev-notes/Issues.md "Keychain
freeze", 2026-09-30): a rebuilt ad-hoc-signed app makes macOS ask again whether
it may read a posting API key, and every page hung until that prompt was answered.

A keyring backend that blocks until released stands in for the open prompt; the
Settings → Posting panel must render (and keep serving) meanwhile, then show the
key once the read finishes. The key is read once per process and cached.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import keyring
import pytest
from nicegui.testing.user_simulation import user_simulation

from sortandpost import secrets_store as ss
from sortandpost.config import AppConfig, Env, Settings
from sortandpost.paths import Paths
from sortandpost.posting.robopost import SPEC as RP
from sortandpost.posts_ui import posting_settings_panel
from tests.conftest import MemoryKeyring


class PromptKeyring(MemoryKeyring):
    """``get_password`` waits for :attr:`answered`, like an open Keychain prompt."""

    def __init__(self) -> None:
        super().__init__()
        self.answered = threading.Event()
        self.reads = 0
        self.read_threads: list[str] = []

    def get_password(self, service: str, username: str) -> str | None:
        self.reads += 1
        self.read_threads.append(threading.current_thread().name)
        assert self.answered.wait(10), "the prompt was never answered"
        return super().get_password(service, username)


@pytest.fixture
def prompt_keyring() -> Iterator[PromptKeyring]:
    kr = PromptKeyring()
    kr.store[(ss.SERVICE, RP.keychain_account)] = "rp_secret_value_1234"
    keyring.set_keyring(kr)
    ss.forget_cached_key()
    yield kr
    kr.answered.set()


@pytest.fixture
def settings(paths: Paths) -> Settings:
    return Settings(env=Env(), paths=paths, config=AppConfig(), device_id="dev-a", hostname="a")


async def test_settings_panel_renders_while_the_keychain_prompt_is_open(
    settings: Settings, prompt_keyring: PromptKeyring
) -> None:
    def root() -> None:
        posting_settings_panel(settings, writable=True)

    async with user_simulation(root) as user:
        await user.open("/")  # would hang here if the read ran on the loop
        await user.should_see("Checking the keychain")
        await user.should_not_see("rp_secret_value_1234")

        prompt_keyring.answered.set()
        await user.should_see("API key ••••1234", retries=50)
        await user.should_not_see("rp_secret_value_1234")  # never sent to the browser

    assert threading.main_thread().name not in prompt_keyring.read_threads


def test_key_is_read_once_and_cached(prompt_keyring: PromptKeyring) -> None:
    prompt_keyring.answered.set()
    assert ss.cached_key(RP) is None  # not read yet: callers show "checking…"
    assert ss.get_key(RP) == ("rp_secret_value_1234", "keychain")
    assert ss.get_key(RP) == ("rp_secret_value_1234", "keychain")
    assert prompt_keyring.reads == 1
    assert ss.cached_key(RP) == ("rp_secret_value_1234", "keychain")


def test_save_and_remove_keep_the_cache_current(prompt_keyring: PromptKeyring) -> None:
    prompt_keyring.answered.set()
    ss.set_key(RP, "rp_new_value_5678")
    assert ss.cached_key(RP) == ("rp_new_value_5678", "keychain")
    ss.clear_key(RP)
    assert ss.get_key(RP) == (None, None)
    assert prompt_keyring.reads == 0  # both answered from the cache


def test_env_var_needs_no_keychain_read(
    prompt_keyring: PromptKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RP.env_var, "docker-key-1234")
    assert ss.cached_key(RP) == ("docker-key-1234", "env")
    assert ss.get_key(RP) == ("docker-key-1234", "env")
    assert prompt_keyring.reads == 0
