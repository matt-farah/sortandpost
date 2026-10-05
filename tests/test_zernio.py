"""Zernio specifics: its create body. (Everything the shared client does is covered
for Zernio by test_posting_client.py.)"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sortandpost import posts as P
from sortandpost.posting.model import Channel, MediaRef
from sortandpost.posting.zernio import SPEC as ZN

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
IG = Channel(id="ig", name="IG", social_network="instagram")
FB = Channel(id="fb", name="Page", social_network="facebook")
PIN = Channel(id="pin", name="Pins", social_network="pinterest")
CHANNELS = [IG, FB, PIN]
MEDIA = [
    MediaRef("k1", "https://media.example/1.jpg"),
    MediaRef("k2", "https://media.example/2.jpg"),
]


def _post(**kw: object) -> P.Post:
    base: dict[str, object] = {"title": "t", "photos": ["a" * 64, "b" * 64], "caption": "hi"}
    base.update(kw)
    return P.Post(**base)  # type: ignore[arg-type]


def _body(p: P.Post, mode: P.SendMode = "draft") -> dict[str, Any]:
    return ZN.build_payload(P.make_request(p, CHANNELS, mode, MEDIA, ZN, now=NOW))


def test_draft_with_no_channel_has_no_platforms_and_is_a_draft() -> None:
    assert _body(_post()) == {
        "content": "hi",
        "mediaItems": [
            {"type": "image", "url": "https://media.example/1.jpg"},
            {"type": "image", "url": "https://media.example/2.jpg"},
        ],
        "platforms": [],
        "isDraft": True,
    }


def test_schedule_sends_the_time_and_no_draft_flag() -> None:
    when = (NOW + timedelta(days=1)).isoformat()
    body = _body(_post(channel_ids=["fb"], schedule_at=when), "schedule")
    assert body["scheduledFor"] == "2026-10-06T12:00:00Z"
    assert "isDraft" not in body
    assert body["platforms"] == [{"platform": "facebook", "accountId": "fb"}]


def test_instagram_story_and_pinterest_settings() -> None:
    p = _post(
        channel_ids=["ig", "pin"],
        network_settings={
            "instagram": {"postType": "story"},
            "pinterest": {"pinTitle": "Hello", "board": "b1", "destinationLink": "https://x.y"},
        },
        first_comment="#tag",
    )
    plat = {x["accountId"]: x for x in _body(p)["platforms"]}
    assert plat["ig"]["platformSpecificData"] == {"contentType": "story"}  # no first comment
    assert plat["pin"]["platformSpecificData"] == {
        "title": "Hello",
        "boardId": "b1",
        "link": "https://x.y",
    }


def test_first_comment_goes_to_networks_that_take_one() -> None:
    plat = {
        x["accountId"]: x
        for x in _body(_post(channel_ids=["ig", "fb", "pin"], first_comment="#tag"))["platforms"]
    }
    assert plat["ig"]["platformSpecificData"] == {"firstComment": "#tag"}  # default type is omitted
    assert plat["fb"]["platformSpecificData"] == {"firstComment": "#tag"}
    assert "platformSpecificData" not in plat["pin"]
