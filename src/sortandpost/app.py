"""NiceGUI web app.

Build-order step 3 delivers real thumbnail-grid browsing and a photo-detail
page, both backed by the working DB and the generated ``sync/thumbnails/`` tree.
Search (step 5), review (step 5) and settings (step 7) are still placeholders.
See CLAUDE.md -> "Frontend (NiceGUI pages)".

Only cheap keyed SELECTs run on the event loop here; image decode / exiftool /
CLIP stay in the indexer process (hard constraint #7).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from nicegui import app as nicegui_app
from nicegui import ui
from sqlmodel import Session

from sortandpost import __version__
from sortandpost.config import Settings
from sortandpost.db import init_db, make_engine
from sortandpost.db.browse import get_paths, get_photo, get_rating, list_photos, photo_count
from sortandpost.db.models import Photo
from sortandpost.sync import SyncManager, WriteMode

log = logging.getLogger(__name__)

_PAGES = [
    ("/", "Library"),
    ("/search", "Search"),
    ("/review", "Review"),
    ("/settings", "Settings"),
]
_PAGE_SIZE = 120
_THUMB_URL = "/thumbs"


class State:
    """App-wide state the sync layer keeps current."""

    write_mode: str = WriteMode.read_only
    lock_holder: str = "nobody"

    @classmethod
    def apply(cls, manager: SyncManager) -> None:
        cls.write_mode = manager.mode
        cls.lock_holder = manager._holder_name()


def _header(active: str) -> None:
    with ui.header().classes("items-center justify-between"):
        ui.label(f"sortandpost {__version__}").classes("text-lg font-bold")
        with ui.row():
            for path, name in _PAGES:
                ui.link(name, path).classes("text-white" + (" underline" if path == active else ""))
        if State.write_mode == WriteMode.writable:
            ui.label("[writable]").classes("text-emerald-300")
        else:
            ui.label(f"[read-only · {State.lock_holder} is writing]").classes("text-amber-300")


def _thumb_src(pixel_hash: str, size: int) -> str:
    return f"{_THUMB_URL}/{pixel_hash[:2]}/{pixel_hash}_{size}.webp"


def build_ui(settings: Settings) -> None:
    engine = make_engine(settings.paths.working_db)
    init_db(engine)  # ensure an empty DB still renders an empty grid

    manager = SyncManager(settings, engine=engine)
    try:
        manager.start()
    except Exception:  # browsing must work even if the sync layer is unhappy
        log.exception("sync layer failed to start; continuing read-only")
    State.apply(manager)
    nicegui_app.on_shutdown(manager.stop)

    settings.paths.thumbnails_dir.mkdir(parents=True, exist_ok=True)
    nicegui_app.add_static_files(_THUMB_URL, str(settings.paths.thumbnails_dir))

    thumb_small, thumb_large = _thumb_sizes(settings)

    @contextmanager
    def _session() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    @ui.page("/")
    def library(page: int = 0) -> None:
        _header("/")
        page = max(page, 0)
        with _session() as session:
            total = photo_count(session)
            photos = list_photos(session, limit=_PAGE_SIZE, offset=page * _PAGE_SIZE)

        pages = max((total + _PAGE_SIZE - 1) // _PAGE_SIZE, 1)
        with ui.row().classes("w-full items-center justify-between px-4 pt-2"):
            ui.label(f"{total} photos").classes("text-sm text-gray-500")
            if pages > 1:
                ui.label(f"page {page + 1} / {pages}").classes("text-sm text-gray-500")

        if not photos:
            ui.label("No photos indexed yet. Run  python -m sortandpost.indexer --once").classes(
                "m-4 text-gray-500"
            )
            return

        with (
            ui.grid()
            .classes("gap-1 p-2")
            .style("grid-template-columns: repeat(auto-fill, minmax(160px, 1fr))")
        ):
            for photo in photos:
                _thumb_cell(photo, thumb_small)

        _pager(page, pages)

    @ui.page("/photo/{pixel_hash}")
    def photo_detail(pixel_hash: str) -> None:
        _header("/")
        with _session() as session:
            photo = get_photo(session, pixel_hash)
            paths = get_paths(session, pixel_hash) if photo else []
            stars = get_rating(session, pixel_hash) if photo else None

        if photo is None:
            ui.label(f"Unknown photo {pixel_hash}").classes("m-4 text-gray-500")
            return

        with ui.row().classes("w-full no-wrap gap-6 p-4"):
            ui.image(_thumb_src(pixel_hash, thumb_large)).classes("max-w-2xl rounded shadow")
            with ui.column().classes("gap-1 text-sm"):
                ui.label("★ " * stars if stars else "unrated").classes("text-amber-500 text-lg")
                _facts(photo)
                ui.separator()
                ui.label("Paths").classes("font-bold mt-2")
                for pp in paths:
                    mark = "" if pp.present else "  (missing)"
                    ui.label(f"{pp.path}{mark}").classes(
                        "text-xs " + ("text-gray-600" if pp.present else "text-red-500")
                    )
        ui.link("< back to library", "/").classes("m-4")

    @ui.page("/search")
    def search() -> None:
        _header("/search")
        ui.label("Semantic search - build-order step 5.").classes("m-4 text-gray-500")

    @ui.page("/review")
    def review() -> None:
        _header("/review")
        ui.label("CLIP prediction review queue - build-order step 5.").classes("m-4 text-gray-500")

    @ui.page("/settings")
    def settings_page() -> None:
        _header("/settings")
        ui.label("Settings & sync status - build-order step 7.").classes("m-4 text-gray-500")
        ui.label(f"device_id: {settings.device_id}").classes("m-4 text-xs text-gray-400")


def _thumb_sizes(settings: Settings) -> tuple[int, int]:
    sizes = sorted(settings.config.thumbnails.sizes) or [320, 1280]
    return sizes[0], sizes[-1]


def _thumb_cell(photo: Photo, size: int) -> None:
    with ui.link(target=f"/photo/{photo.pixel_hash}").classes("block"):
        ui.image(_thumb_src(photo.pixel_hash, size)).classes(
            "w-full aspect-square object-cover bg-gray-100"
        ).props("loading=lazy")


def _facts(photo: Photo) -> None:
    rows: list[tuple[str, str]] = [
        ("Taken", photo.taken_at.isoformat(sep=" ") if photo.taken_at else "-"),
        ("Camera", " ".join(filter(None, (photo.camera_make, photo.camera_model))) or "-"),
        ("Size", f"{photo.width} x {photo.height}"),
        ("Format", f"{photo.format}{'  (RAW)' if photo.is_raw else ''}"),
        ("Orientation", str(photo.orientation) if photo.orientation else "-"),
        (
            "GPS",
            f"{photo.gps_lat:.5f}, {photo.gps_lon:.5f}"
            if photo.gps_lat is not None and photo.gps_lon is not None
            else "-",
        ),
        ("File size", f"{photo.file_size:,} bytes"),
    ]
    with ui.grid(columns=2).classes("gap-x-4 gap-y-1"):
        for label, value in rows:
            ui.label(label).classes("text-gray-400")
            ui.label(value)


def _pager(page: int, pages: int) -> None:
    with ui.row().classes("w-full items-center justify-center gap-4 p-4"):
        if page > 0:
            ui.link("< prev", f"/?page={page - 1}")
        if page + 1 < pages:
            ui.link("next >", f"/?page={page + 1}")


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
