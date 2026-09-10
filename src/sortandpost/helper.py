"""Server-side view of the optional host file-manager helper agent.

``reveal.py`` can only drive Finder / Explorer when the app runs **natively**.
Under Docker the server is a Linux container with no host GUI, so a tiny agent
(:mod:`tools.sortandpost_helper`, run on the host outside Docker) does it
instead: it listens on ``127.0.0.1`` and the browser -- which *is* on the host --
calls it directly.

The agent writes ``local/helper.json`` (``{port, token, os, heartbeat_at}``),
which the container sees through the ``APP_DATA`` mount. This module just reads
that file so the pages know whether to offer the Reveal / Open / Browse buttons
and what port + token to hand the client. Everything else is client-side
``fetch``; the server never talks to the agent.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from sortandpost.paths import Paths

# The agent rewrites ``heartbeat_at`` every 60 s; treat anything older as dead
# (the agent crashed, or the machine slept).
_STALE_AFTER_SEC = 150


@dataclass
class HelperInfo:
    port: int
    token: str
    os: str = ""


def read_helper_info(paths: Paths) -> HelperInfo | None:
    """The live helper agent's ``{port, token}``, or ``None`` if it isn't
    running (no file, malformed, or a stale heartbeat)."""
    try:
        data = json.loads(paths.helper_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        port = int(data["port"])
        token = str(data["token"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 < port < 65536) or not token:
        return None
    beat = data.get("heartbeat_at", data.get("started_at"))
    if isinstance(beat, int | float) and time.time() - beat > _STALE_AFTER_SEC:
        return None
    return HelperInfo(port=port, token=token, os=str(data.get("os", "")))
