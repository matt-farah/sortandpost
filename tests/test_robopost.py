from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

from sortandpost.robopost import Channel, RobopostClient, RobopostError, network_key, redact

KEY = "rp_live_SECRET_abcdef123456"


def _client(handler: httpx.MockTransport) -> RobopostClient:
    return RobopostClient(KEY, transport=handler)


def test_auth_ok_sends_key_as_query_param() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"message": "ok", "team_name": "Matt's team"})

    with _client(httpx.MockTransport(handler)) as rp:
        assert rp.check_auth() == "Matt's team"
    assert seen[0].url.path == "/v1/auth"
    assert seen[0].url.params["apikey"] == KEY
    assert seen[0].url.scheme == "https"


def test_401_is_friendly_and_redacted() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": f"Invalid API key. {req.url}"})

    with _client(httpx.MockTransport(handler)) as rp, pytest.raises(RobopostError) as ei:
        rp.check_auth()
    err = ei.value
    assert err.status == 401
    assert KEY not in str(err) and KEY not in err.friendly
    assert "Manage API Keys" in err.friendly


def test_list_channels_parses_and_sorts() -> None:
    rows = [
        {"id": "c2", "name": "Zed", "social_network": "INSTAGRAM", "must_reconnect": False},
        {"id": "c1", "name": "Alpha", "social_network": "INSTAGRAM", "must_reconnect": True},
        {"id": "c3", "name": "Page", "social_network": "FACEBOOK_PAGE"},
        {"name": "no id -> dropped"},
        {"id": "c9", "name": "Gone", "social_network": "BLUESKY", "is_deleted": True},
    ]
    with _client(httpx.MockTransport(lambda r: httpx.Response(200, json=rows))) as rp:
        chans = rp.list_channels()
    assert [c.id for c in chans] == ["c3", "c1", "c2"]
    assert chans[0].network == "facebook"
    assert chans[1].must_reconnect


def test_network_key() -> None:
    assert network_key("INSTAGRAM_BUSINESS") == "instagram"
    assert network_key("Pinterest") == "pinterest"
    assert network_key("BLUESKY") == "bluesky"
    assert network_key("") == "other"


def test_upload_is_multipart_and_returns_id(tmp_path: Path) -> None:
    f = tmp_path / "00_abcdef12.jpg"
    f.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(
            201,
            json={"id": "rec-1", "name": "x", "extension": "jpg", "storage_object_id": "m-1"},
        )

    with _client(httpx.MockTransport(handler)) as rp:
        assert rp.upload_media(f) == "m-1"
    req = seen[0]
    assert req.method == "POST" and req.url.path == "/v1/medias/upload"
    assert req.headers["content-type"].startswith("multipart/form-data")
    body = req.read()
    assert b'name="file"; filename="00_abcdef12.jpg"' in body
    assert b"fakejpeg" in body


def test_upload_415(tmp_path: Path) -> None:
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x")
    resp = httpx.Response(415, json={"error": "File extension 'xyz' not supported."})
    with _client(httpx.MockTransport(lambda r: resp)) as rp, pytest.raises(RobopostError) as ei:
        rp.upload_media(f)
    assert ei.value.status == 415
    assert "file type" in ei.value.friendly


def test_upload_retries_once_on_network_error(tmp_path: Path) -> None:
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x")
    calls = [0]

    def handler(req: httpx.Request) -> httpx.Response:
        calls[0] += 1
        if calls[0] == 1:
            raise httpx.ConnectError("boom", request=req)
        return httpx.Response(201, json={"id": 1, "storage_object_id": 77})

    with _client(httpx.MockTransport(handler)) as rp:
        assert rp.upload_media(f) == "77"
    assert calls[0] == 2


