"""The optional host file-manager helper agent + its server-side reader."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from sortandpost.helper import read_helper_info
from sortandpost.paths import Paths

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import sortandpost_helper as agent

TOKEN = "test-token-abc"


@pytest.fixture
def app_data(tmp_path: Path) -> Path:
    root = tmp_path / "appdata"
    (root / "local").mkdir(parents=True)
    lib = tmp_path / "photos"
    (lib / "sub").mkdir(parents=True)
    (lib / ".hidden").mkdir()
    (lib / "a.jpg").write_bytes(b"x")
    dev = root / "sync" / "devices" / "box-abc123"
    dev.mkdir(parents=True)
    (dev / "library.json").write_text(
        json.dumps({"roots": [{"container": "/library", "host": str(lib)}]}),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def server(app_data: Path) -> Iterator[str]:
    config = agent.Config(app_data, TOKEN)
    httpd = agent._Server(("127.0.0.1", 0), lambda *a: agent.Handler(*a, config=config))
    Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()


def _req(
    base: str,
    path: str,
    *,
    method: str = "GET",
    token: str | None = TOKEN,
    body: dict | None = None,
) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = Request(f"{base}{path}", data=data, method=method)
    if token is not None:
        req.add_header("X-SP-Token", token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_health_needs_no_token_and_lists_roots(server: str, app_data: Path) -> None:
    code, body = _req(server, "/health", token=None)
    assert code == 200
    assert body["ok"] is True and body["version"]
    assert len(body["roots"]) == 1


def test_token_is_required(server: str) -> None:
    assert _req(server, "/reveal", method="POST", token=None, body={"path": "/x"})[0] == 401
    assert _req(server, "/reveal", method="POST", token="wrong", body={"path": "/x"})[0] == 403


def test_reveal_runs_the_opener_for_a_path_inside_a_root(
    server: str, app_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(agent, "_spawn", lambda args: calls.append(args))
    lib = json.loads((app_data / "sync/devices/box-abc123/library.json").read_text())["roots"][0][
        "host"
    ]
    code, body = _req(server, "/reveal", method="POST", body={"path": f"{lib}/a.jpg"})
    assert code == 200 and body["ok"] is True
    assert calls and str(Path(lib) / "a.jpg") in calls[0][-1]


def test_reveal_refuses_a_path_outside_every_root(
    server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent, "_spawn", lambda args: pytest.fail("must not spawn"))
    code, body = _req(server, "/reveal", method="POST", body={"path": "/etc/passwd"})
    assert code == 403
    assert "registered library folder" in body["error"]


def test_reveal_rejects_a_relative_path(server: str) -> None:
    assert _req(server, "/reveal", method="POST", body={"path": "relative/x"})[0] == 400


# --- multi-path reveal / open ------------------------------------------------


def test_reveal_multi_runs_the_multi_opener_for_paths_inside_a_root(
    server: str, app_data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[Path]] = []
    monkeypatch.setattr(agent, "reveal_multiple", lambda paths: calls.append(paths))
    lib = json.loads((app_data / "sync/devices/box-abc123/library.json").read_text())["roots"][0][
        "host"
    ]
    b = Path(lib) / "b.jpg"
    b.write_bytes(b"x")
    code, body = _req(server, "/reveal", method="POST", body={"paths": [f"{lib}/a.jpg", str(b)]})
    assert code == 200 and body["ok"] is True and body["count"] == 2
    assert len(calls) == 1 and len(calls[0]) == 2


def test_open_multi_runs_the_multi_opener(
    server: str, app_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[Path]] = []
    monkeypatch.setattr(agent, "open_multiple", lambda paths: calls.append(paths))
    lib = json.loads((app_data / "sync/devices/box-abc123/library.json").read_text())["roots"][0][
        "host"
    ]
    code, body = _req(server, "/open", method="POST", body={"paths": [f"{lib}/a.jpg"]})
    assert code == 200 and body["ok"] is True
    assert len(calls) == 1


def test_reveal_multi_refuses_if_any_path_is_outside_every_root(
    server: str, app_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent, "reveal_multiple", lambda paths: pytest.fail("must not spawn"))
    lib = json.loads((app_data / "sync/devices/box-abc123/library.json").read_text())["roots"][0][
        "host"
    ]
    code, _body = _req(
        server, "/reveal", method="POST", body={"paths": [f"{lib}/a.jpg", "/etc/passwd"]}
    )
    assert code == 403


def test_reveal_multi_skips_missing_files_but_keeps_the_rest(
    server: str, app_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[Path]] = []
    monkeypatch.setattr(agent, "reveal_multiple", lambda paths: calls.append(paths))
    lib = json.loads((app_data / "sync/devices/box-abc123/library.json").read_text())["roots"][0][
        "host"
    ]
    code, body = _req(
        server,
        "/reveal",
        method="POST",
        body={"paths": [f"{lib}/a.jpg", f"{lib}/does-not-exist.jpg"]},
    )
    assert code == 200 and body["count"] == 1
    assert len(calls[0]) == 1


def test_reveal_multi_rejects_empty_or_too_many(server: str, app_data: Path) -> None:
    assert _req(server, "/reveal", method="POST", body={"paths": []})[0] == 400
    lib = json.loads((app_data / "sync/devices/box-abc123/library.json").read_text())["roots"][0][
        "host"
    ]
    too_many = [f"{lib}/a.jpg"] * (agent._MAX_MULTI_PATHS + 1)
    assert _req(server, "/reveal", method="POST", body={"paths": too_many})[0] == 400


def test_listdir_lists_directories_only_and_hides_dotfiles(server: str, app_data: Path) -> None:
    lib = json.loads((app_data / "sync/devices/box-abc123/library.json").read_text())["roots"][0][
        "host"
    ]
    code, body = _req(server, f"/listdir?path={lib}")
    assert code == 200
    names = [e["name"] for e in body["entries"]]
    assert names == ["sub"]  # a.jpg (file) and .hidden (dot) excluded


def test_listdir_root_listing_returns_start_points(server: str) -> None:
    code, body = _req(server, "/listdir")
    assert code == 200 and body["parent"] is None
    assert any(e["dir"] for e in body["entries"])


def test_listdir_permission_error_is_empty(server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_self: object) -> Iterator[Path]:
        raise PermissionError

    monkeypatch.setattr(Path, "iterdir", boom)
    code, body = _req(server, "/listdir?path=/some/where")
    assert code == 200 and body["entries"] == []


# --- server-side reader ------------------------------------------------------


def test_read_helper_info_present_and_stale(tmp_path: Path) -> None:
    paths = Paths(app_data=tmp_path / "ad")
    paths.ensure_dirs()
    paths.helper_file.write_text(
        json.dumps({"port": 41847, "token": "t", "os": "darwin", "heartbeat_at": time.time()}),
        encoding="utf-8",
    )
    info = read_helper_info(paths)
    assert info is not None and info.port == 41847 and info.token == "t"

    paths.helper_file.write_text(
        json.dumps({"port": 41847, "token": "t", "heartbeat_at": time.time() - 9999}),
        encoding="utf-8",
    )
    assert read_helper_info(paths) is None


def test_read_helper_info_absent_or_bad(tmp_path: Path) -> None:
    paths = Paths(app_data=tmp_path / "ad")
    paths.ensure_dirs()
    assert read_helper_info(paths) is None
    paths.helper_file.write_text("{garbage", encoding="utf-8")
    assert read_helper_info(paths) is None
