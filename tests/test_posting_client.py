"""The shared posting client, run against **every** registered provider spec.

A fake server answers each spec's endpoints with replies shaped by that spec's
own response maps, so a new provider gets all of these checks just by being
added to ``PROVIDERS``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from sortandpost.paths import Paths
from sortandpost.posting import PROVIDERS
from sortandpost.posting.client import ApiClient, extract
from sortandpost.posting.model import CreateRequest, PostingError
from sortandpost.posting.ratelimit import RateLimiter
from sortandpost.posting.spec import ProviderSpec, RateLimit

KEY = "SECRET-key-for-tests-abcdef123456"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
SPECS = list(PROVIDERS.values())


def _put(path: str, value: Any) -> Any:
    """The smallest JSON whose ``extract(..., path)`` gives ``[value]``
    (a ``[*]`` segment becomes a one-item list)."""
    if not path:
        return value
    head, _, rest = path.partition(".")
    star = head.endswith("[*]")
    name = head[:-3] if star else head
    inner = _put(rest, value)
    node = [inner] if star else inner
    return {name: node} if name else node


def _merge(*parts: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in parts:
        out.update(p)
    return out


class FakeServer:
    """Answers a spec's endpoints the way its response maps expect."""

    def __init__(self, spec: ProviderSpec) -> None:
        self.spec = spec
        self.requests: list[httpx.Request] = []
        self.script: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self.routes = {(m, p): op for op, (m, p) in spec.endpoints.items()}

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        base_path = httpx.URL(self.spec.default_base_url).path.rstrip("/")
        rel = req.url.path[len(base_path) :]
        op = self.routes.get((req.method, rel))
        if op is None:
            return httpx.Response(404, json={"message": "no route"})
        if op in self.script:
            return self.script[op](req)
        return httpx.Response(200, json=self.reply(op))

    def reply(self, op: str) -> Any:
        s = self.spec
        if op == "auth_check":
            parts = []
            if s.auth_label:
                parts.append(_put(s.auth_label, "Acme"))
            if s.auth_ok:
                parts.append(_put(s.auth_ok, True))
            return _merge(*parts) if parts else {}
        if op == "channels":
            f = s.channel_fields

            def row(cid: str, name: str, net: str, **extra: Any) -> dict[str, Any]:
                return _merge(
                    _put(f.get("id", "id"), cid),
                    _put(f.get("name", "name"), name),
                    _put(f.get("network", "network"), net),
                    extra,
                )

            rows = [
                row("c2", "Zed", "instagram"),
                row("c1", "Alpha", "facebook"),
                {"name": "no id -> dropped"},
            ]
            if s.channel_reconnect:
                rows.append(row("c3", "Stale", "instagram", **{s.channel_reconnect[0]: True}))
            if s.channel_skip:
                rows.append(row("c9", "Gone", "instagram", **{s.channel_skip[0]: True}))
            return _put(s.channels_at, rows)
        if op == "upload":
            parts = [_put(s.upload_id, "media-1")]
            if s.upload_path:
                parts.append(_put(s.upload_path, "https://cdn.example/m1.jpg"))
            return _merge(*parts)
        if op == "create":
            return _put(s.created_ids, "post-1")
        raise AssertionError(op)


def _client(spec: ProviderSpec, server: FakeServer, **kw: Any) -> ApiClient:
    return ApiClient(spec, KEY, transport=httpx.MockTransport(server.handler), **kw)


def _req() -> CreateRequest:
    return CreateRequest(mode="draft", caption="hello", media=(), channels=(), now=NOW)


@pytest.fixture(params=SPECS, ids=[s.key for s in SPECS])
def spec(request: pytest.FixtureRequest) -> ProviderSpec:
    s: ProviderSpec = request.param
    return s


def test_every_spec_is_complete(spec: ProviderSpec) -> None:
    assert spec.key.isidentifier() and spec.name
    assert spec.env_var and spec.keychain_account
    assert set(spec.endpoints) >= {"auth_check", "channels", "upload", "create"}
    assert spec.default_base_url.startswith("https://")
    assert spec.created_ids is not None and spec.upload_id
    assert spec.build_payload(_req()) != {}


