"""Render ``sortandpost.ico`` from the same Pillow drawing the tray uses
(``sortandpost.desktop.icon``). Usage: ``python make_icon.py <out_dir>``."""

from __future__ import annotations

import sys
from pathlib import Path

from sortandpost.desktop.icon import render_icon

# The sizes Explorer, the taskbar and the Start menu ask for.
_SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def main(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ico = out_dir / "sortandpost.ico"
    render_icon(256).save(ico, format="ICO", sizes=[(s, s) for s in _SIZES])
    return ico


if __name__ == "__main__":
    print(main(Path(sys.argv[1] if len(sys.argv) > 1 else ".")))
