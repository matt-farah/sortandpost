"""Thin synchronous client for the robopost.app public REST API.

Reference: https://robopost.app/docs/robopost-api/ . Only the four calls the
composer needs: validate the key, list channels, upload one media file, create a
(draft or scheduled) post. The API has no read / edit / delete for a post, so a
post that has been sent is fire-and-forget from here.

Every call blocks, so callers run it off the web event loop (``run.io_bound``;
hard constraint #7). robopost authenticates with an ``?apikey=`` **query
parameter**, which means the key is part of every request URL: this module
redacts ``apikey=...`` in everything it logs or raises, and installs a logging
filter on ``httpx`` / ``httpcore`` (httpx logs each request URL at INFO).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://public-api.robopost.app/v1"
# Exact paths, trailing slash included or not: robopost answers the other form
# with a 307 to a plain-http URL that carries the key (verified 2026-09-25), so
# redirects are never followed.
PATH_AUTH = "/auth"
PATH_CHANNELS = "/channels/"
PATH_UPLOAD = "/medias/upload"
PATH_CREATE = "/scheduled_posts"
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_UPLOAD_TIMEOUT = httpx.Timeout(120.0, connect=10.0)

_APIKEY_RE = re.compile(r"(apikey=)[^&\s'\"]+", re.IGNORECASE)


def redact(text: str, key: str | None = None) -> str:
    """Strip any ``apikey=...`` value (and the literal key, if given) from *text*."""
    text = _APIKEY_RE.sub(r"\1***", text)
    if key:
        text = text.replace(key, "***")
    return text


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "apikey=" in msg.lower():
            record.msg = redact(msg)
            record.args = None
        return True


_FILTER = _RedactFilter()
for _name in ("httpx", "httpcore"):
    _logger = logging.getLogger(_name)
    if _FILTER not in _logger.filters:
        _logger.addFilter(_FILTER)


# Friendly text for the error codes the API documents.
_FRIENDLY = {
    "PLAN_LIMIT_HIT": "robopost says your plan limit is reached.",
    "ACCOUNT_USAGE_FROZEN": "robopost says this account's usage is frozen.",
    "VIDEO_CORRUPTED": "robopost rejected the file as corrupted.",
}


class RobopostError(RuntimeError):
    """An API call failed. ``status`` is the HTTP status (0 for a network
    failure); ``code`` is robopost's machine code when it sent one."""

    def __init__(self, status: int, message: str, code: str | None = None) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)

    @property
    def friendly(self) -> str:
        if self.code in _FRIENDLY:
            return _FRIENDLY[self.code]
        if self.status == 401:
            return (
                "robopost rejected the API key. Check it in Settings, or create a new one "
                "in robopost → Team → Manage API Keys."
            )
        if self.status == 415:
            return f"robopost doesn't accept this file type ({self.message})."
        if self.status == 0:
            return f"Couldn't reach robopost: {self.message}"
        return f"robopost error {self.status}: {self.message}"


@dataclass(frozen=True)
class Channel:
    id: str
    name: str
    social_network: str
    must_reconnect: bool = False
    logo_url: str = ""

    @property
    def network(self) -> str:
        """Lower-case canonical network name (``instagram``, ``facebook``,
        ``pinterest``, …) -- robopost's own casing is not documented."""
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


def network_key(social_network: str) -> str:
    s = social_network.strip().lower()
    for known in ("instagram", "facebook", "pinterest"):
        if known in s:
            return known
    return s or "other"


def _error_from(resp: httpx.Response, key: str) -> RobopostError:
    code: str | None = None
    message = resp.reason_phrase or "request failed"
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        for field in ("msg", "error", "detail"):
            val = body.get(field)
            if isinstance(val, str) and val:
                message = val
                if val.isupper() and "_" in val:
                    code = val
                break
            if isinstance(val, list) and val:  # FastAPI-style validation errors
                message = "; ".join(
                    str(v.get("msg", v)) if isinstance(v, dict) else str(v) for v in val
                )
                break
    elif resp.text:
        message = resp.text[:300]
    return RobopostError(resp.status_code, redact(message, key), code)


class RobopostClient:
    """``with RobopostClient(key) as rp: rp.check_auth()``. *transport* lets
    tests plug in :class:`httpx.MockTransport`."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise RobopostError(401, "no API key configured")
        if not base_url.startswith("https://"):
            raise ValueError("robopost base URL must be https")
        self._key = api_key
        self._http = httpx.Client(
            base_url=base_url,
            params={"apikey": api_key},
            timeout=_TIMEOUT,
            transport=transport,
            follow_redirects=False,
        )

    def __enter__(self) -> RobopostClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # -- plumbing -------------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            resp = self._http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise RobopostError(0, redact(f"{type(exc).__name__}: {exc}", self._key)) from None
        if resp.is_redirect:
            raise RobopostError(
                resp.status_code,
                f"robopost redirected {method} {url} (not followed, to protect the key)",
            )
        if resp.status_code >= 400:
            raise _error_from(resp, self._key)
        try:
            return resp.json()
        except ValueError:
            raise RobopostError(resp.status_code, "robopost sent a non-JSON reply") from None

    # -- API ------------------------------------------------------------------

    def check_auth(self) -> str:
        """Validate the key; returns the robopost team name."""
        body = self._request("GET", PATH_AUTH)
        return str(body.get("team_name") or "") if isinstance(body, dict) else ""

    def list_channels(self) -> list[Channel]:
        body = self._request("GET", PATH_CHANNELS)
        rows = body if isinstance(body, list) else []
        live = [r for r in rows if isinstance(r, dict) and not r.get("is_deleted")]
        out = [c for c in (Channel.from_json(r) for r in live) if c]
        out.sort(key=lambda c: (c.network, c.name.casefold()))
        return out

    def upload_media(self, path: Path) -> str:
        """Upload one file; returns its ``storage_object_id`` -- the UUID that
        ``image_object_ids`` expects (NOT the reply's ``id``, a media-record id
        that robopost silently accepts in a post and then shows no image; see
        "Upload storage UUID used in post payloads" in the API docs). One retry
        on a network failure (an upload is harmless to repeat -- it only
        creates a media object, never a post)."""
        for attempt in (1, 2):
            try:
                with path.open("rb") as fh:
                    body = self._request(
                        "POST",
                        PATH_UPLOAD,
                        files={"file": (path.name, fh, "image/jpeg")},
                        timeout=_UPLOAD_TIMEOUT,
                    )
                break
            except RobopostError as exc:
                if exc.status != 0 or attempt == 2:
                    raise
                log.info("media upload network error, retrying once: %s", exc.message)
        storage_id = body.get("storage_object_id") if isinstance(body, dict) else None
        if storage_id is None or storage_id == "":
            raise RobopostError(201, "upload reply had no storage_object_id")
        return str(storage_id)

    def create_post(self, payload: dict[str, Any]) -> list[str]:
        """Create a draft or scheduled post; returns the ids robopost created
        (one per channel when scheduled). Never retried: a timeout here may
        still have created the post."""
        body = self._request("POST", PATH_CREATE, json=payload)
        rows = body.get("scheduled_posts") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            return []
        return [str(r["id"]) for r in rows if isinstance(r, dict) and r.get("id") is not None]
