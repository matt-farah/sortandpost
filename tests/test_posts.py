from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

from sortandpost import posts as P
from sortandpost.paths import Paths
from sortandpost.robopost import Channel

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
IG = Channel(id="ig", name="My IG", social_network="INSTAGRAM")
FB = Channel(id="fb", name="My Page", social_network="FACEBOOK")
PIN = Channel(id="pin", name="Boards", social_network="PINTEREST")
BSKY = Channel(id="bs", name="Sky", social_network="BLUESKY")
STALE = Channel(id="old", name="Old", social_network="INSTAGRAM", must_reconnect=True)
CHANNELS = [IG, FB, PIN, BSKY, STALE]


def h(n: int) -> str:
    return f"{n:064x}"


def _post(**kw: object) -> P.Post:
    base: dict[str, object] = {"title": "t", "photos": [h(1)], "caption": "hello"}
    base.update(kw)
    return P.Post(**base)  # type: ignore[arg-type]


# --- storage -----------------------------------------------------------------


def test_round_trip_one_file_per_post(paths: Paths) -> None:
    p = P.create_post(
        paths, title="  Beach\tday ", photos=[h(1), h(2), h(1)], caption="c", device_id="dev"
    )
    assert p.photos == [h(1), h(2)]
    assert p.title == "Beach day"
    assert (paths.posts_dir / f"{p.id}.json").is_file()
    got = P.get_post(paths, p.id)
    assert got is not None and got.photos == [h(1), h(2)] and got.updated_by_device == "dev"
    assert [x.id for x in P.list_posts(paths)] == [p.id]
    P.delete_post(paths, p.id)
    assert P.get_post(paths, p.id) is None


def test_conflict_copy_and_junk_are_ignored_not_deleted(paths: Paths) -> None:
    p = P.create_post(paths, title="a", photos=[h(1)])
    conflict = paths.posts_dir / f"{p.id} (1).json"
    conflict.write_text((paths.posts_dir / f"{p.id}.json").read_text())
    (paths.posts_dir / "notes.txt").write_text("x")
    assert [x.id for x in P.list_posts(paths)] == [p.id]
    assert conflict.exists()


def test_corrupt_post_is_moved_aside(paths: Paths) -> None:
    p = P.create_post(paths, title="a", photos=[h(1)])
    f = paths.posts_dir / f"{p.id}.json"
    f.write_text("{not json")
    assert P.list_posts(paths) == []
    assert not f.exists()
    assert any(x.name.startswith(f"{p.id}.json.corrupt-") for x in paths.posts_dir.iterdir())


def test_bad_ids_rejected(paths: Paths) -> None:
    assert P.get_post(paths, "../../etc/passwd") is None
    P.delete_post(paths, "../x")  # no exception, nothing happens


def test_duplicate_is_a_fresh_draft(paths: Paths) -> None:
    p = P.create_post(paths, title="a", photos=[h(1)], caption="c")
    p.status = "scheduled"
    p.media_cache = {h(1): {"media_id": "m"}}
    p.log = [{"action": "send"}]
    P.save_post(paths, p)
    d = P.duplicate_post(paths, p.id)
    assert d is not None and d.id != p.id
    assert d.status == "draft" and d.editable
    assert d.media_cache == {} and d.log == []
    assert d.caption == "c" and d.photos == [h(1)]


