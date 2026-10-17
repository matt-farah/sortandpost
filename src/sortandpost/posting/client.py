"""The one HTTP client every posting back-end goes through, driven by a
:class:`~sortandpost.posting.spec.ProviderSpec`.

Shared rules, whatever the provider:

* Every call blocks: callers run it off the web event loop (``run.io_bound``;
  hard constraint #7).
* **Redirects are never followed.** One provider answers a wrong slash with a
  307 to plain http carrying the key, so a redirect is an error.
* The key never shows up in an error or a log line: the literal key and any
  key-bearing query parameter are redacted, and a logging filter covers
  httpx / httpcore, which log every request URL at INFO.
* An upload may be retried after a network error (it only makes a media
  object). **A create is never retried**: a timeout may still have created
  the post.
* Rate limits from the spec are checked before a call and recorded after it.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import re
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from sortandpost.posting.model import (
    NO_CONNECTION,
    Channel,
    CreateRequest,
    MediaRef,
    PostingError,
)
from sortandpost.posting.ratelimit import RateLimiter
from sortandpost.posting.spec import ProviderSpec

log = logging.getLogger(__name__)

# Query parameters that may carry a key (every provider using query auth adds
# its own name via ApiClient); redacted wherever they appear.
_QUERY_SECRETS: set[str] = {"apikey", "api_key", "access_token", "token", "key"}
_LIVE_KEYS: set[str] = set()  # keys of open clients, scrubbed from log lines


def _query_re() -> re.Pattern[str]:
    names = "|".join(sorted(re.escape(n) for n in _QUERY_SECRETS))
    return re.compile(rf"\b({names})=[^&\s'\"]+", re.IGNORECASE)


def redact(text: str, key: str | None = None) -> str:
    """Strip key-bearing query values (and the literal *key*) from *text*."""
    text = _query_re().sub(r"\1=***", text)
    if key:
        text = text.replace(key, "***")
    return text


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        clean = redact(msg)
        for k in list(_LIVE_KEYS):
            clean = clean.replace(k, "***")
        if clean != msg:
            record.msg = clean
            record.args = None
        return True


_FILTER = _RedactFilter()
for _name in ("httpx", "httpcore"):
    _logger = logging.getLogger(_name)
    if _FILTER not in _logger.filters:
        _logger.addFilter(_FILTER)


# --------------------------------------------------------------------------- #
# base URLs
# --------------------------------------------------------------------------- #
_LAN_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".localhost")


def is_lan_host(host: str) -> bool:
    """Loopback / private / link-local IPs, ``localhost``, and LAN-only names."""
    h = host.strip("[]").lower()
    if h == "localhost" or h.endswith(_LAN_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return "." not in h  # a bare hostname only resolves on the local network
    return ip.is_loopback or ip.is_private or ip.is_link_local


def normalize_base_url(spec: ProviderSpec, raw: str) -> str:
    """Trim, drop a trailing slash, default the scheme to https, and append the
    spec's API path when only a host was given."""
    url = (raw or "").strip().rstrip("/")
    if not url:
        return spec.default_base_url
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.path in ("", "/"):
        host = (parts.hostname or "").lower()
        for suffix, path in spec.base_url_paths:
            if suffix == "*" or host == suffix or host.endswith("." + suffix):
                url = url.rstrip("/") + path
                break
    return url


def check_base_url(spec: ProviderSpec, url: str) -> tuple[str | None, str | None]:
    """``(error, warning)`` for a (normalized) base URL."""
    parts = urlsplit(url)
    if not parts.hostname:
        return "Enter the server's address, e.g. https://postiz.example.com.", None
    if parts.scheme == "https":
        return None, None
    if parts.scheme == "http" and spec.allow_lan_http and is_lan_host(parts.hostname):
        return None, (
            "Plain http: the API key travels unencrypted on your network. "
            "Fine at home; use https anywhere else."
        )
    if parts.scheme == "http":
        return "Use https:// — plain http is only allowed for a server on your own network.", None
    return "The address must start with https://.", None


# --------------------------------------------------------------------------- #
# reading replies
# --------------------------------------------------------------------------- #
def extract(obj: Any, path: str) -> list[Any]:
    """Values at a dotted *path*; ``[*]`` walks a list (``a[*].id``). ``""`` =
    *obj* itself. Missing keys give ``[]``."""
    cur: list[Any] = [obj]
    if not path:
        return cur
    for part in path.split("."):
        star = part.endswith("[*]")
        name = part[:-3] if star else part
        nxt: list[Any] = []
        for item in cur:
            if not name:
                val = item
            elif isinstance(item, dict):
                val = item.get(name)
            else:
                val = None
            if val is None:
                continue
            if star:
                if isinstance(val, list):
                    nxt.extend(val)
            else:
                nxt.append(val)
        cur = nxt
    return cur


def first(obj: Any, path: str) -> Any:
    got = extract(obj, path)
    return got[0] if got else None


