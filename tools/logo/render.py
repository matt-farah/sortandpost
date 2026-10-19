"""Render every logo asset from the two source SVGs (dev-only; outputs are committed).

    uv run python tools/logo/render.py [<light.svg> <dark.svg>]

Without arguments it re-renders from the copies already in
``src/sortandpost/resources/logo/``. Pillow can't read SVG, so the masters are
rasterized by the Playwright Chromium in the dev venv, then everything is
derived from them with Pillow. Writes:

- ``src/sortandpost/resources/logo/`` -- bundled with the app: the SVGs, the
  adaptive ``favicon.svg``, the 1024px app icon (``desktop.icon.render_icon``,
  the ``.icns`` / ``.ico`` / AppImage PNG), the tray icons and the
  ``apple-touch-icon.png``.
- ``docs/brand/`` -- the web kit for the sortandpost.photos site + the README.
"""

from __future__ import annotations

import base64
import re
import shutil
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageOps

REPO = Path(__file__).resolve().parents[2]
RES = REPO / "src" / "sortandpost" / "resources" / "logo"
WEB = REPO / "docs" / "brand"

TRAY = 88  # 2x the 44px pystray asks for; the menu bar shows it at 22pt
TILE_EDGE = (217, 217, 217, 255)


def rasterize(svg: str, height: int) -> Image.Image:
    """The SVG at *height* px (aspect kept), on transparency."""
    from playwright.sync_api import sync_playwright

    m = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    assert m, "the SVG needs a 0-origin viewBox"
    width = round(float(m.group(1)) / float(m.group(2)) * height)
    src = "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
    html = (
        '<html><body style="margin:0;background:transparent">'
        f'<img src="{src}" style="display:block;width:{width}px;height:{height}px">'
        "</body></html>"
    )
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        page.set_content(html)
        png = page.screenshot(omit_background=True, full_page=False)
        browser.close()
    return Image.open(BytesIO(png)).convert("RGBA")


def fit(img: Image.Image, box: int, frac: float = 1.0) -> Image.Image:
    """*img* scaled to *frac* of a *box*-px square (by its longer side), centred
    on transparency."""
    scale = box * frac / max(img.size)
    w, h = round(img.width * scale), round(img.height * scale)
    out = Image.new("RGBA", (box, box), (0, 0, 0, 0))
    out.alpha_composite(
        img.resize((w, h), Image.Resampling.LANCZOS), ((box - w) // 2, (box - h) // 2)
    )
    return out


def template(img: Image.Image) -> Image.Image:
    """A macOS template image: black where the logo is dark, transparent where
    it is white (the menu bar only reads the alpha)."""
    darkness = ImageOps.invert(img.convert("RGB").convert("L"))
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.putalpha(ImageChops.multiply(img.getchannel("A"), darkness))
    return out


def app_icon(logo: Image.Image, size: int = 1024) -> Image.Image:
    """The light polaroid on a white rounded tile, inside the macOS icon grid's
    ~9% margin."""
    s = size * 4  # supersample the tile's edge
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    pad = round(s * 0.09)
    ImageDraw.Draw(img).rounded_rectangle(
        (pad, pad, s - pad, s - pad),
        radius=round(s * 0.2),
        fill=(255, 255, 255, 255),
        outline=TILE_EDGE,
        width=max(1, round(s * 0.006)),
    )
    img.alpha_composite(fit(logo, s, 0.82 * 0.72))
    return img.resize((size, size), Image.Resampling.LANCZOS)


def opaque_tile(logo: Image.Image, size: int) -> Image.Image:
    """Full-bleed white square (iOS / Android round the corners themselves)."""
    out = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    out.alpha_composite(fit(logo, size, 0.72))
    return out.convert("RGB")


def adaptive_svg(light: str) -> str:
    """One favicon for both browser themes: the light logo (black card, see-through
    photo area) turned white by a ``prefers-color-scheme: dark`` rule, on a square
    viewBox."""
    svg = light.replace('fill="black"', 'class="f"')
    m = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    assert m, "the SVG needs a 0-origin viewBox"
    w, h = float(m.group(1)), float(m.group(2))
    side = max(w, h)
    box = f"{(w - side) / 2:g} {(h - side) / 2:g} {side:g} {side:g}"
    svg = re.sub(
        r'<svg width="[\d.]+" height="[\d.]+" viewBox="[^"]+"',
        f'<svg width="{side:g}" height="{side:g}" viewBox="{box}"',
        svg,
    )
    style = "<style>.f{fill:#000}@media (prefers-color-scheme:dark){.f{fill:#fff}}</style>"
    return re.sub(r"(<svg[^>]*>)", r"\1\n" + style, svg, count=1)


def main(light_src: Path, dark_src: Path) -> None:
    RES.mkdir(parents=True, exist_ok=True)
    WEB.mkdir(parents=True, exist_ok=True)
    light_svg = light_src.read_text(encoding="utf-8")
    dark_svg = dark_src.read_text(encoding="utf-8")
    fav_svg = adaptive_svg(light_svg)
    for name, text in (
        ("logo-light.svg", light_svg),
        ("logo-dark.svg", dark_svg),
        ("favicon.svg", fav_svg),
    ):
        for folder in (RES, WEB):
            (folder / name).write_text(text, encoding="utf-8")

    light = rasterize(light_svg, 2600)
    dark = rasterize(dark_svg, 1080)

    icon = app_icon(light)
    icon.save(RES / "app-icon-1024.png", optimize=True)
    shutil.copyfile(RES / "app-icon-1024.png", WEB / "app-icon-1024.png")

    template(fit(light, TRAY)).save(RES / "tray-template.png", optimize=True)
    fit(light, TRAY).save(RES / "tray-light.png", optimize=True)
    fit(dark, TRAY).save(RES / "tray-dark.png", optimize=True)

    touch = opaque_tile(light, 180)
    touch.save(RES / "apple-touch-icon.png", optimize=True)
    touch.save(WEB / "apple-touch-icon.png", optimize=True)
    opaque_tile(light, 192).save(WEB / "icon-192.png", optimize=True)
    opaque_tile(light, 512).save(WEB / "icon-512.png", optimize=True)
    fit(light, 32).save(WEB / "favicon-32.png", optimize=True)
    fit(light, 256).save(WEB / "favicon.ico", format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])
    print(f"wrote {RES.relative_to(REPO)}/ and {WEB.relative_to(REPO)}/")


if __name__ == "__main__":
    if len(sys.argv) == 3:
        main(Path(sys.argv[1]), Path(sys.argv[2]))
    else:
        main(RES / "logo-light.svg", RES / "logo-dark.svg")
