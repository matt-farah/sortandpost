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
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

from nicegui import app as nicegui_app
from nicegui import run as nicegui_run
from nicegui import ui
from nicegui.events import KeyEventArguments
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, func, select

from sortandpost import __version__
from sortandpost.clip.model import ClipModel, ClipModelUnavailable, clip_available
from sortandpost.clip.store import ModelMismatch
from sortandpost.clip.tagging import approve, pending_groups, reject
from sortandpost.clip.vocab import load_vocab
from sortandpost.collections import (
    Collection,
    add_to_collection,
    clean_name,
    collections_containing,
    create_collection,
    delete_collection,
    get_collection,
    load_collections,
    remove_from_collection,
    rename_collection,
    reorder_collection,
    set_collection_notes,
)
from sortandpost.config import Settings
from sortandpost.db import add_manual_tag, init_db, make_engine, set_rating
from sortandpost.db.browse import (
    get_paths,
    get_photo,
    get_rating,
    list_photo_ids,
    photo_count,
    unrated_count,
    unrated_photo_ids,
)
from sortandpost.db.deletion import DeleteResult, delete_photos
from sortandpost.db.flags import (
    EDIT_FLAG,
    clear_all_edit_flags,
    edit_flag_count,
    edit_flagged_hashes,
    set_edit_flag,
)
from sortandpost.db.meta import get_generation
from sortandpost.db.models import EmbeddingRow, Photo, Prediction, PredictionState, Rating
from sortandpost.indexer import scan_clip_once, scan_once
from sortandpost.indexer.service import run_indexer
from sortandpost.metadata import ExiftoolClient
from sortandpost.prefs import Theme, get_theme, set_theme
from sortandpost.reveal import open_in_default_app, reveal_in_file_manager, reveal_supported
from sortandpost.search import PhotoFilter, browse_photos, semantic_search
from sortandpost.searches import SavedSearch, add_search, load_searches, remove_search
from sortandpost.stacks import StackIndex, build_stacks
from sortandpost.sync import SyncManager, WriteMode
from sortandpost.sync.clock import utcnow
from sortandpost.trash import trash_backend

log = logging.getLogger(__name__)

_PAGES = [
    ("/", "Library"),
    ("/collections", "Collections"),
    ("/rate", "Rate"),
    ("/search", "Search"),
    ("/review", "Review"),
    ("/settings", "Settings"),
]
_PAGE_SIZE = 120  # search result cap
_GRID_BATCH = 80  # thumbnails appended per infinite-scroll step
_PICKER_CAP = 500  # photos offered at once in the "add photos to collection" picker
_THUMB_URL = "/thumbs"

# The grid's scroll position + loaded count live in this history entry's
# ``history.state``. The browser keeps per-entry state across navigations and
# restores it on back/forward, so returning here -- however many photo/filmstrip
# hops away -- reads back the exact position, while a fresh visit (nav link,
# reload elsewhere, new tab) lands on a null state and starts at the top.
_GRID_RESTORE_JS = """
(() => {
  history.scrollRestoration = 'manual';
  const s = (history.state && history.state.spGrid) || null;
  return s ? [Number(s.n) || 0, Number(s.y) || 0] : [0, 0];
})()
"""

# Written every tick while browsing the grid; returns "near the bottom?".
_GRID_SAVE_JS = (
    "history.replaceState(Object.assign({}, history.state,"
    " {spGrid: {n: %d, y: Math.round(scrollY)}}), '');"
    "window.innerHeight + window.scrollY >= document.body.offsetHeight - 1400"
)

# How many tiles are in the grid's first row -- the step for up/down keyboard
# focus. Measured from the DOM so it tracks the responsive `auto-fill` reflow.
_GRID_COLS_JS = """
(() => {
  const g = document.querySelector('.nicegui-grid');
  if (!g || !g.firstElementChild) return 1;
  const top = g.firstElementChild.offsetTop;
  let n = 0;
  for (const c of g.children) { if (c.offsetTop === top) n++; else break; }
  return n || 1;
})()
"""

# Keyboard-focus on a grid tile: an amber border (via ::after, so it sits above
# the opaque thumbnail) plus -- like a real mouse hover -- the tile's rating and
# flag/checkbox overlays revealed. Toggled by adding `.kb-focus` to the tile <a>;
# every hover-revealed child carries `.kbreveal`.
_GRID_KB_CSS = """
a.group.kb-focus::after,
.sp-pick.kb-focus::after {
  content: ""; position: absolute; inset: 0; box-sizing: border-box;
  border: 4px solid #fbbf24; pointer-events: none; z-index: 20;
}
a.group.kb-focus .kbreveal,
.sp-pick.kb-focus .kbreveal { opacity: 1 !important; }
"""


def _scroll_to_js(y: int) -> str:
    """Scroll to *y*, then keep re-applying until it sticks. The grid rows we just
    added stream in over the websocket, so the document is still growing when
    this starts -- a single ``scrollTo`` lands short. Keep nudging for up to ~8 s,
    stopping early once we're on target, or once the page height has been stable
    for a while and still can't reach it (content genuinely shorter now)."""
    return (
        "(() => {"
        f"  const target = {y};"
        "  const deadline = performance.now() + 8000;"
        "  let lastH = -1, stable = 0;"
        "  const step = () => {"
        "    window.scrollTo(0, target);"
        "    const h = document.documentElement.scrollHeight;"
        "    stable = (h === lastH) ? stable + 1 : 0;"
        "    lastH = h;"
        "    const onTarget = Math.abs(window.scrollY - target) <= 2;"
        "    const settled = stable > 12;"  # height unchanged ~0.6s
        "    if (!onTarget && !settled && performance.now() < deadline)"
        "      setTimeout(step, 50);"
        "    else window.__spSettled = true;"
        "  };"
        "  window.__spSettled = false;"
        "  step();"
        "})()"
    )


class State:
    """App-wide state the sync layer keeps current."""

    write_mode: str = WriteMode.read_only
    lock_holder: str = "nobody"
    clip_model: ClipModel | None = None
    engine: Engine | None = None
    settings: Settings | None = None
    indexer_started: bool = False
    theme: Theme = "system"  # per-machine UI theme (local/ui_prefs.json)

    @classmethod
    def apply(cls, manager: SyncManager) -> None:
        cls.write_mode = manager.mode
        cls.lock_holder = manager._holder_name()

    @classmethod
    def get_clip_model(cls, settings: Settings) -> ClipModel:
        if cls.clip_model is None:
            cls.clip_model = ClipModel(settings.config.clip, settings.paths.models_dir)
        return cls.clip_model


_THEME_TO_DARK: dict[Theme, bool | None] = {"system": None, "light": False, "dark": True}


