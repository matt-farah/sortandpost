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
import threading
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from urllib.parse import quote

from nicegui import app as nicegui_app
from nicegui import run as nicegui_run
from nicegui import ui
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, func, select

from sortandpost import __version__
from sortandpost.clip.model import ClipModel, ClipModelUnavailable, clip_available
from sortandpost.clip.store import ModelMismatch
from sortandpost.clip.tagging import approve, pending_groups, reject
from sortandpost.clip.vocab import load_vocab
from sortandpost.config import Settings
from sortandpost.db import init_db, make_engine
from sortandpost.db.browse import get_paths, get_photo, get_rating, list_photos, photo_count
from sortandpost.db.meta import get_generation
from sortandpost.db.models import EmbeddingRow, Photo, Prediction, PredictionState
from sortandpost.indexer import scan_clip_once, scan_once
from sortandpost.indexer.service import run_indexer
from sortandpost.metadata import ExiftoolClient
from sortandpost.search import PhotoFilter, browse_photos, semantic_search
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
    engine: Engine | None = None
    settings: Settings | None = None
    indexer_started: bool = False

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
    _status_bar()


def _status_bar() -> None:
    """Persistent footer: index progress, CLIP queue, write-lock state. Polls
    every 5s; a transient DB lock just shows 'status unavailable' for one tick."""
    if State.engine is None:
        return
    with ui.footer().classes(
        "bg-gray-800 text-gray-200 text-xs px-4 py-1 items-center justify-between"
    ):
        summary = ui.label("…")

        def _tick() -> None:
            try:
                with Session(State.engine) as s:
                    total = photo_count(s, include_missing=True)
                    present = photo_count(s)
                    pending = int(
                        s.exec(
                            select(func.count())
                            .select_from(Prediction)
                            .where(Prediction.state == PredictionState.pending)
                        ).one()
                    )
                lock = (
                    "writable"
                    if State.write_mode == WriteMode.writable
                    else f"read-only · {State.lock_holder}"
                )
                summary.set_text(
                    f"{present}/{total} photos indexed · {total - present} missing · "
                    f"{pending} CLIP suggestions pending · {lock}"
                )
            except Exception:  # never let the status bar break a page
                summary.set_text("status unavailable")

        _tick()
        ui.timer(5.0, _tick)


def _thumb_src(pixel_hash: str, size: int) -> str:
    return f"{_THUMB_URL}/{pixel_hash[:2]}/{pixel_hash}_{size}.webp"


