"""Cross-session memory of which APP_DATA folder the user has chosen.

``Env.app_data`` (:mod:`sortandpost.config`) normally comes from the ``APP_DATA``
environment variable or a ``.env`` file, but both only take effect if the launch
command remembers to set them, and ``.env`` is only picked up when the process's
current working directory happens to contain one. Absent either, ``Env`` falls
back to a bare ``./appdata`` resolved against whatever directory the launch
command happened to run from -- a different folder on every launch from a
different terminal/cwd, which looks like "nothing persists between sessions".

This module is a tiny pointer file, kept in a fixed, cwd-independent location
(the user's home directory) rather than inside any APP_DATA tree -- it has to
survive even after the user relocates that tree via the Settings page. It is
consulted only when ``APP_DATA`` is not set for the current launch (see
``Env.resolved_app_data``), and written only when the user explicitly saves a
folder from ``/settings`` (:mod:`sortandpost.app`).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_POINTER_NAME = "app_data_path.json"


def _default_pref_dir() -> Path:
    return Path.home() / ".sortandpost"


def _pointer_file() -> Path:
    return _default_pref_dir() / _POINTER_NAME


def read_remembered_app_data() -> Path | None:
    """The app-data folder the user last saved via the Settings page, if any."""
    path = _pointer_file()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        value = data.get("app_data")
    except (OSError, ValueError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return None
    return Path(value).expanduser() if value else None


def remember_app_data(app_data: Path) -> None:
    """Persist ``app_data`` as the folder future launches should default to."""
    path = _pointer_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"app_data": str(app_data)}, indent=2), encoding="utf-8")
    tmp.replace(path)