def _header(active: str) -> ui.dark_mode:
    dark = ui.dark_mode(_THEME_TO_DARK[State.theme])  # per-machine; None = follow the OS
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
    return dark


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
    ui.add_css(_GRID_KB_CSS, shared=True)  # grid keyboard-focus styling, all pages
    engine = make_engine(settings.paths.working_db)
    init_db(engine)  # ensure an empty DB still renders an empty grid
    State.engine = engine
    State.settings = settings
    State.theme = get_theme(settings.paths)

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
    async def library() -> None:
        _header("/")
        client = ui.context.client  # for a valid slot context after awaits / element deletes
        stack_cfg = settings.config.stacking
        with _session() as session:
            all_ids = list_photo_ids(session)
            stacks = build_stacks(session, stack_cfg)
            flag_n = edit_flag_count(session)
        rep_ids = [h for h in all_ids if not stacks.is_hidden_member(h)]
        total = len(rep_ids)
        noun = "stacks" if stack_cfg.enabled and total != len(all_ids) else "photos"

        # pixel_hash -> "set this tile's flag icon on/off", so bulk actions and
        # "clear all" can repaint the visible tiles without a page reload.
        flag_setters: dict[str, Callable[[bool], Any]] = {}
        # pixel_hash -> the tile element, so a delete can drop it from the grid
        # in place (no reload -> no scroll jump, no torn-down-slot races).
        cell_els: dict[str, ui.element] = {}
        # pixel_hash -> drive that tile's rating / flag from the keyboard (same
        # write path as a click). Only populated for writable sessions.
        rating_setters: dict[str, Callable[[int], Any]] = {}
        flag_toggles: dict[str, Callable[[], Any]] = {}
        writable = State.write_mode == WriteMode.writable

        with ui.row().classes("w-full items-center justify-between px-4 pt-2"):
            counter = ui.label(f"{total} {noun}").classes("text-sm text-gray-500")
            flag_box = ui.row().classes("items-center gap-2")

        def _refresh_flag_link(n: int) -> None:
            flag_link.text = f"⚑ {n} marked for editing" if n else ""
            flag_box.set_visibility(n > 0)

        def _recount_flags() -> None:
            with _session() as session:
                _refresh_flag_link(edit_flag_count(session))

        def _repaint_all_flags(on: bool) -> None:
            for setter in flag_setters.values():
                setter(on)

        async def _do_clear_flags() -> None:
            n = await nicegui_run.io_bound(_apply_clear_flags, settings, engine)
            _repaint_all_flags(False)
            _recount_flags()
            ui.notify(f"cleared {n} flag(s)")

        def _confirm_clear_flags() -> None:
            with ui.dialog() as dialog, ui.card().classes("gap-2"):
                ui.label("Remove the “marked for editing” flag from every photo?")
                ui.label("Only the flag is cleared — no photo files are touched.").classes(
                    "text-xs text-gray-500"
                )
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")

                    async def _go() -> None:
                        dialog.close()
                        await _do_clear_flags()

                    ui.button("Clear all", on_click=_go).props("color=negative")
            dialog.open()

        with flag_box:
            flag_link = ui.link("", "/search?mark=true").classes(
                "text-sm text-amber-600 no-underline hover:underline"
            )
            if writable:
                ui.button("clear all", icon="clear", on_click=_confirm_clear_flags).props(
                    "flat dense size=sm color=amber-7"
                )

        _refresh_flag_link(flag_n)

        if total == 0:
            ui.label("No photos indexed yet. Run  python -m sortandpost.indexer --once").classes(
                "m-4 text-gray-500"
            )
            return

        grid = _photo_grid()
        spinner = ui.spinner(size="lg").classes("mx-auto my-6")
        loaded = 0
        busy = False
        done = False
        restoring = True  # hold the tick off until scroll restore settles

        def _end_restore() -> None:
            nonlocal restoring
            restoring = False

        # -- multi-select + bulk toolbar ----------------------------------- #
        sel = _GridSelection(rep_ids, lambda: _render_toolbar())
        toolbar = ui.element("div").classes(
            "fixed bottom-12 left-1/2 z-50 flex -translate-x-1/2 items-center gap-2 "
            "rounded-full bg-gray-900/95 px-4 py-2 text-white shadow-lg backdrop-blur"
        )
        toolbar.set_visibility(False)
        # shift-state for _GridSelection ranges is tracked in the single _grid_key
        # handler below (one ui.keyboard per page -- two of them don't both fire).

        async def _bulk_rate(stars: int) -> None:
            hashes = list(sel.selected)
            n = await nicegui_run.io_bound(_apply_bulk_rating, settings, engine, hashes, stars)
            ui.notify(f"{stars}★ · {n} photo(s)" if stars else f"rating cleared · {n} photo(s)")
            sel.clear()

        async def _rate_one(pixel_hash: str, stars: int) -> None:
            if State.write_mode != WriteMode.writable:
                ui.notify("read-only — a rating change was ignored", type="warning")
                return
            await nicegui_run.io_bound(_apply_rating, settings, engine, pixel_hash, stars)
            ui.notify(f"{stars}★" if stars else "rating cleared")

        async def _flag_one(pixel_hash: str, marked: bool) -> None:
            if State.write_mode != WriteMode.writable:
                ui.notify("read-only — the change was ignored", type="warning")
                return
            await nicegui_run.io_bound(_apply_flag, settings, engine, pixel_hash, marked)
            ui.notify("added to edit list" if marked else "removed from edit list")
            _recount_flags()

        async def _bulk_tag(box: ui.input) -> None:
            name = (box.value or "").strip()
            if not name:
                return
            hashes = list(sel.selected)
            n = await nicegui_run.io_bound(_apply_bulk_tag, settings, engine, hashes, name)
            ui.notify(f'"{name}" · {n} photo(s)')
            box.set_value("")
            sel.clear()

        async def _bulk_flag(marked: bool) -> None:
            hashes = list(sel.selected)
            n = await nicegui_run.io_bound(_apply_bulk_flag, settings, engine, hashes, marked)
            verb = "marked for editing" if marked else "unmarked"
            ui.notify(f"{verb} · {n} photo(s)")
            for h in hashes:
                setter = flag_setters.get(h)
                if setter is not None:
                    setter(marked)
            sel.clear()
            _recount_flags()

        async def _do_bulk_delete() -> None:
            nonlocal total
            hashes = list(sel.selected)
            result = await nicegui_run.io_bound(_apply_bulk_delete, settings, engine, hashes)
            if result is None:  # io_bound cancelled / app shutting down
                return
            with client:  # the awaited gap + tile deletes can invalidate the ambient slot
                sel.clear()
                _clear_focus()
                removed = 0
                for h in hashes:
                    el = cell_els.pop(h, None)
                    if el is not None:
                        el.delete()  # drop the tile in place -- it's above the load cursor
                        removed += 1
                    flag_setters.pop(h, None)
                    rating_setters.pop(h, None)
                    flag_toggles.pop(h, None)
                total = max(0, total - removed)
                counter.set_text(
                    f"{loaded} of {total} {noun}" if loaded < total else f"{total} {noun}"
                )
                _recount_flags()
                ui.notify(_delete_summary(result))

        def _confirm_bulk_delete() -> None:
            count = len(sel.selected)
            if not count:
                return
            with ui.dialog() as dialog, ui.card().classes("gap-2"):
                ui.label(f"Delete {count} selected photo(s)?").classes("font-medium")
                ui.label(f"The photo files will be {_delete_where_note()}.").classes(
                    "text-xs text-gray-500"
                )
                if settings.config.stacking.enabled:
                    ui.label(
                        "Whole stacks will be deleted."
                        if settings.config.deletion.stack_scope == "whole_stack"
                        else "Only the shown photo of each stack is deleted."
                    ).classes("text-xs text-gray-500")
                ui.label("Their ratings, tags and thumbnails are removed too.").classes(
                    "text-xs text-gray-500"
                )
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")

                    async def _go() -> None:
                        # close after the work so ui.notify still has a live slot context
                        await _do_bulk_delete()
                        dialog.close()

                    ui.button("Delete", on_click=_go).props("color=negative")
            dialog.open()

        def _render_toolbar() -> None:
            count = len(sel.selected)
            toolbar.set_visibility(count > 0)
            toolbar.clear()
            if count == 0:
                return
            with toolbar:
                ui.label(f"{count} selected").classes("text-sm font-medium")
                if State.write_mode == WriteMode.writable:
                    for stars in (1, 2, 3, 4, 5):
                        ui.button(str(stars), on_click=lambda s=stars: _bulk_rate(s)).props(
                            "flat dense round color=amber-4"
                        )
                    ui.button(icon="star_border", on_click=lambda: _bulk_rate(0)).props(
                        "flat dense round"
                    ).tooltip("clear rating")
                    tag_in = ui.input(placeholder="tag…").props("dense dark").classes("w-28")
                    tag_in.on("keydown.enter", lambda: _bulk_tag(tag_in))
                    ui.button("Tag", on_click=lambda: _bulk_tag(tag_in)).props("flat dense")
                    ui.button(icon="flag", on_click=lambda: _bulk_flag(True)).props(
                        "flat dense round color=amber-6"
                    ).tooltip("mark for editing")
                    ui.button(icon="outlined_flag", on_click=lambda: _bulk_flag(False)).props(
                        "flat dense round"
                    ).tooltip("remove from edit list")
                    ui.separator().props("vertical dark")
                    ui.button(icon="delete", on_click=_confirm_bulk_delete).props(
                        "flat dense round color=red-4"
                    ).tooltip("delete — move files to trash")
                else:
                    ui.label("read-only").classes("text-xs text-amber-300")
                _collection_add_button(
                    settings, lambda: list(sel.selected), label=None, on_added=sel.clear
                )
                ui.button(icon="close", on_click=sel.clear).props("flat dense round").tooltip(
                    "deselect all"
                )

        def append_batch() -> None:
            nonlocal loaded, busy, done
            if busy or done:
                return
            busy = True
            try:
                slice_ids = rep_ids[loaded : loaded + _GRID_BATCH]
                if slice_ids:
                    with _session() as session:
                        by_hash = {
                            p.pixel_hash: p
                            for p in session.exec(
                                select(Photo).where(col(Photo.pixel_hash).in_(slice_ids))
                            ).all()
                        }
                        stars_by_hash = {
                            r.pixel_hash: r.stars
                            for r in session.exec(
                                select(Rating).where(
                                    col(Rating.pixel_hash).in_(slice_ids), Rating.stars > 0
                                )
                            ).all()
                        }
                        flagged_ids = edit_flagged_hashes(session, slice_ids)
                    with grid:
                        for h in slice_ids:
                            photo = by_hash.get(h)
                            if photo is not None:
                                _thumb_cell(
                                    photo,
                                    thumb_small,
                                    stack_size=stacks.size(h),
                                    selection=sel,
                                    stars=stars_by_hash.get(h, 0),
                                    on_rate=_rate_one,
                                    flagged=h in flagged_ids,
                                    on_flag=_flag_one,
                                    flag_registry=flag_setters,
                                    cell_registry=cell_els,
                                    rating_registry=rating_setters,
                                    flag_toggle_registry=flag_toggles,
                                    on_hover=_on_hover,
                                )
                loaded += len(slice_ids)
                counter.set_text(
                    f"{loaded} of {total} {noun}" if loaded < total else f"{total} {noun}"
                )
                if loaded >= total:
                    done = True
                    spinner.set_visibility(False)
            finally:
                busy = False

        async def _on_tick() -> None:
            nonlocal restoring
            if busy:
                return
            if restoring:
                # Resume once the scroll settle finishes (or the ceiling timer
                # fires); until then don't save or load.
                try:
                    if not await ui.run_javascript("!!window.__spSettled", timeout=1.0):
                        return
                except TimeoutError:
                    return
                restoring = False
            try:
                # Keep saving the position even after `done` -- the user still
                # scrolls through the last loaded rows, and that's exactly the
                # near-the-bottom case that was freezing at a stale spot.
                near_bottom = await ui.run_javascript(_GRID_SAVE_JS % loaded, timeout=2.0)
            except TimeoutError:
                return
            if near_bottom and not done:
                append_batch()

        # -- keyboard / hover focus: arrows move a ring, hover tracks the pointer;
        # Space selects, digits rate, f flags, Enter opens -- all act on the
        # "target" tile: the keyboard ring if set, else whatever the mouse is over.
        # Additive over infinite scroll -- `focus` is just an index into rep_ids.
        focus = [-1]
        hover_hash: list[str | None] = [None]

        def _on_hover(h: str, entered: bool) -> None:
            if entered:
                hover_hash[0] = h
            elif hover_hash[0] == h:
                hover_hash[0] = None

        def _paint_focus(new_idx: int, *, scroll: bool = True) -> None:
            old = focus[0]
            if old == new_idx:
                return
            if 0 <= old < len(rep_ids):
                el = cell_els.get(rep_ids[old])
                if el is not None:
                    el.classes(remove="kb-focus")
            focus[0] = new_idx
            if 0 <= new_idx < len(rep_ids):
                el = cell_els.get(rep_ids[new_idx])
                if el is not None:
                    el.classes(add="kb-focus")
                    if scroll:
                        ui.run_javascript(
                            f"getElement({el.id})?.$el?.scrollIntoView"
                            "({block:'nearest',inline:'nearest'})"
                        )

        def _clear_focus() -> None:
            _paint_focus(-1)

        async def _columns() -> int:
            try:
                n = await ui.run_javascript(_GRID_COLS_JS, timeout=1.0)
                return max(1, int(n))
            except (TimeoutError, ValueError, TypeError):
                return 1

        async def _move_focus(step: int, *, by_row: bool) -> None:
            if not rep_ids:
                return
            if focus[0] < 0:
                _paint_focus(0)
                return
            delta = step * (await _columns()) if by_row else step
            target = max(0, min(len(rep_ids) - 1, focus[0] + delta))
            while loaded <= target and not done:
                append_batch()
            _paint_focus(target)

        def _target_hash() -> str | None:
            """The tile a shortcut acts on: the keyboard ring if set, else the
            tile under the mouse."""
            if 0 <= focus[0] < len(rep_ids):
                return rep_ids[focus[0]]
            return hover_hash[0]

        async def _grid_key(e: KeyEventArguments) -> None:
            sel.shift_down = bool(e.modifiers.shift)  # for shift-click / shift-space ranges
            if not e.action.keydown or e.action.repeat or restoring:
                return
            if e.modifiers.ctrl or e.modifiers.alt or e.modifiers.meta:
                return
            key = e.key
            if key == "ArrowLeft":
                await _move_focus(-1, by_row=False)
            elif key == "ArrowRight":
                await _move_focus(1, by_row=False)
            elif key == "ArrowUp":
                await _move_focus(-1, by_row=True)
            elif key == "ArrowDown":
                await _move_focus(1, by_row=True)
            elif key == "Escape":
                _clear_focus()
                sel.clear()
            elif key == " ":
                h = _target_hash()
                if h is not None:
                    sel.click(h)  # toggle (or extend a range while Shift is held)
            elif key == "Enter":
                h = _target_hash()
                if h is not None:
                    ui.navigate.to(f"/photo/{h}")
            elif key.number is not None and 0 <= key.number <= 5:
                h = _target_hash()
                setter = rating_setters.get(h) if h is not None else None
                if setter is not None:
                    setter(key.number)  # fires the tile's own write path
            elif key == "f":
                h = _target_hash()
                toggle = flag_toggles.get(h) if h is not None else None
                if toggle is not None:
                    await toggle()

        ui.keyboard(on_key=_grid_key)
        # Space would otherwise scroll the page -- swallow its default over the
        # grid (but not while typing in a field or on a focused button).
        ui.run_javascript(
            "if(!window.__spNoSpaceScroll){window.__spNoSpaceScroll=1;"
            "addEventListener('keydown',e=>{"
            "if((e.key===' '||e.code==='Space')&&"
            "!['INPUT','TEXTAREA','SELECT','BUTTON'].includes"
            "(document.activeElement?.tagName))e.preventDefault();"
            "},true);}"
        )

        append_batch()  # first screenful, synchronously
        ui.timer(0.3, _on_tick)

        # Back/forward: this history entry's state carries where we were. Rebuild
        # enough of the grid, then let _scroll_to_js keep nudging until the rows
        # finish streaming in and the target position is reachable. We keep
        # `restoring` set for the whole settle window so the tick can't overwrite
        # the saved position while the page is still growing.
        try:
            await ui.context.client.connected()
            want_n, want_y = await ui.run_javascript(_GRID_RESTORE_JS, timeout=3.0)
        except TimeoutError:
            _end_restore()
            return
        while loaded < int(want_n) and not done:
            append_batch()
        if want_y:
            ui.run_javascript(_scroll_to_js(int(want_y)))
            ui.timer(10.0, _end_restore, once=True)  # ceiling; the tick resumes sooner
        else:
            _end_restore()

    @ui.page("/photo/{pixel_hash}")
    def photo_detail(pixel_hash: str) -> None:
        _header("/")
        client = ui.context.client
        stacking_on = settings.config.stacking.enabled
        with _session() as session:
            photo = get_photo(session, pixel_hash)
            paths = get_paths(session, pixel_hash) if photo else []
            stars = get_rating(session, pixel_hash) if photo else None
            flagged = bool(edit_flagged_hashes(session, [pixel_hash])) if photo else False
            stacks = build_stacks(session, settings.config.stacking) if stacking_on else None
            members = stacks.members(pixel_hash) if stacks else [pixel_hash]
            all_ids = list_photo_ids(session) if photo else []

        if photo is None:
            ui.label(f"Unknown photo {pixel_hash}").classes("m-4 text-gray-500")
            return

        # -- prev/next order: the grid's representative order --------------- #
        if stacks is not None:
            ordered = [h for h in all_ids if not stacks.is_hidden_member(h)]
            anchor = stacks.representative(pixel_hash)
        else:
            ordered = all_ids
            anchor = pixel_hash
        try:
            nav_idx: int | None = ordered.index(anchor)
        except ValueError:
            nav_idx = None
        prev_hash = ordered[nav_idx - 1] if nav_idx is not None and nav_idx > 0 else None
        next_hash = (
            ordered[nav_idx + 1] if nav_idx is not None and nav_idx < len(ordered) - 1 else None
        )

        ui.button("← Back", on_click=lambda: ui.navigate.back()).props("flat dense").classes(
            "mx-4 mt-2"
        )
        with ui.row().classes("w-full no-wrap gap-6 p-4"):
            ui.image(_thumb_src(pixel_hash, thumb_large)).classes("max-w-2xl rounded shadow")
            with ui.column().classes("gap-1 text-sm"):
                star_widget = _star_widget(settings, engine, pixel_hash, stars or 0)
                flag_switch = _edit_flag_button(settings, engine, pixel_hash, flagged)
                ui.label("← → move · 1-5 rate · 0 clear · f flag").classes(
                    "text-[11px] text-gray-400"
                )
                _facts(photo)
                ui.separator()
                ui.label("Paths").classes("font-bold mt-2")
                can_reveal = reveal_supported()
                for pp in paths:
                    _path_row(pp.path, present=pp.present, can_reveal=can_reveal)

        collections_wrap = ui.column().classes("w-full gap-1 px-4")

        def _paint_collections() -> None:
            collections_wrap.clear()
            member_of = collections_containing(settings.paths, pixel_hash)
            with collections_wrap:
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    ui.label("Collections").classes("font-bold text-sm")
                    _collection_add_button(
                        settings, lambda: [pixel_hash], on_added=_paint_collections
                    )
                if not member_of:
                    ui.label("Not in any collection.").classes("text-xs text-gray-500")
                    return
                with ui.row().classes("items-center gap-1 flex-wrap"):
                    for c in member_of:
                        with ui.row().classes(
                            "items-center gap-0 rounded-full bg-gray-200 pl-2 dark:bg-gray-700"
                        ):
                            ui.button(
                                f"{c.name}  ·  {c.count}",
                                on_click=lambda c=c: ui.navigate.to(f"/collections/{c.id}"),
                            ).props("flat dense no-caps size=sm")
                            ui.button(
                                icon="close", on_click=lambda c=c: _leave_collection(c)
                            ).props("flat dense round size=xs").tooltip(f'remove from "{c.name}"')

        def _leave_collection(c: Collection) -> None:
            remove_from_collection(settings.paths, c.id, [pixel_hash])
            ui.notify(f'removed from "{c.name}"')
            _paint_collections()

        _paint_collections()

        if len(members) > 1:
            _stack_filmstrip(pixel_hash, members, thumb_small)

        with ui.row().classes("w-full items-center justify-between m-4"):
            ui.button("← Back to library", on_click=lambda: ui.navigate.back()).props("flat")
            if State.write_mode == WriteMode.writable:
                whole_stack = (
                    settings.config.stacking.enabled
                    and len(members) > 1
                    and settings.config.deletion.stack_scope == "whole_stack"
                )

                async def _do_delete() -> None:
                    result = await nicegui_run.io_bound(
                        _apply_bulk_delete, settings, engine, [pixel_hash]
                    )
                    if result is None:
                        return
                    with client:
                        ui.notify(_delete_summary(result))
                        ui.navigate.to("/")

                def _confirm_delete() -> None:
                    with ui.dialog() as dialog, ui.card().classes("gap-2"):
                        ui.label(
                            f"Delete this stack ({len(members)} photos)?"
                            if whole_stack
                            else "Delete this photo?"
                        ).classes("font-medium")
                        ui.label(f"The file(s) will be {_delete_where_note()}.").classes(
                            "text-xs text-gray-500"
                        )
                        with ui.row().classes("w-full justify-end gap-2"):
                            ui.button("Cancel", on_click=dialog.close).props("flat")

                            async def _go() -> None:
                                await _do_delete()
                                dialog.close()

                            ui.button("Delete", on_click=_go).props("color=negative")
                    dialog.open()

                ui.button("Delete photo", icon="delete", on_click=_confirm_delete).props(
                    "flat color=negative"
                )

        # -- keyboard: arrows / j k move, digits rate, f flags ------------- #
        def _on_key(e: KeyEventArguments) -> None:
            if not e.action.keydown or e.action.repeat:
                return
            if e.modifiers.ctrl or e.modifiers.alt or e.modifiers.meta:
                return
            key = e.key
            # location.replace (not a push) so a run of arrow presses doesn't
            # stack history -- Back, in-app or browser, returns to the library
            # grid you started from, not through every photo you skimmed. Same
            # rationale as the stack filmstrip.
            if (key == "ArrowLeft" or key == "k") and prev_hash:
                ui.run_javascript(f"location.replace('/photo/{prev_hash}')")
            elif (key == "ArrowRight" or key == "j") and next_hash:
                ui.run_javascript(f"location.replace('/photo/{next_hash}')")
            elif star_widget is not None and key.number is not None and 0 <= key.number <= 5:
                star_widget.set_value(key.number)  # fires the same write path as a click
            elif flag_switch is not None and key == "f":
                flag_switch.set_value(not flag_switch.value)

        ui.keyboard(on_key=_on_key)

    @ui.page("/rate")
    async def rate_page() -> None:
        """Swipe-style triage of unrated photos: a random batch, one big card at a
        time. ``1``-``5`` rates (and auto-advances), ``→`` / ``S`` / ``Space``
        skips, ``←`` steps back. Ratings run the same ``_apply_rating`` path as
        every other page (fan out to stack members, EXIF mirror, ``changes`` row).
        A fresh batch is pulled on every visit -- skipped photos resurface later.
        """
        _header("/rate")
        client = ui.context.client
        stacking_on = settings.config.stacking.enabled
        writable = State.write_mode == WriteMode.writable
        batch = 60

        def _pull(session: Session, exclude: list[str]) -> list[str]:
            ids = unrated_photo_ids(session, limit=batch, exclude=exclude)
            if stacking_on:
                stacks = build_stacks(session, settings.config.stacking)
                ids = [h for h in ids if not stacks.is_hidden_member(h)]
            return ids

        with _session() as session:
            remaining0 = unrated_count(session)
            queue: list[str] = _pull(session, [])
        seen: set[str] = set(queue)
        idx = [0]
        rated = [0]
        remaining = [remaining0]
        flag_holder: list[ui.switch | None] = [None]  # current card's flag switch, for the `f` key

        if not writable:
            ui.label(
                f"Read-only — {State.lock_holder} holds the write lock. "
                "You can look through unrated photos, but rating is disabled."
            ).classes("m-4 text-amber-600")

        progress = ui.label().classes("px-4 pt-2 text-sm text-gray-500")
        card = ui.column().classes("w-full max-w-3xl mx-auto items-center gap-3 p-4")

        def _load_more() -> None:
            with _session() as session:
                for h in _pull(session, list(seen)):
                    if h not in seen:
                        seen.add(h)
                        queue.append(h)

        def _current() -> str | None:
            """The hash to show now, skipping any that vanished (deleted / went
            missing) since the batch was pulled. Pulls another batch if we ran
            off the end."""
            while True:
                if idx[0] >= len(queue):
                    before = len(queue)
                    _load_more()
                    if len(queue) == before:
                        return None
                h = queue[idx[0]]
                with _session() as session:
                    photo = get_photo(session, h)
                if photo is None or photo.missing_since is not None:
                    idx[0] += 1
                    continue
                return h

        def _render() -> None:
            card.clear()
            progress.set_text(f"{rated[0]} rated this session · ~{remaining[0]} unrated left")
            h = _current()
            with card:
                if h is None:
                    if rated[0] == 0:
                        ui.icon("check_circle", size="4rem").classes("text-emerald-500")
                        ui.label("Every photo already has a rating — nothing to triage.").classes(
                            "text-lg"
                        )
                    else:
                        ui.icon("done_all", size="4rem").classes("text-emerald-500")
                        ui.label(f"All caught up — you rated {rated[0]} this session.").classes(
                            "text-lg"
                        )
                    ui.button("Back to library", on_click=lambda: ui.navigate.to("/")).props("flat")
                    return

                with _session() as session:
                    photo = get_photo(session, h)
                    stars = get_rating(session, h) or 0
                    flagged = bool(edit_flagged_hashes(session, [h]))
                if photo is None:  # _current() confirmed it; narrow for the type checker
                    return

                # q-img needs a concrete width or it collapses to zero height;
                # mirror the photo page (max-w-2xl) and cap the height so a tall
                # portrait still fits one screen.
                ui.image(_thumb_src(h, thumb_large)).classes(
                    "w-full max-w-2xl rounded shadow bg-gray-100"
                ).style("max-height: 78vh").props("fit=contain")
                meta = [Path(photo.primary_path).name]
                if photo.taken_at:
                    meta.append(photo.taken_at.isoformat(sep=" ", timespec="minutes"))
                cam = " ".join(filter(None, (photo.camera_make, photo.camera_model)))
                if cam:
                    meta.append(cam)
                ui.label(" · ".join(meta)).classes("text-xs text-gray-500")

                with ui.row().classes("items-center gap-5"):
                    rating = ui.rating(value=stars, max=5, size="2.75rem").props("color=amber-6")
                    if writable:
                        rating.on_value_change(lambda h=h, r=rating: _rate(h, int(r.value or 0)))
                    else:
                        rating.props("readonly")
                    flag_holder[0] = _edit_flag_button(settings, engine, h, flagged)

                with ui.row().classes("items-center gap-2"):
                    ui.button("← Back", on_click=lambda: _step(-1)).props("flat").set_enabled(
                        idx[0] > 0
                    )
                    ui.button("Skip →", on_click=lambda: _step(1)).props("flat")
                    ui.link("open photo page", f"/photo/{h}").classes(
                        "text-xs text-gray-500 self-center"
                    )
                ui.label("1-5 rate · f flag for editing · → / S skip · ← back").classes(
                    "text-[11px] text-gray-400"
                )

        def _step(delta: int) -> None:
            if delta < 0 and idx[0] == 0:
                return
            idx[0] += delta
            _render()

        async def _rate(h: str, stars: int) -> None:
            if not writable:
                return
            await nicegui_run.io_bound(_apply_rating, settings, engine, h, stars)
            with client:  # the awaited gap can invalidate the ambient slot
                with _session() as session:
                    remaining[0] = unrated_count(session)
                if stars >= 1:
                    rated[0] += 1
                    ui.notify(f"rated {stars}★")
                    idx[0] += 1
                else:
                    ui.notify("rating cleared")
                _render()

        async def _on_key(e: KeyEventArguments) -> None:
            if not e.action.keydown or e.action.repeat:
                return
            if e.modifiers.ctrl or e.modifiers.alt or e.modifiers.meta:
                return
            key = e.key
            if key == "ArrowLeft":
                _step(-1)
            elif key == "ArrowRight" or key == "s" or key == " ":
                _step(1)
            elif writable and key.number is not None and 1 <= key.number <= 5:
                h = _current()
                if h is not None:
                    await _rate(h, key.number)
            elif key == "f":
                switch = flag_holder[0]
                if switch is not None:
                    switch.set_value(not switch.value)

        ui.keyboard(on_key=_on_key)
        # Space would otherwise scroll the page -- swallow its default (shared,
        # one-time; the library grid installs the same guard).
        ui.run_javascript(
            "if(!window.__spNoSpaceScroll){window.__spNoSpaceScroll=1;"
            "addEventListener('keydown',e=>{"
            "if((e.key===' '||e.code==='Space')&&"
            "!['INPUT','TEXTAREA','SELECT','BUTTON'].includes"
            "(document.activeElement?.tagName))e.preventDefault();"
            "},true);}"
        )
        _render()

    @ui.page("/collections")
    def collections_index() -> None:
        """All collections -- a big card per collection: a mosaic of the first
        few member photos plus the name and count. Collections are ordered,
        hand-curated photo groups kept in ``sync/collections.json`` (synced like
        ``tags.yaml``, not merged)."""
        _header("/collections")
        colls = load_collections(settings.paths)

        def _confirm_delete(c: Collection) -> None:
            with ui.dialog() as dialog, ui.card().classes("gap-2"):
                ui.label(f'Delete collection "{c.name}"?').classes("font-medium")
                ui.label(
                    "Only the collection is removed — no photo files, ratings or tags are touched."
                ).classes("text-xs text-gray-500")
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")

                    def _go() -> None:
                        delete_collection(settings.paths, c.id)
                        dialog.close()
                        ui.navigate.reload()

                    ui.button("Delete", on_click=_go).props("color=negative")
            dialog.open()

        def _new_collection() -> None:
            with ui.dialog() as dialog, ui.card().classes("gap-2 w-80"):
                ui.label("New collection").classes("font-medium")
                name_in = ui.input(placeholder="name").props("dense autofocus").classes("w-full")
                notes_in = (
                    ui.textarea(placeholder="notes (optional)").props("dense").classes("w-full")
                )

                def _go(*, then_add: bool) -> None:
                    name = clean_name(name_in.value or "")
                    if not name:
                        ui.notify("Enter a name (letters, numbers, spaces).", type="warning")
                        return
                    c = create_collection(settings.paths, name, notes=notes_in.value or "")
                    dialog.close()
                    ui.navigate.to(f"/collections/{c.id}" + ("?add=1" if then_add else ""))

                name_in.on("keydown.enter", lambda: _go(then_add=True))
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")
                    ui.button("Create", on_click=lambda: _go(then_add=False)).props("flat")
                    ui.button("Create & add photos", on_click=lambda: _go(then_add=True)).props(
                        "color=primary"
                    )
            dialog.open()

        def _mosaic(c: Collection) -> None:
            """A square 2x2 preview: the first up-to-4 members, empty slots
            filled with a muted tile. Cells are plain ``aspect-square`` divs with
            a CSS ``background-image`` -- divs honour ``aspect-ratio`` exactly, so
            the block is always a clean square with no overflow."""
            previews = c.photos[:4]
            if not previews:
                with ui.element("div").classes(
                    "flex aspect-square w-full items-center justify-center "
                    "bg-gray-100 text-gray-400 dark:bg-gray-800"
                ):
                    ui.icon("photo_library", size="2.5rem")
                return
            with ui.element("div").classes("grid w-full grid-cols-2 gap-0.5"):
                for i in range(4):
                    cell = ui.element("div").classes(
                        "aspect-square w-full bg-gray-200 dark:bg-gray-700"
                    )
                    if i < len(previews):
                        cell.style(
                            f"background-image:url('{_thumb_src(previews[i], thumb_small)}');"
                            "background-size:cover;background-position:center"
                        )

        def _card(c: Collection) -> None:
            # A clickable <div> (not <a>): NiceGUI's link element collapses to
            # ~0 height around a block child, which was clipping the mosaic.
            card = ui.element("div").classes(
                "group relative flex cursor-pointer flex-col overflow-hidden rounded-lg "
                "border border-gray-200 bg-white transition hover:shadow-lg "
                "dark:border-gray-700 dark:bg-gray-900"
            )
            card.on("click", lambda c=c: ui.navigate.to(f"/collections/{c.id}"))
            with card:
                _mosaic(c)
                with ui.element("div").classes("px-3 py-2"):
                    ui.label(c.name).classes(
                        "block w-full truncate text-base font-semibold "
                        "text-gray-900 dark:text-gray-100"
                    )
                    ui.label(f"{c.count} photo(s)").classes("block text-xs text-gray-500")
                ui.button(icon="delete").props("flat dense round size=sm color=red-4").classes(
                    "absolute right-1 top-1 bg-black/40 opacity-0 group-hover:opacity-100"
                ).on("click.stop", lambda c=c: _confirm_delete(c))

        with ui.row().classes("w-full items-center justify-between px-4 pt-2"):
            ui.label(f"{len(colls)} collection(s)").classes("text-sm text-gray-500")
            ui.button("New collection", icon="add", on_click=_new_collection).props("flat")

        if not colls:
            ui.label(
                "No collections yet. Create one, then add photos from the library "
                "(select tiles, then Add to collection) or an individual photo page."
            ).classes("m-4 text-gray-500")
            return

        with _photo_grid(300):
            for c in colls:
                _card(c)

    @ui.page("/collections/{coll_id}")
    def collection_detail(coll_id: str, add: int = 0) -> None:
        """One collection: its photos in membership order (per-tile remove /
        move-earlier / move-later), with the editable notes at the bottom.
        ``?add=1`` auto-opens the "add photos" picker (used right after
        creating)."""
        _header("/collections")
        coll = get_collection(settings.paths, coll_id)
        if coll is None:
            ui.label("Unknown collection.").classes("m-4 text-gray-500")
            ui.link("← All collections", "/collections").classes("mx-4")
            return

        ui.button("← All collections", on_click=lambda: ui.navigate.to("/collections")).props(
            "flat dense"
        ).classes("mx-4 mt-2")

        title = ui.label(coll.name).classes("px-4 text-2xl font-bold")
        meta = ui.label().classes("px-4 text-sm text-gray-500")

        def _rename() -> None:
            with ui.dialog() as dialog, ui.card().classes("gap-2"):
                ui.label("Rename collection").classes("font-medium")
                name_in = ui.input(value=title.text).props("dense autofocus").classes("w-64")

                def _go() -> None:
                    new = clean_name(name_in.value or "")
                    if not new:
                        ui.notify("Enter a name (letters, numbers, spaces).", type="warning")
                        return
                    rename_collection(settings.paths, coll_id, new)
                    title.set_text(new)
                    dialog.close()

                name_in.on("keydown.enter", _go)
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")
                    ui.button("Save", on_click=_go).props("color=primary")
            dialog.open()

        def _confirm_delete() -> None:
            with ui.dialog() as dialog, ui.card().classes("gap-2"):
                ui.label(f'Delete collection "{title.text}"?').classes("font-medium")
                ui.label("Only the collection is removed — no photo files are touched.").classes(
                    "text-xs text-gray-500"
                )
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")

                    def _go() -> None:
                        delete_collection(settings.paths, coll_id)
                        dialog.close()
                        ui.navigate.to("/collections")

                    ui.button("Delete", on_click=_go).props("color=negative")
            dialog.open()

        def _add_photos_dialog() -> None:
            c = get_collection(settings.paths, coll_id)
            if c is None:
                return
            have = set(c.photos)
            with _session() as session:
                candidates = [h for h in list_photo_ids(session) if h not in have]
            shown = candidates[:_PICKER_CAP]

            cells: dict[str, ui.element] = {}
            focus = [-1]
            hover_hash: list[str | None] = [None]

            def _on_change() -> None:
                count_lbl.set_text(f"{len(sel.selected)} selected")
                for hh, cell in cells.items():
                    if hh in sel.selected:
                        cell.classes(add="ring-2 ring-primary")
                    else:
                        cell.classes(remove="ring-2 ring-primary")

            # Same multi-select model as the library grid: hover checkbox,
            # click / shift-click ranges. Space (below) drives the same .click().
            sel = _GridSelection(shown, _on_change)

            def _paint_focus(idx: int) -> None:
                old = focus[0]
                if old == idx:
                    return
                if 0 <= old < len(shown):
                    prev = cells.get(shown[old])
                    if prev is not None:
                        prev.classes(remove="kb-focus")
                focus[0] = idx
                if 0 <= idx < len(shown):
                    cur = cells.get(shown[idx])
                    if cur is not None:
                        cur.classes(add="kb-focus")
                        # Scroll the focused tile into view inside the picker's
                        # overflow container. Target the raw DOM node by position
                        # -- `getElement(id).$el` isn't the <div> for a plain
                        # ui.element, so scrollIntoView through it is a no-op.
                        ui.run_javascript(
                            f"document.querySelectorAll('.q-dialog .sp-pick')[{idx}]"
                            "?.scrollIntoView({block:'nearest',inline:'nearest'})"
                        )

            _cols_cache = [0]  # measured once on first up/down; reflow is not a concern here

            async def _cols() -> int:
                if _cols_cache[0]:
                    return _cols_cache[0]
                try:
                    n = await ui.run_javascript(
                        "(()=>{const g=document.querySelector('.q-dialog .nicegui-grid');"
                        "if(!g||!g.firstElementChild)return 1;"
                        "const t=g.firstElementChild.offsetTop;let n=0;"
                        "for(const c of g.children){if(c.offsetTop===t)n++;else break;}"
                        "return n||1;})()",
                        timeout=2.0,
                    )
                    val = max(1, int(n))
                except (TimeoutError, ValueError, TypeError):
                    return 1  # don't cache a failed read -- retry next press
                _cols_cache[0] = val
                return val

            async def _move(step: int, *, by_row: bool) -> None:
                if not shown:
                    return
                if focus[0] < 0:
                    _paint_focus(0)
                    return
                delta = step * (await _cols()) if by_row else step
                _paint_focus(max(0, min(len(shown) - 1, focus[0] + delta)))

            def _target() -> str | None:
                if 0 <= focus[0] < len(shown):
                    return shown[focus[0]]
                return hover_hash[0]

            def _hover(h: str, entered: bool) -> None:
                if entered:
                    hover_hash[0] = h
                elif hover_hash[0] == h:
                    hover_hash[0] = None

            with (
                ui.dialog().props("maximized") as dialog,
                ui.card().classes("flex h-full w-full flex-col gap-2"),
            ):

                def _do_add() -> None:
                    if sel.selected:
                        added = add_to_collection(
                            settings.paths, coll_id, [h for h in shown if h in sel.selected]
                        )
                        ui.notify(f"added {added} photo(s)")
                    dialog.close()
                    _render_grid()

                async def _key(e: KeyEventArguments) -> None:
                    if not dialog.value:  # stale handler from a previous open
                        return
                    sel.shift_down = bool(e.modifiers.shift)
                    if not e.action.keydown or e.action.repeat:
                        return
                    if e.modifiers.ctrl or e.modifiers.alt or e.modifiers.meta:
                        return
                    key = e.key
                    if key == "ArrowLeft":
                        await _move(-1, by_row=False)
                    elif key == "ArrowRight":
                        await _move(1, by_row=False)
                    elif key == "ArrowUp":
                        await _move(-1, by_row=True)
                    elif key == "ArrowDown":
                        await _move(1, by_row=True)
                    elif key == "Escape":
                        dialog.close()
                    elif key == " ":
                        h = _target()
                        if h is not None:
                            sel.click(h)  # toggle, or extend a range while Shift is held
                    elif key == "Enter":
                        _do_add()

                with ui.row().classes("w-full items-center justify-between"):
                    with ui.row().classes("items-center gap-3"):
                        ui.label("Add photos to this collection").classes("text-lg font-medium")
                        ui.label("↔ move · space select · shift+space range · enter add").classes(
                            "text-xs text-gray-400"
                        )
                    count_lbl = ui.label("0 selected").classes("text-sm text-gray-500")
                if len(candidates) > _PICKER_CAP:
                    ui.label(
                        f"Showing the first {_PICKER_CAP} of {len(candidates)} — add the rest "
                        "from the library's bulk toolbar."
                    ).classes("text-xs text-amber-600")
                if not shown:
                    ui.label("Every photo is already in this collection.").classes("text-gray-500")

                # a plain overflow-y-auto div (not ui.scroll_area): q-scroll-area
                # moves its content with a transform, so native scrollIntoView --
                # what the keyboard focus uses to stay in view -- has no effect.
                with (
                    ui.element("div")
                    .classes("w-full flex-1 overflow-y-auto")
                    .style("min-height: 0"),  # let the flex item shrink so it can scroll
                    _photo_grid(150),
                ):
                    for h in shown:
                        cell = ui.element("div").classes(
                            "sp-pick group relative block cursor-pointer overflow-hidden "
                            "rounded ring-0"
                        )
                        cells[h] = cell
                        with cell:
                            ui.image(_thumb_src(h, thumb_small)).classes(
                                "aspect-square w-full bg-gray-100 object-cover"
                            ).props("loading=lazy")
                            sel.checkbox(h)
                        cell.on("click", lambda h=h: sel.click(h))
                        cell.on("mouseenter", lambda h=h: _hover(h, True))
                        cell.on("mouseleave", lambda h=h: _hover(h, False))

                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")
                    ui.button("Add selected", icon="add", on_click=_do_add).props("color=primary")

                # ignore keys only while a real text field is focused -- NOT while
                # a <button> is (the default), or the arrow/space shortcuts go
                # dead whenever the picker is opened from a button click.
                ui.keyboard(on_key=_key, ignore=["input", "select", "textarea"])
                # don't let Space scroll the picker (shared one-time guard)
                ui.run_javascript(
                    "if(!window.__spNoSpaceScroll){window.__spNoSpaceScroll=1;"
                    "addEventListener('keydown',e=>{"
                    "if((e.key===' '||e.code==='Space')&&"
                    "!['INPUT','TEXTAREA','SELECT','BUTTON'].includes"
                    "(document.activeElement?.tagName))e.preventDefault();"
                    "},true);}"
                )
            dialog.open()
            # When opened from the "Add photos" button, that <button> keeps DOM
            # focus -- and ui.keyboard ignores keys while a button is focused, so
            # the arrow/space shortcuts silently do nothing. Blur it; q-dialog's
            # focus trap then holds focus on the dialog itself (a div).
            ui.run_javascript(
                "setTimeout(() => { const a = document.activeElement;"
                " if (a && a.tagName === 'BUTTON') a.blur(); }, 60)"
            )

        with ui.row().classes("items-center gap-2 px-4"):
            ui.button("Add photos", icon="add_photo_alternate", on_click=_add_photos_dialog).props(
                "color=primary flat"
            )
            ui.button("Rename", icon="edit", on_click=_rename).props("flat dense")
            ui.button("Delete", icon="delete", on_click=_confirm_delete).props(
                "flat dense color=negative"
            )

        grid_wrap = ui.element("div").classes("w-full")

        def _members() -> tuple[list[Photo], list[str]]:
            c = get_collection(settings.paths, coll_id)
            member_hashes = c.photos if c else []
            if not member_hashes:
                return [], []
            with _session() as session:
                found = {
                    p.pixel_hash: p
                    for p in session.exec(
                        select(Photo).where(col(Photo.pixel_hash).in_(member_hashes))
                    ).all()
                }
            present = [
                found[h] for h in member_hashes if h in found and found[h].missing_since is None
            ]
            gone = [
                h for h in member_hashes if h not in found or found[h].missing_since is not None
            ]
            return present, gone

        def _move(pixel_hash: str, step: int, ids: list[str]) -> None:
            order = list(ids)
            if pixel_hash not in order:
                return
            i = order.index(pixel_hash)
            j = i + step
            if not 0 <= j < len(order):
                return
            order[i], order[j] = order[j], order[i]
            reorder_collection(settings.paths, coll_id, order)
            _render_grid()

        def _remove(hashes: list[str]) -> None:
            n = remove_from_collection(settings.paths, coll_id, hashes)
            if n:
                ui.notify(f"removed {n} from collection")
            _render_grid()

        def _member_tile(photo: Photo, i: int, ids: list[str]) -> None:
            with ui.element("div").classes("group relative block"):
                with ui.link(target=f"/photo/{photo.pixel_hash}"):
                    ui.image(_thumb_src(photo.pixel_hash, thumb_small)).classes(
                        "w-full aspect-square object-cover bg-gray-100"
                    ).props("loading=lazy")
                ui.label(str(i + 1)).classes(
                    "absolute left-1 top-1 rounded bg-black/70 px-1.5 py-0.5 text-xs "
                    "font-medium text-white"
                )
                with ui.row().classes(
                    "absolute right-1 top-1 gap-0.5 opacity-0 transition group-hover:opacity-100"
                ):
                    if i > 0:
                        ui.button(
                            icon="arrow_back",
                            on_click=lambda h=photo.pixel_hash: _move(h, -1, ids),
                        ).props("flat dense round size=xs color=white").classes(
                            "bg-black/55"
                        ).tooltip("move earlier")
                    if i < len(ids) - 1:
                        ui.button(
                            icon="arrow_forward",
                            on_click=lambda h=photo.pixel_hash: _move(h, 1, ids),
                        ).props("flat dense round size=xs color=white").classes(
                            "bg-black/55"
                        ).tooltip("move later")
                    ui.button(
                        icon="close",
                        on_click=lambda h=photo.pixel_hash: _remove([h]),
                    ).props("flat dense round size=xs color=white").classes("bg-black/55").tooltip(
                        "remove from collection"
                    )

        def _render_grid() -> None:
            grid_wrap.clear()
            present, gone = _members()
            meta.set_text(
                f"{len(present)} photo(s)" + (f" · {len(gone)} unavailable" if gone else "")
            )
            with grid_wrap:
                if gone:
                    with ui.row().classes("items-center gap-2 px-4 text-xs text-amber-600"):
                        ui.label(f"{len(gone)} photo(s) missing or deleted")
                        ui.button("Remove them", on_click=lambda g=gone: _remove(g)).props(
                            "flat dense size=sm"
                        )
                if not present:
                    with ui.column().classes("m-4 items-start gap-2 text-gray-500"):
                        ui.label("Empty — no photos in this collection yet.")
                        ui.button(
                            "Add photos", icon="add_photo_alternate", on_click=_add_photos_dialog
                        ).props("color=primary flat")
                    return
                ids = [p.pixel_hash for p in present]
                with _photo_grid():
                    for i, photo in enumerate(present):
                        _member_tile(photo, i, ids)

        _render_grid()

        # Notes live at the bottom -- below the photos.
        with ui.column().classes("w-full gap-1 px-4 pt-4 pb-8"):
            ui.label("Notes").classes("text-sm font-bold")
            ui.label("Reserved for the caption / post copy this collection feeds.").classes(
                "text-xs text-gray-400"
            )
            notes_box = (
                ui.textarea(value=coll.notes).props("outlined autogrow").classes("w-full max-w-2xl")
            )

            def _save_notes() -> None:
                set_collection_notes(settings.paths, coll_id, notes_box.value or "")
                ui.notify("notes saved")

            notes_box.on("blur", _save_notes)
            ui.button("Save notes", icon="save", on_click=_save_notes).props("flat dense")

        if add:
            ui.timer(0.4, _add_photos_dialog, once=True)

    @ui.page("/search")
    async def search(
        q: str = "",
        stars: int = 0,
        camera: str = "",
        gps: bool = False,
        dups: bool = False,
        missing: bool = False,
        mark: bool = False,
    ) -> None:
        _header("/search")
        query = q.strip()
        flt = PhotoFilter(
            min_stars=stars or None,
            camera=camera.strip() or None,
            has_gps=True if gps else None,
            only_duplicates=dups,
            include_missing=missing,
            tags_all=[EDIT_FLAG] if mark else [],
        )

        saved = load_searches(settings.paths)

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
            mark_cb = ui.checkbox("⚑ to edit", value=mark)

            def _current(name: str = "") -> SavedSearch:
                return SavedSearch(
                    name=name,
                    q=(box.value or "").strip(),
                    stars=int(stars_sel.value or 0),
                    camera=(cam_in.value or "").strip(),
                    gps=bool(gps_cb.value),
                    dups=bool(dup_cb.value),
                    missing=bool(miss_cb.value),
                    mark=bool(mark_cb.value),
                )

            def _apply() -> None:
                ui.navigate.to(_current().query_string())

            box.on("keydown.enter", lambda: _apply())
            ui.button("Apply", on_click=_apply)

            def _confirm_save() -> None:
                if _current().is_empty():
                    ui.notify("Nothing to save — type a query or pick a filter.", type="warning")
                    return
                with ui.dialog() as dialog, ui.card().classes("gap-2"):
                    ui.label("Save this search").classes("font-medium")
                    name_in = ui.input(placeholder="name").props("dense autofocus").classes("w-64")

                    def _do() -> None:
                        name = (name_in.value or "").strip()
                        if not name:
                            return
                        add_search(settings.paths, _current(name))
                        dialog.close()
                        ui.navigate.to(_current(name).query_string())

                    name_in.on("keydown.enter", _do)
                    with ui.row().classes("w-full justify-end gap-2"):
                        ui.button("Cancel", on_click=dialog.close).props("flat")
                        ui.button("Save", on_click=_do).props("color=primary")
                dialog.open()

            ui.button(icon="bookmark_add", on_click=_confirm_save).props("flat dense").tooltip(
                "save this search"
            )

        saved_row = ui.row().classes("w-full items-center gap-1 px-4 flex-wrap")

        def _render_saved() -> None:
            saved_row.clear()
            with saved_row:
                if not saved:
                    ui.label("No saved searches yet.").classes("text-xs text-gray-500")
                    return
                ui.label("Saved:").classes("text-xs text-gray-500 mr-1")
                for s in saved:
                    with ui.row().classes(
                        "items-center gap-0 rounded-full bg-gray-200 dark:bg-gray-700 pl-1"
                    ):
                        ui.button(
                            s.name,
                            icon="search",
                            on_click=lambda s=s: ui.navigate.to(s.query_string()),
                        ).props("flat dense no-caps size=sm").tooltip(s.summary())
                        ui.button(icon="close", on_click=lambda s=s: _delete(s)).props(
                            "flat dense round size=xs"
                        ).tooltip(f'delete "{s.name}"')

        def _delete(s: SavedSearch) -> None:
            nonlocal saved
            saved = remove_search(settings.paths, s.name)
            _render_saved()
            ui.notify(f'deleted "{s.name}"')

        _render_saved()

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
            result = await nicegui_run.io_bound(_run_search, settings, engine, query, flt)
            photos, stack_sizes = result if result else ([], {})
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
        with _photo_grid():
            for photo in photos:
                _thumb_cell(photo, thumb_small, stack_size=stack_sizes.get(photo.pixel_hash, 1))

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
                with _photo_grid(150):
                    for pixel_hash in group.sample_hashes:
                        with ui.link(target=f"/photo/{pixel_hash}"):
                            ui.image(_thumb_src(pixel_hash, thumb_small)).classes(
                                "w-full aspect-square object-cover bg-gray-100"
                            ).props("loading=lazy")

    @ui.page("/settings")
    async def settings_page() -> None:
        dark = _header("/settings")
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
            _theme_editor(settings, dark)

            ui.separator()
            if State.write_mode == WriteMode.writable:

                async def _scan_now() -> None:
                    ui.notify("indexer pass started…")
                    result = await nicegui_run.io_bound(_run_index_pass, settings, engine)
                    ui.notify(result)

                ui.button("Run indexer pass now", on_click=_scan_now)
                _exclude_editor(settings, engine)
                _stacking_editor(settings)
                _deletion_editor(settings)
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