def build_ui(settings: Settings) -> None:
    engine = make_engine(settings.paths.working_db)
    init_db(engine)  # ensure an empty DB still renders an empty grid
    State.engine = engine
    State.settings = settings

    manager = SyncManager(settings, engine=engine)
    try:
        manager.start()
    except Exception:  # browsing must work even if the sync layer is unhappy
        log.exception("sync layer failed to start; continuing read-only")
    State.apply(manager)
    nicegui_app.on_shutdown(manager.stop)

    if settings.config.indexer.run_in_app and State.write_mode == WriteMode.writable:
        _start_indexer_thread(settings, manager)
    _start_lock_watch(settings, manager)

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
    async def search(
        q: str = "",
        stars: int = 0,
        camera: str = "",
        gps: bool = False,
        dups: bool = False,
        missing: bool = False,
    ) -> None:
        _header("/search")
        query = q.strip()
        flt = PhotoFilter(
            min_stars=stars or None,
            camera=camera.strip() or None,
            has_gps=True if gps else None,
            only_duplicates=dups,
            include_missing=missing,
        )

        with ui.row().classes("w-full items-center gap-2 p-4 flex-wrap"):
            box = (
                ui.input(placeholder="Describe a photo… (blank = browse by filters)", value=query)
                .props("clearable dense")
                .classes("min-w-64 flex-1")
            )
            stars_sel = ui.select(
                {0: "any ★", 1: "≥1★", 2: "≥2★", 3: "≥3★", 4: "≥4★", 5: "5★"}, value=stars
            ).props("dense options-dense")
            cam_in = ui.input(placeholder="camera", value=camera).props("dense").classes("w-36")
            gps_cb = ui.checkbox("GPS", value=gps)
            dup_cb = ui.checkbox("dupes", value=dups)
            miss_cb = ui.checkbox("incl. missing", value=missing)

            def _apply() -> None:
                parts: list[str] = []
                if (box.value or "").strip():
                    parts.append(f"q={quote(box.value.strip())}")
                if stars_sel.value:
                    parts.append(f"stars={stars_sel.value}")
                if (cam_in.value or "").strip():
                    parts.append(f"camera={quote(cam_in.value.strip())}")
                if gps_cb.value:
                    parts.append("gps=true")
                if dup_cb.value:
                    parts.append("dups=true")
                if miss_cb.value:
                    parts.append("missing=true")
                ui.navigate.to("/search?" + "&".join(parts) if parts else "/search")

            box.on("keydown.enter", lambda: _apply())
            ui.button("Apply", on_click=_apply)

        if not query and not flt.is_active() and not missing:
            ui.label("Enter a query or pick filters.").classes("m-4 text-gray-500")
            return
        if query and not clip_available():
            ui.label(
                "Semantic search needs CLIP (torch + open-clip-torch), not installed here. "
                "Run in Docker, or search with filters only."
            ).classes("m-4 text-amber-600")
            return

        try:
            photos = await nicegui_run.io_bound(_run_search, settings, engine, query, flt)
        except ClipModelUnavailable:
            ui.label("CLIP model could not be loaded.").classes("m-4 text-amber-600")
            return
        except Exception:
            log.exception("search failed")
            ui.label("Search failed — see logs.").classes("m-4 text-red-500")
            return

        label = f'"{query}"' if query else "filtered browse"
        ui.label(f"{len(photos)} result(s) · {label}").classes("px-4 text-sm text-gray-500")
        if not photos:
            ui.label("No matches.").classes("m-4 text-gray-500")
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
    async def settings_page() -> None:
        _header("/settings")
        ccfg = settings.config.clip
        with _session() as session:
            gen = get_generation(session)
            embeddings = int(session.exec(select(func.count()).select_from(EmbeddingRow)).one())
            pending = int(
                session.exec(
                    select(func.count())
                    .select_from(Prediction)
                    .where(Prediction.state == PredictionState.pending)
                ).one()
            )
            approved = int(
                session.exec(
                    select(func.count())
                    .select_from(Prediction)
                    .where(Prediction.state == PredictionState.approved)
                ).one()
            )
            total = photo_count(session, include_missing=True)
            present = photo_count(session)
        snaps = sorted(settings.paths.snapshots_dir.glob("*.db.zst"))
        last_pub = getattr(manager, "_last_publish_at", None)

        rows: list[tuple[str, str]] = [
            ("Device", f"{settings.device_id[:8]}… on {manager.hostname}"),
            (
                "Write mode",
                State.write_mode
                + ("" if State.write_mode == WriteMode.writable else f"  ({State.lock_holder})"),
            ),
            ("DB generation", str(gen)),
            (
                "Last publish",
                last_pub.isoformat(sep=" ", timespec="seconds") if last_pub else "—",
            ),
            ("Publishes this session", str(getattr(manager, "_publish_count", 0))),
            ("Snapshots", f"{len(snaps)} (newest {snaps[-1].name})" if snaps else "0"),
            ("Library roots", ", ".join(str(r) for r in settings.config.library_roots)),
            ("Photos", f"{present} indexed · {total - present} missing"),
            ("CLIP model", f"{ccfg.model} / {ccfg.pretrained} on {ccfg.device}"),
            ("CLIP embeddings", str(embeddings)),
            ("Predictions", f"{pending} pending · {approved} approved"),
        ]
        if manager.last_merge is not None:
            rows.append(("Last merge", str(manager.last_merge)))
        if manager.restored_from_snapshot is not None:
            rows.append(("Restored from", manager.restored_from_snapshot.name))

        with ui.column().classes("p-4 gap-4 max-w-3xl"):
            with ui.grid(columns=2).classes("gap-x-6 gap-y-1 text-sm"):
                for key, value in rows:
                    ui.label(key).classes("text-gray-400")
                    ui.label(value)

            ui.separator()
            if State.write_mode == WriteMode.writable:

                async def _scan_now() -> None:
                    ui.notify("indexer pass started…")
                    result = await nicegui_run.io_bound(_run_index_pass, settings, engine)
                    ui.notify(result)

                ui.button("Run indexer pass now", on_click=_scan_now)
            else:
                ui.label("Read-only — indexer actions are disabled.").classes(
                    "text-amber-600 text-sm"
                )
            ui.label(
                "Rebuild the CLIP matrix from the CLI:  python -m sortandpost.indexer --reembed"
            ).classes("text-xs text-gray-400")


