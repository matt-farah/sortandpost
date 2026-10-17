"""Per-photo alt text: stored on the post, mapped by each provider's spec, left
out where it can't go. (The refused-then-resent flow is in test_send_post.py.)"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sortandpost import posts as P
from sortandpost.paths import Paths
from sortandpost.posting.model import Channel, MediaRef
from sortandpost.posting.postiz import SPEC as PZ
from sortandpost.posting.robopost import SPEC as RP
from sortandpost.posting.spec import ProviderSpec
from sortandpost.posting.upload_post import SPEC as UP
from sortandpost.posting.zernio import SPEC as ZN

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
A, B = "a" * 64, "b" * 64
MEDIA = [MediaRef("m1", "https://u/1.jpg"), MediaRef("m2", "https://u/2.jpg")]
BSKY = Channel("bs", "Sky", "bluesky", account="me")
IG = Channel("ig", "IG", "instagram", account="me")
X = Channel("x", "X", "x", account="me")
MASTO = Channel("ma", "Toot", "mastodon", account="me")
PIN = Channel("pin", "Pins", "pinterest", account="me")
ALL = [BSKY, IG, X, MASTO, PIN]


def _post(**kw: object) -> P.Post:
    base: dict[str, object] = {
        "title": "t",
        "photos": [A, B],
        "caption": "hi",
        "alt_texts": {A: "A red door", B: "  "},
        "schedule_at": (NOW + timedelta(days=1)).isoformat(),
    }
    base.update(kw)
    return P.Post(**base)  # type: ignore[arg-type]


def _body(p: P.Post, spec: ProviderSpec, chans: list[Channel]) -> dict[str, Any]:
    p.channel_ids = [c.id for c in chans]
    req = P.make_request(p, ALL, "schedule", MEDIA, spec, now=NOW)
    return spec.build_payload(req)


def test_post_round_trips_alt_text_and_duplicate_keeps_it(paths: Paths) -> None:
    p = P.save_post(paths, _post(omit_alt=True))
    raw = json.loads((paths.posts_dir / f"{p.id}.json").read_text())
    assert raw["alt_texts"] == {A: "A red door", B: "  "} and raw["omit_alt"] is True
    got = P.get_post(paths, p.id)
    assert got is not None and got.omit_alt and got.alt_text(A) == "A red door"
    assert got.alt_text(B) == "" and got.has_alt_text()
    copy = P.duplicate_post(paths, p.id)
    assert copy is not None and copy.alt_texts[A] == "A red door" and not copy.omit_alt


def test_old_post_file_has_no_alt_text(paths: Paths) -> None:
    p = P.save_post(paths, _post(alt_texts={}))
    f = paths.posts_dir / f"{p.id}.json"
    raw = json.loads(f.read_text())
    del raw["alt_texts"], raw["omit_alt"]
    f.write_text(json.dumps(raw))
    got = P.get_post(paths, p.id)
    assert got is not None and got.alt_texts == {} and not got.omit_alt


def test_postiz_sends_alt_on_each_image() -> None:
    body = _body(_post(), PZ, [BSKY])
    assert body["posts"][0]["value"][0]["image"] == [
        {"id": "m1", "path": "https://u/1.jpg", "alt": "A red door"},
        {"id": "m2", "path": "https://u/2.jpg"},
    ]


def test_postiz_leaves_alt_out_when_no_channel_reads_it() -> None:
    body = _body(_post(), PZ, [IG])
    assert all("alt" not in i for i in body["posts"][0]["value"][0]["image"])


def test_omit_alt_sends_none() -> None:
    body = _body(_post(omit_alt=True), PZ, [BSKY])
    assert all("alt" not in i for i in body["posts"][0]["value"][0]["image"])


def test_zernio_alt_text_on_media_items() -> None:
    body = _body(_post(), ZN, [IG, X])
    assert body["mediaItems"] == [
        {"type": "image", "url": "https://u/1.jpg", "altText": "A red door"},
        {"type": "image", "url": "https://u/2.jpg"},
    ]


def test_upload_post_alt_fields_per_network() -> None:
    form = _body(_post(alt_texts={A: "A red door", B: "a | b"}), UP, [BSKY, IG, X, MASTO, PIN])
    assert json.loads(form["bluesky_alt_text"]) == ["A red door", "a | b"]
    assert json.loads(form["instagram_alt_text"]) == ["A red door", "a | b"]
    assert form["x_alt_text[]"] == ["A red door", "a | b"]
    assert form["mastodon_alt_text"] == "A red door|a / b"
    assert form["pinterest_alt_text"] == "A red door"


def test_upload_post_story_gets_no_instagram_alt() -> None:
    p = _post(network_settings={"instagram": {"postType": "story"}})
    form = _body(p, UP, [IG, BSKY])
    assert "instagram_alt_text" not in form and "bluesky_alt_text" in form


def test_upload_post_without_alt_text_has_no_fields() -> None:
    form = _body(_post(alt_texts={}), UP, [BSKY, X])
    assert not [k for k in form if "alt_text" in k]


def test_robopost_never_sends_alt_and_warns() -> None:
    p = _post(channel_ids=["ig"])
    req = P.make_request(p, ALL, "schedule", MEDIA, RP, now=NOW)
    assert req.alt_texts == ()
    assert "alt" not in json.dumps(RP.build_payload(req))
    _err, warn = P.validate(p, ALL, "schedule", RP, now=NOW)
    assert "Robopost can't send alt text; it is left out." in warn


def test_warnings_name_networks_alt_text_misses_and_long_texts() -> None:
    p = _post(alt_texts={A: "x" * 1200}, channel_ids=["bs", "ig", "x"])
    _err, warn = P.validate(p, ALL, "schedule", PZ, now=NOW)
    assert any(w.startswith("Alt text isn't sent to Instagram, X through Postiz") for w in warn)
    # Bluesky (via Postiz) has no limit below the input's maximum
    assert not any("allows 1000" in w for w in warn)
    _err, warn = P.validate(p, ALL, "schedule", ZN, now=NOW)
    assert "Alt text for photo 1 is 1200 characters; X allows 1000." in warn
    assert "Alt text for photo 1 is 1200 characters; Instagram allows 1000." in warn


def test_no_warnings_without_alt_text() -> None:
    p = _post(alt_texts={}, channel_ids=["bs"])
    _err, warn = P.validate(p, ALL, "schedule", RP, now=NOW)
    assert not [w for w in warn if "lt text" in w]


def test_omitted_alt_text_is_noted() -> None:
    p = _post(omit_alt=True, channel_ids=["bs"])
    _err, warn = P.validate(p, ALL, "schedule", PZ, now=NOW)
    assert "Alt text is left out of this send (you chose to send without it)." in warn
