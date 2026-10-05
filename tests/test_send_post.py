from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image
from sqlalchemy.engine import Engine
from sqlmodel import Session

from sortandpost import posts as P
from sortandpost.config import AppConfig, Env, Settings
from sortandpost.db.models import Photo, PhotoPath
from sortandpost.paths import Paths
from sortandpost.post_export import IgSpec, export_jpeg
from sortandpost.post_send import resolve_interrupted, send_post
from sortandpost.posting import limiter
from sortandpost.posting.client import ApiClient, PostingClient
from sortandpost.posting.model import Channel
from sortandpost.posting.postiz import SPEC as PZ
from sortandpost.posting.robopost import SPEC as RP
from sortandpost.posting.spec import ProviderSpec
from sortandpost.secrets_store import clear_key, set_key
from sortandpost.sync.clock import utcnow

KEY = "rp_live_TOPSECRET_998877"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
IG = Channel(id="ig1", name="IG", social_network="INSTAGRAM")


def seq_exporter(
    jobs: list[tuple[str, str]], edge: int, quality: int, ig: IgSpec | None = None
) -> None:
    for src, dest in jobs:
        export_jpeg(src, dest, edge, quality, ig)


class FakeRobopost:
    """Records calls; ``fail_create`` / ``timeout_create`` script the create call."""

    def __init__(self) -> None:
        self.uploads: list[str] = []
        self.creates: list[dict[str, object]] = []
        self.fail_create: int | None = None
        self.timeout_create = False
        # per-create script, consumed in order: an HTTP status, "timeout" or None (ok)
        self.outcomes: list[int | str | None] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        assert req.url.params["apikey"] == KEY
        if req.url.path == "/v1/medias/upload":
            mid = f"m{len(self.uploads) + 1}"
            self.uploads.append(mid)
            return httpx.Response(201, json={"id": f"rec-{mid}", "storage_object_id": mid})
        if req.url.path == "/v1/scheduled_posts":
            self.creates.append(json.loads(req.read()))
            if self.outcomes:
                out = self.outcomes.pop(0)
                if out == "timeout":
                    raise httpx.ReadTimeout("slow", request=req)
                if isinstance(out, int):
                    return httpx.Response(out, json={"msg": "PLAN_LIMIT_HIT"})
                n = len(self.creates)
                return httpx.Response(200, json={"scheduled_posts": [{"id": f"rp-{n}"}]})
            if self.timeout_create:
                raise httpx.ReadTimeout("slow", request=req)
            if self.fail_create:
                return httpx.Response(self.fail_create, json={"msg": "PLAN_LIMIT_HIT"})
            return httpx.Response(200, json={"scheduled_posts": [{"id": "rp-1"}]})
        return httpx.Response(404, json={"detail": "not found"})

    def factory(self) -> Callable[[Settings, ProviderSpec, str], PostingClient]:
        return lambda _s, spec, key: ApiClient(
            spec, key, transport=httpx.MockTransport(self.handler)
        )


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
    set_key(RP, KEY)
    P.save_channel_cache(settings.paths, "robopost", [IG])
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
    assert saved.log[-1]["remote_post_ids"] == ["rp-1"]
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
    clear_key(RP)
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


# --- split Instagram send ------------------------------------------------------

FB = Channel(id="fb1", name="FB", social_network="FACEBOOK")


class RecordingExporter:
    def __init__(self) -> None:
        self.calls: list[tuple[int, IgSpec | None]] = []

    def __call__(
        self, jobs: list[tuple[str, str]], edge: int, quality: int, ig: IgSpec | None = None
    ) -> None:
        self.calls.append((len(jobs), ig))
        seq_exporter(jobs, edge, quality, ig)


@pytest.fixture
def split_post(settings: Settings, post: P.Post) -> P.Post:
    P.save_channel_cache(settings.paths, "robopost", [IG, FB])
    post.channel_ids = ["ig1", "fb1"]
    return P.save_post(settings.paths, post)