def _start_indexer_thread(settings: Settings, manager: SyncManager) -> None:
    """Run the continuous indexer (step 6) in-process while this machine is the
    writer. It stops on app shutdown; it never runs in read-only mode. Idempotent
    -- a later call (e.g. from ``_start_lock_watch`` on promotion) is a no-op."""
    if State.indexer_started:
        return
    State.indexer_started = True
    stop = threading.Event()
    thread = threading.Thread(
        target=run_indexer,
        kwargs={
            "settings": settings,
            "is_writable": lambda: manager.mode is WriteMode.writable,
            "stop": stop,
            # Share the one process-wide CLIP model with search so the two don't
            # each hold ~600MB of weights, and so the loop stops re-loading it.
            "clip_model": State.get_clip_model(settings),
        },
        name="sortandpost-indexer",
        daemon=True,
    )
    thread.start()

    def _shutdown() -> None:
        stop.set()
        thread.join(timeout=10)

    nicegui_app.on_shutdown(_shutdown)


def _start_lock_watch(settings: Settings, manager: SyncManager) -> None:
    """Re-check the write lock on a timer so an instance that started **read-only**
    promotes itself once the lock holder releases (or its lock goes stale),
    without a restart. Mirrors the standalone indexer's
    ``is_writable=lambda: manager.refresh()``. ``manager.refresh()`` is a cheap
    no-op once we already hold the lock, so this keeps running harmlessly after
    promotion (and reflects a future takeover in ``State`` for the status bar)."""
    stop = threading.Event()
    interval = max(5, settings.config.sync.lock_heartbeat_sec)

    def _watch() -> None:
        while not stop.wait(interval):
            try:
                before = manager.mode
                mode = manager.refresh()
                State.apply(manager)
                if mode is not before:
                    log.info("write-lock state changed: %s -> %s", before, mode)
                if mode is WriteMode.writable and settings.config.indexer.run_in_app:
                    _start_indexer_thread(settings, manager)  # no-op if already running
            except Exception:  # a bad tick must not kill the watch
                log.exception("lock-watch tick failed")

    thread = threading.Thread(target=_watch, name="sortandpost-lockwatch", daemon=True)
    thread.start()
    nicegui_app.on_shutdown(stop.set)


def _run_search(settings: Settings, engine: Engine, query: str, flt: PhotoFilter) -> list[Photo]:
    """Runs off the event loop (``run.io_bound``). With a query: CLIP text-embed
    + ranked lookup intersected with *flt*. Without: filtered browse by date."""
    with Session(engine) as session:
        if query:
            model = State.get_clip_model(settings)
            hits = semantic_search(session, settings, model, query, limit=_PAGE_SIZE, flt=flt)
            order = {hit.pixel_hash: i for i, hit in enumerate(hits)}
            if not order:
                return []
            photos = session.exec(select(Photo).where(col(Photo.pixel_hash).in_(list(order)))).all()
            photos = sorted(photos, key=lambda p: order[p.pixel_hash])
        else:
            photos = browse_photos(session, flt, limit=_PAGE_SIZE)
        session.expunge_all()
        return list(photos)


def _run_index_pass(settings: Settings, engine: Engine) -> str:
    stats = scan_once(settings)
    try:
        clip_stats = scan_clip_once(settings, model=State.get_clip_model(settings))
    except ModelMismatch as exc:
        return f"{stats}  |  CLIP: {exc}"
    return f"{stats}  |  {clip_stats}"


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
