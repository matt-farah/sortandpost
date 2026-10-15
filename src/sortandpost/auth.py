"""Server-mode protection (V3 P6b; Security.md H1 / M1 / L1 / L3).

One pure-ASGI middleware, :class:`ServerGuard`, sits in front of the whole app,
so it sees plain HTTP **and** websocket connections -- NiceGUI's pages, the
``/thumbs`` mount, the trash-thumbnail route, uploads and the socket.io endpoint
(``/_nicegui_ws/``) all pass through it. It does three things:

1. **Host allowlist** (always on, M1 -- DNS rebinding): without a password only
   ``localhost`` / ``127.0.0.1`` / ``[::1]`` / ``*.localhost`` and the names in
   ``SORTANDPOST_ALLOWED_HOSTS`` may reach the app. With a password, any Host may
   reach the login page.
2. **Origin check** (always on, M1 -- cross-site websocket / requests): a
   websocket, a socket.io request or any non-GET request whose ``Origin`` isn't
   this same site is refused. NiceGUI's socket.io server itself allows every origin.
3. **Login** (only when a password is configured -- ``SORTANDPOST_PASSWORD`` or
   ``SORTANDPOST_PASSWORD_FILE``): one shared password, kept only as a scrypt
   hash in memory. A signed session cookie (HMAC with a random per-machine secret
   in ``local/session_secret``, never the device id -- L1) gates everything but
   ``/login``, ``/healthz`` and the desktop tray's token-gated ``/_desktop/*``.
   Failed logins are rate-limited per client address (L3).

The login page is plain server-rendered HTML (no NiceGUI, so no socket.io and no
assets need opening up before a login). The desktop app and dev runs set no
password, so for them only the Host / Origin checks apply.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import logging
import os
import secrets
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from sortandpost.themes import SeasonTokens

log = logging.getLogger(__name__)

PASSWORD_ENV = "SORTANDPOST_PASSWORD"
PASSWORD_FILE_ENV = "SORTANDPOST_PASSWORD_FILE"
ALLOWED_HOSTS_ENV = "SORTANDPOST_ALLOWED_HOSTS"
SECRET_FILE = "session_secret"

LOGIN_PATH = "/login"
LOGOUT_PATH = "/logout"
# Reachable without a session: the login form itself, the Docker healthcheck, and
# the desktop tray's control API (which checks its own token).
_OPEN_PATHS = frozenset({LOGIN_PATH, "/healthz", "/favicon.ico"})
_OPEN_PREFIXES = ("/_desktop/",)
_SOCKET_PREFIX = "/_nicegui_ws/"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

SESSION_MAX_AGE_S = 30 * 24 * 3600
_MAX_FORM_BYTES = 4096

# scrypt cost: ~50 ms per check, so a login is cheap but guessing is not.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class PasswordConfigError(RuntimeError):
    """A password file was named but can't be read. Starting anyway would leave
    the server open, so the app refuses to start."""


# --------------------------------------------------------------------------- #
# Password
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PasswordHash:
    salt: bytes
    digest: bytes

    @classmethod
    def of(cls, password: str) -> PasswordHash:
        salt = secrets.token_bytes(16)
        return cls(salt=salt, digest=_scrypt(password, salt))

    def verify(self, password: str) -> bool:
        return hmac.compare_digest(_scrypt(password, self.salt), self.digest)


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )


def load_password(environ: MutableMapping[str, str] | None = None) -> PasswordHash | None:
    """The configured password, hashed, or ``None`` (login off). The file
    (``SORTANDPOST_PASSWORD_FILE``, a Docker secret) wins over the plain env var.
    Only one trailing newline is dropped from a file, so a password may end in
    spaces."""
    env = os.environ if environ is None else environ
    file_name = env.get(PASSWORD_FILE_ENV, "").strip()
    if file_name:
        try:
            raw = Path(file_name).read_text(encoding="utf-8")
        except OSError as exc:
            raise PasswordConfigError(
                f"{PASSWORD_FILE_ENV} is set but {file_name} can't be read ({exc.strerror}); "
                "refusing to start without the login"
            ) from exc
        password = raw[:-1] if raw.endswith("\n") else raw
        password = password[:-1] if password.endswith("\r") else password
        if not password:
            raise PasswordConfigError(f"{PASSWORD_FILE_ENV} ({file_name}) is empty")
        return PasswordHash.of(password)
    password = env.get(PASSWORD_ENV, "")
    return PasswordHash.of(password) if password else None


def load_allowed_hosts(environ: MutableMapping[str, str] | None = None) -> frozenset[str]:
    env = os.environ if environ is None else environ
    raw = env.get(ALLOWED_HOSTS_ENV, "")
    return frozenset(h.strip().lower().rstrip(".") for h in raw.split(",") if h.strip())


# --------------------------------------------------------------------------- #
# Per-machine secret (L1)
# --------------------------------------------------------------------------- #
def load_or_create_secret(local_dir: Path) -> str:
    """A random secret in ``local/session_secret`` (0600), created once. Signs
    the login cookie and NiceGUI's storage cookie. It lives in ``local/``, which
    is never synced, unlike the device id that shows up in ``sync/``."""
    path = local_dir / SECRET_FILE
    try:
        value = path.read_text(encoding="utf-8").strip()
        if len(value) >= 32:
            return value
    except FileNotFoundError:
        pass
    local_dir.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(32)
    tmp = path.with_name(f".{SECRET_FILE}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return value


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
class Sessions:
    """Signed, expiring session tokens: ``v1.<issued>.<nonce>.<sig>``.

    The signing key mixes in the password hash, so changing the password logs
    every browser out. Logging out revokes that token's nonce (in memory; a
    restart forgets revocations, but the cookie is deleted in the browser too).
    """

    def __init__(self, secret: str, password: PasswordHash, *, max_age_s: int = SESSION_MAX_AGE_S):
        self._key = hmac.new(
            secret.encode(), b"sortandpost-session\0" + password.digest, hashlib.sha256
        ).digest()
        self._max_age = max_age_s
        self._revoked: set[str] = set()
        self._lock = threading.Lock()

    @property
    def max_age_s(self) -> int:
        return self._max_age

    def _sign(self, body: str) -> str:
        return hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()

    def issue(self, now: float | None = None) -> str:
        body = f"v1.{int(time.time() if now is None else now)}.{secrets.token_hex(16)}"
        return f"{body}.{self._sign(body)}"

    def valid(self, token: str | None, now: float | None = None) -> bool:
        if not token:
            return False
        parts = token.split(".")
        if len(parts) != 4 or parts[0] != "v1":
            return False
        body, sig = ".".join(parts[:3]), parts[3]
        if not hmac.compare_digest(self._sign(body), sig):
            return False
        try:
            issued = int(parts[1])
        except ValueError:
            return False
        age = (time.time() if now is None else now) - issued
        if age < -60 or age > self._max_age:
            return False
        with self._lock:
            return parts[2] not in self._revoked

    def revoke(self, token: str | None) -> None:
        if token and self.valid(token):
            with self._lock:
                self._revoked.add(token.split(".")[2])


# --------------------------------------------------------------------------- #
# Login rate limiting (L3)
# --------------------------------------------------------------------------- #
@dataclass
class LoginLimiter:
    """At most ``max_failures`` wrong passwords per client address in
    ``window_s``; then that address waits until the oldest failure ages out."""

    max_failures: int = 5
    window_s: float = 300.0
    _fails: dict[str, deque[float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def retry_after(self, client: str, now: float | None = None) -> float:
        """Seconds until *client* may try again (0 = now)."""
        t = time.monotonic() if now is None else now
        with self._lock:
            q = self._fails.get(client)
            if not q:
                return 0.0
            while q and t - q[0] > self.window_s:
                q.popleft()
            if len(q) < self.max_failures:
                return 0.0
            return max(0.0, self.window_s - (t - q[0]))

    def failed(self, client: str, now: float | None = None) -> None:
        t = time.monotonic() if now is None else now
        with self._lock:
            if len(self._fails) > 10_000:  # never grows without bound
                self._fails.clear()
            self._fails.setdefault(client, deque(maxlen=self.max_failures)).append(t)

    def succeeded(self, client: str) -> None:
        with self._lock:
            self._fails.pop(client, None)


# --------------------------------------------------------------------------- #
# Host / Origin (M1)
# --------------------------------------------------------------------------- #
_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})


def host_name(host_header: str) -> str:
    """``Host`` header -> bare lower-case name (no port, no IPv6 brackets)."""
    h = host_header.strip().lower()
    if h.startswith("["):
        end = h.find("]")
        return h[1:end] if end > 0 else h
    name = h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    return name.rstrip(".")


def is_loopback_host(host_header: str) -> bool:
    name = host_name(host_header)
    return name in _LOOPBACK_NAMES or name.endswith(".localhost")


def host_allowed(host_header: str, extra: Iterable[str] = ()) -> bool:
    if not host_header:
        return False
    return is_loopback_host(host_header) or host_name(host_header) in set(extra)


def same_origin(origin: str | None, host_header: str) -> bool:
    """True when there's no ``Origin`` (not a browser cross-site request) or it
    names this same host:port."""
    if origin is None:
        return True
    if origin == "null" or not host_header:
        return False
    netloc = urlsplit(origin).netloc.lower()
    return netloc == host_header.strip().lower()


# --------------------------------------------------------------------------- #
# Login page (plain HTML)
# --------------------------------------------------------------------------- #
def login_page_html(
    *,
    light: SeasonTokens,
    dark: SeasonTokens,
    error: str | None = None,
    next_path: str = "/",
) -> str:
    def tokens(t: SeasonTokens) -> str:
        return (
            f"--bg:{t.bg};--surface:{t.surface};--border:{t.border};--text:{t.text};"
            f"--dim:{t.text_dim};--primary:{t.primary};--on-primary:{t.on_primary};"
        )

    err = f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in · sortandpost</title>
<style>
:root{{{tokens(light)}color-scheme:light}}
@media (prefers-color-scheme: dark){{:root{{{tokens(dark)}color-scheme:dark}}}}
*{{box-sizing:border-box}}
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:var(--bg);color:var(--text);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
padding:16px}}
form{{width:100%;max-width:360px;background:var(--surface);border:1px solid var(--border);
border-radius:12px;padding:28px}}
h1{{margin:0 0 4px;font-size:22px}}
p{{margin:0 0 18px;color:var(--dim);font-size:14px}}
label{{display:block;font-size:14px;margin-bottom:6px}}
input{{width:100%;padding:10px 12px;font:inherit;color:var(--text);background:var(--bg);
border:1px solid var(--border);border-radius:8px}}
input:focus{{outline:2px solid var(--primary);outline-offset:1px}}
button{{margin-top:16px;width:100%;padding:10px;font:inherit;font-weight:600;border:0;
border-radius:8px;background:var(--primary);color:var(--on-primary);cursor:pointer}}
.err{{color:#c0392b;margin:12px 0 0}}
</style></head>
<body><form method="post" action="{LOGIN_PATH}">
<h1>sortandpost.photos</h1>
<p>Enter the password to open this library.</p>
<label for="pw">Password</label>
<input id="pw" name="password" type="password" autocomplete="current-password" autofocus required>
<input type="hidden" name="next" value="{html.escape(next_path, quote=True)}">
<button type="submit">Sign in</button>
{err}
</form></body></html>"""


