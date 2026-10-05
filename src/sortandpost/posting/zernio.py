"""Zernio (https://docs.zernio.com), formerly Late, as a ProviderSpec.

From its OpenAPI spec (checked 2026-10-05; to be verified live): base
``https://zernio.com/api``, ``Authorization: Bearer <key>``. Uploads are
presigned: ``POST /v1/media/presign`` then a ``PUT`` to storage; the returned
``publicUrl`` goes in ``mediaItems``. A create is one body for every channel
(``platforms[]``); with no ``scheduledFor`` / ``publishNow`` it is a draft, and
a draft may omit ``platforms``. A first comment is a per-network setting inside
``platformSpecificData``. The free plan is limited to 60 requests a minute.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from sortandpost.posting.model import CreateRequest
from sortandpost.posting.spec import AuthSpec, FieldSpec, Option, ProviderSpec, RateLimit

# networks whose platformSpecificData takes a firstComment
_FIRST_COMMENT_NETWORKS = {"instagram", "facebook", "threads", "linkedin", "youtube"}


def build_payload(req: CreateRequest) -> dict[str, Any]:
    """The ``POST /v1/posts`` body."""
    platforms: list[dict[str, Any]] = []
    for c in req.channels:
        data: dict[str, Any] = dict(req.settings_for(c))
        if data.get("contentType") == "post":  # the default; only a Story is spelled out
            del data["contentType"]
        story = c.network == "instagram" and data.get("contentType") == "story"
        if req.first_comment.strip() and c.network in _FIRST_COMMENT_NETWORKS and not story:
            data["firstComment"] = req.first_comment
        entry: dict[str, Any] = {"platform": c.social_network, "accountId": c.id}
        if data:
            entry["platformSpecificData"] = data
        platforms.append(entry)
    body: dict[str, Any] = {
        "content": req.caption,
        "mediaItems": [{"type": "image", "url": m.path} for m in req.media],
        "platforms": platforms,
    }
    if req.mode == "draft":
        body["isDraft"] = True  # wins over a scheduledFor, which is kept as the intended time
    if req.schedule_at is not None:
        body["scheduledFor"] = req.schedule_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return body


SPEC = ProviderSpec(
    key="zernio",
    name="Zernio",
    about="uploads clean JPEG copies of your photos to Zernio (free for 2 accounts) and creates "
    "a draft or scheduled post.",
    key_help="Create an API key at zernio.com → Dashboard → API keys.",
    env_var="ZERNIO_API_KEY",
    keychain_account="zernio_api_key",
    default_base_url="https://zernio.com/api",
    auth=AuthSpec("header", "Authorization", "Bearer "),
    endpoints={
        "auth_check": ("GET", "/v1/accounts"),
        "channels": ("GET", "/v1/accounts"),
        "upload": ("POST", "/v1/media/presign"),
        "create": ("POST", "/v1/posts"),
    },
    channels_at="accounts",
    channel_fields={
        "id": "_id",
        "name": "displayName",
        "network": "platform",
        "logo": "profilePicture",
    },
    channel_reconnect=("needsReconnection",),
    channel_require=("isActive",),
    upload_style="presigned",
    upload_put_url="uploadUrl",
    upload_id="key",
    upload_path="publicUrl",
    created_ids="post._id",
    error_fields=("error", "message", "msg", "detail"),
    status_hints={
        401: "Zernio rejected the API key. Check it in Settings, or create a new one at "
        "zernio.com → Dashboard → API keys.",
        409: "Zernio already has this exact post for that account (it refuses the same text and "
        "photos twice within 24 hours). Change the caption or photos to send it again.",
    },
    # 60 requests a minute on the free plan; each photo costs one presign request
    rate_limits={"upload": RateLimit(50, 60)},
    idempotency_header="Idempotency-Key",
    channelless_drafts=True,
    first_comment=True,
    network_fields={
        "instagram": (
            FieldSpec(
                "postType",
                "Instagram post type",
                "select",
                options=(Option("post", "Post"), Option("story", "Story")),
                default="post",
                wire_key="contentType",
            ),
        ),
        "pinterest": (
            FieldSpec("pinTitle", "Pin title (optional)", wire_key="title"),
            FieldSpec("board", "Board ID (optional)", wire_key="boardId"),
            FieldSpec(
                "destinationLink",
                "Destination link (optional)",
                "url",
                wire_key="link",
                placeholder="https://…",
            ),
        ),
    },
    build_payload=build_payload,
)
