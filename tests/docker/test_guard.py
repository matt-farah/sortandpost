"""Layer 3 -- the server guard as a network client sees it through the published port
(docs/docker.md "Must-have"; sortandpost.auth.ServerGuard)."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from tests.docker import harness

_EVIL = "http://evil.example"
_SOCKET = "/_nicegui_ws/socket.io/?EIO=4&transport=polling"


def test_no_password_serves_localhost_only(make_stack: Callable[..., harness.Stack]) -> None:
    stack = make_stack().up()
    with httpx.Client(base_url=stack.url) as c:
        assert c.get("/").status_code == 200
        assert c.get("/", headers={"Host": f"nas.local:{stack.port}"}).status_code == 403
        assert c.get("/", headers={"Host": f"192.168.1.20:{stack.port}"}).status_code == 403
        # Cross-site: a socket.io handshake or a POST from another Origin.
        assert c.get(_SOCKET, headers={"Origin": _EVIL}).status_code == 403
        assert c.post("/login", headers={"Origin": _EVIL}).status_code == 403
        assert c.get("/healthz").text.strip() == "ok"


def test_allowed_hosts_lets_a_name_through(make_stack: Callable[..., harness.Stack]) -> None:
    stack = make_stack(env={"SORTANDPOST_ALLOWED_HOSTS": "nas.local"}).up()
    with httpx.Client(base_url=stack.url) as c:
        assert c.get("/", headers={"Host": f"nas.local:{stack.port}"}).status_code == 200
        assert c.get("/", headers={"Host": f"other.local:{stack.port}"}).status_code == 403


def test_password_gates_everything_but_login_and_health(
    make_stack: Callable[..., harness.Stack],
) -> None:
    stack = make_stack(env={"SORTANDPOST_PASSWORD": "correct horse"}).up()
    html = {"Accept": "text/html"}
    with httpx.Client(base_url=stack.url) as c:
        r = c.get("/settings", headers=html)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
        assert c.get("/thumbs/00/x_320.webp").status_code == 401
        assert c.get(_SOCKET).status_code == 401
        assert c.get("/healthz").status_code == 200
        # With a password any Host reaches the login page (a LAN name, a proxy).
        assert c.get("/login", headers={"Host": f"nas.local:{stack.port}"}).status_code == 200

        assert c.post("/login", data={"password": "nope"}).status_code == 401
        ok = c.post("/login", data={"password": "correct horse", "next": "/settings"})
        assert ok.status_code == 303 and ok.headers["location"] == "/settings"
        cookie = ok.headers["set-cookie"]
        assert "HttpOnly" in cookie and "sp_session_" in cookie
        assert c.get("/settings", headers=html).status_code == 200
        assert c.get(_SOCKET).status_code != 401

        # Sign out revokes the session.
        assert c.post("/logout").status_code == 303
        assert c.get("/settings", headers=html).status_code == 303


def test_wrong_passwords_are_rate_limited(make_stack: Callable[..., harness.Stack]) -> None:
    stack = make_stack(env={"SORTANDPOST_PASSWORD": "pw"}).up()
    with httpx.Client(base_url=stack.url) as c:
        codes = [c.post("/login", data={"password": f"bad{i}"}).status_code for i in range(6)]
        assert codes[:5] == [401] * 5
        assert codes[5] == 429
        # Even the right password waits now.
        assert c.post("/login", data={"password": "pw"}).status_code == 429


def test_password_file_wins_over_the_env_var(make_stack: Callable[..., harness.Stack]) -> None:
    stack = make_stack(
        env={
            "SORTANDPOST_PASSWORD": "from-env",
            "SORTANDPOST_PASSWORD_FILE": "/data/local/password.txt",
        }
    )
    (stack.local / "password.txt").write_text("from-file\n", encoding="utf-8")
    stack.up()
    with httpx.Client(base_url=stack.url) as c:
        assert c.post("/login", data={"password": "from-env"}).status_code == 401
        assert c.post("/login", data={"password": "from-file"}).status_code == 303


def test_unreadable_password_file_refuses_to_start(
    make_stack: Callable[..., harness.Stack],
) -> None:
    stack = make_stack(env={"SORTANDPOST_PASSWORD_FILE": "/data/local/missing.txt"})
    stack.up(wait=False)

    def _refused() -> bool:
        state = stack.inspect()["State"]
        return state["Status"] in ("exited", "restarting") or state.get("RestartCount", 0) > 0

    harness.wait_for(_refused, timeout=90, what="the container to refuse to start")
    assert "can't be read" in stack.logs()
    # and it never served the app in between
    try:
        r = httpx.get(stack.url + "/", timeout=2)
        assert r.status_code != 200
    except httpx.HTTPError:
        pass
