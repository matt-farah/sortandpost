"""Render ``sortandpost.png`` (the AppImage / menu icon) from the same Pillow
drawing the tray uses (``sortandpost.desktop.icon``).
Usage: ``python make_icon.py <out_dir>``."""

from __future__ import annotations

import sys
from pathlib import Path

from sortandpost.desktop.icon import render_icon

SIZE = 256  # the hicolor size AppImage desktop integration looks for


def main(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    png = out_dir / "sortandpost.png"
    render_icon(SIZE).save(png)
    return png


if __name__ == "__main__":
    print(main(Path(sys.argv[1] if len(sys.argv) > 1 else ".")))
