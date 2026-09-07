"""NiceGUI web app.

Scaffold: pages are registered with placeholder bodies and a shared header that
shows the write-lock state. Real UI lands across build-order steps 3, 5 and 7.
See CLAUDE.md → "Frontend (NiceGUI pages)".
"""

from __future__ import annotations

from nicegui import ui

from sortandpost import __version__
from sortandpost.config import Settings

_PAGES = [
    ("/", "Library"),
    ("/search", "Search"),
    ("/review", "Review"),
    ("/settings", "Settings"),
]


class State:
    """Placeholder for app-wide reactive state until the sync layer sets it."""

    write_mode = "read-only"


def _header(active: str) -> None:
    with ui.header().classes("items-center justify-between"):
        ui.label(f"sortandpost {__version__}").classes("text-lg font-bold")
        with ui.row():
            for path, name in _PAGES:
                ui.link(name, path).classes(
                    "text-white" + (" underline" if path == active else "")
                )
        ui.label(f"[{State.write_mode}]").classes("text-amber-300")


def build_ui(settings: Settings) -> None:
    @ui.page("/")
    def library() -> None:
        _header("/")
        ui.label("Thumbnail grid — build-order step 3.").classes("m-4 text-gray-500")
        ui.label(f"library_roots: {', '.join(map(str, settings.config.library_roots))}").classes(
            "m-4 text-xs text-gray-400"
        )

    @ui.page("/photo/{pixel_hash}")
    def photo(pixel_hash: str) -> None:
        _header("/")
        ui.label(f"Photo detail for {pixel_hash} — build-order step 3.").classes(
            "m-4 text-gray-500"
        )

    @ui.page("/search")
    def search() -> None:
        _header("/search")
        ui.label("Semantic search — build-order step 5.").classes("m-4 text-gray-500")

    @ui.page("/review")
    def review() -> None:
        _header("/review")
        ui.label("CLIP prediction review queue — build-order step 5.").classes(
            "m-4 text-gray-500"
        )

    @ui.page("/settings")
    def settings_page() -> None:
        _header("/settings")
        ui.label("Settings & sync status — build-order step 7.").classes("m-4 text-gray-500")
        ui.label(f"device_id: {settings.device_id}").classes("m-4 text-xs text-gray-400")


def run(settings: Settings) -> None:
    build_ui(settings)
    ui.run(
        host=settings.env.host,
        port=settings.env.port,
        title="sortandpost",
        reload=False,
        show=False,
        storage_secret=settings.device_id,
    )
