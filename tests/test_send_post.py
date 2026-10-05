from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from PIL import Image
from sqlalchemy.engine import Engine
from sqlmodel import Session

from sortandpost import posts as P
from sortandpost.config import AppConfig, Env, Settings
from sortandpost.db.models import Photo, PhotoPath
from sortandpost.paths import Paths
from sortandpost.post_export import export_jpeg
from sortandpost.post_send import resolve_interrupted, send_post
from sortandpost.robopost import Channel, RobopostClient
from sortandpost.secrets_store import clear_robopost_key, set_robopost_key
from sortandpost.sync.clock import utcnow

KEY = "rp_live_TOPSECRET_998877"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
IG = Channel(id="ig1", name="IG", social_network="INSTAGRAM")


def seq_exporter(jobs: list[tuple[str, str]], edge: int, quality: int) -> None:
    for src, dest in jobs:
        export_jpeg(src, dest, edge, quality)


class FakeRobopost:
    """Records calls; ``fail_create`` / ``timeout_create`` script the create call."""

    def __init__(self) -> None:
        self.uploads: list[str] = []
        self.creates: list[dict[str, object]] = []
        self.fail_create: int | None = None
        self.timeout_create = False

    def handler(self, req: httpx.Request) -> httpx.Response:
        assert req.url.params["apikey"] == KEY
        if req.url.path == "/v1/medias/upload":
            mid = f"m{len(self.uploads) + 1}"
            self.uploads.append(mid)
            return httpx.Response(201, json={"id": f"rec-{mid}", "storage_object_id": mid})
        if req.url.path == "/v1/scheduled_posts":
            self.creates.append(json.loads(req.read()))
            if self.timeout_create:
                raise httpx.ReadTimeout("slow", request=req)
            if self.fail_create:
                return httpx.Response(self.fail_create, json={"msg": "PLAN_LIMIT_HIT"})
            return httpx.Response(200, json={"scheduled_posts": [{"id": "rp-1"}]})
        return httpx.Response(404, json={"detail": "not found"})

    def factory(self) -> Callable[[str], RobopostClient]:
        return lambda key: RobopostClient(key, transport=httpx.MockTransport(self.handler))


@pytest.fixture
def settings(paths: Paths) -> Settings:
    cfg = AppConfig()
    cfg.posting.export_long_edge = 512
    return Settings(env=Env(), paths=paths, config=cfg, device_id="dev-a", hostname="a")


@pytest.fixture
def photos(tmp_path: Path, engine: Engine) -> list[str]:
    lib = tmp_path / "library"
    lib.mkdir()
    hashes = []
    now = utcnow()
    with Session(engine) as s:
        for i in range(3):
            f = lib / f"img{i}.jpg"
            Image.new("RGB", (1200, 800), (i * 40, 90, 20)).save(f, "JPEG")
            ph = f"{i + 1:064x}"
            hashes.append(ph)
            s.add(
                Photo(
                    pixel_hash=ph,
                    primary_path=str(f),
                    width=1200,
                    height=800,
                    format="JPEG",
                    file_size=f.stat().st_size,
                    file_mtime_ns=f.stat().st_mtime_ns,
                    first_indexed_at=now,
                    last_indexed_at=now,
                )
            )
            s.add(PhotoPath(pixel_hash=ph, path=str(f), size=1, mtime_ns=1, present=True))
        s.commit()
    return hashes


@pytest.fixture
def post(settings: Settings, photos: list[str]) -> P.Post:
    set_robopost_key(KEY)
    P.save_channel_cache(settings.paths, [IG])
    p = P.create_post(settings.paths, title="walk", photos=photos, caption="caption")
    p.channel_ids = ["ig1"]
    p.schedule_at = (NOW + timedelta(days=2)).isoformat()
    return P.save_post(settings.paths, p)