def _run_search(
    settings: Settings, engine: Engine, query: str, flt: PhotoFilter
) -> tuple[list[Photo], dict[str, int]]:
    """Runs off the event loop (``run.io_bound``). With a query: CLIP text-embed
    + ranked lookup intersected with *flt*. Without: filtered browse by date.
    Returns ``(photos, stack_sizes)`` -- when stacking is on, results are
    collapsed to representatives and ``stack_sizes`` maps each to its member
    count."""
    with Session(engine) as session:
        if query:
            model = State.get_clip_model(settings)
            hits = semantic_search(session, settings, model, query, limit=_PAGE_SIZE, flt=flt)
            order = {hit.pixel_hash: i for i, hit in enumerate(hits)}
            if not order:
                return [], {}
            found = session.exec(select(Photo).where(col(Photo.pixel_hash).in_(list(order)))).all()
            photos = sorted(found, key=lambda p: order[p.pixel_hash])
        else:
            photos = list(browse_photos(session, flt, limit=_PAGE_SIZE))

        sizes: dict[str, int] = {}
        if settings.config.stacking.enabled:
            stacks = build_stacks(session, settings.config.stacking)
            photos = _collapse_stacks(photos, stacks, session)
            sizes = {p.pixel_hash: stacks.size(p.pixel_hash) for p in photos}

        session.expunge_all()
        return list(photos), sizes


