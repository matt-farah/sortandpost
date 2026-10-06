"""Upload-Post specifics: its channel list, its create form, and the inline
(multipart) send. (The shared client checks run on it in test_posting_client.py.)"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from sortandpost import posts as P
from sortandpost.posting.client import ApiClient
from sortandpost.posting.model import Channel, MediaRef
from sortandpost.posting.upload_post import SPEC as UP
from sortandpost.posting.upload_post import parse_channels

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
USERS = {
    "success": True,
    "profiles": [
        {
            "username": "mine",
            "social_accounts": {
                "instagram": {"display_name": "Me IG", "social_images": "https://i/x.png"},
                "x": {"handle": "me", "reauth_required": True},
                "tiktok": "",
                "bluesky": None,
            },
        },
        {"username": "other", "social_accounts": {"facebook": {"username": "pg"}}},
        {"social_accounts": {"instagram": {}}},
    ],
}
IG = Channel("mine:instagram", "Me IG (mine)", "instagram", account="mine")
PIN = Channel("mine:pinterest", "Pins (mine)", "pinterest", account="mine")
FB2 = Channel("other:facebook", "pg (other)", "facebook", account="other")


def _post(**kw: object) -> P.Post:
    base: dict[str, object] = {"title": "t", "photos": ["a" * 64], "caption": "hi"}
    base.update(kw)
    return P.Post(**base)  # type: ignore[arg-type]


def _form(p: P.Post, chans: list[Channel], **kw: object) -> dict[str, object]:
    when = NOW + timedelta(days=1)
    p.schedule_at = when.isoformat()
    req = P.make_request(
        p, chans, "schedule", [MediaRef("x", "/x.jpg")], UP, now=NOW, only=tuple(chans)
    )
    return UP.build_payload(req)


def test_channels_are_connected_platforms_per_profile() -> None:
    chans = parse_channels(USERS)
    assert [(c.id, c.account, c.must_reconnect) for c in chans] == [
        ("mine:instagram", "mine", False),
        ("mine:x", "mine", True),
        ("other:facebook", "other", False),
    ]
    assert chans[0].name == "Me IG (mine)" and chans[0].logo_url == "https://i/x.png"


def test_form_has_user_platforms_and_utc_schedule() -> None:
    form = _form(_post(first_comment="tags"), [IG, PIN])
    assert form["user"] == "mine"
    assert form["platform[]"] == ["instagram", "pinterest"]
    assert form["title"] == "hi" and form["timezone"] == "UTC"
    assert form["scheduled_date"] == "2026-10-06T12:00:00"
    assert form["instagram_first_comment"] == "tags"


def test_story_and_network_settings_use_wire_keys() -> None:
    p = _post(
        first_comment="x",
        network_settings={
            "instagram": {"postType": "story"},
            "pinterest": {"pinTitle": "T", "board": "b1"},
        },
    )
    form = _form(p, [IG, PIN])
    assert form["media_type"] == "STORIES"
    assert "instagram_first_comment" not in form  # a Story has no comment
    assert form["pinterest_title"] == "T" and form["pinterest_board_id"] == "b1"


def test_drafts_and_mixed_profiles_are_refused() -> None:
    p = _post(channel_ids=[IG.id, FB2.id])
    errors, _ = P.validate(p, [IG, FB2], "draft", UP, now=NOW)
    assert any("no drafts" in e for e in errors)
    errors, _ = P.validate(p, [IG, FB2], "schedule", UP, now=NOW)
    assert any("one account at a time" in e for e in errors)
    ok = _post(channel_ids=[IG.id], schedule_at=(NOW + timedelta(days=1)).isoformat())
    assert P.validate(ok, [IG], "schedule", UP, now=NOW)[0] == []


def test_create_posts_the_files_as_multipart_with_the_form(tmp_path: Path) -> None:
    f = tmp_path / "00_abc.jpg"
    f.write_bytes(b"\xff\xd8jpg")
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        req.read()
        return httpx.Response(202, json={"success": True, "job_id": "job-9"})

    with ApiClient(UP, "k" * 20, transport=httpx.MockTransport(handler)) as api:
        ref = api.upload_media(f)  # no request: the path stands in
        assert not seen and ref.path == str(f)
        req = P.make_request(
            _post(schedule_at=(NOW + timedelta(days=1)).isoformat()),
            [IG],
            "schedule",
            [ref],
            UP,
            now=NOW,
            only=(IG,),
        )
        assert api.create_post(req) == ["job-9"]
    (r,) = seen
    assert r.url.path == "/api/upload_photos"
    assert r.headers["authorization"] == "Apikey " + "k" * 20
    body = r.content
    assert b'name="photos[]"; filename="00_abc.jpg"' in body
    assert b'name="user"' in body and b'name="platform[]"' in body and b"mine" in body
