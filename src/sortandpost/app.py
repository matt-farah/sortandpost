"""NiceGUI web app.

Step 3 delivers thumbnail-grid browsing + a photo-detail page; step 5a adds
``/search`` (semantic text search over the CLIP matrix). ``/review`` (step 5b)
and ``/settings`` (step 7) are still placeholders.
See CLAUDE.md -> "Frontend (NiceGUI pages)".

Only cheap keyed SELECTs run on the event loop here; image decode / exiftool /
CLIP stay off it -- the indexer process, or ``run.io_bound`` for query embedding
(hard constraint #7).
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from urllib.parse import quote

from nicegui import app as nicegui_app
from nicegui import run as nicegui_run
from nicegui import ui
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from sortandpost import __version__
from sortandpost.clip.model import ClipModel, ClipModelUnavailable, clip_available
from sortandpost.clip.tagging import approve, pending_groups, reject
from sortandpost.clip.vocab import load_vocab
from sortandpost.config import Settings
from sortandpost.db import init_db, make_engine
from sortandpost.db.browse import get_paths, get_photo, get_rating, list_photos, photo_count
from sortandpost.db.models import Photo, Prediction, PredictionState
from sortandpost.metadata import ExiftoolClient
from sortandpost.search.service import semantic_search
from sortandpost.sync import SyncManager, WriteMode
from sortandpost.sync.clock import utcnow

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
    clip_model: ClipModel | None = None

    @classmethod
    def apply(cls, manager: SyncManager) -> None:
        cls.write_mode = manager.mode
        cls.lock_holder = manager._holder_name()

    @classmethod
    def get_clip_model(cls, settings: Settings) -> ClipModel:
        if cls.clip_model is None:
            cls.clip_model = ClipModel(settings.config.clip, settings.paths.models_dir)
        return cls.clip_model


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
    async def search(q: str = "") -> None:
        _header("/search")
        query = q.strip()

        def _go(text: str) -> None:
            ui.navigate.to(f"/search?q={quote(text.strip())}" if text.strip() else "/search")

        with ui.row().classes("w-full items-center gap-2 p-4"):
            box = ui.input(placeholder="Describe a photo...", value=query).classes("flex-1")
            box.props("clearable")
            box.on("keydown.enter", lambda: _go(box.value or ""))
            ui.button("Search", on_click=lambda: _go(box.value or ""))

        if not query:
            ui.label("Type a query to search your library semantically.").classes(
                "m-4 text-gray-500"
            )
            return
        if not clip_available():
            ui.label(
                "Semantic search needs CLIP (torch + open-clip-torch), which isn't "
                "installed in this environment. Run the app in Docker to enable it."
            ).classes("m-4 text-amber-600")
            return

        try:
            photos = await nicegui_run.io_bound(_semantic_search_photos, settings, engine, query)
        except ClipModelUnavailable:
            ui.label("CLIP model could not be loaded.").classes("m-4 text-amber-600")
            return
        except Exception:
            log.exception("semantic search failed")
            ui.label("Search failed - see logs.").classes("m-4 text-red-500")
            return

        ui.label(f'{len(photos)} result(s) for "{query}"').classes("px-4 text-sm text-gray-500")
        if not photos:
            ui.label(
                "No matches yet. Has the CLIP pass run? (python -m sortandpost.indexer --once)"
            ).classes("m-4 text-gray-500")
            return
        with (
            ui.grid()
            .classes("gap-1 p-2")
            .style("grid-template-columns: repeat(auto-fill, minmax(160px, 1fr))")
        ):
            for photo in photos:
                _thumb_cell(photo, thumb_small)

    @ui.page("/review")
    async def review() -> None:
        _header("/review")
        model_name = settings.config.clip.model
        vocab = load_vocab(settings.paths.tags_file)
        with _session() as session:
            groups = pending_groups(session, model=model_name, vocab=vocab)
        pending_total = sum(g.count for g in groups)

        ui.label(f"{pending_total} pending CLIP suggestion(s) across {len(groups)} tag(s)").classes(
            "px-4 pt-2 text-sm text-gray-500"
        )

        if State.write_mode != WriteMode.writable:
            ui.label(
                f"Read-only — {State.lock_holder} holds the write lock. "
                "Approve / reject is disabled."
            ).classes("m-4 text-amber-600")
            return
        if not groups:
            ui.label(
                "Nothing to review. Run  python -m sortandpost.indexer --once  "
                "to generate suggestions."
            ).classes("m-4 text-gray-500")
            return

        async def _bulk(tag_name: str, decision: str) -> None:
            done = await nicegui_run.io_bound(
                _apply_review_bulk, settings, engine, tag_name, decision
            )
            ui.notify(f"{decision}d {done} · {tag_name}")
            ui.navigate.reload()

        for group in groups:
            title = f"{group.tag_name}  ·  {group.count}" + (
                f"  ({group.facet})" if group.facet else ""
            )
            with ui.expansion(title).classes("w-full"):
                with ui.row().classes("gap-2 p-2"):
                    ui.button(
                        "Approve all",
                        on_click=lambda g=group: _bulk(g.tag_name, "approve"),
                    ).props("color=positive")
                    ui.button(
                        "Reject all",
                        on_click=lambda g=group: _bulk(g.tag_name, "reject"),
                    ).props("color=negative flat")
                with (
                    ui.grid()
                    .classes("gap-1 p-2")
                    .style("grid-template-columns: repeat(auto-fill, minmax(120px, 1fr))")
                ):
                    for pixel_hash in group.sample_hashes:
                        with ui.link(target=f"/photo/{pixel_hash}"):
                            ui.image(_thumb_src(pixel_hash, thumb_small)).classes(
                                "w-full aspect-square object-cover bg-gray-100"
                            ).props("loading=lazy")

    @ui.page("/settings")
    def settings_page() -> None:
        _header("/settings")
        ui.label("Settings & sync status - build-order step 7.").classes("m-4 text-gray-500")
        ui.label(f"device_id: {settings.device_id}").classes("m-4 text-xs text-gray-400")


def _semantic_search_photos(settings: Settings, engine: Engine, query: str) -> list[Photo]:
    """Runs off the event loop (``run.io_bound``): CLIP text-embed + ranked lookup."""
    model = State.get_clip_model(settings)
    with Session(engine) as session:
        hits = semantic_search(session, settings, model, query, limit=_PAGE_SIZE)
        order = {hit.pixel_hash: i for i, hit in enumerate(hits)}
        if not order:
            return []
        photos = session.exec(select(Photo).where(col(Photo.pixel_hash).in_(list(order)))).all()
        session.expunge_all()
    return sorted(photos, key=lambda p: order[p.pixel_hash])


def _apply_review_bulk(settings: Settings, engine: Engine, tag_name: str, decision: str) -> int:
    """Off the event loop (``run.io_bound``): approve/reject every pending
    prediction for *tag_name*. Approve embeds keywords via exiftool, so it must
    not touch the loop (hard constraint #7)."""
    model_name = settings.config.clip.model
    facet = load_vocab(settings.paths.tags_file).facet_of(tag_name)
    approving = decision == "approve"
    want_exif = approving and shutil.which("exiftool") is not None
    exif_ctx = (
        ExiftoolClient(write_windows_compat_tags=settings.config.rating.write_windows_compat_tags)
        if want_exif
        else nullcontext(None)
    )

    done = 0
    with Session(engine) as session, exif_ctx as exiftool:
        preds = session.exec(
            select(Prediction).where(
                Prediction.model == model_name,
                Prediction.tag_name == tag_name,
                Prediction.state == PredictionState.pending,
            )
        ).all()
        for pred in preds:
            if approving:
                approve(
                    session,
                    device_id=settings.device_id,
                    model=model_name,
                    pixel_hash=pred.pixel_hash,
                    tag_name=tag_name,
                    facet=facet,
                    now=utcnow(),
                    exiftool=exiftool,
                )
            else:
                reject(
                    session,
                    device_id=settings.device_id,
                    model=model_name,
                    pixel_hash=pred.pixel_hash,
                    tag_name=tag_name,
                    now=utcnow(),
                )
            done += 1
        session.commit()
    return done


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