def _collapse_stacks(photos: list[Photo], stacks: StackIndex, session: Session) -> list[Photo]:
    """Replace each result with its stack representative, keeping the first
    (best-ranked) hit per stack and pulling in any representative not itself a
    hit."""
    have = {p.pixel_hash: p for p in photos}
    ordered_reps: list[str] = []
    seen: set[str] = set()
    for photo in photos:
        rep = stacks.representative(photo.pixel_hash)
        if rep not in seen:
            seen.add(rep)
            ordered_reps.append(rep)
    missing = [h for h in ordered_reps if h not in have]
    if missing:
        for photo in session.exec(select(Photo).where(col(Photo.pixel_hash).in_(missing))).all():
            have[photo.pixel_hash] = photo
    return [have[h] for h in ordered_reps if h in have]


def _run_index_pass(settings: Settings, engine: Engine) -> str:
    stats = scan_once(settings)
    try:
        clip_stats = scan_clip_once(settings, model=State.get_clip_model(settings))
    except ModelMismatch as exc:
        return f"{stats}  |  CLIP: {exc}"
    except ClipModelUnavailable:
        return f"{stats}  |  CLIP: skipped (torch / open-clip not installed)"
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


def _apply_rating(settings: Settings, engine: Engine, pixel_hash: str, stars: int) -> bool:
    """Off the event loop (``run.io_bound``): set the rating (``ratings`` row +
    ``changes`` row) and mirror it into the present, embed-capable file copies via
    exiftool when available (hard constraints #4, #7). With stacking on, the
    rating is applied to every member of the stack (stack-level ratings)."""
    want_exif = shutil.which("exiftool") is not None
    exif_ctx = (
        ExiftoolClient(write_windows_compat_tags=settings.config.rating.write_windows_compat_tags)
        if want_exif
        else nullcontext(None)
    )
    with Session(engine) as session, exif_ctx as exiftool:
        targets = [pixel_hash]
        if settings.config.stacking.enabled:
            targets = build_stacks(session, settings.config.stacking).members(pixel_hash)
        changed = False
        for target in targets:
            changed |= set_rating(
                session,
                pixel_hash=target,
                stars=stars,
                device_id=settings.device_id,
                now=utcnow(),
                exiftool=exiftool,
            )
        session.commit()
    return changed


