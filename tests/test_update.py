"""In-app updates (sortandpost.update): the signed manifest, the check, the
download, install-source rules, the tray's install hand-off and the
newer-schema guard."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sortandpost.desktop import control, runtime
from sortandpost.update import check, source
from sortandpost.update.check import (
    Updater,
    download_asset,
    make_client,
    take_pending,
    take_result,
    write_pending,
    write_result,
)
from sortandpost.update.manifest import (
    Asset,
    ManifestError,
    is_newer,
    load_verified,
    parse_manifest,
    parse_version,
)

BASE = "https://github.com/matt-farah/sortandpost/releases/latest/download/"
PAYLOAD = b"new app bytes" * 100


def _key() -> tuple[Ed25519PrivateKey, str]:
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return key, base64.b64encode(pub).decode()


def _manifest(version: str = "9.0.0", payload: bytes = PAYLOAD) -> bytes:
    asset = {
        "url": f"https://github.com/matt-farah/sortandpost/releases/download/v{version}/app.dmg",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }
    keys = ("macos-arm64", "windows-x64", "linux-x86_64", "linux-aarch64")
    return json.dumps(
        {"version": version, "notes": "## New", "assets": dict.fromkeys(keys, asset)}
    ).encode()


def _sign(key: Ed25519PrivateKey, data: bytes) -> bytes:
    return base64.b64encode(key.sign(data))


@pytest.fixture
def signer(monkeypatch: pytest.MonkeyPatch) -> Ed25519PrivateKey:
    key, pub = _key()
    monkeypatch.setattr(check.pubkey, "PUBLIC_KEYS", (pub,))
    monkeypatch.delenv(check.PUBKEY_ENV, raising=False)
    monkeypatch.delenv(check.BASE_URL_ENV, raising=False)
    monkeypatch.delenv(check.DISABLE_ENV, raising=False)
    return key


def _serve(files: dict[str, bytes], seen: list[str] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        body = files.get(str(request.url))
        return httpx.Response(404) if body is None else httpx.Response(200, content=body)

    return make_client(httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #
def test_versions() -> None:
    assert parse_version("v3.1.0") == (3, 1, 0)
    assert parse_version("3.1.0-rc1") is None  # pre-releases are never offered
    assert is_newer("2.10.0", "2.9.9")
    assert not is_newer("2.0.0", "2.0.0")
    assert not is_newer("junk", "2.0.0")


def test_signature_must_match_a_trusted_key() -> None:
    key, pub = _key()
    _other, other_pub = _key()
    data = _manifest()
    assert load_verified(data, _sign(key, data), (pub,)).version == "9.0.0"
    assert load_verified(data, _sign(key, data), (other_pub, pub)).version == "9.0.0"  # rotation
    with pytest.raises(ManifestError, match="doesn't match"):
        load_verified(data, _sign(key, data), (other_pub,))
    with pytest.raises(ManifestError, match="doesn't match"):
        load_verified(data + b" ", _sign(key, data), (pub,))  # tampered
    with pytest.raises(ManifestError, match="no update signing key"):
        load_verified(data, _sign(key, data), ())


@pytest.mark.parametrize(
    ("field", "value"),
    [("url", "http://github.com/x.dmg"), ("sha256", "abc"), ("size", 0), ("size", True)],
)
def test_bad_assets_are_refused(field: str, value: object) -> None:
    raw = json.loads(_manifest())
    raw["assets"]["macos-arm64"][field] = value
    with pytest.raises(ManifestError):
        parse_manifest(json.dumps(raw).encode())


def test_only_github_hosts_are_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(check.BASE_URL_ENV, raising=False)
    assert check.allowed_url(BASE + "latest.json")
    assert check.allowed_url("https://release-assets.githubusercontent.com/x")
    assert not check.allowed_url("http://github.com/x")
    assert not check.allowed_url("https://evil.example/x")
    monkeypatch.setenv(check.BASE_URL_ENV, "http://127.0.0.1:8765/")
    assert check.allowed_url("http://127.0.0.1:8765/latest.json")
    monkeypatch.setenv(check.BASE_URL_ENV, "http://lan-box:8765/")
    assert not check.allowed_url("http://lan-box:8765/latest.json")  # http only on loopback


def test_a_redirect_off_github_is_refused(signer: Ed25519PrivateKey) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://evil.example/latest.json"})

    with pytest.raises(httpx.HTTPError):
        check.fetch_latest(make_client(httpx.MockTransport(handler)))


def test_pubkey_override_is_ignored_in_a_frozen_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(check.PUBKEY_ENV, "dGVzdA==")
    assert check.trusted_keys() == ("dGVzdA==",)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert check.trusted_keys() == check.pubkey.PUBLIC_KEYS


# --------------------------------------------------------------------------- #
# the checker + its state
# --------------------------------------------------------------------------- #
def test_check_finds_a_newer_release_and_remembers_it(
    tmp_path: Path, signer: Ed25519PrivateKey
) -> None:
    data = _manifest("9.0.0")
    client = _serve({BASE + "latest.json": data, BASE + "latest.json.sig": _sign(signer, data)})
    up = Updater(tmp_path, current="2.0.0")
    assert up.available() is None and up.due()
    m = up.check_now(client)
    assert m is not None and m.version == "9.0.0" and m.notes == "## New"
    assert not up.due() and up.last_error == ""

    again = Updater(tmp_path, current="2.0.0")  # a restart re-verifies the saved copy
    assert again.available() is not None
    assert Updater(tmp_path, current="9.0.0").available() is None  # already on it

    again.skip("9.0.0")
    assert Updater(tmp_path, current="2.0.0").available() is None
    assert Updater(tmp_path, current="2.0.0").latest() is not None  # Settings still shows it


def test_a_tampered_saved_manifest_is_dropped(tmp_path: Path, signer: Ed25519PrivateKey) -> None:
    data = _manifest()
    client = _serve({BASE + "latest.json": data, BASE + "latest.json.sig": _sign(signer, data)})
    Updater(tmp_path, current="2.0.0").check_now(client)
    state = json.loads((tmp_path / "state.json").read_text())
    evil = json.loads(data)
    evil["assets"]["macos-arm64"]["url"] = "https://github.com/evil/x.dmg"
    state["manifest"] = base64.b64encode(json.dumps(evil).encode()).decode()
    (tmp_path / "state.json").write_text(json.dumps(state))
    assert Updater(tmp_path, current="2.0.0").latest() is None


def test_a_failed_check_is_recorded_and_retried_later(
    tmp_path: Path, signer: Ed25519PrivateKey
) -> None:
    up = Updater(tmp_path, current="2.0.0")
    assert up.check_now(_serve({})) is None
    assert "404" in up.last_error
    now = datetime.now(UTC)
    assert not up.due(now)
    assert up.due(now + check.RETRY_AFTER_FAILURE + timedelta(minutes=1))


def test_switching_the_check_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    up = Updater(tmp_path)
    up.set_enabled(False)
    assert not Updater(tmp_path).due()
    up.set_enabled(True)
    monkeypatch.setenv(check.DISABLE_ENV, "1")
    assert not Updater(tmp_path).due()


# --------------------------------------------------------------------------- #
# download
# --------------------------------------------------------------------------- #
URL = "https://github.com/matt-farah/sortandpost/releases/download/v9.0.0/app.dmg"


def _asset(payload: bytes = PAYLOAD, **kw: object) -> Asset:
    fields: dict[str, object] = {
        "url": URL,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }
    fields.update(kw)
    return Asset(**fields)  # type: ignore[arg-type]


def test_download_is_verified(tmp_path: Path) -> None:
    seen: list[int] = []
    path = download_asset(
        _asset(),
        tmp_path,
        client=_serve({URL: PAYLOAD}),
        progress=lambda done, _total: seen.append(done),
    )
    assert path.read_bytes() == PAYLOAD and seen[-1] == len(PAYLOAD)
    # A second try reuses the verified file without fetching.
    assert download_asset(_asset(), tmp_path, client=_serve({})) == path


@pytest.mark.parametrize(
    ("served", "match"),
    [
        (PAYLOAD[:-1] + b"X", "checksum"),
        (PAYLOAD + b"more", "larger"),
        (PAYLOAD[:10], "cut short"),
    ],
)
def test_a_bad_download_leaves_nothing(tmp_path: Path, served: bytes, match: str) -> None:
    with pytest.raises(ManifestError, match=match):
        download_asset(_asset(), tmp_path, client=_serve({URL: served}))
    assert list(tmp_path.iterdir()) == []


def test_pending_and_result_notes(tmp_path: Path) -> None:
    f = tmp_path / "app.dmg"
    f.write_bytes(PAYLOAD)
    sha = hashlib.sha256(PAYLOAD).hexdigest()
    write_pending(check.PendingUpdate(str(f), "9.0.0", sha), tmp_path)
    got = take_pending(tmp_path)
    assert got is not None and got.version == "9.0.0"
    assert take_pending(tmp_path) is None  # read once

    write_pending(check.PendingUpdate(str(f), "9.0.0", sha), tmp_path)
    f.write_bytes(b"swapped after the check")
    assert take_pending(tmp_path) is None  # changed since verified: not installed

    write_result(False, "disk full", tmp_path)
    assert take_result(tmp_path) == (False, "disk full")
    assert take_result(tmp_path) is None
    (tmp_path / check.RESULT_NAME).write_text("ok 9.0.0 \r\n")  # what the .cmd helper writes
    assert take_result(tmp_path) == (True, "9.0.0")


# --------------------------------------------------------------------------- #
# who may self-update
# --------------------------------------------------------------------------- #
def test_install_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source, "running_in_docker", lambda: False)
    assert source.install_source() == "source"
    assert not source.can_self_update()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    exe = tmp_path / "bin" / "sortandpost"
    exe.parent.mkdir()
    exe.touch()
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(source, "_BREW_PREFIXES", (tmp_path / "brew",))
    monkeypatch.delenv("HOMEBREW_PREFIX", raising=False)
    if sys.platform != "win32":
        assert source.install_source() == "direct"
    (exe.parent / source.SOURCE_FILE).write_text("apt\n")
    assert source.install_source() == "apt"
    assert not source.can_self_update("apt")
    assert "apt" in source.manual_hint("apt")

    monkeypatch.setattr(source, "running_in_docker", lambda: True)
    assert source.install_source() == "docker"


@pytest.mark.skipif(sys.platform != "darwin", reason="Homebrew cask detection is macOS-only")
def test_a_brew_install_still_self_updates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source, "running_in_docker", lambda: False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "x"))
    (tmp_path / "brew" / "Caskroom" / "sortandpost").mkdir(parents=True)
    monkeypatch.setattr(source, "_BREW_PREFIXES", (tmp_path / "brew",))
    monkeypatch.delenv("HOMEBREW_PREFIX", raising=False)
    assert source.install_source() == "brew"
    monkeypatch.setenv(control.TOKEN_ENV, "t")
    monkeypatch.setattr(source, "install_target", lambda: tmp_path / "sortandpost.app")
    assert source.can_self_update("brew")  # the cask says auto_updates true


def test_self_update_needs_the_tray(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source, "install_target", lambda: tmp_path / "sortandpost.app")
    monkeypatch.delenv(control.TOKEN_ENV, raising=False)
    assert not source.can_self_update("direct")
    monkeypatch.setenv(control.TOKEN_ENV, "t")
    assert source.can_self_update("direct")


# --------------------------------------------------------------------------- #
# server -> tray hand-off
# --------------------------------------------------------------------------- #
def test_install_request_exits_for_the_tray(monkeypatch: pytest.MonkeyPatch) -> None:
    from sortandpost import app, update_ui

    monkeypatch.setenv(control.TOKEN_ENV, "t0k")
    monkeypatch.setattr(update_ui.INSTALL_REQUESTED, "is_set", lambda: True)
    monkeypatch.setattr(app.logging, "shutdown", lambda: None)
    monkeypatch.setattr(app.os, "execv", lambda *_a: pytest.fail("must not exec"))
    with pytest.raises(SystemExit) as exc:
        app._reexec_if_requested()
    assert exc.value.code == control.UPDATE_EXIT_CODE


def _exiting_server(tmp_path: Path, code: int):  # type: ignore[no-untyped-def]
    from sortandpost.desktop.supervisor import ServerProcess

    server = ServerProcess(port=1, token="t", log_file=tmp_path / "out.log")
    server.proc = subprocess.Popen([sys.executable, "-c", f"raise SystemExit({code})"])
    return server


def test_tray_starts_the_installer_then_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sortandpost.desktop.tray import TrayApp
    from sortandpost.update import apply

    pending = check.PendingUpdate("/x.dmg", "9.0.0", "0" * 64)
    monkeypatch.setattr(check, "take_pending", lambda: pending)
    monkeypatch.setattr(source, "install_target", lambda: tmp_path / "sortandpost.app")
    launched: list[tuple[object, Path]] = []
    monkeypatch.setattr(
        apply, "launch_installer", lambda p, t, *, tray_pid: launched.append((p, t))
    )
    tray = TrayApp(
        _exiting_server(tmp_path, control.UPDATE_EXIT_CODE),
        runtime.instance_lock(tmp_path),
        headless=True,
    )
    restarted: list[bool] = []
    monkeypatch.setattr(tray, "_restart_server", lambda *, open_browser: restarted.append(True))
    tray._watch_for_restart()
    assert tray._shutdown_done.wait(5)
    assert launched == [(pending, tmp_path / "sortandpost.app")] and not restarted
    tray._poll_once()
    assert not tray.crashed


def test_tray_keeps_the_old_version_if_the_update_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sortandpost.desktop.tray import TrayApp

    monkeypatch.setattr(check, "take_pending", lambda: None)
    written: list[tuple[bool, str]] = []
    monkeypatch.setattr(check, "write_result", lambda ok, text: written.append((ok, text)))
    tray = TrayApp(
        _exiting_server(tmp_path, control.UPDATE_EXIT_CODE),
        runtime.instance_lock(tmp_path),
        headless=True,
    )
    restarted: list[bool] = []
    monkeypatch.setattr(tray, "_restart_server", lambda *, open_browser: restarted.append(True))
    tray._watch_for_restart()
    deadline = time.monotonic() + 5
    while not restarted and time.monotonic() < deadline:
        time.sleep(0.05)
    assert restarted == [True] and not tray.quitting
    assert written and written[0][0] is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell helper")
def test_linux_helper_swaps_the_appimage(tmp_path: Path) -> None:
    from sortandpost.update import apply

    target = tmp_path / "sortandpost.AppImage"
    target.write_text("#!/bin/sh\necho old\n")
    new = tmp_path / "new.AppImage"
    new.write_text('#!/bin/sh\necho started > "$(dirname "$0")/started"\n')
    u = tmp_path / "u"
    script = apply._write_script(u, "update-linux.sh", apply._LINUX_SH)
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    subprocess.run(
        ["/bin/sh", str(script), str(gone.pid), str(new), str(target), "9.0.0", str(u)],
        check=True,
        timeout=30,
    )
    assert target.read_text() == new.read_text()
    assert take_result(u) == (True, "9.0.0")
    deadline = time.monotonic() + 5
    while not (tmp_path / "started").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (tmp_path / "started").exists()  # the new version was started


# --------------------------------------------------------------------------- #
# a newer app upgraded the library
# --------------------------------------------------------------------------- #
def test_newer_schema_keeps_this_machine_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from sortandpost.config import Settings
    from sortandpost.db.meta import SCHEMA_VERSION
    from sortandpost.sync.manager import SyncManager, WriteMode
    from sortandpost.sync.publish import read_meta_int
    from tests.factories import make_db

    monkeypatch.setenv("APP_DATA", str(tmp_path / "a"))
    settings = Settings.resolve()
    make_db(settings.paths.working_db, photos=["a"])

    mgr = SyncManager(settings)
    assert mgr.start(background=False) is WriteMode.writable
    assert read_meta_int(settings.paths.published_db, "schema_version") == SCHEMA_VERSION
    mgr.stop()

    with sqlite3.connect(settings.paths.published_db) as conn:
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION + 1),)
        )
    mgr = SyncManager(settings)
    assert mgr.start(background=False) is WriteMode.read_only
    assert mgr.needs_update and "update this computer" in (mgr.writer_error or "")
    mgr.stop()


@pytest.mark.skipif(sys.platform != "darwin", reason="hdiutil")
@pytest.mark.parametrize("good", [True, False])
def test_macos_helper_swaps_the_app_from_the_dmg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, good: bool
) -> None:
    from sortandpost.update import apply

    stage = tmp_path / "stage"
    app_dir = stage / ("sortandpost.app" if good else "Other.app") / "Contents"
    app_dir.mkdir(parents=True)
    (app_dir / "version").write_text("new")
    dmg = tmp_path / "new.dmg"
    subprocess.run(
        ["hdiutil", "create", "-quiet", "-srcfolder", str(stage), "-format", "UDZO", str(dmg)],
        check=True,
    )
    target = tmp_path / "Applications" / "sortandpost.app"
    (target / "Contents").mkdir(parents=True)
    (target / "Contents" / "version").write_text("old")
    opened = tmp_path / "opened"
    monkeypatch.setenv("SORTANDPOST_UPDATE_OPEN", f"touch {opened}")
    u = tmp_path / "u"
    script = apply._write_script(u, "update-macos.sh", apply._MACOS_SH)
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    subprocess.run(
        ["/bin/sh", str(script), str(gone.pid), str(dmg), str(target), "9.0.0", str(u)],
        timeout=60,
    )
    want = "new" if good else "old"  # a bad update leaves the old app in place
    assert (target / "Contents" / "version").read_text() == want
    assert not (target.parent / "sortandpost.app.new").exists()
    assert not (target.parent / "sortandpost.app.old").exists()
    result = take_result(u)
    assert result is not None and result[0] is good
    assert opened.exists()  # relaunched either way
    assert not (u / "mnt").is_dir() or not any((u / "mnt").iterdir())  # unmounted