def test_posted_hashes_tracks_sent_posts(paths: Paths) -> None:
    a = P.create_post(paths, title="a", photos=[h(1), h(2)])
    P.create_post(paths, title="b", photos=[h(3)])
    assert P.posted_hashes(paths) == frozenset()
    a.status = "scheduled"
    P.save_post(paths, a)
    # mtime granularity: force the directory mtime forward so the cache sees it
    st = paths.posts_dir.stat()
    os.utime(paths.posts_dir, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    assert P.posted_hashes(paths) == frozenset({h(1), h(2)})
    assert [p.title for p in P.posts_containing(paths, h(2))] == ["a"]


def test_coerce_tolerates_bad_fields(paths: Paths) -> None:
    pid = "0123456789abcdef"
    (paths.posts_dir / f"{pid}.json").write_text(
        json.dumps(
            {
                "id": pid,
                "title": "",
                "photos": [1, h(1), None],
                "status": "bogus",
                "network_settings": {"pinterest": {"pinTitle": "x"}, "junk": 3},
                "log": ["x", {"action": "send"}],
            }
        )
    )
    p = P.get_post(paths, pid)
    assert p is not None
    assert p.photos == [h(1)] and p.status == "draft" and p.title == "Untitled post"
    assert p.network_settings == {"pinterest": {"pinTitle": "x"}}
    assert p.log == [{"action": "send"}]


def test_channel_cache_holds_no_key(paths: Paths) -> None:
    P.save_channel_cache(paths, [IG, PIN])
    chans, fetched = P.load_channel_cache(paths)
    assert chans == [IG, PIN] and fetched
    assert "apikey" not in paths.robopost_file.read_text().lower()


# --- validation ----------------------------------------------------------------


def _errors(post: P.Post, mode: P.SendMode = "schedule", **kw: object) -> list[str]:
    return P.validate(post, CHANNELS, mode, now=NOW, **kw)[0]  # type: ignore[arg-type]


def test_schedule_needs_channel_and_future_time() -> None:
    p = _post()
    errs = _errors(p)
    assert any("channel" in e for e in errs) and any("date and time" in e for e in errs)
    p.channel_ids = ["fb"]
    p.schedule_at = (NOW + timedelta(minutes=1)).isoformat()
    assert any("future" in e for e in _errors(p))
    p.schedule_at = (NOW + timedelta(days=1)).isoformat()
    assert _errors(p) == []


def test_draft_needs_only_photos() -> None:
    assert _errors(_post(), "draft") == []
    assert any("at least one photo" in e for e in _errors(_post(photos=[]), "draft"))


def test_instagram_ten_photo_limit() -> None:
    p = _post(channel_ids=["ig"], photos=[h(i) for i in range(11)])
    assert any("at most 10" in e for e in _errors(p, "draft"))
    p.photos = p.photos[:10]
    assert _errors(p, "draft") == []


def test_soft_limits_are_warnings() -> None:
    p = _post(channel_ids=["bs"], photos=[h(i) for i in range(5)], caption="x" * 301)
    errors, warnings = P.validate(p, CHANNELS, "draft", now=NOW)
    assert errors == []
    assert any("at most 4" in w for w in warnings)
    assert any("301 characters" in w for w in warnings)


def test_pinterest_needs_title_and_valid_link() -> None:
    p = _post(channel_ids=["pin"])
    assert any("pin title" in e for e in _errors(p, "draft"))
    p.network_settings = {"pinterest": {"pinTitle": "T", "destinationLink": "ftp://x"}}
    assert any("http" in e for e in _errors(p, "draft"))
    p.network_settings["pinterest"]["destinationLink"] = "https://example.com"
    assert _errors(p, "draft") == []


def test_reconnect_unknown_unavailable_and_sent() -> None:
    assert any("reconnected" in e for e in _errors(_post(channel_ids=["old"]), "draft"))
    assert any("no longer" in e for e in _errors(_post(channel_ids=["gone"]), "draft"))
    assert any("no file" in e for e in _errors(_post(), "draft", unavailable={h(1)}))
    assert any("already sent" in e for e in _errors(_post(status="scheduled"), "draft"))
    assert any("already sent" in e for e in _errors(_post(status="sending"), "draft"))


# --- payload -------------------------------------------------------------------


def test_payload_draft_minimal() -> None:
    p = _post()
    assert P.build_payload(p, CHANNELS, "draft", ["m1"], now=NOW) == {
        "text": "hello",
        "image_object_ids": ["m1"],
        "is_draft": True,
    }


def test_payload_schedule_full() -> None:
    local = datetime(2026, 10, 1, 9, 30, tzinfo=UTC).astimezone()
    p = _post(
        channel_ids=["ig", "pin", "fb"],
        schedule_at=local.isoformat(),
        first_comment="#tags",
        network_settings={
            "instagram": {"postType": "STORIES"},
            "facebook": {"postType": "STORIES"},  # not valid for FB -> POST
            "pinterest": {"pinTitle": " Title ", "destinationLink": ""},
        },
    )
    body = P.build_payload(p, CHANNELS, "schedule", ["m1", "m2"], now=NOW)
    assert body == {
        "text": "hello",
        "image_object_ids": ["m1", "m2"],
        "is_draft": False,
        "channel_ids": ["ig", "pin", "fb"],
        "schedule_at": "2026-10-01T09:30:00Z",
        "first_comment": "#tags",
        "instagram_settings": {"postType": "STORIES"},
        "facebook_settings": {"postType": "POST"},
        "pinterest_settings": {"pinTitle": "Title"},
    }


def test_payload_omits_settings_for_unticked_networks_and_past_time_on_draft() -> None:
    p = _post(
        channel_ids=["bs"],
        schedule_at=(NOW - timedelta(hours=1)).isoformat(),
        network_settings={"instagram": {"postType": "REELS"}},
    )
    body = P.build_payload(p, CHANNELS, "draft", ["m"], now=NOW)
    assert "instagram_settings" not in body and "schedule_at" not in body
    assert body["channel_ids"] == ["bs"]


def test_instagram_sizing_settings_validated_and_not_in_payload() -> None:
    p = P.Post(title="t", photos=["a" * 64], channel_ids=["ig1"])
    assert (p.ig_format(), p.ig_border(), p.ig_color()) == ("portrait", 0, "#ffffff")
    p.network_settings["instagram"] = {
        "format": "square",
        "border": "999",
        "padColor": "#ABCDEF",
    }
    assert (p.ig_format(), p.ig_border(), p.ig_color()) == ("square", 100, "#abcdef")
    p.network_settings["instagram"].update(format="story", border="x", padColor="red")
    assert (p.ig_format(), p.ig_border(), p.ig_color()) == ("portrait", 0, "#ffffff")
    p.network_settings["instagram"]["postType"] = "STORIES"
    assert p.ig_format() == "story"
    body = P.build_payload(
        p, [Channel(id="ig1", name="IG", social_network="INSTAGRAM")], "draft", ["m1"]
    )
    assert body["instagram_settings"] == {"postType": "STORIES"}
    assert "padColor" not in str(body) and "border" not in str(body)