def test_auth_placement_and_label(spec: ProviderSpec) -> None:
    server = FakeServer(spec)
    with _client(spec, server) as api:
        label = api.check_auth()
    assert label == ("Acme" if spec.auth_label else "")
    req = server.requests[0]
    assert req.url.scheme == "https"
    if spec.auth.place == "query":
        assert req.url.params[spec.auth.name] == KEY
    else:
        assert req.headers[spec.auth.name] == spec.auth.prefix + KEY
        assert KEY not in str(req.url)


def test_auth_ok_false_is_a_401(spec: ProviderSpec) -> None:
    if not spec.auth_ok:
        pytest.skip("this provider has no connected flag")
    server = FakeServer(spec)
    server.script["auth_check"] = lambda r: httpx.Response(200, json=_put(spec.auth_ok, False))
    with _client(spec, server) as api, pytest.raises(PostingError) as ei:
        api.check_auth()
    assert ei.value.status == 401


def test_channels_parsed_skipped_and_sorted(spec: ProviderSpec) -> None:
    with _client(spec, FakeServer(spec)) as api:
        chans = api.list_channels()
    ids = [c.id for c in chans]
    assert "c9" not in ids
    assert ids[0] == "c1"  # facebook sorts before instagram
    assert chans[0].network == "facebook"
    if spec.channel_reconnect:
        assert {c.id for c in chans if c.must_reconnect} == {"c3"}


def test_exact_endpoint_paths(spec: ProviderSpec, tmp_path: Path) -> None:
    f = tmp_path / "a.jpg"
    f.write_bytes(b"\xff\xd8fake")
    server = FakeServer(spec)
    with _client(spec, server) as api:
        api.check_auth()
        api.list_channels()
        api.upload_media(f)
        api.create_post(_req())
    base = httpx.URL(spec.default_base_url).path.rstrip("/")
    assert [(r.method, r.url.path) for r in server.requests] == [
        (m, base + p)
        for m, p in (spec.endpoints[op] for op in ("auth_check", "channels", "upload", "create"))
    ]


def test_upload_is_multipart_and_retried_once(spec: ProviderSpec, tmp_path: Path) -> None:
    f = tmp_path / "00_abcdef12.jpg"
    f.write_bytes(b"\xff\xd8fakejpeg")
    server = FakeServer(spec)
    calls = [0]

    def flaky(req: httpx.Request) -> httpx.Response:
        calls[0] += 1
        if calls[0] == 1:
            raise httpx.ConnectError("boom", request=req)
        assert req.headers["content-type"].startswith("multipart/form-data")
        assert f'name="{spec.upload_field}"; filename="00_abcdef12.jpg"'.encode() in req.read()
        return httpx.Response(200, json=server.reply("upload"))

    server.script["upload"] = flaky
    with _client(spec, server) as api:
        ref = api.upload_media(f)
    assert calls[0] == 1 + spec.upload_retries
    assert ref.id == "media-1"
    assert ref.path == ("https://cdn.example/m1.jpg" if spec.upload_path else "")


def test_upload_without_id_is_an_error(spec: ProviderSpec, tmp_path: Path) -> None:
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x")
    server = FakeServer(spec)
    server.script["upload"] = lambda r: httpx.Response(200, json={"other": 1})
    with _client(spec, server) as api, pytest.raises(PostingError):
        api.upload_media(f)


def test_create_returns_ids_and_is_never_retried(spec: ProviderSpec) -> None:
    server = FakeServer(spec)
    with _client(spec, server) as api:
        assert api.create_post(_req()) == ["post-1"]

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    server.requests.clear()
    server.script["create"] = boom
    with _client(spec, server) as api, pytest.raises(PostingError) as ei:
        api.create_post(_req())
    assert ei.value.status == 0
    assert len(server.requests) == 1