_HOST_REFUSED_HTML = """<!doctype html><meta charset="utf-8"><title>Not allowed</title>
<body style="font:16px/1.5 system-ui,sans-serif;max-width:560px;margin:10vh auto;padding:16px">
<h1 style="font-size:20px">This address isn't allowed</h1>
<p>sortandpost only answers on <code>localhost</code> until it has a password. To open it from
other devices, set <code>SORTANDPOST_PASSWORD</code> (or <code>SORTANDPOST_PASSWORD_FILE</code>),
or add this name to <code>SORTANDPOST_ALLOWED_HOSTS</code> on a network you trust.</p>"""


# --------------------------------------------------------------------------- #
# The middleware
# --------------------------------------------------------------------------- #
@dataclass
class GuardConfig:
    password: PasswordHash | None
    sessions: Sessions | None
    allowed_hosts: frozenset[str] = frozenset()
    cookie_name: str = "sp_session"
    limiter: LoginLimiter = field(default_factory=LoginLimiter)
    # (light, dark) tokens for the login page -- read per request so it follows
    # the machine's season.
    page_tokens: Callable[[], tuple[SeasonTokens, SeasonTokens]] | None = None

    @property
    def login_required(self) -> bool:
        return self.password is not None and self.sessions is not None


def safe_next(raw: str | None) -> str:
    """Only a local path: never ``//host`` or a full URL (no open redirect)."""
    if not raw or not raw.startswith("/") or raw.startswith("//") or "\\" in raw:
        return "/"
    return raw


