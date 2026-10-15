"""First-run onboarding (V2 #23): split layout, folder inspection, wizard state."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sortandpost import launcher_pref, onboarding
from sortandpost.config import Settings, bootstrap
from sortandpost.indexer.__main__ import main as indexer_main
from sortandpost.library_roots import LibraryRoot, save_roots
from sortandpost.onboarding import (
    NEW_LIBRARY_SUBDIR,
    inspect_folder,
    is_complete,
    mark_complete,
    settle_on_startup,
    suggested_roots,
)
from sortandpost.paths import Paths, local_dir_for


# --------------------------------------------------------------------------- #
# split layout
# --------------------------------------------------------------------------- #
def test_paths_need_a_layout() -> None:
    with pytest.raises(ValueError):
        Paths()


def test_each_shared_folder_gets_its_own_local_dir(tmp_path: Path) -> None:
    a = local_dir_for(tmp_path / "Dropbox" / "photos", tmp_path / "m")
    b = local_dir_for(tmp_path / "Drive" / "photos", tmp_path / "m")
    assert a != b
    assert a == local_dir_for(tmp_path / "Dropbox" / "photos", tmp_path / "m")  # stable
    assert a.parent == tmp_path / "m" / "libraries"


def test_split_paths_keep_machine_data_out_of_the_shared_folder(tmp_path: Path) -> None:
    p = Paths.for_sync_dir(tmp_path / "shared")
    assert p.sync == tmp_path / "shared"
    assert p.published_db == tmp_path / "shared" / "sortandpost.db"
    for private in (p.working_db, p.device_file, p.logs_dir, p.ui_prefs_file, p.models_dir):
        assert not private.is_relative_to(tmp_path / "shared")
    assert set(p.roots()) == {p.local, p.sync}


def test_pointer_prefers_sync_dir(tmp_path: Path) -> None:
    launcher_pref.remember_sync_dir(tmp_path / "s")
    ptr = launcher_pref.read_pointer()
    assert ptr is not None and ptr.kind == "sync_dir" and ptr.path == tmp_path / "s"
    assert launcher_pref.read_remembered_app_data() is None


# --------------------------------------------------------------------------- #
# settle_on_startup
# --------------------------------------------------------------------------- #
def test_fresh_folder_is_pending(tmp_path: Path) -> None:
    paths = Paths(app_data=tmp_path / "a")
    state = settle_on_startup(paths)
    assert state.pending and state.fresh_device and not state.joined_existing


def test_existing_single_folder_install_is_marked_done(tmp_path: Path) -> None:
    paths = Paths(app_data=tmp_path / "a")
    bootstrap(paths)  # what every pre-onboarding run did
    state = settle_on_startup(paths)
    assert not state.pending
    assert is_complete(paths)


def test_split_layout_half_finished_wizard_stays_pending(tmp_path: Path) -> None:
    paths = Paths.for_sync_dir(tmp_path / "shared")
    settle_on_startup(paths)
    bootstrap(paths)  # device.json now exists, wizard not finished
    assert settle_on_startup(paths).pending


def test_new_vs_joined_survives_a_restart_mid_wizard(tmp_path: Path) -> None:
    paths = Paths.for_sync_dir(tmp_path / "shared")
    assert not settle_on_startup(paths).joined_existing
    bootstrap(paths)  # creates config.yaml -- would look like "joined" now
    assert not settle_on_startup(paths).joined_existing
    mark_complete(paths, how="new")
    assert not (paths.local / "onboarding_started.json").exists()


def test_joining_an_existing_library(tmp_path: Path) -> None:
    first = Paths.for_sync_dir(tmp_path / "shared")
    bootstrap(first)
    other_machine = Paths.split(local=tmp_path / "machine-b", sync=tmp_path / "shared")
    state = settle_on_startup(other_machine)
    assert state.pending and state.joined_existing


# --------------------------------------------------------------------------- #
# inspect_folder
# --------------------------------------------------------------------------- #
def _make_library(sync: Path, photos: int = 3, host: str = "laptop") -> None:
    sync.mkdir(parents=True)
    (sync / "config.yaml").write_text("{}\n")
    con = sqlite3.connect(sync / "sortandpost.db")
    con.execute("CREATE TABLE photos (pixel_hash TEXT, missing_since TEXT)")
    con.executemany("INSERT INTO photos VALUES (?, NULL)", [(str(i),) for i in range(photos)])
    con.execute("INSERT INTO photos VALUES ('gone', '2026-01-01')")
    con.commit()
    con.close()
    (sync / ".sortandpost.lock").write_text(json.dumps({"hostname": host}))


def test_missing_or_empty_folder_is_a_new_library(tmp_path: Path) -> None:
    ins = inspect_folder(str(tmp_path / "nope"))
    assert ins.kind == "new" and ins.sync_dir == tmp_path / "nope"
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / ".DS_Store").write_text("")
    assert inspect_folder(str(empty)).sync_dir == empty


def test_busy_folder_gets_a_subfolder(tmp_path: Path) -> None:
    (tmp_path / "Dropbox").mkdir()
    (tmp_path / "Dropbox" / "taxes.pdf").write_text("x")
    ins = inspect_folder(f'"{tmp_path / "Dropbox"}"')  # pasted with quotes
    assert ins.kind == "new"
    assert ins.sync_dir == tmp_path / "Dropbox" / NEW_LIBRARY_SUBDIR


def test_existing_library_found_directly(tmp_path: Path) -> None:
    _make_library(tmp_path / "lib")
    ins = inspect_folder(str(tmp_path / "lib"))
    assert ins.kind == "existing" and ins.sync_dir == tmp_path / "lib"
    assert ins.photos == 3 and ins.last_host == "laptop"
    assert "3 photos" in ins.message


def test_wizard_ignores_its_own_lock(tmp_path: Path) -> None:
    # The wizard runs with the library open: the lock is this machine's own.
    _make_library(tmp_path / "lib")
    lock = tmp_path / "lib" / ".sortandpost.lock"
    lock.write_text(json.dumps({"hostname": "docker", "device_id": "me"}))
    assert inspect_folder(str(tmp_path / "lib"), this_device="me").last_host is None
    assert inspect_folder(str(tmp_path / "lib"), this_device="other").last_host == "docker"


def test_legacy_app_data_folder_uses_its_sync_subfolder(tmp_path: Path) -> None:
    _make_library(tmp_path / "appdata" / "sync")
    (tmp_path / "appdata" / "local").mkdir()
    ins = inspect_folder(str(tmp_path / "appdata"))
    assert ins.kind == "existing" and ins.sync_dir == tmp_path / "appdata" / "sync"


def test_parent_of_a_library_subfolder(tmp_path: Path) -> None:
    _make_library(tmp_path / "Drive" / NEW_LIBRARY_SUBDIR)
    ins = inspect_folder(str(tmp_path / "Drive"))
    assert ins.kind == "existing" and ins.sync_dir == tmp_path / "Drive" / NEW_LIBRARY_SUBDIR


@pytest.mark.parametrize("raw", ["", "  ", "relative/path"])
def test_unusable_input(raw: str) -> None:
    assert inspect_folder(raw).kind == "invalid"


def test_file_or_private_folder_is_rejected(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("x")
    assert inspect_folder(str(f)).kind == "invalid"
    machine = tmp_path / "machine"
    assert inspect_folder(str(machine / "x"), local_root=machine).kind == "invalid"


# --------------------------------------------------------------------------- #
# library-folder suggestions
# --------------------------------------------------------------------------- #
def test_suggests_other_devices_folders_that_exist_here(tmp_path: Path) -> None:
    paths = Paths.for_sync_dir(tmp_path / "shared")
    paths.ensure_dirs()
    (tmp_path / "Photos").mkdir()
    save_roots(
        paths,
        "aaaaaaaa1111",
        "laptop",
        [
            LibraryRoot(container=str(tmp_path / "Photos"), host=str(tmp_path / "Photos")),
            LibraryRoot(container="/nowhere", host="/nowhere"),
        ],
    )
    save_roots(paths, "bbbbbbbb2222", "me", [LibraryRoot(container="/mine", host="/mine")])
    got = suggested_roots(paths, "bbbbbbbb2222")
    assert [r.host for r in got] == [str(tmp_path / "Photos")]


# --------------------------------------------------------------------------- #
# Settings.resolve + CLI gating
# --------------------------------------------------------------------------- #
def test_fresh_native_machine_is_pending_and_not_seeded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("sortandpost.config.running_in_docker", lambda: False)
    launcher_pref.remember_sync_dir(tmp_path / "shared")
    monkeypatch.delenv("APP_DATA", raising=False)
    s = Settings.resolve()
    assert s.onboarding_pending
    assert s.library_roots == []  # no bogus "/library" from the config template


def test_fresh_docker_container_registers_its_library_mount(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Joining a library a Mac made: config.yaml's roots are Mac paths, which
    # don't exist in the container -- the compose mount /library is what to scan.
    monkeypatch.setattr("sortandpost.config.running_in_docker", lambda: True)
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "config.yaml").write_text("library_roots: [/Users/me/Pictures]\n")
    launcher_pref.remember_sync_dir(shared)
    monkeypatch.delenv("APP_DATA", raising=False)
    monkeypatch.setenv("SORTANDPOST_HOST_LIBRARY_DIR", "/Users/me/Pictures")
    s = Settings.resolve()
    assert s.library_roots == [LibraryRoot(container="/library", host="/Users/me/Pictures")]


def test_upgrading_install_is_not_pending(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    app_data = tmp_path / "appdata"
    bootstrap(Paths(app_data=app_data))
    monkeypatch.setenv("APP_DATA", str(app_data))
    assert not Settings.resolve().onboarding_pending


def test_indexer_cli_waits_for_onboarding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("APP_DATA", str(tmp_path / "fresh"))
    assert indexer_main(["--once"]) == 1
    assert "setup isn't finished" in capsys.readouterr().err


def test_indexer_cli_without_any_folder(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("APP_DATA", raising=False)
    monkeypatch.chdir(Path(onboarding.__file__).parent)  # no .env here
    assert indexer_main(["--once"]) == 1
    assert "hasn't been set up" in capsys.readouterr().err


def test_gated_pages() -> None:
    from sortandpost.app import _is_gated

    assert _is_gated("/") and _is_gated("/settings") and _is_gated("/photo/abc")
    assert not _is_gated("/welcome")
    assert not _is_gated("/_nicegui/3.16.0/static/quasar.js")
    assert not _is_gated("/thumbs/ab/abc_320.webp")
    assert not _is_gated("/postsomething")