@pytest.mark.parametrize("status", [400, 401, 409, 415, 422, 500, 503])
def test_http_errors_carry_status_and_never_the_key(spec: ProviderSpec, status: int) -> None:
    server = FakeServer(spec)
    field = spec.error_fields[0]
    server.script["create"] = lambda r: httpx.Response(
        status, json={field: f"bad things {r.url} {KEY}"}
    )
    with _client(spec, server) as api, pytest.raises(PostingError) as ei:
        api.create_post(_req())
    err = ei.value
    assert err.status == status
    assert KEY not in str(err) and KEY not in err.friendly
    assert spec.name in err.friendly or err.status in spec.status_hints or err.code


def test_network_error_is_redacted(spec: ProviderSpec) -> None:
    server = FakeServer(spec)

    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {req.url} with {KEY}", request=req)

    server.script["auth_check"] = down
    with _client(spec, server) as api, pytest.raises(PostingError) as ei:
        api.check_auth()
    assert ei.value.status == 0 and KEY not in str(ei.value)
    assert f"Couldn't reach {spec.name}" in ei.value.friendly


def test_redirect_is_an_error_and_never_followed(spec: ProviderSpec) -> None:
    server = FakeServer(spec)
    server.script["auth_check"] = lambda r: httpx.Response(
        307, headers={"location": f"http://evil.example/?apikey={KEY}"}
    )
    with _client(spec, server) as api, pytest.raises(PostingError) as ei:
        api.check_auth()
    assert len(server.requests) == 1
    assert ei.value.status == 307 and KEY not in str(ei.value)


def test_429_reports_retry_after(spec: ProviderSpec) -> None:
    server = FakeServer(spec)
    server.script["create"] = lambda r: httpx.Response(
        429, headers={"retry-after": "600"}, json={"message": "slow down"}
    )
    with _client(spec, server) as api, pytest.raises(PostingError) as ei:
        api.create_post(_req())
    assert ei.value.status == 429 and ei.value.retry_after == 600
    assert "10 minutes" in ei.value.friendly


def test_live_key_is_scrubbed_from_http_logs(
    spec: ProviderSpec, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with _client(spec, FakeServer(spec)) as api:
        api.check_auth()
        logging.getLogger("httpx").info("HTTP Request: GET https://x/?apikey=%s %s", KEY, KEY)
    assert KEY not in caplog.text


def test_rate_limit_stops_the_call_before_it_is_made(spec: ProviderSpec, paths: Paths) -> None:
    server = FakeServer(spec)
    limiter = RateLimiter(paths, spec, {"create": RateLimit(2, 3600)})
    with _client(spec, server, limiter=limiter) as api:
        api.create_post(_req())
        api.create_post(_req())
        with pytest.raises(PostingError) as ei:
            api.create_post(_req())
    assert ei.value.status == 429 and ei.value.retry_after
    assert len(server.requests) == 2


def test_extract_paths() -> None:
    body = {"a": {"b": [{"id": 1}, {"id": 2}, {"x": 3}]}, "n": None}
    assert extract(body, "a.b[*].id") == [1, 2]
    assert extract([{"postId": "p"}], "[*].postId") == ["p"]
    assert extract(body, "") == [body]
    assert extract(body, "missing.x") == [] and extract(body, "n") == []


def test_posting_imports_first_without_a_cycle() -> None:
    """``sortandpost.posting`` must import on its own (scripts, the CLI), not
    only after the app has loaded ``sortandpost.sync``."""
    import subprocess
    import sys

    subprocess.run([sys.executable, "-c", "import sortandpost.posting"], check=True)


def test_create_uses_the_specs_create_timeout(spec: ProviderSpec) -> None:
    server = FakeServer(spec)
    seen: list[object] = []

    def record(req: httpx.Request) -> httpx.Response:
        seen.append(req.extensions["timeout"]["read"])
        return httpx.Response(200, json=server.reply("create"))

    server.script["create"] = record
    with _client(spec, server) as api:
        api.create_post(_req())
    assert seen == [spec.create_timeout_s]