def _send_x(  # type: ignore[no-untyped-def]
    settings: Settings,
    engine: Engine,
    post: P.Post,
    rp: FakeRobopost,
    exporter: Callable[..., None] = seq_exporter,
    mode: P.SendMode = "schedule",
):
    return send_post(
        settings,
        engine,
        post.id,
        mode,
        writable=True,
        exporter=exporter,
        client_factory=rp.factory(),
        now=NOW,
    )


def _saved(settings: Settings, post: P.Post) -> P.Post:
    saved = P.get_post(settings.paths, post.id)
    assert saved is not None
    return saved


def test_split_send_makes_two_creates(
    settings: Settings, engine: Engine, split_post: P.Post
) -> None:
    rp = FakeRobopost()
    ex = RecordingExporter()
    res = _send_x(settings, engine, split_post, rp, ex)
    assert res.ok, res.message
    assert "2 scheduled posts" in res.message
    other, ig = rp.creates
    assert other["channel_ids"] == ["fb1"] and "instagram_settings" not in other
    assert other["facebook_settings"] == {"postType": "POST"}
    assert other["image_object_ids"] == ["m1", "m2", "m3"]
    assert ig["channel_ids"] == ["ig1"] and "facebook_settings" not in ig
    assert ig["instagram_settings"] == {"postType": "POST"}
    assert ig["image_object_ids"] == ["m4", "m5", "m6"]
    assert ex.calls == [(3, None), (3, IgSpec(1080, 1350, 0, (255, 255, 255)))]
    saved = _saved(settings, split_post)
    assert saved.status == "scheduled" and saved.sent and not saved.partly_sent
    assert [(pt["group"], pt["status"]) for pt in saved.parts] == [
        ("other", "sent"),
        ("instagram", "sent"),
    ]
    assert {k[64:] for k in saved.media_cache} == {"", "#ig"}


def test_split_ig_uses_border_and_colour(
    settings: Settings, engine: Engine, split_post: P.Post
) -> None:
    split_post.network_settings["instagram"] = {
        "postType": "POST",
        "format": "square",
        "border": "40",
        "padColor": "#102030",
    }
    P.save_post(settings.paths, split_post)
    ex = RecordingExporter()
    rp = FakeRobopost()
    assert _send_x(settings, engine, split_post, rp, ex).ok
    assert ex.calls[1] == (3, IgSpec(1080, 1080, 40, (16, 32, 48)))
    assert "padColor" not in json.dumps(rp.creates)


def test_ig_create_unknown_keeps_other_half_sent(
    settings: Settings, engine: Engine, split_post: P.Post
) -> None:
    rp = FakeRobopost()
    rp.outcomes = [None, "timeout"]
    res = _send_x(settings, engine, split_post, rp)
    assert not res.ok and "may or may not" in res.message and "went through" in res.message
    saved = _saved(settings, split_post)
    assert saved.status == "sending"
    assert [(pt["group"], pt["status"]) for pt in saved.parts] == [
        ("other", "sent"),
        ("instagram", "sending"),
    ]
    assert P.posted_hashes(settings.paths) == frozenset(split_post.photos)

    # never re-sent on its own
    assert not _send_x(settings, engine, split_post, rp).ok
    assert len(rp.creates) == 2

    resolved = resolve_interrupted(settings, split_post.id, was_sent=False)
    assert resolved is not None and resolved.status == "failed"
    assert resolved.partly_sent and resolved.editable
    assert resolved.log[-1] == {**resolved.log[-1], "action": "resolved", "part": "instagram"}

    assert _send_x(settings, engine, split_post, rp).ok
    assert len(rp.creates) == 3
    assert rp.creates[-1]["channel_ids"] == ["ig1"]
    assert rp.creates[-1]["image_object_ids"] == ["m4", "m5", "m6"]  # cached IG uploads
    assert len(rp.uploads) == 6
    assert _saved(settings, split_post).status == "scheduled"


