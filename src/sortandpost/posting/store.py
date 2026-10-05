"""Per-provider, per-machine, non-secret state: ``local/<provider>.json``.

Holds the cached channel list and the rate-limit call log. Never synced and
never a secret: API keys live in the OS keychain / env (``secrets_store``).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from sortandpost.paths import Paths
from sortandpost.posting.model import Channel, utcnow

_LOCK = threading.Lock()  # read-modify-write of one file from several threads


def state_file(paths: Paths, provider: str) -> Path:
    if not provider.isidentifier():
        raise ValueError(f"bad provider key {provider!r}")
    return paths.provider_file(provider)


def load_state(paths: Paths, provider: str) -> dict[str, Any]:
    try:
        raw = json.loads(state_file(paths, provider).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def update_state(paths: Paths, provider: str, **values: Any) -> None:
    """Merge *values* into the file (tmp + atomic replace)."""
    path = state_file(paths, provider)
    with _LOCK:
        data = load_state(paths, provider)
        data.update(values)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)


def load_channel_cache(paths: Paths, provider: str) -> tuple[list[Channel], str | None]:
    raw = load_state(paths, provider)
    rows = raw.get("channels_cache")
    chans = (
        [c for c in (Channel.from_json(r) for r in rows if isinstance(r, dict)) if c]
        if isinstance(rows, list)
        else []
    )
    fetched = raw.get("channels_fetched_at")
    return chans, fetched if isinstance(fetched, str) else None


def save_channel_cache(paths: Paths, provider: str, channels: list[Channel]) -> None:
    update_state(
        paths,
        provider,
        channels_cache=[c.to_json() for c in channels],
        channels_fetched_at=utcnow().isoformat(),
    )
