"""Cross-session memory of which data folder the user has chosen.

A real ``APP_DATA`` environment variable always decides (Docker, dev), but an end
user never sets one: they pick a shared folder in the first-run wizard or
Settings, and that choice has to survive every later launch regardless of which
directory the launch happened to run from.

This module is a tiny pointer file, kept in a fixed, cwd-independent location
(the user's home directory) rather than inside any APP_DATA tree -- it has to
survive even after the user relocates that tree via the Settings page. It is
consulted only when ``APP_DATA`` is not set for the current launch (see
``Env.resolve_paths``), and written only when the user picks a folder in the
first-run wizard or saves one from ``/settings`` (:mod:`sortandpost.app`).

It holds one of two keys:

* ``sync_dir`` -- the **shared** folder (split layout, :meth:`Paths.split`);
  machine-local data lives in :func:`sortandpost.paths.default_local_root`.
  What onboarding writes.
* ``app_data`` -- a legacy single folder holding both ``local/`` and ``sync/``
  (saved from /settings before the split existed). Still honoured.

``SORTANDPOST_PREF_DIR`` overrides the pointer's folder (tests, or simulating a
fresh machine without touching the real ``~/.sortandpost``).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

_POINTER_NAME = "app_data_path.json"


def _default_pref_dir() -> Path:
    override = os.environ.get("SORTANDPOST_PREF_DIR")
    return Path(override).expanduser() if override else Path.home() / ".sortandpost"


def _pointer_file() -> Path:
    return _default_pref_dir() / _POINTER_NAME


@dataclass(frozen=True)
class Pointer:
    kind: Literal["sync_dir", "app_data"]
    path: Path


def read_pointer() -> Pointer | None:
    """The folder the user last chose, if any (``sync_dir`` wins if both)."""
    path = _pointer_file()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    for kind in ("sync_dir", "app_data"):
        value = data.get(kind)
        if isinstance(value, str) and value.strip():
            return Pointer(kind=kind, path=Path(value).expanduser())
    return None


def read_remembered_app_data() -> Path | None:
    """A legacy single app-data folder saved from the Settings page, if any."""
    ptr = read_pointer()
    return ptr.path if ptr is not None and ptr.kind == "app_data" else None


def _write(payload: dict[str, str]) -> None:
    path = _pointer_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def remember_app_data(app_data: Path) -> None:
    """Persist a legacy single ``app_data`` folder (``local/`` + ``sync/``)."""
    _write({"app_data": str(app_data)})


def remember_sync_dir(sync_dir: Path) -> None:
    """Persist the shared folder future launches use (split layout)."""
    _write({"sync_dir": str(sync_dir)})


def forget_pointer() -> None:
    """Forget the saved folder ("Start setup over" in /settings): the next
    launch has no folder chosen and runs first-run setup again."""
    _pointer_file().unlink(missing_ok=True)