def _send(settings: Settings, engine: Engine, post: P.Post, rp: FakeRobopost, mode: P.SendMode):  # type: ignore[no-untyped-def]
    return send_post(
        settings,
        engine,
        post.id,
        mode,
        writable=True,
        exporter=seq_exporter,
        client_factory=rp.factory(),
        now=NOW,
    )


def test_schedule_happy_path(settings: Settings, engine: Engine, post: P.Post) -> None:
    rp = FakeRobopost()
    res = _send(settings, engine, post, rp, "schedule")
    assert res.ok, res.message
    assert rp.uploads == ["m1", "m2", "m3"]
    assert rp.creates == [
        {
            "text": "caption",
            "image_object_ids": ["m1", "m2", "m3"],
            "is_draft": False,
            "channel_ids": ["ig1"],
            "schedule_at": "2026-09-27T12:00:00Z",
            "instagram_settings": {"postType": "POST"},
        }
    ]
    saved = P.get_post(settings.paths, post.id)
    assert saved is not None and saved.status == "scheduled" and not saved.editable
    assert saved.log[-1]["robopost_post_ids"] == ["rp-1"]
    assert not (settings.paths.post_exports_dir / post.id).exists()
    assert P.posted_hashes(settings.paths) == frozenset(post.photos)


def test_draft_mode(settings: Settings, engine: Engine, post: P.Post) -> None:
    rp = FakeRobopost()
    res = _send(settings, engine, post, rp, "draft")
    assert res.ok
    assert rp.creates[0]["is_draft"] is True
    saved = P.get_post(settings.paths, post.id)
    assert saved is not None and saved.status == "sent_draft"


def test_sent_post_cannot_be_sent_again(settings: Settings, engine: Engine, post: P.Post) -> None:
    rp = FakeRobopost()
    assert _send(settings, engine, post, rp, "draft").ok
    res = _send(settings, engine, post, rp, "draft")
    assert not res.ok and "already sent" in res.message
    assert len(rp.creates) == 1


def test_read_only_refuses(settings: Settings, engine: Engine, post: P.Post) -> None:
    rp = FakeRobopost()
    res = send_post(settings, engine, post.id, "draft", writable=False, client_factory=rp.factory())
    assert not res.ok and "read-only" in res.message
    assert rp.uploads == [] and rp.creates == []


def test_no_key(settings: Settings, engine: Engine, post: P.Post) -> None:
    clear_robopost_key()
    res = _send(settings, engine, post, FakeRobopost(), "draft")
    assert not res.ok and "API key" in res.message


def test_validation_blocks_without_touching_status(
    settings: Settings, engine: Engine, post: P.Post
) -> None:
    post.schedule_at = None
    P.save_post(settings.paths, post)
    rp = FakeRobopost()
    res = _send(settings, engine, post, rp, "schedule")
    assert not res.ok and "date and time" in res.message
    saved = P.get_post(settings.paths, post.id)
    assert saved is not None and saved.status == "draft"
    assert rp.uploads == []


def test_http_error_fails_then_retry_reuses_uploads(
    settings: Settings, engine: Engine, post: P.Post
) -> None:
    rp = FakeRobopost()
    rp.fail_create = 409
    res = _send(settings, engine, post, rp, "schedule")
    assert not res.ok and "plan limit" in res.message
    saved = P.get_post(settings.paths, post.id)
    assert saved is not None and saved.status == "failed" and saved.editable
    assert saved.log[-1]["action"] == "send_failed"
    assert len(saved.media_cache) == 3

    rp.fail_create = None
    assert _send(settings, engine, saved, rp, "schedule").ok
    assert rp.uploads == ["m1", "m2", "m3"]  # nothing re-uploaded
    assert rp.creates[-1]["image_object_ids"] == ["m1", "m2", "m3"]