def test_create_post_returns_ids_and_is_never_retried() -> None:
    calls: list[dict[str, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(json.loads(req.read()))
        return httpx.Response(200, json={"scheduled_posts": [{"id": "p1"}, {"id": "p2"}]})

    with _client(httpx.MockTransport(handler)) as rp:
        assert rp.create_post({"text": "hi", "is_draft": True}) == ["p1", "p2"]
    assert calls == [{"text": "hi", "is_draft": True}]

    def boom(req: httpx.Request) -> httpx.Response:
        calls.append({})
        raise httpx.ReadTimeout("slow", request=req)

    calls.clear()
    with _client(httpx.MockTransport(boom)) as rp, pytest.raises(RobopostError) as ei:
        rp.create_post({"text": "hi"})
    assert ei.value.status == 0
    assert len(calls) == 1


def test_plan_limit_code() -> None:
    resp = httpx.Response(409, json={"msg": "PLAN_LIMIT_HIT"})
    with _client(httpx.MockTransport(lambda r: resp)) as rp, pytest.raises(RobopostError) as ei:
        rp.create_post({"text": "x"})
    assert ei.value.code == "PLAN_LIMIT_HIT"
    assert "plan limit" in ei.value.friendly


def test_network_error_message_is_redacted() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {req.url}", request=req)

    with _client(httpx.MockTransport(handler)) as rp, pytest.raises(RobopostError) as ei:
        rp.check_auth()
    assert KEY not in str(ei.value)
    assert "apikey=***" in str(ei.value)


def test_httpx_request_log_is_redacted(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    ok = httpx.Response(200, json={"team_name": "t"})
    with _client(httpx.MockTransport(lambda r: ok)) as rp:
        rp.check_auth()
    logging.getLogger("httpx").info("HTTP Request: GET https://x/v1/auth/?apikey=%s", KEY)
    assert KEY not in caplog.text


def test_redact() -> None:
    assert redact(f"GET /v1/x?apikey={KEY}&a=1") == "GET /v1/x?apikey=***&a=1"
    assert redact(f"key is {KEY}", KEY) == "key is ***"


def test_https_only() -> None:
    with pytest.raises(ValueError):
        RobopostClient(KEY, base_url="http://public-api.robopost.app/v1")


def test_channel_json_round_trip() -> None:
    c = Channel(id="1", name="n", social_network="PINTEREST", must_reconnect=True)
    assert Channel.from_json(c.to_json()) == c


def test_redirect_is_an_error_and_never_followed() -> None:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(
            307, headers={"location": f"http://public-api.robopost.app/v1/auth?apikey={KEY}"}
        )

    with _client(httpx.MockTransport(handler)) as rp, pytest.raises(RobopostError) as ei:
        rp.check_auth()
    assert len(calls) == 1  # the plain-http redirect target was never requested
    assert ei.value.status == 307 and KEY not in str(ei.value)


def test_exact_endpoint_paths(tmp_path: Path) -> None:
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x")
    paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        paths.append(f"{req.method} {req.url.path}")
        if req.url.path.endswith("upload"):
            return httpx.Response(201, json={"id": "rec", "storage_object_id": "m"})
        if req.url.path.endswith("scheduled_posts"):
            return httpx.Response(200, json={"scheduled_posts": []})
        return httpx.Response(200, json=[] if "channels" in req.url.path else {"team_name": "t"})

    with _client(httpx.MockTransport(handler)) as rp:
        rp.check_auth()
        rp.list_channels()
        rp.upload_media(f)
        rp.create_post({"text": "x"})
    assert paths == [
        "GET /v1/auth",
        "GET /v1/channels/",
        "POST /v1/medias/upload",
        "POST /v1/scheduled_posts",
    ]


def test_upload_returns_storage_object_id_not_record_id(tmp_path: Path) -> None:
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x")
    ok = httpx.Response(201, json={"id": "record-id", "storage_object_id": "storage-uuid"})
    with _client(httpx.MockTransport(lambda r: ok)) as rp:
        assert rp.upload_media(f) == "storage-uuid"
    no_storage = httpx.Response(201, json={"id": "record-id"})
    with _client(httpx.MockTransport(lambda r: no_storage)) as rp, pytest.raises(RobopostError):
        rp.upload_media(f)
