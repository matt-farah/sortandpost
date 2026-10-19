"""The app mark, loaded from the bundled logo PNGs (``resources/logo/``,
rendered from the source SVGs by ``tools/logo/render.py``): one source for the
menu-bar / tray icon (at runtime) and the ``.icns`` / ``.ico`` / AppImage icon
(at build time, ``packaging/*/make_icon.py``)."""

from __future__ import annotations

import sys
from importlib import resources
from typing import Literal

from PIL import Image

TrayVariant = Literal["template", "light", "dark"]


def _load(name: str) -> Image.Image:
    ref = resources.files("sortandpost.resources").joinpath("logo", name)
    with ref.open("rb") as f:
        img = Image.open(f)
        img.load()
    return img.convert("RGBA")


def render_icon(size: int = 1024) -> Image.Image:
    """The full-colour app icon: the polaroid logo on a white rounded tile."""
    img = _load("app-icon-1024.png")
    return img if img.size == (size, size) else img.resize((size, size), Image.Resampling.LANCZOS)


def render_tray_icon(size: int = 44, variant: TrayVariant | None = None) -> Image.Image:
    """The menu-bar / tray icon. ``template`` is black + alpha only (macOS
    recolours it for the menu bar); ``light`` / ``dark`` suit a light / dark
    taskbar. Defaults to :func:`tray_variant`."""
    img = _load(f"tray-{variant or tray_variant()}.png")
    return img.resize((size, size), Image.Resampling.LANCZOS)


def tray_variant() -> TrayVariant:
    """macOS: a template image. Windows: follows the taskbar's light / dark
    setting (read once at startup). Linux panels are mostly dark."""
    if sys.platform == "darwin":
        return "template"
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ) as key:
                light, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
            return "light" if light else "dark"
        except OSError:
            return "dark"
    return "dark"