def test_ig_create_unknown_resolved_as_sent(
    settings: Settings, engine: Engine, split_post: P.Post
) -> None:
    rp = FakeRobopost()
    rp.outcomes = [None, 502]
    _send_x(settings, engine, split_post, rp)
    resolved = resolve_interrupted(settings, split_post.id, was_sent=True)
    assert resolved is not None and resolved.status == "scheduled" and resolved.sent


def test_ig_4xx_after_other_sent_retries_only_instagram(
    settings: Settings, engine: Engine, split_post: P.Post
) -> None:
    rp = FakeRobopost()
    rp.outcomes = [None, 409]
    res = _send_x(settings, engine, split_post, rp)
    assert not res.ok and "Instagram post failed" in res.message
    saved = _saved(settings, split_post)
    assert saved.status == "failed" and saved.partly_sent
    assert saved.sent_channel_ids() == {"fb1"}

    assert _send_x(settings, engine, split_post, rp).ok
    assert [c["channel_ids"] for c in rp.creates] == [["fb1"], ["ig1"], ["ig1"]]
    assert len(rp.uploads) == 6


def test_other_half_4xx_never_starts_instagram(
    settings: Settings, engine: Engine, split_post: P.Post
) -> None:
    rp = FakeRobopost()
    rp.outcomes = [400]
    res = _send_x(settings, engine, split_post, rp)
    assert not res.ok
    assert len(rp.creates) == 1 and len(rp.uploads) == 3
    saved = _saved(settings, split_post)
    assert saved.status == "failed" and not saved.partly_sent

    assert _send_x(settings, engine, split_post, rp).ok
    assert [c["channel_ids"] for c in rp.creates] == [["fb1"], ["fb1"], ["ig1"]]
    assert len(rp.uploads) == 6  # the other half's uploads were reused


def test_other_half_unknown_then_resolved_sent_sends_only_instagram(
    settings: Settings, engine: Engine, split_post: P.Post
) -> None:
    rp = FakeRobopost()
    rp.outcomes = ["timeout"]
    _send_x(settings, engine, split_post, rp)
    assert len(rp.creates) == 1
    resolved = resolve_interrupted(settings, split_post.id, was_sent=True)
    assert resolved is not None and resolved.status == "failed" and resolved.partly_sent
    assert _send_x(settings, engine, split_post, rp).ok
    assert [c["channel_ids"] for c in rp.creates] == [["fb1"], ["ig1"]]


def test_partly_sent_with_remaining_unticked_finishes_without_create(
    settings: Settings, engine: Engine, split_post: P.Post
) -> None:
    rp = FakeRobopost()
    rp.outcomes = [None, 409]
    _send_x(settings, engine, split_post, rp)
    saved = _saved(settings, split_post)
    saved.channel_ids = ["fb1"]
    P.save_post(settings.paths, saved)
    res = _send_x(settings, engine, split_post, rp)
    assert res.ok and "Nothing left" in res.message
    assert len(rp.creates) == 2
    assert _saved(settings, split_post).status == "scheduled"


def test_instagram_ratio_photos_go_in_one_create(
    settings: Settings, engine: Engine, split_post: P.Post
) -> None:
    with Session(engine) as s:
        for h in split_post.photos:
            ph = s.get(Photo, h)
            assert ph is not None
            ph.width, ph.height = 1080, 1350
            s.add(ph)
        s.commit()
    rp = FakeRobopost()
    ex = RecordingExporter()
    assert _send_x(settings, engine, split_post, rp, ex).ok
    assert len(rp.creates) == 1
    assert rp.creates[0]["channel_ids"] == ["ig1", "fb1"]
    assert "instagram_settings" in rp.creates[0] and "facebook_settings" in rp.creates[0]
    assert ex.calls == [(3, None)]


def test_instagram_only_post_is_sized(settings: Settings, engine: Engine, post: P.Post) -> None:
    rp = FakeRobopost()
    ex = RecordingExporter()
    assert _send_x(settings, engine, post, rp, ex).ok
    assert ex.calls == [(3, IgSpec(1080, 1350, 0, (255, 255, 255)))]
    assert len(rp.creates) == 1 and set(_saved(settings, post).media_cache) == {
        h + "#ig" for h in post.photos
    }