def test_changed_export_settings_force_reupload(
    settings: Settings, engine: Engine, post: P.Post
) -> None:
    rp = FakeRobopost()
    rp.fail_create = 400
    _send(settings, engine, post, rp, "schedule")
    settings.config.posting.export_quality = 70
    rp.fail_create = None
    assert _send(settings, engine, post, rp, "schedule").ok
    assert len(rp.uploads) == 6


def test_unknown_create_outcome_is_never_resent(
    settings: Settings, engine: Engine, post: P.Post
) -> None:
    rp = FakeRobopost()
    rp.timeout_create = True
    res = _send(settings, engine, post, rp, "schedule")
    assert not res.ok and "may or may not" in res.message
    saved = P.get_post(settings.paths, post.id)
    assert saved is not None and saved.status == "sending" and saved.sending_mode == "schedule"

    rp.timeout_create = False
    again = _send(settings, engine, post, rp, "schedule")
    assert not again.ok
    assert len(rp.creates) == 1  # no silent second create

    resolved = resolve_interrupted(settings, post.id, was_sent=True)
    assert resolved is not None and resolved.status == "scheduled"
    assert resolved.log[-1]["action"] == "resolved"


def test_resolve_not_sent_makes_it_retryable(
    settings: Settings, engine: Engine, post: P.Post
) -> None:
    post.status = "sending"
    post.sending_mode = "draft"
    P.save_post(settings.paths, post)
    resolved = resolve_interrupted(settings, post.id, was_sent=False)
    assert resolved is not None and resolved.status == "failed" and resolved.editable


def test_missing_file_blocks(
    settings: Settings, engine: Engine, post: P.Post, photos: list[str]
) -> None:
    src = P.get_post(settings.paths, post.id)
    assert src is not None
    with Session(engine) as s:
        path = s.get(Photo, photos[0])
        assert path is not None
        Path(path.primary_path).unlink()
    res = _send(settings, engine, post, FakeRobopost(), "draft")
    assert not res.ok and "no file" in res.message


def test_key_never_written_to_app_data_or_logs(
    settings: Settings, engine: Engine, post: P.Post, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    rp = FakeRobopost()
    rp.fail_create = 409
    _send(settings, engine, post, rp, "schedule")
    rp.fail_create = None
    rp.timeout_create = True
    fresh = P.duplicate_post(settings.paths, post.id)
    assert fresh is not None
    _send(settings, engine, fresh, rp, "schedule")
    assert KEY not in caplog.text
    for f in settings.paths.app_data.rglob("*"):
        if f.is_file():
            assert KEY.encode() not in f.read_bytes(), f


def test_pool_exporter_runs_in_worker_processes(tmp_path: Path) -> None:
    from sortandpost.post_send import pool_exporter

    src = tmp_path / "a.jpg"
    Image.new("RGB", (900, 300), (1, 2, 3)).save(src)
    dest = tmp_path / "out" / "a.jpg"
    pool_exporter([(str(src), str(dest))], 300, 85)
    with Image.open(dest) as im:
        assert im.size == (300, 100)


def test_5xx_on_create_is_an_unknown_outcome(
    settings: Settings, engine: Engine, post: P.Post
) -> None:
    rp = FakeRobopost()
    rp.fail_create = 500
    res = _send(settings, engine, post, rp, "schedule")
    assert not res.ok and "may or may not" in res.message
    saved = P.get_post(settings.paths, post.id)
    assert saved is not None and saved.status == "sending"


def test_legacy_cache_entries_are_reuploaded(
    settings: Settings, engine: Engine, post: P.Post
) -> None:
    from sortandpost.post_export import export_key

    ekey = export_key(
        settings.config.posting.export_long_edge, settings.config.posting.export_quality
    )
    post.media_cache = {h: {"export_key": ekey, "media_id": "record-id"} for h in post.photos}
    P.save_post(settings.paths, post)
    rp = FakeRobopost()
    assert _send(settings, engine, post, rp, "schedule").ok
    assert rp.creates[0]["image_object_ids"] == ["m1", "m2", "m3"]
