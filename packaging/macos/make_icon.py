"""Render ``sortandpost.icns`` from the same Pillow drawing the tray uses
(``sortandpost.desktop.icon``). Usage: ``python make_icon.py <out_dir>``."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from sortandpost.desktop.icon import render_icon

# iconutil's expected iconset members: (points, scale)
_SIZES = [(pts, scale) for pts in (16, 32, 128, 256, 512) for scale in (1, 2)]


def main(out_dir: Path) -> Path:
    iconset = out_dir / "sortandpost.iconset"
    iconset.mkdir(parents=True, exist_ok=True)
    master = render_icon(1024)
    for pts, scale in _SIZES:
        px = pts * scale
        name = f"icon_{pts}x{pts}{'@2x' if scale == 2 else ''}.png"
        master.resize((px, px)).save(iconset / name)
    icns = out_dir / "sortandpost.icns"
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], check=True)
    return icns


if __name__ == "__main__":
    print(main(Path(sys.argv[1] if len(sys.argv) > 1 else ".")))