def _bulk_targets(session: Session, settings: Settings, hashes: list[str]) -> list[str]:
    """Expand a selection to the pixel_hashes that should actually be written --
    every stack member when stacking is on."""
    if not settings.config.stacking.enabled:
        return list(hashes)
    stacks = build_stacks(session, settings.config.stacking)
    out: list[str] = []
    seen: set[str] = set()
    for pixel_hash in hashes:
        for member in stacks.members(pixel_hash):
            if member not in seen:
                seen.add(member)
                out.append(member)
    return out


def _apply_bulk_rating(settings: Settings, engine: Engine, hashes: list[str], stars: int) -> int:
    """Off the event loop: rate every selected photo (fanning to stack members)."""
    want_exif = shutil.which("exiftool") is not None
    exif_ctx = (
        ExiftoolClient(write_windows_compat_tags=settings.config.rating.write_windows_compat_tags)
        if want_exif
        else nullcontext(None)
    )
    with Session(engine) as session, exif_ctx as exiftool:
        for target in _bulk_targets(session, settings, hashes):
            set_rating(
                session,
                pixel_hash=target,
                stars=stars,
                device_id=settings.device_id,
                now=utcnow(),
                exiftool=exiftool,
            )
        session.commit()
    return len(hashes)


def _apply_bulk_tag(settings: Settings, engine: Engine, hashes: list[str], tag_name: str) -> int:
    """Off the event loop: add *tag_name* to every selected photo (+ stack members)."""
    want_exif = shutil.which("exiftool") is not None
    exif_ctx = (
        ExiftoolClient(write_windows_compat_tags=settings.config.rating.write_windows_compat_tags)
        if want_exif
        else nullcontext(None)
    )
    with Session(engine) as session, exif_ctx as exiftool:
        for target in _bulk_targets(session, settings, hashes):
            add_manual_tag(
                session,
                pixel_hash=target,
                tag_name=tag_name,
                device_id=settings.device_id,
                now=utcnow(),
                exiftool=exiftool,
            )
        session.commit()
    return len(hashes)