def test_old_post_json_without_parts_loads(settings: Settings, post: P.Post) -> None:
    f = settings.paths.posts_dir / f"{post.id}.json"
    raw = json.loads(f.read_text())
    raw.pop("parts")
    raw["status"] = "sending"
    raw["sending_mode"] = "draft"
    f.write_text(json.dumps(raw))
    loaded = _saved(settings, post)
    assert loaded.parts == [] and loaded.sending_part() is None
    resolved = resolve_interrupted(settings, post.id, was_sent=True)
    assert resolved is not None and resolved.status == "sent_draft"


def test_bad_parts_are_dropped(settings: Settings, post: P.Post) -> None:
    f = settings.paths.posts_dir / f"{post.id}.json"
    raw = json.loads(f.read_text())
    raw["parts"] = [{"status": "bogus"}, "x", {"status": "sent", "channel_ids": ["a", 3]}]
    f.write_text(json.dumps(raw))
    loaded = _saved(settings, post)
    assert len(loaded.parts) == 1 and loaded.parts[0]["channel_ids"] == ["a", "3"]


def test_duplicate_resets_parts(settings: Settings, engine: Engine, split_post: P.Post) -> None:
    assert _send_x(settings, engine, split_post, FakeRobopost()).ok
    copy = P.duplicate_post(settings.paths, split_post.id)
    assert copy is not None and copy.parts == [] and copy.status == "draft"


# --- providers -------------------------------------------------------------------

PZ_KEY = "pz_TOPSECRET_445566"
PZ_IG = Channel(id="pz-ig", name="IG (Postiz)", social_network="instagram")


class FakePostiz:
    def __init__(self) -> None:
        self.uploads: list[str] = []
        self.creates: list[dict[str, object]] = []
        self.outcomes: list[int | None] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == PZ_KEY and PZ_KEY not in str(req.url)
        if req.url.path == "/public/v1/upload":
            mid = f"pz{len(self.uploads) + 1}"
            self.uploads.append(mid)
            return httpx.Response(200, json={"id": mid, "path": f"https://u/{mid}.jpg"})
        if req.url.path == "/public/v1/posts":
            self.creates.append(json.loads(req.read()))
            out = self.outcomes.pop(0) if self.outcomes else None
            if out:
                return httpx.Response(out, json={"message": "nope"})
            return httpx.Response(200, json=[{"postId": "pz-post-1", "integration": "pz-ig"}])
        return httpx.Response(404, json={"message": "no route"})

    def factory(self) -> Callable[[Settings, ProviderSpec, str], PostingClient]:
        # like posting.open_client: the spec's rate limits apply
        return lambda s, spec, key: ApiClient(
            spec, key, limiter=limiter(s, spec), transport=httpx.MockTransport(self.handler)
        )


def _use_postiz(settings: Settings, post: P.Post) -> P.Post:
    set_key(PZ, PZ_KEY)
    settings.config.posting.provider = "postiz"
    P.save_channel_cache(settings.paths, "postiz", [PZ_IG])
    post.channel_ids = ["pz-ig"]
    return P.save_post(settings.paths, post)


def _send_with(settings: Settings, engine: Engine, post: P.Post, fake: object, mode: P.SendMode):  # type: ignore[no-untyped-def]
    return send_post(
        settings,
        engine,
        post.id,
        mode,
        writable=True,
        exporter=seq_exporter,
        client_factory=fake.factory(),  # type: ignore[attr-defined]
        now=NOW,
    )