class PostingClient(Protocol):
    """What the send path and the settings page use. :class:`ApiClient` is the
    implementation for every provider; tests swap in its transport."""

    spec: ProviderSpec

    def __enter__(self) -> PostingClient: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    def check_auth(self) -> str: ...

    def list_channels(self) -> list[Channel]: ...

    def upload_media(self, path: Path) -> MediaRef: ...

    def create_post(self, req: CreateRequest) -> list[str]: ...


class ApiClient:
    """``with ApiClient(spec, key) as api: api.check_auth()``."""

    def __init__(
        self,
        spec: ProviderSpec,
        api_key: str,
        *,
        base_url: str | None = None,
        limiter: RateLimiter | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.spec = spec
        if not api_key:
            raise self._error(401, "no API key configured")
        url = normalize_base_url(spec, base_url or spec.default_base_url)
        err, warning = check_base_url(spec, url)
        if err:
            raise ValueError(err)
        if warning:
            log.info("%s: %s", spec.name, warning)
        self._key = api_key
        self._transport = transport
        self._limiter = limiter
        params: dict[str, str] = {}
        headers: dict[str, str] = {}
        if spec.auth.place == "query":
            _QUERY_SECRETS.add(spec.auth.name.lower())
            params[spec.auth.name] = api_key
        else:
            headers[spec.auth.name] = spec.auth.prefix + api_key
        _LIVE_KEYS.add(api_key)
        self._http = httpx.Client(
            base_url=url,
            params=params,
            headers=headers,
            timeout=httpx.Timeout(spec.timeout_s, connect=spec.connect_timeout_s),
            transport=transport,
            follow_redirects=False,
        )
        self._bare: httpx.Client | None = None  # no key: for presigned PUTs to storage

    def __enter__(self) -> ApiClient:
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
        if self._bare is not None:
            self._bare.close()
        _LIVE_KEYS.discard(self._key)

    # -- plumbing ---------------------------------------------------------------

    def _error(
        self, status: int, message: str, code: str | None = None, retry_after: int | None = None
    ) -> PostingError:
        return PostingError(
            status,
            message,
            code,
            provider=self.spec.name,
            hints=dict(self.spec.status_hints),
            friendly_codes=dict(self.spec.friendly_codes),
            retry_after=retry_after,
        )

    def _error_from(self, resp: httpx.Response) -> PostingError:
        code: str | None = None
        message = resp.reason_phrase or "request failed"
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            for name in self.spec.error_fields:
                val = body.get(name)
                if isinstance(val, str) and val:
                    message = val
                    if val.isupper() and "_" in val:
                        code = val
                    break
                if isinstance(val, list) and val:  # validation-error lists
                    message = "; ".join(
                        str(v.get("msg", v.get("message", v))) if isinstance(v, dict) else str(v)
                        for v in val
                    )
                    break
        elif resp.text:
            message = resp.text[:300]
        retry_after: int | None = None
        raw = resp.headers.get("retry-after", "")
        if raw.isdigit():
            retry_after = int(raw)
        return self._error(resp.status_code, redact(message, self._key), code, retry_after)

    def _request(self, op: str, **kwargs: Any) -> Any:
        try:
            method, path = self.spec.endpoints[op]
        except KeyError:
            raise self._error(0, f"{self.spec.name} has no '{op}' endpoint") from None
        if self._limiter is not None:
            wait = self._limiter.check(op)
            if wait is not None:
                lim = self._limiter.limit(op)
                raise self._error(
                    429,
                    f"rate limit reached ({lim.describe(op) if lim else op})",
                    retry_after=wait,
                )
            self._limiter.record(op)  # counted as sent: the server counts it on arrival
        try:
            resp = self._http.request(method, path, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise self._error(
                0, redact(f"{type(exc).__name__}: {exc}", self._key), NO_CONNECTION
            ) from None
        except httpx.HTTPError as exc:
            raise self._error(0, redact(f"{type(exc).__name__}: {exc}", self._key)) from None
        if resp.is_redirect:
            raise self._error(
                resp.status_code,
                f"{self.spec.name} redirected {method} {path} (not followed, to protect the key)",
            )
        if resp.status_code >= 400:
            raise self._error_from(resp)
        try:
            return resp.json()
        except ValueError:
            raise self._error(resp.status_code, f"{self.spec.name} sent a non-JSON reply") from None

    # -- API ----------------------------------------------------------------------

    def check_auth(self) -> str:
        """Validate the key; returns the account label (may be empty)."""
        body = self._request("auth_check")
        if self.spec.auth_ok and first(body, self.spec.auth_ok) is not True:
            raise self._error(401, "the API key is not connected")
        label = first(body, self.spec.auth_label) if self.spec.auth_label else None
        return str(label or "")

    def list_channels(self) -> list[Channel]:
        body = self._request("channels")
        if self.spec.parse_channels is not None:
            parsed = self.spec.parse_channels(body)
            parsed.sort(key=lambda c: (c.network, c.name.casefold()))
            return parsed
        rows = first(body, self.spec.channels_at) if self.spec.channels_at else body
        f = self.spec.channel_fields
        out: list[Channel] = []
        for r in rows if isinstance(rows, list) else []:
            if (
                not isinstance(r, dict)
                or any(first(r, k) for k in self.spec.channel_skip)
                or not all(first(r, k) for k in self.spec.channel_require)
            ):
                continue
            cid = first(r, f.get("id", "id"))
            if cid is None or cid == "":
                continue
            out.append(
                Channel(
                    id=str(cid),
                    name=str(first(r, f.get("name", "name")) or cid),
                    social_network=str(first(r, f.get("network", "network")) or ""),
                    must_reconnect=any(bool(first(r, k)) for k in self.spec.channel_reconnect),
                    logo_url=str(first(r, f.get("logo", "logo")) or ""),
                )
            )
        out.sort(key=lambda c: (c.network, c.name.casefold()))
        return out

    def _put_to_storage(self, url: str, path: Path) -> None:
        """PUT the file to a presigned URL. A separate client: that host is not the
        provider's API, so it never sees the API key."""
        if urlsplit(url).scheme != "https":
            raise self._error(200, "the upload address is not https")
        if self._bare is None:
            self._bare = httpx.Client(
                timeout=httpx.Timeout(
                    self.spec.upload_timeout_s, connect=self.spec.connect_timeout_s
                ),
                transport=self._transport,
                follow_redirects=False,
            )
        try:
            resp = self._bare.put(
                url, content=path.read_bytes(), headers={"Content-Type": "image/jpeg"}
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise self._error(
                0, redact(f"{type(exc).__name__}: {exc}", self._key), NO_CONNECTION
            ) from None
        except httpx.HTTPError as exc:
            raise self._error(0, redact(f"{type(exc).__name__}: {exc}", self._key)) from None
        if resp.status_code >= 300:
            raise self._error(resp.status_code, "the file could not be stored")

    def _upload_once(self, path: Path) -> Any:
        if self.spec.upload_style == "presigned":
            names = self.spec.presign_fields
            wanted = {
                "filename": path.name,
                "content_type": "image/jpeg",
                "size": path.stat().st_size,
            }
            body = self._request(
                "upload", json={names[k]: v for k, v in wanted.items() if k in names}
            )
            put_url = first(body, self.spec.upload_put_url)
            if not isinstance(put_url, str) or not put_url:
                raise self._error(200, f"upload reply had no {self.spec.upload_put_url}")
            self._put_to_storage(put_url, path)
            return body
        with path.open("rb") as fh:
            return self._request(
                "upload",
                files={self.spec.upload_field: (path.name, fh, "image/jpeg")},
                timeout=httpx.Timeout(
                    self.spec.upload_timeout_s, connect=self.spec.connect_timeout_s
                ),
            )

    def upload_media(self, path: Path) -> MediaRef:
        """Upload one file; one retry (``spec.upload_retries``) on a network error.
        An ``inline`` provider has no upload: the file's path stands in for it and
        goes out with the create."""
        if self.spec.upload_style == "inline":
            return MediaRef(id=str(path), path=str(path))
        tries = 1 + max(0, self.spec.upload_retries)
        body: Any = None
        for attempt in range(1, tries + 1):
            try:
                body = self._upload_once(path)
                break
            except PostingError as exc:
                if exc.status != 0 or attempt == tries:
                    raise
                log.info("media upload network error, retrying: %s", exc.message)
        media_id = first(body, self.spec.upload_id)
        if media_id is None or media_id == "":
            raise self._error(200, f"upload reply had no {self.spec.upload_id}")
        media_path = first(body, self.spec.upload_path) if self.spec.upload_path else ""
        return MediaRef(id=str(media_id), path=str(media_path or ""))

    def create_post(self, req: CreateRequest) -> list[str]:
        """Create a draft or scheduled post; returns the ids the provider made.
        Never retried."""
        with contextlib.ExitStack() as stack:
            body = self._request("create", **self._create_kwargs(req, stack))
        return [str(v) for v in extract(body, self.spec.created_ids) if v is not None and v != ""]

    def _create_kwargs(self, req: CreateRequest, stack: contextlib.ExitStack) -> dict[str, Any]:
        payload = self.spec.build_payload(req)
        body: dict[str, Any]
        if self.spec.upload_style == "inline":
            files = []
            for m in req.media:
                fh = stack.enter_context(Path(m.path).open("rb"))  # noqa: SIM115
                files.append((self.spec.upload_field, (Path(m.path).name, fh, "image/jpeg")))
            body = {"data": payload, "files": files}
        else:
            body = {"json": payload}
        return {
            **body,
            "headers": (
                {self.spec.idempotency_header: req.idempotency_key}
                if self.spec.idempotency_header and req.idempotency_key
                else None
            ),
            "timeout": httpx.Timeout(
                self.spec.create_timeout_s, connect=self.spec.connect_timeout_s
            ),
        }
