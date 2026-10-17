"""The background sender (sortandpost.post_queue): queued sends, "send when
online", held scheduled posts, and the double-post rules carried over from
post_send."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import httpx
import pytest
from sqlalchemy.engine import Engine

from sortandpost import post_queue as Q
from sortandpost import posts as P
from sortandpost.config import Settings
from sortandpost.post_send import _offline, _unknown
from sortandpost.posting import limiter
from sortandpost.posting.model import NO_CONNECTION, PostingError
from sortandpost.posting.robopost import SPEC as RP
from tests import test_send_post as _send_tests
from tests.test_send_post import IG, NOW, FakeRobopost, seq_exporter

# the send tests' fixtures: a robopost post with three photos and an IG channel
settings = _send_tests.settings
photos = _send_tests.photos
post = _send_tests.post


class FlakyRobopost(FakeRobopost):
    """``offline``: every request fails to connect. ``drop_uploads``: the
    auth check answers but uploads lose the connection."""

    def __init__(self) -> None:
        super().__init__()
        self.offline = False
        self.drop_uploads = False
        self.probes = 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        if self.offline:
            raise httpx.ConnectError("no route to host", request=req)
        if req.url.path == "/v1/auth":
            self.probes += 1
            return httpx.Response(200, json={"connected": True})
        if self.drop_uploads and req.url.path == "/v1/medias/upload":
            raise httpx.ConnectError("connection lost", request=req)
        return super().handler(req)


@pytest.fixture(autouse=True)
def _fresh_queue() -> Iterator[None]:
    Q._STOP.clear()
    yield
    Q._STOP.clear()


def _queue(
    settings: Settings, engine: Engine, post: P.Post, mode: P.SendMode = "schedule", **kw: bool
) -> P.Post:
    res = Q.queue_post(
        settings,
        engine,
        post.id,
        mode,
        wait_online=kw.get("wait_online", False),
        writable=kw.get("writable", True),
        now=NOW,
    )
    assert res.ok, res.message
    assert res.post is not None
    return res.post


def _drain(
    settings: Settings, engine: Engine, rp: FakeRobopost, *, now=NOW, **kw: bool
) -> list[Q.QueueEvent]:  # type: ignore[no-untyped-def]
    return Q.drain_queue(
        settings,
        engine,
        writable=lambda: kw.get("writable", True),
        foreign_ok=kw.get("foreign_ok", True),
        exporter=seq_exporter,
        client_factory=rp.factory(),
        now=now,
    )


def _get(settings: Settings, post: P.Post) -> P.Post:
    got = P.get_post(settings.paths, post.id)
    assert got is not None
    return got


def test_queue_then_drain_sends_in_background(
    settings: Settings, engine: Engine, post: P.Post
) -> None:
    queued = _queue(settings, engine, post)
    assert queued.status == "queued" and not queued.editable
    assert queued.queued_mode == "schedule" and queued.queued_by == "dev-a"
    rp = FlakyRobopost()
    events = _drain(settings, engine, rp)
    assert [e.ok for e in events] == [True]
    done = _get(settings, post)
    assert done.status == "scheduled"
    assert done.queued_mode is None and done.queue_note == ""
    assert len(rp.creates) == 1
    assert rp.probes == 0  # an immediate send isn't probed
    assert [e["action"] for e in done.log][:1] == ["queued"]


def test_queue_refuses_read_only_and_invalid(
    settings: Settings, engine: Engine, post: P.Post
) -> None:
    res = Q.queue_post(settings, engine, post.id, "draft", wait_online=True, writable=False)
    assert not res.ok and "read-only" in res.message
    post.channel_ids = []
    P.save_post(settings.paths, post)
    res = Q.queue_post(settings, engine, post.id, "schedule", wait_online=True, writable=True)
    assert not res.ok and _get(settings, post).status == "draft"


def test_wait_online_stays_queued_while_offline(
    settings: Settings, engine: Engine, post: P.Post
) -> None:
    _queue(settings, engine, post, wait_online=True)
    rp = FlakyRobopost()
    rp.offline = True
    assert _drain(settings, engine, rp) == []
    waiting = _get(settings, post)
    assert waiting.status == "queued" and waiting.queue_note == Q.NOTE_OFFLINE
    assert waiting.parts == [] and rp.uploads == []
    # a long offline spell doesn't rewrite the synced file every tick
    assert _drain(settings, engine, rp) == []
    assert _get(settings, post).updated_at == waiting.updated_at
    rp.offline = False
    events = _drain(settings, engine, rp)
    assert [e.ok for e in events] == [True]
    assert _get(settings, post).status == "scheduled"


def test_connection_lost_mid_upload_goes_back_to_queue(
    settings: Settings, engine: Engine, post: P.Post
) -> None:
    _queue(settings, engine, post, wait_online=True)
    rp = FlakyRobopost()
    rp.drop_uploads = True
    assert _drain(settings, engine, rp) == []
    back = _get(settings, post)
    assert back.status == "queued" and back.queue_note == Q.NOTE_OFFLINE
    assert [pt["status"] for pt in back.parts] == ["failed"]
    assert rp.creates == []
    rp.drop_uploads = False
    _drain(settings, engine, rp)
    assert _get(settings, post).status == "scheduled"
    assert len(rp.creates) == 1


def test_immediate_send_offline_fails_and_says_so(
    settings: Settings, engine: Engine, post: P.Post
) -> None:
    _queue(settings, engine, post, wait_online=False)
    rp = FlakyRobopost()
    rp.offline = True
    events = _drain(settings, engine, rp)
    assert len(events) == 1 and not events[0].ok and events[0].offline
    failed = _get(settings, post)
    assert failed.status == "failed" and failed.editable
    assert failed.queued_mode is None


def test_past_schedule_time_is_held_not_sent(
    settings: Settings, engine: Engine, post: P.Post
) -> None:
    _queue(settings, engine, post, wait_online=True)
    rp = FlakyRobopost()
    late = NOW + timedelta(days=3)  # the post was for NOW + 2 days
    events = _drain(settings, engine, rp, now=late)
    assert len(events) == 1 and events[0].held and not events[0].ok
    held = _get(settings, post)
    assert held.status == "queued" and held.queue_note == Q.NOTE_TIME_PASSED
    assert rp.creates == [] and rp.uploads == []
    assert _drain(settings, engine, rp, now=late) == []  # announced once
    Q._refresh_status(settings)
    assert "need a new time" in Q.status_text()
    back = Q.unqueue_post(settings, post.id)
    assert back is not None and back.status == "draft" and back.editable
    assert back.queue_note == ""


def test_read_only_machine_sends_nothing(settings: Settings, engine: Engine, post: P.Post) -> None:
    _queue(settings, engine, post)
    rp = FlakyRobopost()
    assert _drain(settings, engine, rp, writable=False) == []
    assert _get(settings, post).status == "queued" and rp.creates == []


def test_other_devices_posts_wait_for_sync_to_settle(
    settings: Settings, engine: Engine, post: P.Post
) -> None:
    _queue(settings, engine, post)
    p = _get(settings, post)
    p.queued_by = "dev-b"
    P.save_post(settings.paths, p)
    rp = FlakyRobopost()
    assert _drain(settings, engine, rp, foreign_ok=False) == []
    assert _get(settings, post).status == "queued"
    _drain(settings, engine, rp, foreign_ok=True)
    assert _get(settings, post).status == "scheduled"


def test_unknown_create_stays_sending_and_is_never_resent(
    settings: Settings, engine: Engine, post: P.Post
) -> None:
    _queue(settings, engine, post, wait_online=True)
    rp = FlakyRobopost()
    rp.timeout_create = True
    events = _drain(settings, engine, rp)
    assert len(events) == 1 and not events[0].ok
    assert _get(settings, post).status == "sending"
    rp.timeout_create = False
    assert _drain(settings, engine, rp) == []
    assert len(rp.creates) == 1


def test_refused_at_send_time_leaves_the_queue_as_failed(
    settings: Settings, engine: Engine, post: P.Post
) -> None:
    _queue(settings, engine, post, wait_online=True)
    P.save_channel_cache(settings.paths, "robopost", [])  # the channel went away
    rp = FlakyRobopost()
    events = _drain(settings, engine, rp)
    assert len(events) == 1 and not events[0].ok
    failed = _get(settings, post)
    assert failed.status == "failed" and failed.queued_mode is None
    assert failed.log[-1]["action"] == "send_failed"


def test_rate_limit_keeps_a_wait_online_post_queued(
    settings: Settings, engine: Engine, post: P.Post
) -> None:
    settings.config.posting.rate_limit_overrides = {"robopost": {"create": 1}}
    limiter(settings, RP).record("create", now=NOW)  # the hour's one create is used
    _queue(settings, engine, post, wait_online=True)
    rp = FlakyRobopost()
    assert _drain(settings, engine, rp) == []
    waiting = _get(settings, post)
    assert waiting.status == "queued" and "Try again in" in waiting.queue_note
    assert rp.creates == []


def test_unqueue_is_refused_while_in_flight(
    settings: Settings, post: P.Post, engine: Engine
) -> None:
    _queue(settings, engine, post)
    from sortandpost import post_send

    post_send._IN_FLIGHT.add(post.id)
    try:
        still = Q.unqueue_post(settings, post.id)
        assert still is not None and still.queued
    finally:
        post_send._IN_FLIGHT.discard(post.id)


def test_queued_fields_round_trip_and_bad_values_fall_back(
    settings: Settings, engine: Engine, post: P.Post
) -> None:
    _queue(settings, engine, post, wait_online=True)
    loaded = _get(settings, post)
    assert loaded.queue_wait_online is True and loaded.queued_at
    raw = P._coerce({"id": post.id, "title": "x", "status": "parked", "queued_mode": "now"})
    assert raw is not None and raw.status == "draft" and raw.queued_mode is None


def test_failed_connection_is_never_an_unknown_create() -> None:
    no_conn = PostingError(0, "ConnectError", NO_CONNECTION)
    timeout = PostingError(0, "ReadTimeout")
    assert not _unknown("create", no_conn) and _offline("create", no_conn)
    assert _unknown("create", timeout) and not _offline("create", timeout)
    assert _offline("export", timeout) and not _unknown("export", timeout)
    assert IG.network == "instagram"
