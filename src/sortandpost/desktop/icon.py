"""The app mark, drawn with Pillow: one source for the menu-bar icon (at
runtime) and the ``.icns`` app icon (at build time, ``packaging/macos``) -- no
binary image files in the repo."""

from __future__ import annotations

import math

from PIL import Image, ImageDraw

# Summer palette (sortandpost.themes.PALETTES): deepened coral tile, cream star.
TILE = (242, 92, 84, 255)  # #f25c54
TILE_EDGE = (244, 132, 95, 255)  # #f4845f
STAR = (255, 247, 237, 255)


def _star(cx: float, cy: float, r_out: float, r_in: float) -> list[tuple[float, float]]:
    pts = []
    for i in range(10):
        r = r_out if i % 2 == 0 else r_in
        a = -math.pi / 2 + i * math.pi / 5
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def render_icon(size: int = 1024) -> Image.Image:
    """The full-colour app icon: a rounded coral tile with a rating star."""
    scale = 4  # supersample for smooth edges
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = round(s * 0.09)  # macOS icon grid margin
    d.rounded_rectangle(
        (pad, pad, s - pad, s - pad),
        radius=round(s * 0.2),
        fill=TILE,
        outline=TILE_EDGE,
        width=max(1, round(s * 0.012)),
    )
    d.polygon(_star(s / 2, s * 0.53, s * 0.3, s * 0.13), fill=STAR)
    return img.resize((size, size), Image.Resampling.LANCZOS)


def render_tray_icon(size: int = 44) -> Image.Image:
    """The menu-bar / tray icon: the same star on a small coral disc, legible
    on both light and dark menu bars."""
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = round(s * 0.12)  # menu-bar glyphs sit inside the bar with a margin
    d.ellipse((m, m, s - m, s - m), fill=TILE)
    d.polygon(_star(s / 2, s * 0.54, s * 0.34, s * 0.15), fill=STAR)
    return img.resize((size, size), Image.Resampling.LANCZOS)
