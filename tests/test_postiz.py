"""Postiz specifics: its create body and its base-URL rules. (Everything the
shared client does is covered for Postiz by test_posting_client.py.)"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from sortandpost import posts as P
from sortandpost.posting.client import check_base_url, is_lan_host, normalize_base_url
from sortandpost.posting.model import Channel, MediaRef
from sortandpost.posting.postiz import CLOUD_URL
from sortandpost.posting.postiz import SPEC as PZ
from sortandpost.posting.robopost import SPEC as RP

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
IG = Channel(id="ig", name="IG", social_network="instagram-standalone")
FB = Channel(id="fb", name="Page", social_network="facebook")
PIN = Channel(id="pin", name="Pins", social_network="pinterest")
CHANNELS = [IG, FB, PIN]
MEDIA = [MediaRef("m1", "https://u/1.jpg"), MediaRef("m2", "https://u/2.jpg")]


def _post(**kw: object) -> P.Post:
    base: dict[str, object] = {"title": "t", "photos": ["a" * 64, "b" * 64], "caption": "hi"}
    base.update(kw)
    return P.Post(**base)  # type: ignore[arg-type]


def _body(p: P.Post, mode: P.SendMode = "draft") -> dict[str, Any]:
    return PZ.build_payload(P.make_request(p, CHANNELS, mode, MEDIA, PZ, now=NOW))


def test_draft_body_one_item_per_channel() -> None:
    body = _body(_post(channel_ids=["ig", "fb"]))
    assert body == {
        "type": "draft",
        "date": "2026-10-05T12:00:00.000Z",
        "shortLink": False,
        "tags": [],
        "posts": [
            {
                "integration": {"id": "ig"},
                "value": [
                    {
                        "content": "hi",
                        "image": [
                            {"id": "m1", "path": "https://u/1.jpg"},
                            {"id": "m2", "path": "https://u/2.jpg"},
                        ],
                    }
                ],
                "settings": {"__type": "instagram-standalone", "post_type": "post"},
            },
            {
                "integration": {"id": "fb"},
                "value": body["posts"][0]["value"],
                "settings": {"__type": "facebook"},
            },
        ],
    }


def test_schedule_story_pinterest_and_first_comment() -> None:
    when = NOW + timedelta(days=1, hours=2)
    p = _post(
        channel_ids=["ig", "pin"],
        schedule_at=when.isoformat(),
        first_comment="#tags",
        network_settings={
            "instagram": {"postType": "STORIES"},
            "pinterest": {"board": "b-1", "pinTitle": " Title ", "destinationLink": ""},
        },
    )
    body = _body(p, "schedule")
    assert body["type"] == "schedule" and body["date"] == "2026-10-06T14:00:00.000Z"
    ig, pin = body["posts"]
    assert ig["settings"] == {"__type": "instagram-standalone", "post_type": "story"}
    assert pin["settings"] == {"__type": "pinterest", "board": "b-1", "title": "Title"}
    assert ig["value"][1] == {"content": "#tags", "image": []}


def test_reels_is_not_a_postiz_choice() -> None:
    p = _post(channel_ids=["ig"], network_settings={"instagram": {"postType": "REELS"}})
    assert _body(p)["posts"][0]["settings"]["post_type"] == "post"


def test_postiz_drafts_need_a_channel_and_pinterest_a_board() -> None:
    errs = P.validate(_post(), CHANNELS, "draft", PZ, now=NOW)[0]
    assert any("Pick at least one channel" in e for e in errs)
    assert P.validate(_post(), CHANNELS, "draft", RP, now=NOW)[0] == []  # robopost doesn't
    errs = P.validate(_post(channel_ids=["pin"]), CHANNELS, "draft", PZ, now=NOW)[0]
    assert any("board id" in e for e in errs)
    assert not any("pin title" in e for e in errs)  # optional on Postiz


def test_neutral_settings_survive_a_provider_switch() -> None:
    p = _post(
        channel_ids=["pin"],
        network_settings={"pinterest": {"pinTitle": "T", "destinationLink": "https://x.y"}},
    )
    assert P.network_values(p, RP, "pinterest") == {
        "pinTitle": "T",
        "destinationLink": "https://x.y",
    }
    assert P.network_values(p, PZ, "pinterest") == {
        "board": "",
        "title": "T",
        "link": "https://x.y",
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", CLOUD_URL),
        ("api.postiz.com", CLOUD_URL),
        ("https://api.postiz.com/", CLOUD_URL),
        ("https://postiz.example.com", "https://postiz.example.com/api/public/v1"),
        ("postiz.example.com/", "https://postiz.example.com/api/public/v1"),
        ("http://192.168.1.20:5000", "http://192.168.1.20:5000/api/public/v1"),
        ("https://p.example.com/custom/v1/", "https://p.example.com/custom/v1"),
    ],
)
def test_normalize_base_url(raw: str, expected: str) -> None:
    assert normalize_base_url(PZ, raw) == expected


@pytest.mark.parametrize(
    ("url", "ok", "warns"),
    [
        (CLOUD_URL, True, False),
        ("http://192.168.1.20:5000/api/public/v1", True, True),
        ("http://localhost:5000/api/public/v1", True, True),
        ("http://nas.local/api/public/v1", True, True),
        ("http://postiz/api/public/v1", True, True),
        ("http://postiz.example.com/api/public/v1", False, False),
        ("http://8.8.8.8/api/public/v1", False, False),
        ("ftp://x.example/api", False, False),
    ],
)
def test_https_or_lan_http(url: str, ok: bool, warns: bool) -> None:
    err, warning = check_base_url(PZ, url)
    assert (err is None) == ok
    assert (warning is not None) == warns
    rp_err, _ = check_base_url(RP, url)
    assert (rp_err is None) == url.startswith("https://")  # robopost: https only


def test_is_lan_host() -> None:
    assert is_lan_host("10.0.0.5") and is_lan_host("[::1]") and is_lan_host("box.home.arpa")
    assert not is_lan_host("example.com") and not is_lan_host("1.1.1.1")