def test_postiz_send_end_to_end(settings: Settings, engine: Engine, post: P.Post) -> None:
    post = _use_postiz(settings, post)
    pz = FakePostiz()
    res = _send_with(settings, engine, post, pz, "schedule")
    assert res.ok and "Postiz" in res.message, res.message
    assert pz.uploads == ["pz1", "pz2", "pz3"]
    (body,) = pz.creates
    assert body["type"] == "schedule" and body["date"] == "2026-09-27T12:00:00.000Z"
    posts: list[dict[str, Any]] = body["posts"]  # type: ignore[assignment]
    (item,) = posts
    assert item["integration"] == {"id": "pz-ig"}
    assert item["settings"] == {"__type": "instagram", "post_type": "post"}
    assert [i["id"] for i in item["value"][0]["image"]] == ["pz1", "pz2", "pz3"]
    saved = _saved(settings, post)
    assert saved.status == "scheduled" and saved.provider == "postiz"
    assert saved.parts[-1]["provider"] == "postiz"
    assert saved.parts[-1]["remote_post_ids"] == ["pz-post-1"]
    assert all(e["provider"] == "postiz" for e in saved.media_cache.values())


def test_switching_provider_reuploads_a_draft(
    settings: Settings, engine: Engine, post: P.Post
) -> None:
    rp = FakeRobopost()
    rp.fail_create = 400  # robopost got the uploads, then refused the post
    assert not _send(settings, engine, post, rp, "schedule").ok
    failed = _saved(settings, post)
    assert failed.status == "failed" and not failed.partly_sent and len(failed.media_cache) == 3

    post = _use_postiz(settings, failed)
    pz = FakePostiz()
    assert _send_with(settings, engine, post, pz, "schedule").ok
    assert pz.uploads == ["pz1", "pz2", "pz3"]  # robopost's media ids never reused
    assert _saved(settings, post).provider == "postiz"


def test_partly_sent_post_stays_with_its_provider(
    settings: Settings, engine: Engine, split_post: P.Post
) -> None:
    rp = FakeRobopost()
    rp.outcomes = [None, 409]
    _send_x(settings, engine, split_post, rp)
    saved = _saved(settings, split_post)
    assert saved.partly_sent and saved.provider == "robopost"

    settings.config.posting.provider = "postiz"
    pz = FakePostiz()
    res = _send_with(settings, engine, saved, pz, "schedule")
    assert not res.ok and "Switch back to Robopost" in res.message
    assert pz.uploads == [] and pz.creates == []

    settings.config.posting.provider = "robopost"
    assert _send_x(settings, engine, split_post, rp).ok


def test_rate_limit_refuses_the_whole_send_up_front(
    settings: Settings, engine: Engine, post: P.Post
) -> None:
    post = _use_postiz(settings, post)
    settings.config.posting.rate_limit_overrides = {"postiz": {"create": 1}}
    pz = FakePostiz()
    assert _send_with(settings, engine, post, pz, "schedule").ok
    second = P.duplicate_post(settings.paths, post.id)
    assert second is not None
    second.schedule_at = post.schedule_at
    P.save_post(settings.paths, second)
    res = _send_with(settings, engine, second, pz, "schedule")
    assert not res.ok and "Try again in" in res.message
    assert len(pz.creates) == 1 and len(pz.uploads) == 3  # nothing exported or uploaded
    assert _saved(settings, second).status == "draft"


def test_legacy_post_file_loads_as_robopost(settings: Settings, post: P.Post) -> None:
    raw = json.loads((settings.paths.posts_dir / f"{post.id}.json").read_text())
    raw.update(
        status="scheduled",
        media_cache={post.photos[0]: {"export_key": "k", "storage_object_id": "m9"}},
        parts=[
            {
                "group": "all",
                "status": "sent",
                "mode": "schedule",
                "channel_ids": ["ig1"],
                "robopost_post_ids": ["rp-7"],
            }
        ],
    )
    raw.pop("provider", None)
    (settings.paths.posts_dir / f"{post.id}.json").write_text(json.dumps(raw))
    old = _saved(settings, post)
    assert old.provider == "robopost"
    assert old.parts[0]["remote_post_ids"] == ["rp-7"] and old.parts[0]["provider"] == "robopost"
    assert old.media_cache[post.photos[0]] == {
        "export_key": "k",
        "media_id": "m9",
        "provider": "robopost",
    }
    settings.config.posting.provider = "postiz"
    from sortandpost.posting import provider_for

    assert provider_for(old, settings).key == "robopost"  # a sent post keeps its provider
