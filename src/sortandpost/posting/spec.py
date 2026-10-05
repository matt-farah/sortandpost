"""What a posting back-end *is*: a declarative :class:`ProviderSpec`.

Everything a provider differs in is a value here -- where the key goes, the
endpoint paths, where the answers sit in its JSON, its rate limits, what it
supports, the per-network settings it takes. Shared code (the HTTP client, the
rate limiter, validation, the composer and settings UI) reads these values and
never branches on a vendor. The one thing data can't express -- the shape of
the create body -- is the spec's ``build_payload`` function.

Adding a provider = one module that builds a ``ProviderSpec`` and an entry in
:data:`sortandpost.posting.PROVIDERS`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from sortandpost.posting.model import CreateRequest

FieldKind = Literal["text", "url", "select"]


def _frozen(d: Mapping[Any, Any] | None = None) -> Mapping[Any, Any]:
    return MappingProxyType(dict(d or {}))


@dataclass(frozen=True)
class AuthSpec:
    """Where the API key goes: a query parameter or a header."""

    place: Literal["query", "header"]
    name: str
    prefix: str = ""  # e.g. "Bearer "


@dataclass(frozen=True)
class RateLimit:
    """At most ``calls`` of one operation per ``per_seconds`` (sliding window)."""

    calls: int
    per_seconds: int

    def describe(self, what: str = "calls") -> str:
        """``"90 new posts per hour"``."""
        if self.per_seconds == 3600:
            return f"{self.calls} {what} per hour"
        if self.per_seconds == 86400:
            return f"{self.calls} {what} per day"
        return f"{self.calls} {what} per {self.per_seconds} s"


@dataclass(frozen=True)
class Option:
    """One choice of a ``select`` field: stored as ``value``, sent as ``wire``."""

    value: str
    label: str
    wire: str = ""

    @property
    def sent(self) -> str:
        return self.wire or self.value


@dataclass(frozen=True)
class FieldSpec:
    """A per-network post setting. Stored in ``Post.network_settings[network][key]``
    (provider-neutral key, so a draft keeps it across a provider switch) and
    sent as ``wire_key`` (default: ``key``). Rendered and validated generically."""

    key: str
    label: str
    kind: FieldKind = "text"
    required: bool = False
    options: tuple[Option, ...] = ()
    default: str = ""
    wire_key: str = ""
    placeholder: str = ""

    @property
    def sent_key(self) -> str:
        return self.wire_key or self.key

    def option(self, value: str) -> Option | None:
        """The option for a stored *value*, else the default one."""
        by_value = {o.value: o for o in self.options}
        return (
            by_value.get(value)
            or by_value.get(self.default)
            or (self.options[0] if self.options else None)
        )


@dataclass(frozen=True)
class BaseUrlPreset:
    label: str
    url: str
    hint: str = ""


@dataclass(frozen=True)
class ProviderSpec:
    # -- identity and copy -------------------------------------------------------
    key: str  # stable id, stored in config and on posts
    name: str  # shown in the UI
    key_help: str  # where to get an API key
    about: str = ""  # one line for Settings
    account_label: str = "account"  # what check_auth's label names ("team", …)

    # -- secrets (never in a file; see secrets_store) ------------------------------
    env_var: str = ""
    keychain_account: str = ""

    # -- where the API is ----------------------------------------------------------
    default_base_url: str = ""
    base_url_editable: bool = False
    base_url_presets: tuple[BaseUrlPreset, ...] = ()
    # (host suffix or "*", path) appended when a base URL is given with no path
    base_url_paths: tuple[tuple[str, str], ...] = ()
    allow_lan_http: bool = False  # http:// only to loopback / private hosts

    auth: AuthSpec = AuthSpec("header", "Authorization")
    # op -> (method, path); ops: auth_check, channels, upload, create
    endpoints: Mapping[str, tuple[str, str]] = field(default_factory=_frozen)

    # -- where the answers are (dotted paths; "[*]" walks a list) --------------------
    auth_label: str = ""  # check_auth's label ("team_name")
    auth_ok: str = ""  # a boolean that must be true ("connected")
    channels_at: str = ""  # the channel list ("" = the whole reply)
    channel_fields: Mapping[str, str] = field(default_factory=_frozen)  # id/name/network/logo
    channel_reconnect: tuple[str, ...] = ()  # any truthy -> must reconnect
    channel_skip: tuple[str, ...] = ()  # any truthy -> not listed (deleted, …)
    channel_require: tuple[str, ...] = ()  # any falsy -> not listed (inactive, …)
    upload_id: str = "id"
    upload_path: str = ""
    upload_field: str = "file"
    # "multipart": POST the file to the upload endpoint. "presigned": POST a small JSON
    # body (``presign_fields``) to it, then PUT the bytes -- with no API key -- to the
    # URL found at ``upload_put_url`` in the reply.
    upload_style: Literal["multipart", "presigned"] = "multipart"
    upload_put_url: str = ""
    presign_fields: Mapping[str, str] = field(
        default_factory=lambda: _frozen(
            {"filename": "filename", "content_type": "contentType", "size": "size"}
        )
    )
    created_ids: str = ""

    # -- errors -------------------------------------------------------------------
    error_fields: tuple[str, ...] = ("msg", "error", "detail", "message")
    friendly_codes: Mapping[str, str] = field(default_factory=_frozen)
    status_hints: Mapping[int, str] = field(default_factory=_frozen)

    # -- limits and behaviour -------------------------------------------------------
    rate_limits: Mapping[str, RateLimit] = field(default_factory=_frozen)  # op -> limit
    timeout_s: float = 30.0
    upload_timeout_s: float = 120.0
    create_timeout_s: float = 30.0  # a create that outlives this is an unknown outcome
    connect_timeout_s: float = 10.0
    upload_retries: int = 1  # an upload is harmless to repeat; a create never is

    # -- capabilities ---------------------------------------------------------------
    channelless_drafts: bool = True  # a draft may go out with no channel chosen
    first_comment: bool = True
    idempotency_header: str = ""  # header that carries CreateRequest.idempotency_key
    can_delete_remote: bool = False  # (future: cancel a sent post)

    # -- per-network post settings --------------------------------------------------------
    network_fields: Mapping[str, tuple[FieldSpec, ...]] = field(default_factory=_frozen)

    build_payload: Callable[[CreateRequest], dict[str, Any]] = field(
        default=lambda req: {}, compare=False, repr=False
    )

    def fields_for(self, network: str) -> tuple[FieldSpec, ...]:
        return tuple(self.network_fields.get(network, ()))