def _apply_flag(settings: Settings, engine: Engine, pixel_hash: str, marked: bool) -> bool:
    """Off the event loop: add/remove the "marked for editing" flag. With
    stacking on it applies to every member of the stack (like ratings). Never
    touches the photo file -- it's a workflow marker, not a keyword."""
    with Session(engine) as session:
        targets = [pixel_hash]
        if settings.config.stacking.enabled:
            targets = build_stacks(session, settings.config.stacking).members(pixel_hash)
        changed = False
        for target in targets:
            changed |= set_edit_flag(
                session,
                pixel_hash=target,
                marked=marked,
                device_id=settings.device_id,
                now=utcnow(),
            )
        session.commit()
    return changed


def _apply_bulk_flag(settings: Settings, engine: Engine, hashes: list[str], marked: bool) -> int:
    """Off the event loop: flag/unflag every selected photo (+ stack members)."""
    with Session(engine) as session:
        for target in _bulk_targets(session, settings, hashes):
            set_edit_flag(
                session,
                pixel_hash=target,
                marked=marked,
                device_id=settings.device_id,
                now=utcnow(),
            )
        session.commit()
    return len(hashes)


def _apply_clear_flags(settings: Settings, engine: Engine) -> int:
    """Off the event loop: drop the "marked for editing" flag from every photo."""
    with Session(engine) as session:
        n = clear_all_edit_flags(session, device_id=settings.device_id, now=utcnow())
        session.commit()
    return n


