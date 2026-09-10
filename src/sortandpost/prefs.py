"""Per-machine UI preferences, stored in ``APP_DATA/local/ui_prefs.json``.

These are machine-specific and **never synced** (they live in ``local/``, not
``config.yaml``): a laptop and a desktop can each pick their own theme.
"""

from __future__ import annotations

import json
import logging
from typing import Literal

from sortandpost.paths import Paths

log = logging.getLogger(__name__)

Theme = Literal["system", "light", "dark"]
_THEMES: tuple[Theme, ...] = ("system", "light", "dark")
_DEFAULT_THEME: Theme = "system"


def load_prefs(paths: Paths) -> dict[str, object]:
    path = paths.ui_prefs_file
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:  # corrupt / unreadable -> fall back to defaults
        log.warning("could not read %s: %s", path, exc)
        return {}


def save_prefs(paths: Paths, prefs: dict[str, object]) -> None:
    path = paths.ui_prefs_file
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(prefs, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def get_theme(paths: Paths) -> Theme:
    value = load_prefs(paths).get("theme")
    for theme in _THEMES:
        if value == theme:
            return theme
    return _DEFAULT_THEME


def set_theme(paths: Paths, theme: Theme) -> None:
    prefs = load_prefs(paths)
    prefs["theme"] = theme if theme in _THEMES else _DEFAULT_THEME
    save_prefs(paths, prefs)
