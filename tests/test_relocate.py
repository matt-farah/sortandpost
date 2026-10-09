"""Moving a single-folder install's ``local/`` out of a cloud-sync folder
(sortandpost.relocate, sortandpost.cloud_folders)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from sortandpost.cloud_folders import cloud_provider
from sortandpost.config import Settings, bootstrap
from sortandpost.launcher_pref import read_pointer, remember_app_data
from sortandpost.paths import Paths
from sortandpost.relocate import maybe_relocate, read_record
from tests.factories import make_db

needs_chmod = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 000 doesn't make a file unreadable here",
)


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("Library/CloudStorage/SynologyDrive-me/Film/sortandpost/local", "Synology Drive"),
        ("Library/CloudStorage/OneDrive-Personal/x", "OneDrive"),
        ("Library/CloudStorage/GoogleDrive-me@example.com/My Drive/x", "Google Drive"),
        ("Library/CloudStorage/Dropbox/x", "Dropbox"),
        ("Library/Mobile Documents/com~apple~CloudDocs/x", "iCloud Drive"),
        ("Dropbox/photos/x", "Dropbox"),
        ("OneDrive - Contoso/x", "OneDrive"),
        ("SynologyDrive/x", "Synology Drive"),
        ("Library/Application Support/sortandpost/libraries/x", None),
        ("Pictures/sortandpost", None),
    ],
)
def test_cloud_provider_from_well_known_locations(
    tmp_path: Path, rel: str, expected: str | None
) -> None:
    assert cloud_provider(tmp_path / rel, home=tmp_path) == expected


def test_cloud_provider_from_a_marker_at_the_sync_root(tmp_path: Path) -> None:
    root = tmp_path / "nas"
    (root / ".SynologyWorkingDirectory").mkdir(parents=True)
    assert cloud_provider(root / "a" / "b", home=tmp_path / "home") == "Synology Drive"
    assert cloud_provider(tmp_path / "elsewhere", home=tmp_path / "home") is None


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


def _legacy_install(home: Path) -> Paths:
    """A single-folder install inside Synology Drive with real-looking local/."""
    old = Paths(app_data=home / "Library/CloudStorage/SynologyDrive-me/Film/sortandpost")
    bootstrap(old)
    make_db(old.working_db, photos=["a", "b"], generation=7)
    batch = old.local / "trash" / "20261003-212006-178081"
    (batch / "files" / "0000").mkdir(parents=True)
    (batch / "files" / "0000" / "DSCF4965.JPG").write_bytes(b"jpeg" * 500)
    (batch / "manifest.json").write_text('{"version": 2}', encoding="utf-8")
    (old.local / "onboarding.json").write_text("{}", encoding="utf-8")
    (old.local / "ui_prefs.json").write_text('{"theme": "dark"}', encoding="utf-8")
    weights = old.local / "models" / "models--laion--clip" / "blob"
    weights.parent.mkdir(parents=True)
    weights.write_bytes(b"w" * 100)
    return old


def test_not_in_a_cloud_folder_is_left_alone(tmp_path: Path, home: Path) -> None:
    p = Paths(app_data=tmp_path / "plain")
    bootstrap(p)
    assert maybe_relocate(p, home=home) is None
    assert read_pointer() is None
    split = Paths.for_sync_dir(tmp_path / "sync")
    assert maybe_relocate(split, home=home) is None  # already split


def test_moves_local_out_and_keeps_this_machines_identity(home: Path) -> None:
    old = _legacy_install(home)
    device = json.loads(old.device_file.read_text(encoding="utf-8"))["device_id"]
    db_bytes = old.working_db.read_bytes()

    new = maybe_relocate(old, home=home)

    assert new is not None and new.is_split
    assert new.sync == old.sync  # the shared folder stays put and keeps syncing
    assert cloud_provider(new.local, home=home) is None
    assert json.loads(new.device_file.read_text(encoding="utf-8"))["device_id"] == device
    assert new.working_db.read_bytes() == db_bytes
    assert (new.local / "onboarding.json").exists()
    assert (new.local / "ui_prefs.json").read_text(encoding="utf-8") == '{"theme": "dark"}'
    moved_photo = new.local / "trash/20261003-212006-178081/files/0000/DSCF4965.JPG"
    assert moved_photo.read_bytes() == b"jpeg" * 500
    assert (new.models_dir / "models--laion--clip" / "blob").exists()
    assert not old.local.exists()  # nothing of this machine's left in the cloud folder
    ptr = read_pointer()
    assert ptr is not None and ptr.kind == "sync_dir" and ptr.path == old.sync
    rec = read_record(new)
    assert rec is not None and rec.provider == "Synology Drive"
    assert rec.old_kept_at is None and rec.leftovers == [] and not rec.db_salvaged
    assert not new.local.with_name(new.local.name + ".moving").exists()


@needs_chmod
def test_unreadable_journal_hands_the_db_to_recovery(home: Path) -> None:
    old = _legacy_install(home)
    journal = old.working_db.with_name("sortandpost.db-journal")
    journal.write_bytes(b"\0" * 4096)
    journal.chmod(0)

    new = maybe_relocate(old, home=home)

    assert new is not None
    assert not new.working_db.exists()
    salvaged = list((new.local / "salvage").glob("relocated-*.db"))
    assert len(salvaged) == 1
    rec = read_record(new)
    assert rec is not None and rec.db_salvaged


@needs_chmod
def test_a_trashed_photo_that_cant_be_copied_keeps_the_old_folder(home: Path) -> None:
    old = _legacy_install(home)
    stuck = old.local / "trash/20261003-212006-178081/files/0000/DSCF4965.JPG"
    stuck.chmod(0)

    new = maybe_relocate(old, home=home)

    assert new is not None
    rec = read_record(new)
    assert rec is not None and rec.leftovers and rec.old_kept_at
    kept = Path(rec.old_kept_at)
    assert kept.name.startswith("local.moved-")
    (kept / "trash/20261003-212006-178081/files/0000/DSCF4965.JPG").chmod(0o600)
    assert (kept / "trash/20261003-212006-178081/files/0000/DSCF4965.JPG").exists()


def test_not_moved_while_another_process_is_using_it(home: Path) -> None:
    old = _legacy_install(home)
    device = json.loads(old.device_file.read_text(encoding="utf-8"))["device_id"]
    from datetime import UTC, datetime

    old.lock_file.write_text(
        json.dumps(
            {
                "device_id": device,
                "hostname": "me",
                "pid": os.getppid(),  # alive, and not us
                "acquired_at": datetime.now(UTC).isoformat(),
                "heartbeat_at": datetime.now(UTC).isoformat(),
                "db_generation": 7,
            }
        ),
        encoding="utf-8",
    )
    assert maybe_relocate(old, home=home) is None
    assert old.working_db.exists()


def test_settings_resolve_relocates_a_remembered_cloud_folder(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _legacy_install(home)
    assert old.app_data is not None
    remember_app_data(old.app_data)
    monkeypatch.delenv("APP_DATA", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(home)  # no stray .env

    s = Settings.resolve()

    assert s.relocated
    assert s.paths.is_split and s.paths.sync == old.sync
    assert not s.onboarding_pending  # an existing install never sees the wizard
    assert Settings.resolve().relocated is False  # once only


def test_a_real_app_data_env_var_is_never_moved(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _legacy_install(home)
    monkeypatch.setenv("APP_DATA", str(old.app_data))
    monkeypatch.setenv("HOME", str(home))
    s = Settings.resolve()
    assert not s.relocated
    assert s.paths.local == old.local
