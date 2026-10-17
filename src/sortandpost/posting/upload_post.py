"""Upload-Post (https://docs.upload-post.com) as a ProviderSpec.

From its OpenAPI spec (checked 2026-10-05; to be verified live): base
``https://api.upload-post.com/api``, ``Authorization: Apikey <key>``. There is
no upload step: ``POST /upload_photos`` takes the photos as multipart files
together with the post (``upload_style="inline"``). There are no drafts either:
a create publishes at once or, with ``scheduled_date``, later -- sortandpost
only offers the scheduled kind (``drafts=False``).

A *profile* (``user``) owns the connected social accounts, and one create goes
to one profile, so a channel is ``<profile>:<platform>`` carrying its profile
as ``Channel.account`` (``single_account``). The free plan allows 10 uploads a
month.

Alt text is a field per network, ``<network>_alt_text`` (docs checked
2026-10-16; not yet verified live), each with its own spelling of "one per
photo": a JSON array, a repeated ``[]`` field, ``|``-separated, or -- for
networks that post one image -- a single string (:data:`_ALT_STYLE`).
"""

from __future__ import annotations

import json
from typing import Any, Literal

from sortandpost.posting.model import Channel, CreateRequest
from sortandpost.posting.spec import AuthSpec, FieldSpec, Option, ProviderSpec, RateLimit

# networks that take <network>_first_comment
_FIRST_COMMENT_NETWORKS = {"instagram", "facebook", "x", "threads", "bluesky"}
_ID_SEP = ":"

# how each network's <network>_alt_text spells one value per photo
AltStyle = Literal["json", "repeat", "pipe", "first"]
_ALT_STYLE: dict[str, AltStyle] = {
    "linkedin": "json",
    "instagram": "json",
    "bluesky": "json",
    "x": "repeat",
    "facebook": "repeat",
    "threads": "repeat",
    "mastodon": "pipe",
    "discord": "pipe",
    "pinterest": "first",
    "lemmy": "first",
    "wordpress": "first",
}


def channel_id(profile: str, platform: str) -> str:
    return f"{profile}{_ID_SEP}{platform}"


def split_channel_id(cid: str) -> tuple[str, str]:
    profile, _, platform = cid.rpartition(_ID_SEP)
    return profile, platform


def parse_channels(body: Any) -> list[Channel]:
    """``GET /uploadposts/users``: ``profiles[].social_accounts`` maps a platform
    to its account object; an empty / null value means not connected."""
    out: list[Channel] = []
    profiles = body.get("profiles") if isinstance(body, dict) else None
    for prof in profiles if isinstance(profiles, list) else []:
        if not isinstance(prof, dict) or not prof.get("username"):
            continue
        user = str(prof["username"])
        accounts = prof.get("social_accounts")
        for platform, acc in accounts.items() if isinstance(accounts, dict) else ():
            if not acc:
                continue
            info = acc if isinstance(acc, dict) else {}
            who = info.get("display_name") or info.get("handle") or info.get("username") or ""
            out.append(
                Channel(
                    id=channel_id(user, str(platform)),
                    name=f"{who} ({user})" if who else user,
                    social_network=str(platform),
                    must_reconnect=bool(info.get("reauth_required")),
                    logo_url=str(info.get("social_images") or ""),
                    account=user,
                )
            )
    return out


def build_payload(req: CreateRequest) -> dict[str, Any]:
    """The ``POST /upload_photos`` form fields (the photos go beside them)."""
    profiles = {c.account or split_channel_id(c.id)[0] for c in req.channels}
    form: dict[str, Any] = {
        "user": next(iter(profiles), ""),
        "platform[]": [split_channel_id(c.id)[1] for c in req.channels],
        "title": req.caption,
    }
    networks = {c.network for c in req.channels}
    if req.schedule_at is not None:
        form["scheduled_date"] = req.schedule_at.strftime("%Y-%m-%dT%H:%M:%S")
        form["timezone"] = "UTC"
    story = req.settings.get("instagram", {}).get("media_type") == "STORIES"
    if req.first_comment.strip():
        for net in sorted(networks & _FIRST_COMMENT_NETWORKS):
            if not (net == "instagram" and story):
                form[f"{net}_first_comment"] = req.first_comment
    if "pinterest" in networks:
        form["pinterest_description"] = req.caption
    if any(req.alt_texts):
        alts = [req.alt_for(i) for i in range(len(req.media))]
        for net in sorted(networks & _ALT_STYLE.keys()):
            if not (net == "instagram" and story):  # alt text is for feed images only
                form.update(_alt_field(net, alts))
    for values in req.settings.values():
        form.update(values)
    return form


def _alt_field(net: str, alts: list[str]) -> dict[str, Any]:
    style = _ALT_STYLE[net]
    if style == "json":
        return {f"{net}_alt_text": json.dumps(alts, ensure_ascii=False)}
    if style == "repeat":
        return {f"{net}_alt_text[]": alts}
    if style == "pipe":  # a "|" inside a text would split it, so it becomes "/"
        return {f"{net}_alt_text": "|".join(a.replace("|", "/") for a in alts)}
    return {f"{net}_alt_text": alts[0]} if alts and alts[0] else {}


SPEC = ProviderSpec(
    key="upload_post",
    name="Upload-Post",
    about="sends clean JPEG copies of your photos to Upload-Post (free: 10 posts a month) "
    "and schedules the post. It has no drafts.",
    key_help="Create an API key at upload-post.com → Manage API Keys.",
    account_label="account",
    env_var="UPLOAD_POST_API_KEY",
    keychain_account="upload_post_api_key",
    default_base_url="https://api.upload-post.com/api",
    auth=AuthSpec("header", "Authorization", "Apikey "),
    endpoints={
        "auth_check": ("GET", "/uploadposts/me"),
        "channels": ("GET", "/uploadposts/users"),
        "create": ("POST", "/upload_photos"),
    },
    auth_label="email",
    auth_ok="success",
    parse_channels=parse_channels,
    upload_style="inline",
    upload_field="photos[]",
    created_ids="job_id",
    error_fields=("message", "error"),
    status_hints={
        401: "Upload-Post rejected the API key. Check it in Settings, or create a new one at "
        "upload-post.com → Manage API Keys.",
        403: "Upload-Post refused this (usually a plan restriction).",
    },
    # the free plan: 10 uploads a month (override for a paid plan in config.yaml)
    rate_limits={"create": RateLimit(10, 30 * 86400)},
    create_timeout_s=120.0,
    drafts=False,
    channelless_drafts=False,
    single_account=True,
    first_comment=True,
    alt_text=True,
    alt_text_networks=frozenset(_ALT_STYLE),
    network_fields={
        "instagram": (
            FieldSpec(
                "postType",
                "Instagram post type",
                "select",
                options=(Option("post", "Post", "IMAGE"), Option("story", "Story", "STORIES")),
                default="post",
                wire_key="media_type",
            ),
        ),
        "pinterest": (
            FieldSpec("pinTitle", "Pin title", wire_key="pinterest_title", required=True),
            FieldSpec("board", "Board ID", wire_key="pinterest_board_id", required=True),
            FieldSpec(
                "destinationLink",
                "Destination link (optional)",
                "url",
                wire_key="pinterest_link",
                placeholder="https://…",
            ),
        ),
    },
    build_payload=build_payload,
)
