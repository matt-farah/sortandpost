"""Postiz (https://docs.postiz.com/public-api), cloud or self-hosted, as a ProviderSpec.

From the published OpenAPI spec (checked 2026-10-05): the key is the
``Authorization`` header (no "Bearer"); cloud is
``https://api.postiz.com/public/v1``, a self-hosted server
``https://<host>/api/public/v1``. A create takes one ``posts[]`` item per
channel, each with ``settings.__type`` = the channel's identifier. Only
creating posts is rate-limited (90 an hour self-hosted by default, 100 on
cloud; self-hosters raise it with ``API_LIMIT``, here with
``posting.rate_limit_overrides``).
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from sortandpost.posting.model import CreateRequest
from sortandpost.posting.spec import (
    AuthSpec,
    BaseUrlPreset,
    FieldSpec,
    Option,
    ProviderSpec,
    RateLimit,
)

CLOUD_URL = "https://api.postiz.com/public/v1"


def build_payload(req: CreateRequest) -> dict[str, Any]:
    """The ``POST /posts`` body: one ``posts[]`` item per channel. The first
    comment goes as the thread's second value."""
    when = (req.schedule_at or req.now).astimezone(UTC)
    value: list[dict[str, Any]] = [
        {"content": req.caption, "image": [{"id": m.id, "path": m.path} for m in req.media]}
    ]
    if req.first_comment.strip():
        value.append({"content": req.first_comment, "image": []})
    return {
        "type": "draft" if req.mode == "draft" else "schedule",
        "date": when.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "shortLink": False,
        "tags": [],
        "posts": [
            {
                "integration": {"id": c.id},
                "value": value,
                "settings": {"__type": c.social_network, **req.settings_for(c)},
            }
            for c in req.channels
        ],
    }


SPEC = ProviderSpec(
    key="postiz",
    name="Postiz",
    about="uploads clean JPEG copies of your photos to Postiz (cloud or your own server) and "
    "creates a draft or scheduled post.",
    key_help="Get an API key in Postiz → Settings → Public API.",
    env_var="POSTIZ_API_KEY",
    keychain_account="postiz_api_key",
    default_base_url=CLOUD_URL,
    base_url_editable=True,
    base_url_presets=(
        BaseUrlPreset("Postiz cloud", CLOUD_URL),
        BaseUrlPreset(
            "Self-hosted",
            "https://",
            "Your server's address, e.g. https://postiz.example.com or "
            "http://192.168.1.20:5000 — the /api/public/v1 path is added for you.",
        ),
    ),
    base_url_paths=(("api.postiz.com", "/public/v1"), ("*", "/api/public/v1")),
    allow_lan_http=True,
    auth=AuthSpec("header", "Authorization"),
    endpoints={
        "auth_check": ("GET", "/is-connected"),
        "channels": ("GET", "/integrations"),
        "upload": ("POST", "/upload"),
        "create": ("POST", "/posts"),
    },
    auth_ok="connected",
    channel_fields={
        "id": "id",
        "name": "name",
        "network": "identifier",
        "logo": "picture",
    },
    channel_reconnect=("disabled", "refreshNeeded", "inBetweenSteps"),
    channel_skip=("deletedAt",),
    upload_id="id",
    upload_path="path",
    created_ids="[*].postId",
    status_hints={
        401: "Postiz rejected the API key. Check it in Settings, or copy it again from "
        "Postiz → Settings → Public API.",
    },
    rate_limits={"create": RateLimit(90, 3600)},
    # a self-hosted server took > 60 s to answer a one-photo draft (it was created;
    # verified live 2026-10-05), so wait well past that before calling it unknown
    create_timeout_s=180.0,
    channelless_drafts=False,
    first_comment=True,
    network_fields={
        "instagram": (
            FieldSpec(
                "postType",
                "Instagram post type",
                "select",
                options=(Option("POST", "Post", "post"), Option("STORIES", "Stories", "story")),
                default="POST",
                wire_key="post_type",
            ),
        ),
        "pinterest": (
            FieldSpec("board", "Board ID", required=True, placeholder="the Pinterest board's id"),
            FieldSpec("pinTitle", "Pin title (optional)", wire_key="title"),
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