def _delete_targets(session: Session, settings: Settings, hashes: list[str]) -> list[str]:
    """Which pixel_hashes a delete of *hashes* should actually remove, per
    ``config.deletion.stack_scope``: only the clicked photo(s), or every member
    of each selected photo's stack."""
    if settings.config.deletion.stack_scope == "whole_stack":
        return _bulk_targets(session, settings, hashes)
    return list(dict.fromkeys(hashes))


def _apply_bulk_delete(settings: Settings, engine: Engine, hashes: list[str]) -> DeleteResult:
    """Off the event loop: move the selected photos' files to trash and purge
    their rows (CLAUDE.md constraint #1, exception 2)."""
    with Session(engine) as session:
        targets = _delete_targets(session, settings, hashes)
        result = delete_photos(
            session,
            pixel_hashes=targets,
            paths=settings.paths,
            clip_model=settings.config.clip.model,
            thumb_sizes=settings.config.thumbnails.sizes,
            device_id=settings.device_id,
            now=utcnow(),
        )
        session.commit()
    return result


def _delete_summary(result: DeleteResult) -> str:
    n = len(result.purged)
    where = "Trash" if result.trash and result.trash.backend == "os" else "the app trash folder"
    msg = f"deleted {n} photo(s); {result.files_trashed} file(s) moved to {where}"
    if result.files_failed:
        msg += f"; {result.files_failed} file(s) could not be moved"
    return msg


def _delete_where_note() -> str:
    return (
        "moved to your system Trash"
        if trash_backend() == "os"
        else "moved to APP_DATA/local/trash — recoverable, with a manifest"
    )


def _stacking_editor(settings: Settings) -> None:
    """`/settings` toggles for image stacking. Read-time, so saving just persists
    to ``config.yaml`` + updates the live config -- no re-index; the next library
    or search view reflects it."""
    cfg = settings.config.stacking
    ui.label("Image stacking").classes("font-bold mt-4")
    ui.label(
        "Group near-duplicate photos in the same folder into one entry (the "
        "newest by modified time is shown). Ratings apply to the whole stack."
    ).classes("text-xs text-gray-400 max-w-xl")

    enabled = ui.switch("Enabled", value=cfg.enabled)
    raw_jpeg = ui.checkbox("Pair a RAW with its same-named JPEG", value=cfg.raw_jpeg)
    similar = ui.checkbox(
        'Group similar names — "img.jpg", "img copy.jpg", "img (1).jpg", "copy img.jpg"',
        value=cfg.similar_names,
    )

    def _save() -> None:
        cfg.enabled = bool(enabled.value)
        cfg.raw_jpeg = bool(raw_jpeg.value)
        cfg.similar_names = bool(similar.value)
        settings.config.dump(settings.paths.config_file)
        ui.notify("saved — reload the library to see it")

    ui.button("Save", on_click=_save).props("color=primary")


def _theme_editor(settings: Settings, dark: ui.dark_mode) -> None:
    """Light / Dark / System theme. Per-machine (``local/ui_prefs.json``), never
    synced -- each machine keeps its own. Applies to the current tab immediately;
    other open tabs pick it up on their next load."""
    ui.label("Appearance").classes("font-bold mt-4")
    ui.label("Light, Dark, or follow the operating system. Saved on this machine only.").classes(
        "text-xs text-gray-400 max-w-xl"
    )

    choice = ui.toggle(
        {"system": "System", "light": "Light", "dark": "Dark"},
        value=State.theme,
    ).props("no-caps")

    def _on_change() -> None:
        value: Theme = choice.value if choice.value in ("system", "light", "dark") else "system"
        State.theme = value
        set_theme(settings.paths, value)
        dark.set_value(_THEME_TO_DARK[value])

    choice.on_value_change(_on_change)


def _deletion_editor(settings: Settings) -> None:
    """`/settings` control for the "Delete photos" action (CLAUDE.md constraint
    #1, exception 2). Only the stack scope is user-configurable; the trash
    destination is chosen automatically per platform."""
    cfg = settings.config.deletion
    ui.label("Deleting photos").classes("font-bold mt-4")
    ui.label(
        f"The bulk toolbar's Delete and the photo page's “Delete photo” move the "
        f"files ({_delete_where_note()}) and remove their ratings, tags, "
        f"thumbnails and embeddings. When stacking is on, choose whether a delete "
        f"takes only the shown photo or the whole stack."
    ).classes("text-xs text-gray-400 max-w-xl")

    scope = (
        ui.select(
            {
                "selected": "Delete only the selected / shown photo",
                "whole_stack": "Delete every photo in the stack",
            },
            value=cfg.stack_scope,
        )
        .props("dense options-dense")
        .classes("w-96")
    )

    def _save() -> None:
        cfg.stack_scope = "whole_stack" if scope.value == "whole_stack" else "selected"
        settings.config.dump(settings.paths.config_file)
        ui.notify("saved")

    ui.button("Save", on_click=_save).props("color=primary")


def _exclude_editor(settings: Settings, engine: Engine) -> None:
    """`/settings` editor for ``indexer.exclude_dir_names``. Save persists to
    ``config.yaml``, updates the live config, and kicks a rescan -- the sweep
    then drops photos from newly-excluded folders (and revives un-excluded ones,
    hard constraint #8: nothing is purged)."""
    names: list[str] = list(settings.config.indexer.exclude_dir_names)

    ui.label("Excluded folders").classes("font-bold mt-4")
    ui.label(
        "Folder names skipped while scanning — case-insensitive, matched at any "
        "depth. Photos already indexed from an excluded folder drop out on the "
        "next scan; removing an entry brings them back."
    ).classes("text-xs text-gray-400 max-w-xl")

    chips = ui.row().classes("items-center gap-2 flex-wrap min-h-8")

    def render() -> None:
        chips.clear()
        with chips:
            if not names:
                ui.label("none").classes("text-xs text-gray-500")
            for name in names:
                ui.chip(name, removable=True).on_value_change(lambda _, n=name: _drop(n))

    def _drop(name: str) -> None:
        if name in names:
            names.remove(name)
            render()

    def _add() -> None:
        value = (box.value or "").strip()
        if value and value.casefold() not in {n.casefold() for n in names}:
            names.append(value)
            render()
        box.set_value("")

    with ui.row().classes("items-center gap-2"):
        box = ui.input(placeholder="folder name, e.g. RAW").props("dense").classes("w-52")
        box.on("keydown.enter", _add)
        ui.button("Add", on_click=_add).props("flat dense")

    async def _save() -> None:
        settings.config.indexer.exclude_dir_names = list(names)
        settings.config.dump(settings.paths.config_file)
        ui.notify("saved — rescanning…")
        result = await nicegui_run.io_bound(_run_index_pass, settings, engine)
        ui.notify(result)

    ui.button("Save & rescan", on_click=_save).props("color=primary")
    render()


def _thumb_sizes(settings: Settings) -> tuple[int, int]:
    sizes = sorted(settings.config.thumbnails.sizes) or [320, 1280]
    return sizes[0], sizes[-1]


def _stars_caption(stars: int) -> str:
    return "★" * stars if stars else "unrated"


def _star_widget(
    settings: Settings, engine: Engine, pixel_hash: str, stars: int
) -> ui.rating | None:
    """Interactive 1-5 star rating on the photo page. Clicking the current value
    (or the clear button) sets 0 = unrated. Writes hop off the event loop; the
    widget is read-only while another machine holds the write lock.

    Returns the ``ui.rating`` element so the photo page's keyboard handler can
    drive it (``set_value`` fires the same write path); ``None`` in read-only."""
    writable = State.write_mode == WriteMode.writable
    with ui.row().classes("items-center gap-2"):
        rating = ui.rating(value=stars, max=5, size="lg").props("color=amber-6")
        caption = ui.label(_stars_caption(stars)).classes("text-xs text-gray-500")
        clear = ui.button(icon="clear", on_click=lambda: rating.set_value(0)).props(
            "flat dense round size=sm"
        )

    if not writable:
        rating.props("readonly")
        clear.set_visibility(False)
        caption.set_text(f"read-only — {State.lock_holder} is writing")
        return None

    async def _on_change() -> None:
        new = int(rating.value or 0)
        changed = await nicegui_run.io_bound(_apply_rating, settings, engine, pixel_hash, new)
        caption.set_text(_stars_caption(new))
        if changed:
            ui.notify(f"rated {new}★" if new else "rating cleared")

    rating.on_value_change(_on_change)
    return rating


def _edit_flag_button(
    settings: Settings, engine: Engine, pixel_hash: str, flagged: bool
) -> ui.switch | None:
    """Toggle the "marked for editing" queue flag on the photo page. Writes hop
    off the event loop; in read-only mode it shows only if already set. With
    stacking on the flag is stack-level, matching ratings.

    Returns the ``ui.switch`` so the keyboard handler can toggle it; ``None`` in
    read-only."""
    if State.write_mode != WriteMode.writable:
        if flagged:
            ui.label("⚑ Marked for editing").classes("text-amber-600 text-xs")
        return None

    async def _toggle(e: Any) -> None:
        await nicegui_run.io_bound(_apply_flag, settings, engine, pixel_hash, bool(e.value))
        ui.notify("added to edit list" if e.value else "removed from edit list")

    return ui.switch("Mark for editing", value=flagged, on_change=_toggle).props("color=amber")


def _photo_grid(min_px: int = 220) -> ui.grid:
    """A full-bleed, auto-reflowing thumbnail grid. ``w-full`` is required: the
    NiceGUI page-content column is ``align-items: start``, so without it the grid
    collapses to a single content-width column regardless of ``auto-fill``."""
    return (
        ui.grid()
        .classes("w-full gap-2 p-2")
        .style(f"grid-template-columns: repeat(auto-fill, minmax({min_px}px, 1fr))")
    )


def _copy_path(path: str) -> None:
    ui.clipboard.write(path)
    ui.notify("path copied")


def _path_row(path: str, *, present: bool, can_reveal: bool) -> None:
    """One row in the photo page's Paths list: the path text plus copy / reveal /
    open buttons. Reveal + open only appear when the server can drive a file
    manager (native run) and the file is present."""
    with ui.row().classes("items-center gap-1"):
        ui.label(path + ("" if present else "  (missing)")).classes(
            "text-xs break-all " + ("text-gray-600" if present else "text-red-500")
        )
        ui.button(icon="content_copy", on_click=lambda: _copy_path(path)).props(
            "flat dense round size=xs"
        ).tooltip("Copy path")
        if can_reveal and present:
            ui.button(
                icon="folder_open",
                on_click=lambda: reveal_in_file_manager(Path(path)),
            ).props("flat dense round size=xs").tooltip("Reveal in file manager")
            ui.button(
                icon="open_in_new",
                on_click=lambda: open_in_default_app(Path(path)),
            ).props("flat dense round size=xs").tooltip("Open in default app")


