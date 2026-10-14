"""robopost.app (https://robopost.app/docs/robopost-api/), as a ProviderSpec.

API facts verified live 2026-09-25: the key is an ``?apikey=`` query parameter;
the exact paths below matter (the other slash form gets a 307 to plain http
carrying the key); ``image_object_ids`` takes the upload's
``storage_object_id``, not its ``id`` (robopost silently accepts the wrong id
and posts no image). There is no read / edit / delete for a post.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from sortandpost.posting.model import CreateRequest
from sortandpost.posting.spec import AuthSpec, FieldSpec, Option, ProviderSpec

_POST_TYPES = (
    Option("POST", "Post"),
    Option("REELS", "Reels"),
    Option("STORIES", "Stories"),
)


def build_payload(req: CreateRequest) -> dict[str, Any]:
    """The ``POST /v1/scheduled_posts`` body. Network settings go in
    ``<network>_settings``."""
    payload: dict[str, Any] = {
        "text": req.caption,
        "image_object_ids": [m.id for m in req.media],
        "is_draft": req.mode == "draft",
    }
    if req.channels:
        payload["channel_ids"] = [c.id for c in req.channels]
    if req.schedule_at is not None:
        payload["schedule_at"] = req.schedule_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if req.first_comment.strip():
        payload["first_comment"] = req.first_comment
    for network, values in req.settings.items():
        if values:
            payload[f"{network}_settings"] = dict(values)
    return payload


SPEC = ProviderSpec(
    key="robopost",
    name="Robopost",
    about="uploads JPEG copies of your photos to robopost.app and creates a draft or "
    "scheduled post.",
    key_help="Get an API key in robopost → Team → Manage API Keys.",
    account_label="team",
    env_var="ROBOPOST_API_KEY",
    keychain_account="robopost_api_key",
    default_base_url="https://public-api.robopost.app/v1",
    auth=AuthSpec("query", "apikey"),
    endpoints={
        "auth_check": ("GET", "/auth"),
        "channels": ("GET", "/channels/"),
        "upload": ("POST", "/medias/upload"),
        "create": ("POST", "/scheduled_posts"),
    },
    auth_label="team_name",
    channel_fields={
        "id": "id",
        "name": "name",
        "network": "social_network",
        "logo": "logo_url",
    },
    channel_reconnect=("must_reconnect",),
    channel_skip=("is_deleted",),
    upload_id="storage_object_id",
    created_ids="scheduled_posts[*].id",
    friendly_codes={
        "PLAN_LIMIT_HIT": "robopost says your plan limit is reached.",
        "ACCOUNT_USAGE_FROZEN": "robopost says this account's usage is frozen.",
        "VIDEO_CORRUPTED": "robopost rejected the file as corrupted.",
    },
    status_hints={
        401: "robopost rejected the API key. Check it in Settings, or create a new one in "
        "robopost → Team → Manage API Keys.",
    },
    channelless_drafts=True,
    first_comment=True,
    network_fields={
        "instagram": (
            FieldSpec(
                "postType", "Instagram post type", "select", options=_POST_TYPES, default="POST"
            ),
        ),
        "facebook": (
            FieldSpec(
                "postType", "Facebook post type", "select", options=_POST_TYPES[:2], default="POST"
            ),
        ),
        "pinterest": (
            FieldSpec("pinTitle", "Pin title", required=True),
            FieldSpec(
                "destinationLink",
                "Destination link (optional)",
                "url",
                placeholder="https://…",
            ),
        ),
    },
    build_payload=build_payload,
)
