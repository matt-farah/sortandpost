"""Provider-neutral posting types: what every posting back-end is mapped onto.

Nothing here knows a vendor. A provider's spec (:mod:`sortandpost.posting.spec`)
says how its API's JSON maps onto these types, and how a :class:`CreateRequest`
becomes its create body.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

SendMode = Literal["draft", "schedule"]

# Social networks every provider's raw channel type is folded onto, so limits
# and per-network settings are written once. Matched as a substring of the
# lower-cased raw type ("INSTAGRAM_BUSINESS", "instagram-standalone", …);
# ``_EXACT`` covers names too short or odd to match that way.
_KNOWN_NETWORKS = (
    "instagram",
    "facebook",
    "pinterest",
    "linkedin",
    "threads",
    "bluesky",
    "tiktok",
    "youtube",
    "mastodon",
    "telegram",
)
_EXACT = {"x": "x", "twitter": "x"}


def network_key(raw: str) -> str:
    """Lower-case canonical network name (``instagram``, ``facebook``, ``x``, …)."""
    s = raw.strip().lower()
    if s in _EXACT:
        return _EXACT[s]
    for known in _KNOWN_NETWORKS:
        if known in s:
            return known
    if "twitter" in s:
        return "x"
    return s or "other"


@dataclass(frozen=True)
class Channel:
    """One connected social account in a posting back-end. ``social_network``
    is the provider's raw type (sent back as-is where an API wants it)."""

    id: str
    name: str
    social_network: str
    must_reconnect: bool = False
    logo_url: str = ""

    @property
    def network(self) -> str:
        return network_key(self.social_network)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "social_network": self.social_network,
            "must_reconnect": self.must_reconnect,
            "logo_url": self.logo_url,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Channel | None:
        cid = raw.get("id")
        if cid is None or cid == "":
            return None
        return cls(
            id=str(cid),
            name=str(raw.get("name") or cid),
            social_network=str(raw.get("social_network") or ""),
            must_reconnect=bool(raw.get("must_reconnect")),
            logo_url=str(raw.get("logo_url") or ""),
        )


@dataclass(frozen=True)
class MediaRef:
    """An uploaded file as the provider knows it: its id, plus a URL for APIs
    that want both in a post."""

    id: str
    path: str = ""


@dataclass(frozen=True)
class CreateRequest:
    """One create call, already resolved: the channels of one send group, the
    uploads in photo order, and each network's settings mapped to the
    provider's wire keys/values (only networks with a chosen channel)."""

    mode: SendMode
    caption: str
    media: tuple[MediaRef, ...]
    channels: tuple[Channel, ...]
    now: datetime  # aware UTC
    schedule_at: datetime | None = None  # aware UTC; None = no time sent
    first_comment: str = ""
    settings: dict[str, dict[str, str]] = field(default_factory=dict)
    idempotency_key: str = ""  # one per create call, for providers that dedupe on it

    def settings_for(self, channel: Channel) -> dict[str, str]:
        return dict(self.settings.get(channel.network, {}))


class PostingError(RuntimeError):
    """A posting API call failed. ``status`` is the HTTP status (0 = no reply);
    ``code`` the provider's machine code when it sent one. ``friendly`` is the
    text the UI shows; it is built from the provider's spec, so no message
    here names a vendor."""

    def __init__(
        self,
        status: int,
        message: str,
        code: str | None = None,
        *,
        provider: str = "the posting service",
        hints: dict[int, str] | None = None,
        friendly_codes: dict[str, str] | None = None,
        retry_after: int | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.provider = provider
        self.retry_after = retry_after
        self._hints = hints or {}
        self._codes = friendly_codes or {}
        super().__init__(message)

    @property
    def friendly(self) -> str:
        if self.code and self.code in self._codes:
            return self._codes[self.code]
        if self.status in self._hints:
            return self._hints[self.status]
        if self.status == 429:
            wait = f" Try again in {_minutes(self.retry_after)}." if self.retry_after else ""
            return f"{self.provider} is limiting how many requests can be made.{wait}"
        if self.status == 415:
            return f"{self.provider} doesn't accept this file type ({self.message})."
        if self.status == 0:
            return f"Couldn't reach {self.provider}: {self.message}"
        return f"{self.provider} error {self.status}: {self.message}"


def _minutes(seconds: int | None) -> str:
    if not seconds or seconds < 60:
        return "a minute"
    m = -(-seconds // 60)
    return f"{m} minutes"


def utcnow() -> datetime:
    """Aware UTC now. (Not ``sync.clock.utcnow``: importing ``sortandpost.sync``
    from here would make ``import sortandpost.posting`` circular.)"""
    return datetime.now(UTC)
