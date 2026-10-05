"""Posting back-ends behind one interface.

* :mod:`.spec` -- :class:`ProviderSpec`, the declarative description of a back-end
* :mod:`.model` -- neutral types (:class:`Channel`, :class:`CreateRequest`, …)
* :mod:`.client` -- :class:`ApiClient`, the one HTTP client, driven by a spec
* :mod:`.ratelimit` / :mod:`.store` -- generic rate limits and per-provider local state
* one module per provider (:mod:`.robopost`, :mod:`.postiz`, :mod:`.zernio`), registered below

Which back-end is active is ``posting.provider`` in config.yaml. A post that
already went out (fully, partly, or with a send in doubt) stays with the
provider it went through (:func:`provider_for`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sortandpost.posting import postiz, robopost, zernio
from sortandpost.posting.client import ApiClient, PostingClient
from sortandpost.posting.ratelimit import RateLimiter, effective_limits
from sortandpost.posting.spec import ProviderSpec

if TYPE_CHECKING:
    from sortandpost.config import Settings
    from sortandpost.posts import Post

PROVIDERS: dict[str, ProviderSpec] = {s.key: s for s in (robopost.SPEC, postiz.SPEC, zernio.SPEC)}
DEFAULT_PROVIDER = robopost.SPEC.key


def active_provider(settings: Settings) -> ProviderSpec:
    return PROVIDERS.get(settings.config.posting.provider) or PROVIDERS[DEFAULT_PROVIDER]


def provider_for(post: Post, settings: Settings) -> ProviderSpec:
    """The provider a post's next send (or its status text) belongs to: the one
    it already went through, if any part went out or is in doubt; otherwise
    the active one (a draft follows the Settings choice)."""
    tied = post.sent or post.partly_sent or post.status == "sending"
    if tied and post.provider in PROVIDERS:
        return PROVIDERS[post.provider]
    return active_provider(settings)


def base_url(settings: Settings, spec: ProviderSpec) -> str:
    from sortandpost.posting.client import normalize_base_url

    return normalize_base_url(spec, settings.config.posting.base_urls.get(spec.key, ""))


def limiter(settings: Settings, spec: ProviderSpec) -> RateLimiter:
    limits = effective_limits(spec, settings.config.posting.rate_limit_overrides)
    return RateLimiter(settings.paths, spec, limits)


def open_client(settings: Settings, spec: ProviderSpec, api_key: str) -> PostingClient:
    """The production client: the spec's base URL (or the configured one) and
    its rate limits."""
    return ApiClient(
        spec, api_key, base_url=base_url(settings, spec), limiter=limiter(settings, spec)
    )


__all__ = [
    "DEFAULT_PROVIDER",
    "PROVIDERS",
    "ApiClient",
    "PostingClient",
    "ProviderSpec",
    "active_provider",
    "base_url",
    "limiter",
    "open_client",
    "provider_for",
]
