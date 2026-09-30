"""Logging configuration. Console + a rotating file under ``local/logs``."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

_CONFIGURED = False


def configure(
    logs_dir: Path | None = None,
    level: int = logging.INFO,
    *,
    filename: str = "sortandpost.log",
    console: bool = True,
) -> None:
    """``console=False`` for a process with no terminal (the desktop app's
    server child, whose stdout is only a crash catcher)."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)

    # A windowed Windows app started from Explorer has no stderr at all.
    if console and sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        root.addHandler(stream)

    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            logs_dir / filename, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    logging.getLogger("watchdog").setLevel(logging.WARNING)
    _CONFIGURED = True
