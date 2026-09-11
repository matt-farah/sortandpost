"""``sortandpost.indexer.placeholder.is_online_only`` -- cloud stub detection."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from sortandpost.indexer import placeholder
from sortandpost.indexer.placeholder import is_online_only


def _stat(**kw: object) -> os.stat_result:
    # is_online_only only touches st_size / st_blocks / st_flags / st_file_attributes.
    fields: dict[str, object] = {"st_size": 0, "st_blocks": 1, "st_flags": 0}
    fields.update(kw)
    return SimpleNamespace(**fields)  # type: ignore[return-value]


def test_a_real_local_file_is_not_online_only(tmp_path: object) -> None:
    p = tmp_path / "a.jpg"  # type: ignore[operator]
    p.write_bytes(b"x" * 20_000)
    assert is_online_only(os.stat(p)) is False


def test_zero_blocks_with_a_real_size_looks_like_a_stub() -> None:
    assert is_online_only(_stat(st_size=5_000_000, st_blocks=0)) is True


def test_zero_blocks_but_tiny_is_below_the_floor() -> None:
    assert is_online_only(_stat(st_size=100, st_blocks=0)) is False


def test_macos_dataless_flag_is_definitive() -> None:
    assert is_online_only(_stat(st_size=10, st_blocks=8, st_flags=0x40000000)) is True


def test_missing_st_blocks_says_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(placeholder.sys, "platform", "sunos5")
    st = SimpleNamespace(st_size=5_000_000, st_flags=0)  # no st_blocks at all
    assert is_online_only(st) is False  # type: ignore[arg-type]


def test_windows_recall_attribute_is_a_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(placeholder.sys, "platform", "win32")
    st = SimpleNamespace(st_size=5_000_000, st_file_attributes=0x400000)
    assert is_online_only(st) is True  # type: ignore[arg-type]

    st_plain = SimpleNamespace(st_size=5_000_000, st_file_attributes=0x20)
    assert is_online_only(st_plain) is False  # type: ignore[arg-type]
