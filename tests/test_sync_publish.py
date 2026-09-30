from __future__ import annotations

import os
from pathlib import Path

import pytest

from sortandpost.sync import publish
from sortandpost.sync.publish import (
    atomic_publish,
    pull_published,
    quick_check,
    read_generation,
    sha256_file,
)
from tests.factories import make_db


def test_atomic_publish_copies_bytes_and_leaves_no_tmp(tmp_path: Path) -> None:
    working = tmp_path / "local" / "sortandpost.db"
    published = tmp_path / "sync" / "sortandpost.db"
    make_db(working, photos=["a", "b"], generation=4)

    atomic_publish(working, published, device_id="dev-x")

    assert sha256_file(published) == sha256_file(working)
    assert not list((tmp_path / "sync").glob(".sortandpost.db.tmp-*"))


def test_replace_retries_while_windows_holds_the_target_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = tmp_path / "new", tmp_path / "db"
    src.write_text("new")
    dst.write_text("old")
    real_replace = os.replace
    failures = iter([PermissionError(13, "in use"), PermissionError(13, "in use")])

    def flaky(a: object, b: object) -> None:
        err = next(failures, None)
        if err is not None:
            raise err
        real_replace(a, b)  # type: ignore[arg-type]

    monkeypatch.setattr(publish.sys, "platform", "win32")
    monkeypatch.setattr(publish.os, "replace", flaky)
    monkeypatch.setattr(publish.time, "sleep", lambda _s: None)
    publish.replace_file(src, dst)
    assert dst.read_text() == "new"


def test_replace_gives_up_after_the_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def always(_a: object, _b: object) -> None:
        raise PermissionError(13, "in use")

    monkeypatch.setattr(publish.sys, "platform", "win32")
    monkeypatch.setattr(publish.os, "replace", always)
    monkeypatch.setattr(publish.time, "sleep", lambda _s: None)
    with pytest.raises(PermissionError):
        publish.replace_file(tmp_path / "a", tmp_path / "b")


def test_read_generation_reads_meta_or_zero(tmp_path: Path) -> None:
    db = tmp_path / "sortandpost.db"
    make_db(db, photos=["a"], generation=9)
    assert read_generation(db) == 9
    assert read_generation(tmp_path / "absent.db") == 0

    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not sqlite")
    assert read_generation(junk) == 0


def test_quick_check(tmp_path: Path) -> None:
    good = tmp_path / "good.db"
    make_db(good, photos=["a"], generation=1)
    assert quick_check(good) is True

    junk = tmp_path / "junk.db"
    junk.write_bytes(b"corrupt")
    assert quick_check(junk) is False
    assert quick_check(tmp_path / "missing.db") is False


def test_pull_published_overwrites_working_copy(tmp_path: Path) -> None:
    working = tmp_path / "local" / "sortandpost.db"
    published = tmp_path / "sync" / "sortandpost.db"
    make_db(working, photos=["a"], generation=1)
    make_db(published, photos=["a", "b", "c"], generation=7)

    pull_published(published, working, device_id="dev-x")

    assert read_generation(working) == 7
    assert sha256_file(working) == sha256_file(published)