class ServerGuard:
    def __init__(self, app: ASGIApp, config: GuardConfig) -> None:
        self.app = app
        self.config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        cfg = self.config
        headers = _headers(scope)
        host = headers.get("host", "")
        path: str = scope.get("path", "")
        method: str = scope.get("method", "GET") if kind == "http" else "GET"

        if not cfg.login_required and not host_allowed(host, cfg.allowed_hosts):
            log.warning("refused a request for Host %r (not in the allowlist)", host)
            await _refuse(kind, scope, receive, send, 403, _HOST_REFUSED_HTML, html_body=True)
            return

        cross_site_risk = (
            kind == "websocket" or method not in _SAFE_METHODS or path.startswith(_SOCKET_PREFIX)
        )
        if cross_site_risk and not same_origin(headers.get("origin"), host):
            log.warning("refused a cross-site %s %s from %r", method, path, headers.get("origin"))
            await _refuse(kind, scope, receive, send, 403, "cross-site request refused")
            return

        if kind == "http" and path in (LOGIN_PATH, LOGOUT_PATH):
            await self._login_routes(scope, receive, send, headers, path, method)
            return

        if not cfg.login_required or path in _OPEN_PATHS or path.startswith(_OPEN_PREFIXES):
            await self.app(scope, receive, send)
            return

        assert cfg.sessions is not None
        if cfg.sessions.valid(_cookie(headers, cfg.cookie_name)):
            await self.app(scope, receive, send)
            return

        wants_page = "text/html" in headers.get("accept", "")
        if kind == "http" and method in ("GET", "HEAD") and wants_page:
            qs = scope.get("query_string", b"").decode("latin-1")
            target = path + (f"?{qs}" if qs else "")
            await _send_http(send, 303, b"", [(b"location", _login_url(target).encode())])
            return
        await _refuse(kind, scope, receive, send, 401, "login required")

    async def _login_routes(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        headers: dict[str, str],
        path: str,
        method: str,
    ) -> None:
        cfg = self.config
        if not cfg.login_required:
            await _send_http(send, 303, b"", [(b"location", b"/")])
            return
        assert cfg.sessions is not None and cfg.password is not None
        token = _cookie(headers, cfg.cookie_name)

        if path == LOGOUT_PATH:
            if method != "POST":
                await _send_http(send, 405, b"use POST", [(b"allow", b"POST")])
                return
            cfg.sessions.revoke(token)
            await _send_http(
                send,
                303,
                b"",
                [(b"location", LOGIN_PATH.encode()), self._cookie_header(scope, headers, None)],
            )
            return

        if method in ("GET", "HEAD"):
            nxt = safe_next(_query(scope).get("next"))
            if cfg.sessions.valid(token):
                await _send_http(send, 303, b"", [(b"location", nxt.encode())])
                return
            await self._page(send, 200, None, nxt)
            return
        if method != "POST":
            await _send_http(send, 405, b"", [(b"allow", b"GET, POST")])
            return

        form = await _read_form(receive)
        nxt = safe_next(form.get("next"))
        client = (scope.get("client") or ("?", 0))[0]
        wait = cfg.limiter.retry_after(client)
        if wait > 0:
            minutes = max(1, round(wait / 60))
            await self._page(
                send,
                429,
                f"Too many wrong passwords. Try again in about {minutes} minute(s).",
                nxt,
                extra=[(b"retry-after", str(int(wait) + 1).encode())],
            )
            return
        if not cfg.password.verify(form.get("password", "")):
            cfg.limiter.failed(client)
            log.warning("failed login from %s", client)
            await self._page(send, 401, "Wrong password.", nxt)
            return
        cfg.limiter.succeeded(client)
        log.info("login from %s", client)
        await _send_http(
            send,
            303,
            b"",
            [
                (b"location", nxt.encode()),
                self._cookie_header(scope, headers, cfg.sessions.issue()),
            ],
        )

    async def _page(
        self,
        send: Send,
        status: int,
        error: str | None,
        nxt: str,
        extra: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        from sortandpost.themes import TOKENS

        light, dark = (
            self.config.page_tokens()
            if self.config.page_tokens is not None
            else (TOKENS[("fall", False)], TOKENS[("fall", True)])
        )
        body = login_page_html(light=light, dark=dark, error=error, next_path=nxt).encode()
        await _send_http(
            send,
            status,
            body,
            [
                (b"content-type", b"text/html; charset=utf-8"),
                (b"cache-control", b"no-store"),
                (b"x-frame-options", b"DENY"),
                *(extra or []),
            ],
        )

    def _cookie_header(
        self, scope: Scope, headers: dict[str, str], token: str | None
    ) -> tuple[bytes, bytes]:
        secure = scope.get("scheme") == "https" or headers.get("x-forwarded-proto") == "https"
        parts = [f"{self.config.cookie_name}={token or ''}", "Path=/", "HttpOnly", "SameSite=Lax"]
        if token is None:
            parts.append("Max-Age=0")
        else:
            assert self.config.sessions is not None
            parts.append(f"Max-Age={self.config.sessions.max_age_s}")
        if secure:
            parts.append("Secure")
        return (b"set-cookie", "; ".join(parts).encode())


def _login_url(target: str) -> str:
    return LOGIN_PATH if target in ("", "/") else f"{LOGIN_PATH}?next={quote(target, safe='')}"


def _headers(scope: Scope) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in scope.get("headers", []):
        out[k.decode("latin-1").lower()] = v.decode("latin-1")
    return out


def _cookie(headers: dict[str, str], name: str) -> str | None:
    raw = headers.get("cookie")
    if not raw:
        return None
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:
        return None
    morsel = jar.get(name)
    return morsel.value if morsel is not None else None


def _query(scope: Scope) -> dict[str, str]:
    qs = scope.get("query_string", b"").decode("latin-1")
    return {k: v[0] for k, v in parse_qs(qs).items()}


async def _read_form(receive: Receive) -> dict[str, str]:
    body = b""
    while True:
        msg = await receive()
        if msg["type"] != "http.request":
            break
        body += msg.get("body", b"")
        if len(body) > _MAX_FORM_BYTES:
            return {}
        if not msg.get("more_body"):
            break
    return {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}


async def _send_http(
    send: Send, status: int, body: bytes, headers: list[tuple[bytes, bytes]]
) -> None:
    if not any(k == b"content-type" for k, _ in headers):
        headers = [*headers, (b"content-type", b"text/plain; charset=utf-8")]
    headers = [*headers, (b"content-length", str(len(body)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _refuse(
    kind: str,
    scope: Scope,
    receive: Receive,
    send: Send,
    status: int,
    text: str,
    *,
    html_body: bool = False,
) -> None:
    if kind == "websocket":
        msg = await receive()  # websocket.connect
        if msg.get("type") == "websocket.connect":
            await send({"type": "websocket.close", "code": 1008})
        return
    ctype = b"text/html; charset=utf-8" if html_body else b"text/plain; charset=utf-8"
    await _send_http(send, status, text.encode(), [(b"content-type", ctype)])