def _stack_filmstrip(current: str, members: list[str], size: int) -> None:
    """The other photos in this stack. ``members[0]`` is the representative
    (newest mtime); the photo being viewed is outlined.

    Filmstrip cells navigate with ``location.replace`` rather than a real link,
    so hopping between stack members never grows the history -- Back (in-app or
    browser) always returns to wherever you opened the stack from."""
    with ui.column().classes("w-full px-4 gap-1"):
        ui.label(f"Stack · {len(members)} photos — rating is shared").classes("font-bold text-sm")
        with ui.row().classes("gap-2 flex-wrap"):
            for i, member in enumerate(members):
                is_current = member == current
                cell = ui.element("div").classes(
                    "relative block w-28 overflow-hidden rounded ring-2 "
                    + ("ring-primary" if is_current else "ring-transparent cursor-pointer")
                )
                with cell:
                    ui.image(_thumb_src(member, size)).classes(
                        "w-full aspect-square object-cover bg-gray-100"
                    ).props("loading=lazy")
                    if i == 0:
                        ui.label("top").classes(
                            "absolute left-1 top-1 rounded bg-black/70 px-1 text-[10px] text-white"
                        )
                if not is_current:
                    cell.on(
                        "click",
                        lambda m=member: ui.run_javascript(f"location.replace('/photo/{m}')"),
                    )


class _GridSelection:
    """Multi-select state for the library grid: a hover checkbox per tile,
    shift-click for ranges. Transient -- a page load starts empty."""

    def __init__(self, rep_ids: list[str], on_change: Callable[[], None]) -> None:
        self._order = rep_ids
        self._pos = {h: i for i, h in enumerate(rep_ids)}
        self.selected: set[str] = set()
        self._anchor: int | None = None
        self.shift_down = False
        self._boxes: dict[str, tuple[ui.element, ui.icon]] = {}
        self._on_change = on_change

    def checkbox(self, pixel_hash: str) -> None:
        """Render the selection box for a tile (call inside the tile's link)."""
        selected = pixel_hash in self.selected
        box = ui.element("div").classes(self._box_cls(selected))
        with box:
            icon = ui.icon("check", size="1rem").classes(
                "text-white" + ("" if selected else " invisible")
            )
        self._boxes[pixel_hash] = (box, icon)
        box.on("click.stop.prevent", lambda h=pixel_hash: self.click(h))

    def click(self, pixel_hash: str) -> None:
        idx = self._pos.get(pixel_hash)
        if idx is None:
            return
        before = set(self.selected)
        if self.shift_down and self._anchor is not None:
            lo, hi = sorted((self._anchor, idx))
            self.selected.update(self._order[lo : hi + 1])
        else:
            self.selected.symmetric_difference_update({pixel_hash})
            self._anchor = idx
        self._repaint(self.selected ^ before)

    def clear(self) -> None:
        stale = set(self.selected)
        self.selected.clear()
        self._anchor = None
        self._repaint(stale)

    def _repaint(self, hashes: set[str]) -> None:
        for h in hashes:
            ref = self._boxes.get(h)
            if ref is None:
                continue
            box, icon = ref
            sel = h in self.selected
            box.classes(replace=self._box_cls(sel))
            icon.classes(replace="text-white" + ("" if sel else " invisible"))
        self._on_change()

    @staticmethod
    def _box_cls(selected: bool) -> str:
        base = (
            "kbreveal absolute left-1 top-1 z-10 flex h-6 w-6 cursor-pointer items-center "
            "justify-center rounded-md border-2 border-white transition "
        )
        return base + (
            "bg-primary opacity-100"
            if selected
            else "bg-black/30 opacity-0 group-hover:opacity-100"
        )


def _collection_add_button(
    settings: Settings,
    get_hashes: Callable[[], list[str]],
    *,
    label: str | None = "Add to collection",
    on_added: Callable[[], None] | None = None,
) -> None:
    """A button whose menu lists every collection (plus "New collection…").
    Picking one appends ``get_hashes()`` to that collection in order, skipping
    hashes already in it. ``label=None`` renders an icon-only round button (for
    the bulk toolbar). Collections live in ``sync/collections.json`` and are not
    lock-guarded -- like saved searches, they can be edited in read-only mode."""

    def _pick(coll: Collection) -> None:
        hashes = get_hashes()
        if not hashes:
            ui.notify("Nothing selected.", type="warning")
            return
        added = add_to_collection(settings.paths, coll.id, hashes)
        skipped = len(hashes) - added
        msg = f'added {added} to "{coll.name}"'
        if skipped:
            msg += f" · {skipped} already in it"
        ui.notify(msg)
        if on_added is not None:
            on_added()

    def _new() -> None:
        with ui.dialog() as dialog, ui.card().classes("gap-2"):
            ui.label("New collection").classes("font-medium")
            name_in = ui.input(placeholder="name").props("dense autofocus").classes("w-64")

            def _go() -> None:
                name = (name_in.value or "").strip()
                if not name:
                    return
                coll = create_collection(settings.paths, name)
                dialog.close()
                _pick(coll)

            name_in.on("keydown.enter", _go)
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Create", on_click=_go).props("color=primary")
        dialog.open()

    if label is None:
        btn = ui.button(icon="library_add").props("flat dense round").tooltip("Add to collection")
    else:
        btn = ui.button(label, icon="library_add").props("flat dense no-caps")
    with btn:
        menu = ui.menu()

    def _rebuild() -> None:
        menu.clear()
        with menu:
            colls = load_collections(settings.paths)
            for coll in colls:
                ui.menu_item(f"{coll.name}  ·  {coll.count}", on_click=lambda c=coll: _pick(c))
            if colls:
                ui.separator()
            ui.menu_item("New collection…", on_click=_new)

    btn.on("click", _rebuild)
    _rebuild()


def _thumb_cell(
    photo: Photo,
    size: int,
    *,
    stack_size: int = 1,
    selection: _GridSelection | None = None,
    stars: int = 0,
    on_rate: Callable[[str, int], Any] | None = None,
    flagged: bool = False,
    on_flag: Callable[[str, bool], Any] | None = None,
    flag_registry: dict[str, Callable[[bool], Any]] | None = None,
    cell_registry: dict[str, ui.element] | None = None,
    rating_registry: dict[str, Callable[[int], Any]] | None = None,
    flag_toggle_registry: dict[str, Callable[[], Any]] | None = None,
    on_hover: Callable[[str, bool], Any] | None = None,
) -> None:
    with ui.link(target=f"/photo/{photo.pixel_hash}").classes("group relative block") as cell:
        if cell_registry is not None:
            cell_registry[photo.pixel_hash] = cell
        if on_hover is not None:
            cell.on("mouseenter", lambda: on_hover(photo.pixel_hash, True))
            cell.on("mouseleave", lambda: on_hover(photo.pixel_hash, False))
        ui.image(_thumb_src(photo.pixel_hash, size)).classes(
            "w-full aspect-square object-cover bg-gray-100"
        ).props("loading=lazy")
        if stack_size > 1:
            ui.label(f"⧉ {stack_size}").classes(
                "absolute left-1 bottom-1 rounded bg-black/70 px-1.5 py-0.5 "
                "text-xs font-medium text-white"
            )
        if on_flag is not None:
            _grid_flag(photo.pixel_hash, flagged, on_flag, flag_registry, flag_toggle_registry)
        elif flagged:
            ui.icon("flag", size="1.1rem").classes(
                "absolute right-1 top-1 z-10 rounded bg-black/60 p-0.5 text-amber-400"
            ).tooltip("marked for editing")
        if selection is not None:
            selection.checkbox(photo.pixel_hash)
        if on_rate is not None:
            _grid_rating(photo.pixel_hash, stars, on_rate, rating_registry)


def _grid_rating(
    pixel_hash: str,
    stars: int,
    on_rate: Callable[[str, int], Any],
    registry: dict[str, Callable[[int], Any]] | None = None,
) -> None:
    """Inline, clickable star rating on a grid tile -- top row next to the
    checkbox, shown only while the tile is hovered. Clicks are swallowed so they
    don't open the photo.

    When *registry* is given (writable sessions only), the tile registers a
    ``set(stars)`` callback so the keyboard handler can rate it -- ``set_value``
    fires the same write path as a click."""
    box = ui.element("div").classes(
        "kbreveal absolute left-9 top-1 z-10 flex items-center rounded bg-black/55 px-1 "
        "opacity-0 transition group-hover:opacity-100"
    )
    box.on("click.stop.prevent", lambda: None)
    with box:
        widget = (
            ui.rating(value=stars, max=5, size="1.25rem")
            .props("color=amber-4")
            .style("filter: saturate(0.55) brightness(0.9)")
        )
    if State.write_mode != WriteMode.writable:
        widget.props("readonly")
        return

    async def _changed() -> None:
        result = on_rate(pixel_hash, int(widget.value or 0))
        if result is not None:
            await result

    widget.on_value_change(_changed)
    if registry is not None:
        registry[pixel_hash] = lambda n: widget.set_value(n)


def _grid_flag(
    pixel_hash: str,
    flagged: bool,
    on_flag: Callable[[str, bool], Any],
    registry: dict[str, Callable[[bool], Any]] | None = None,
    toggle_registry: dict[str, Callable[[], Any]] | None = None,
) -> None:
    """Clickable "marked for editing" flag in a grid tile's top-right corner.
    Solid amber and always visible when set; dim and revealed on hover when not.
    The click is swallowed so it doesn't open the photo. In read-only mode it is
    a static indicator (shown only when set).

    When *registry* is given, the tile registers a ``set(on)`` callback under its
    ``pixel_hash`` so bulk actions and "clear all" can repaint it live.
    *toggle_registry* (writable only) gets a no-arg ``toggle()`` so the keyboard
    handler can flip it through the same write + paint path as a click."""
    writable = State.write_mode == WriteMode.writable
    base = "kbreveal absolute right-1 top-1 z-10 rounded bg-black/55 p-0.5 transition" + (
        " cursor-pointer" if writable else ""
    )
    state = {"on": flagged}
    box = ui.element("div").classes(base)
    with box:
        icon = ui.icon("flag", size="1.1rem")
    box.tooltip("mark for editing")

    def _paint() -> None:
        on = state["on"]
        box.classes(replace=base + (" opacity-100" if on else " opacity-0 group-hover:opacity-100"))
        icon.classes(replace="text-amber-400" if on else "text-white/90")
        if not writable:
            box.set_visibility(on)

    def _set(on: bool) -> None:
        state["on"] = on
        _paint()

    _paint()
    if registry is not None:
        registry[pixel_hash] = _set

    if not writable:
        return

    async def _toggle() -> None:
        _set(not state["on"])
        result = on_flag(pixel_hash, state["on"])
        if result is not None:
            await result

    box.on("click.stop.prevent", _toggle)
    if toggle_registry is not None:
        toggle_registry[pixel_hash] = _toggle


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
