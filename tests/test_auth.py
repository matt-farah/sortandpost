"""Server-mode guard (V3 P6b): Host / Origin checks, login, sessions, rate limit."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from sortandpost.auth import (
    GuardConfig,
    LoginLimiter,
    PasswordConfigError,
    PasswordHash,
    ServerGuard,
    Sessions,
    host_allowed,
    load_allowed_hosts,
    load_or_create_secret,
    load_password,
    safe_next,
    same_origin,
)

PASSWORD = "correct horse"
BASE = "http://localhost:8080"
WS = "ws://localhost:8080/_nicegui_ws/socket.io/"  # the ws client ignores base_url


def _app(tmp_path: Path, config: GuardConfig) -> TestClient:
    inner = FastAPI()
    thumbs = tmp_path / "thumbs"
    thumbs.mkdir(exist_ok=True)
    (thumbs / "a.webp").write_bytes(b"thumb")
    inner.mount("/thumbs", StaticFiles(directory=thumbs), name="thumbs")

    @inner.get("/")
    def _root() -> PlainTextResponse:
        return PlainTextResponse("library")

    @inner.get("/trash-thumb/{batch}/{h}")
    def _trash(batch: str, h: str) -> PlainTextResponse:
        return PlainTextResponse("trash thumb")

    @inner.get("/healthz")
    def _health() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @inner.get("/_desktop/status")
    def _desktop() -> PlainTextResponse:
        return PlainTextResponse("tray")

    @inner.get("/_nicegui_ws/socket.io/")
    def _poll() -> PlainTextResponse:
        return PlainTextResponse("0{}")

    @inner.post("/_nicegui/upload")
    def _upload() -> PlainTextResponse:
        return PlainTextResponse("uploaded")

    @inner.websocket("/_nicegui_ws/socket.io/")
    async def _ws(ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_text("hello")
        await ws.close()

    inner.add_middleware(ServerGuard, config=config)
    return TestClient(inner, base_url=BASE, follow_redirects=False)


def _locked(tmp_path: Path, **kw: object) -> tuple[TestClient, GuardConfig]:
    pw = PasswordHash.of(PASSWORD)
    cfg = GuardConfig(password=pw, sessions=Sessions("s" * 64, pw), cookie_name="sp", **kw)  # type: ignore[arg-type]
    return _app(tmp_path, cfg), cfg


def _open(tmp_path: Path, **kw: object) -> TestClient:
    return _app(tmp_path, GuardConfig(password=None, sessions=None, **kw))  # type: ignore[arg-type]


def _login(client: TestClient, password: str = PASSWORD, nxt: str = "/") -> object:
    return client.post("/login", data={"password": password, "next": nxt}, headers={"origin": BASE})


# --------------------------------------------------------------------------- #
# Login on
# --------------------------------------------------------------------------- #
def test_every_route_class_needs_a_session(tmp_path: Path) -> None:
    client, _ = _locked(tmp_path)
    page = client.get("/photo/abc?x=1", headers={"accept": "text/html"})
    assert page.status_code == 303
    assert page.headers["location"] == "/login?next=%2Fphoto%2Fabc%3Fx%3D1"
    assert client.get("/thumbs/a.webp").status_code == 401
    assert client.get("/trash-thumb/b/h").status_code == 401
    assert client.get("/_nicegui_ws/socket.io/").status_code == 401
    assert client.post("/_nicegui/upload", headers={"origin": BASE}).status_code == 401
    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(WS):
        pass
    assert exc.value.code == 1008


def test_open_paths_stay_open(tmp_path: Path) -> None:
    client, _ = _locked(tmp_path)
    assert client.get("/healthz").text == "ok"
    assert client.get("/_desktop/status").text == "tray"  # checks its own token
    login = client.get("/login")
    assert login.status_code == 200
    assert 'type="password"' in login.text


def test_login_then_everything_works_and_logout_revokes(tmp_path: Path) -> None:
    client, _ = _locked(tmp_path)
    res = _login(client, nxt="/photo/abc")
    assert res.status_code == 303
    assert res.headers["location"] == "/photo/abc"
    cookie = res.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Secure" not in cookie
    token = client.cookies.get("sp")
    assert token

    assert client.get("/").text == "library"
    assert client.get("/thumbs/a.webp").content == b"thumb"
    assert client.get("/trash-thumb/b/h").text == "trash thumb"
    with client.websocket_connect(WS) as ws:
        assert ws.receive_text() == "hello"
    # Already signed in: the login page sends you on.
    assert client.get("/login?next=/rate").headers["location"] == "/rate"

    out = client.post("/logout", headers={"origin": BASE})
    assert out.status_code == 303
    assert "Max-Age=0" in out.headers["set-cookie"]
    # The old token is revoked server-side too, not only deleted in the browser.
    client.cookies.set("sp", token)
    assert client.get("/thumbs/a.webp").status_code == 401


def test_wrong_password_and_rate_limit(tmp_path: Path) -> None:
    client, _ = _locked(tmp_path, limiter=LoginLimiter(max_failures=3, window_s=60))
    for _ in range(3):
        bad = _login(client, "nope")
        assert bad.status_code == 401
        assert "Wrong password" in bad.text
    blocked = _login(client)  # even the right password waits now
    assert blocked.status_code == 429
    assert "retry-after" in blocked.headers
    assert client.cookies.get("sp") is None


def test_limiter_window_and_success_reset() -> None:
    lim = LoginLimiter(max_failures=2, window_s=10)
    lim.failed("a", now=0)
    assert lim.retry_after("a", now=1) == 0
    lim.failed("a", now=1)
    assert lim.retry_after("a", now=2) == pytest.approx(8)
    assert lim.retry_after("b", now=2) == 0  # per client address
    assert lim.retry_after("a", now=11.5) == 0  # the first failure aged out
    lim.failed("a", now=12)
    lim.succeeded("a")
    assert lim.retry_after("a", now=12) == 0


def test_any_host_reaches_the_login_when_a_password_is_set(tmp_path: Path) -> None:
    client, _ = _locked(tmp_path)
    res = client.get("/", headers={"host": "nas.lan:8080", "accept": "text/html"})
    assert res.status_code == 303  # to /login, not refused


def test_secure_cookie_behind_https_proxy(tmp_path: Path) -> None:
    client, _ = _locked(tmp_path)
    res = client.post(
        "/login",
        data={"password": PASSWORD},
        headers={"origin": BASE, "x-forwarded-proto": "https"},
    )
    assert "Secure" in res.headers["set-cookie"]


def test_sessions_expire_and_follow_the_password() -> None:
    pw = PasswordHash.of(PASSWORD)
    s = Sessions("k" * 64, pw, max_age_s=100)
    tok = s.issue(now=1000)
    assert s.valid(tok, now=1050)
    assert not s.valid(tok, now=1101)
    assert not s.valid(tok[:-1] + ("0" if tok[-1] != "0" else "1"), now=1050)
    assert not s.valid("garbage", now=1050)
    # A new password (new hash) invalidates every old cookie.
    assert not Sessions("k" * 64, PasswordHash.of("other"), max_age_s=100).valid(tok, now=1050)
    # ...and so does a new secret.
    assert not Sessions("j" * 64, pw, max_age_s=100).valid(tok, now=1050)


def test_next_is_never_an_open_redirect() -> None:
    assert safe_next("/photo/x?y=1") == "/photo/x?y=1"
    for bad in (None, "", "https://evil.example", "//evil.example", "/\\evil.example", "rate"):
        assert safe_next(bad) == "/"


# --------------------------------------------------------------------------- #
# Login off: Host / Origin only
# --------------------------------------------------------------------------- #
def test_no_password_changes_nothing_on_localhost(tmp_path: Path) -> None:
    client = _open(tmp_path)
    assert client.get("/").text == "library"
    assert client.get("/thumbs/a.webp").content == b"thumb"
    with client.websocket_connect(WS) as ws:
        assert ws.receive_text() == "hello"
    assert client.get("/login").headers["location"] == "/"


def test_foreign_host_refused_without_password(tmp_path: Path) -> None:
    client = _open(tmp_path)
    res = client.get("/", headers={"host": "evil.example"})
    assert res.status_code == 403
    assert "SORTANDPOST_PASSWORD" in res.text
    assert client.get("/", headers={"host": "192.168.1.20:8080"}).status_code == 403
    for ok in ("localhost:8080", "127.0.0.1:8080", "[::1]:8080", "sortandpost.localhost:8081"):
        assert client.get("/", headers={"host": ok}).status_code == 200, ok


def test_allowed_hosts_env(tmp_path: Path) -> None:
    hosts = load_allowed_hosts({"SORTANDPOST_ALLOWED_HOSTS": " NAS.lan , photos.example. ,"})
    assert hosts == frozenset({"nas.lan", "photos.example"})
    client = _open(tmp_path, allowed_hosts=hosts)
    assert client.get("/", headers={"host": "nas.lan:8080"}).status_code == 200
    assert client.get("/", headers={"host": "other.lan"}).status_code == 403


def test_cross_site_websocket_and_posts_refused(tmp_path: Path) -> None:
    client = _open(tmp_path)
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(WS, headers={"origin": "https://evil.example"}),
    ):
        pass
    evil = {"origin": "https://evil.example"}
    assert client.post("/_nicegui/upload", headers=evil).status_code == 403
    assert client.get("/_nicegui_ws/socket.io/", headers=evil).status_code == 403
    # A cross-site GET of a page is harmless (no state change, can't be read).
    assert client.get("/", headers=evil).status_code == 200
    assert client.post("/_nicegui/upload", headers={"origin": BASE}).text == "uploaded"


def test_host_and_origin_helpers() -> None:
    assert host_allowed("LOCALHOST:8080")
    assert host_allowed("a.b.localhost")
    assert not host_allowed("")
    assert not host_allowed("localhost.evil.example")
    assert same_origin(None, "localhost:8080")
    assert same_origin("http://localhost:8080", "localhost:8080")
    assert not same_origin("http://localhost:9999", "localhost:8080")
    assert not same_origin("null", "localhost:8080")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def test_load_password_sources(tmp_path: Path) -> None:
    assert load_password({}) is None
    assert load_password({"SORTANDPOST_PASSWORD": ""}) is None
    env_pw = load_password({"SORTANDPOST_PASSWORD": "abc"})
    assert env_pw is not None and env_pw.verify("abc") and not env_pw.verify("abd")

    f = tmp_path / "pw"
    f.write_text("from file \n", encoding="utf-8")
    both = load_password({"SORTANDPOST_PASSWORD": "abc", "SORTANDPOST_PASSWORD_FILE": str(f)})
    assert both is not None and both.verify("from file ")  # file wins, keeps the space


def test_unreadable_password_file_refuses_to_start(tmp_path: Path) -> None:
    with pytest.raises(PasswordConfigError):
        load_password({"SORTANDPOST_PASSWORD_FILE": str(tmp_path / "missing")})
    empty = tmp_path / "empty"
    empty.write_text("\n", encoding="utf-8")
    with pytest.raises(PasswordConfigError):
        load_password({"SORTANDPOST_PASSWORD_FILE": str(empty)})


def test_secret_is_random_private_and_persists(tmp_path: Path) -> None:
    first = load_or_create_secret(tmp_path)
    assert len(first) == 64
    assert load_or_create_secret(tmp_path) == first  # survives a restart
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / "session_secret").stat().st_mode) == 0o600
    assert load_or_create_secret(tmp_path / "other") != first
