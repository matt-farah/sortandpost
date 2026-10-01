"""NiceGUI web app.

``/`` is the library *and* search: a filter bar + text query drive the grid via
URL params (:mod:`sortandpost.library_view`); ``/search`` just redirects there.
Plus ``/photo``, ``/rate``, ``/collections``, ``/review`` and ``/settings``.
See CLAUDE.md -> "Frontend (NiceGUI pages)".

Only cheap keyed SELECTs run on the event loop here; image decode / exiftool /
CLIP stay off it -- the indexer process, or ``run.io_bound`` for query embedding
(hard constraint #7).
"""

from __future__ import annotations

import asyncio
import bisect
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, replace
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from nicegui import app as nicegui_app
from nicegui import run as nicegui_run
from nicegui import ui
from nicegui.events import KeyEventArguments, ValueChangeEventArguments
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, func, select

from sortandpost import __version__
from sortandpost.clip.model import (
    ClipModel,
    ClipModelUnavailable,
    clip_available,
)
from sortandpost.clip.model import (
    check_cached as clip_check_cached,
)
from sortandpost.clip.model import (
    ensure_downloaded as clip_ensure_downloaded,
)
from sortandpost.clip.store import ModelMismatch
from sortandpost.clip.tagging import (
    ReviewGroup,
    approve,
    facet_sections,
    is_pending,
    pending_for_tag,
    pending_groups,
    reject,
)
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
from sortandpost.config import Env, Settings
from sortandpost.cpu import default_workers
from sortandpost.db import add_manual_tag, init_db, make_engine, set_rating
from sortandpost.db.browse import (
    get_paths,
    get_photo,
    get_rating,
    list_photo_ids,
    online_only_count,
    photo_count,
    unrated_count,
    unrated_photo_ids,
)
from sortandpost.db.deletion import DeleteResult, delete_photos
from sortandpost.db.flags import (
    clear_all_edit_flags,
    edit_flag_count,
    edit_flagged_hashes,
    set_edit_flag,
)
from sortandpost.db.meta import get_generation
from sortandpost.db.models import EmbeddingRow, Photo, Prediction, PredictionState, Rating
from sortandpost.desktop import control as desktop_control
from sortandpost.grouping import PhotoGroup, parse_folder_sort
from sortandpost.helper import HelperInfo, read_helper_info
from sortandpost.indexer import scan_clip_once, scan_once
from sortandpost.indexer.hydrate import hydrate_online_only
from sortandpost.indexer.progress import PROGRESS, ProgressSnapshot
from sortandpost.indexer.service import run_indexer
from sortandpost.launcher_pref import remember_sync_dir
from sortandpost.library_roots import (
    LibraryRoot,
    clean_path,
    compose_volume_line,
    running_in_docker,
    save_roots,
    to_host_path,
)
from sortandpost.library_view import (
    DEFAULT_GROUP,
    FLAT,
    UNRATED,
    LibraryQuery,
    LibraryView,
    camera_choices,
    dates_label,
    dates_to_picker,
    folder_choices,
    neighbors,
    picker_to_dates,
    resolve_view,
    tag_choices,
)
from sortandpost.metadata import ExiftoolClient
from sortandpost.metadata.shared import EXIFTOOL
from sortandpost.onboarding import (
    FolderInspection,
    cloud_folder_candidates,
    inspect_folder,
    mark_complete,
    suggested_roots,
)
from sortandpost.paths import default_local_root
from sortandpost.post_export import resolve_sources
from sortandpost.posts import posted_hashes
from sortandpost.posts_ui import (
    compose_button,
    posted_note,
    posting_settings_panel,
    register_post_pages,
)
from sortandpost.prefs import (
    Theme,
    get_open_with_app,
    get_season,
    get_theme,
    set_open_with_app,
    set_season,
    set_theme,
)
from sortandpost.reveal import (
    open_in_default_app,
    open_multiple_in_default_app,
    open_multiple_with_app,
    open_with_app,
    reveal_in_file_manager,
    reveal_multiple_in_file_manager,
    reveal_supported,
)
from sortandpost.robopost import SERVICE_NAME as POSTING_SERVICE
from sortandpost.searches import (
    SavedSearch,
    add_search,
    legacy_query,
    load_searches,
    remove_search,
)
from sortandpost.stacks import build_stacks
from sortandpost.sync import SyncManager, WriteMode
from sortandpost.sync.clock import utcnow
from sortandpost.themes import (
    PALETTES,
    SEASON_CHOICES,
    SEASON_ICONS,
    SEASON_LABELS,
    SEASONS,
    SeasonChoice,
    body_class,
    first_paint_css,
    resolve_season,
    theme_css,
)
from sortandpost.trash import (
    TrashBatch,
    find_batch,
    flush_to_system_trash,
    list_batches,
    system_trash_available,
)
from sortandpost.undelete import UndeleteResult, put_back_batch

log = logging.getLogger(__name__)

_PAGES = [
    ("/", "Library", "photo_library"),
    ("/collections", "Collections", "collections_bookmark"),
    ("/posts", "Posts", "send"),
    ("/rate", "Rate", "star_rate"),
    ("/review", "Review", "rate_review"),
    ("/settings", "Settings", "settings"),
]
_REVIEW_PAGE = 48  # per-photo tiles loaded per /review "Show more" step
_GRID_BATCH = 80  # thumbnails appended per infinite-scroll step
_PICKER_CAP = 500  # photos offered at once in the "add photos to collection" picker
# Reveal is one OS call regardless of count (Finder/Explorer/dbus all batch), so it
# only needs a confirm well past normal selection sizes. "Open in default app" can
# spawn one process/window per file on Windows/Linux (no batching there), so it
# stays more conservative.
_BULK_REVEAL_CONFIRM_THRESHOLD = 200
_BULK_OPEN_CONFIRM_THRESHOLD = 25
_THUMB_URL = "/thumbs"
_TRASH_THUMB_URL = "/trash-thumb"
_HEX64 = re.compile(r"[0-9a-f]{64}")
# Prebuilt Tailwind (tools/tailwind/build.sh), served in place of NiceGUI's
# in-browser compiler -- that one rescans every element's classes on each DOM
# mutation, so the infinite-scroll grid grew it quadratically until the tab
# froze, dropped its websocket and reloaded. Hence `ui.run(tailwind=False)`.
_TAILWIND_CSS = "tailwind.css"
_TAILWIND_URL = "/_sp/tailwind.css"

# The grid's scroll position + loaded count live in this history entry's
# ``history.state``. The browser keeps per-entry state across navigations and
# restores it on back/forward, so returning here -- however many photo/filmstrip
# hops away -- reads back the exact position, while a fresh visit (nav link,
# reload elsewhere, new tab) lands on a null state and starts at the top.
_GRID_RESTORE_JS = """
(() => {
  history.scrollRestoration = 'manual';
  const s = (history.state && history.state.spGrid) || null;
  return s ? [Number(s.n) || 0, Number(s.y) || 0, s.c || []] : [0, 0, []];
})()
"""

# Written every tick while browsing the grid; returns "near the bottom?".
# `c` carries the collapsed group-by section keys (group-by mode only) so
# back/forward returns to the same folded view.
_GRID_SAVE_JS = (
    "history.replaceState(Object.assign({}, history.state,"
    " {spGrid: {n: %d, y: Math.round(scrollY), c: (window.__spCollapsed || [])}}), '');"
    "window.innerHeight + window.scrollY >= document.body.offsetHeight - 1400"
)

# How many tiles are in the grid's first row -- the step for up/down keyboard
# focus. Read from the resolved `grid-template-columns` track list so it tracks
# the responsive `auto-fill` reflow and ignores full-width group headers (which
# would break a first-row DOM walk).
_GRID_COLS_JS = """
(() => {
  const gs = [...document.querySelectorAll('.nicegui-grid')];
  if (!gs.length) return 1;
  const g = gs.reduce((a, b) => (b.children.length > a.children.length ? b : a), gs[0]);
  const tracks = getComputedStyle(g).gridTemplateColumns.split(' ').filter(Boolean).length;
  return tracks || 1;
})()
"""

# ui.keyboard silently drops every key while an <input>/<select>/<textarea>/
# <button> holds focus, so focus stranded there made the page shortcuts go dead
# with nothing on screen to say why -- e.g. a filter dropdown or the search box
# dismissed with Esc keeps focus in its (hidden) input. Capture phase, so it runs
# before ui.keyboard's own focus check: a shortcut key on a focused button blurs
# the button and goes through, and Esc in a field lets go of it on keyup (so the
# Esc itself is still the field's, and doesn't also clear the grid). Space /
# Enter / Tab are left alone on a button -- they're its own activate / move.
# Shared, one-time per browser tab.
_KEY_FOCUS_GUARD_JS = (
    "if(!window.__spKeyFocusGuard){window.__spKeyFocusGuard=1;"
    "const f=()=>document.activeElement,m=e=>e.ctrlKey||e.metaKey||e.altKey;"
    "addEventListener('keyup',e=>{const a=f();"
    "if(a&&!m(e)&&e.key==='Escape'&&['INPUT','TEXTAREA','SELECT'].includes(a.tagName))"
    "a.blur();},true);"
    "addEventListener('keydown',e=>{const a=f();"
    "if(a&&!m(e)&&a.tagName==='BUTTON'&&![' ','Enter','Tab','Escape'].includes(e.key)"
    "&&(e.key.length===1||e.key.startsWith('Arrow')))a.blur();"
    "},true);}"
)

# Keyboard-focus on a grid tile: an amber border (via ::after, so it sits above
# the opaque thumbnail) plus -- like a real mouse hover -- the tile's rating and
# flag/checkbox overlays revealed. Toggled by adding `.kb-focus` to the tile <a>;
# every hover-revealed child carries `.kbreveal`.
_GRID_KB_CSS = """
a.group.kb-focus::after,
.sp-pick.kb-focus::after {
  content: ""; position: absolute; inset: 0; box-sizing: border-box;
  border: 4px solid var(--sp-focus, #fbbf24); pointer-events: none; z-index: 20;
}
a.group.kb-focus .kbreveal,
.sp-pick.kb-focus .kbreveal { opacity: 1 !important; }

/* keep keyboard scrollIntoView from parking a tile under the fixed app header
   or a sticky group-by section header */
.nicegui-grid > a.nicegui-link.group { scroll-margin-top: 4.5rem; }
"""

# Global design layer -- "warm editorial": an old-style serif for display type,
# colors from design tokens. The seasonal themes (:mod:`sortandpost.themes`,
# added after this block) set every token per season x mode on the <body>
# classes; the :root values here are only the fallback. Amber stays for
# warnings and the edit flag. System fonts only -- the app is offline-first.
_THEME_CSS = """
:root {
  --sp-bg: #faf8f4;
  --sp-surface: #ffffff;
  --sp-surface-2: #f2ede3;
  --sp-border: #ddd0ba;
  --sp-text: #2c2823;
  --sp-text-dim: #6b6357;
  --sp-text-faint: #9c9284;
  --sp-shadow: 0 1px 2px rgba(44,40,35,.06), 0 10px 28px -14px rgba(44,40,35,.22);
  --sp-shadow-lg: 0 2px 6px rgba(44,40,35,.09), 0 22px 48px -20px rgba(44,40,35,.34);
  --sp-primary: #4f46e5;
  --sp-on-primary: #ffffff;
  --sp-focus: #fbbf24;
  --sp-statusbar-bg: #201b15;
  --sp-statusbar-fg: #c9beac;
  --sp-statusbar-border: #3c332a;
}
body.body--dark {
  --sp-bg: #1b1815;
  --sp-surface: #262019;
  --sp-surface-2: #2f2820;
  --sp-border: #3c332a;
  --sp-text: #f0e9dd;
  --sp-text-dim: #b8ad9c;
  --sp-text-faint: #877c6b;
  --sp-shadow: 0 1px 2px rgba(0,0,0,.32), 0 12px 30px -16px rgba(0,0,0,.62);
  --sp-shadow-lg: 0 2px 6px rgba(0,0,0,.4), 0 26px 56px -22px rgba(0,0,0,.72);
}

body, .q-page, .nicegui-content {
  background-color: var(--sp-bg);
  color: var(--sp-text);
}
::selection { background: color-mix(in srgb, var(--q-primary) 26%, transparent); }
/* text on a primary fill: Quasar hard-codes white, which a pastel primary
   (Spring / Winter dark) can't carry. In the `theme` layer because Quasar's
   `.text-white !important` sits in NiceGUI's `quasar_importants` layer, and for
   !important the earliest-declared layer wins. */
@layer theme {
  .bg-primary.text-white, .q-btn.bg-primary, .q-badge.bg-primary, .q-chip.bg-primary {
    color: var(--sp-on-primary) !important;
  }
}

.sp-display {
  font-family: "Iowan Old Style", "Palatino Linotype", Palatino, "Book Antiqua",
               "Hoefler Text", Cambria, Georgia, serif;
  letter-spacing: .005em;
  font-feature-settings: "liga" 1, "kern" 1;
}
.sp-muted { color: var(--sp-text-dim); }
.sp-placeholder { background-color: var(--sp-surface-2); }  /* image wells, pills */
.sp-faint { color: var(--sp-text-faint); }

/* app bar --------------------------------------------------------------- */
.sp-appbar.q-header {
  background-color: color-mix(in srgb, var(--sp-bg) 82%, transparent) !important;
  color: var(--sp-text) !important;
  border-bottom: 1px solid var(--sp-border);
  -webkit-backdrop-filter: saturate(1.6) blur(10px);
  backdrop-filter: saturate(1.6) blur(10px);
}
.sp-appbar.q-header::after {  /* the season's palette as a hairline stripe */
  content: ""; position: absolute; left: 0; right: 0; bottom: -1px; height: 3px;
  background: var(--sp-season-stripe, transparent); pointer-events: none;
}
.sp-season-glyph { color: var(--sp-primary); }
.sp-nav {
  color: var(--sp-text-dim);
  text-decoration: none !important;
  font-size: .875rem;
  padding: .2rem .55rem;
  border-radius: .4rem;
  transition: color .12s, background-color .12s;
}
.sp-nav:hover { color: var(--sp-text); background-color: var(--sp-surface-2); }
.sp-nav-active { color: var(--q-primary); font-weight: 600; }
.sp-nav .q-icon { font-size: 1.05rem; }
.sp-chip {
  font-size: .68rem;
  font-weight: 700;
  letter-spacing: .04em;
  text-transform: uppercase;
  padding: .16rem .5rem;
  border-radius: 999px;
  border: 1px solid currentColor;
  white-space: nowrap;
}

/* status bar ---------------------------------------------------------------- */
.sp-statusbar.q-footer {
  background-color: var(--sp-statusbar-bg) !important;
  color: var(--sp-statusbar-fg) !important;
  border-top: 1px solid var(--sp-statusbar-border);
  font-size: .72rem;
}

/* surfaces ------------------------------------------------------------------ */
.sp-card {
  background-color: var(--sp-surface);
  border: 1px solid var(--sp-border);
  border-radius: .6rem;
  box-shadow: var(--sp-shadow);
  transition: box-shadow .15s ease, transform .15s ease;
}
.sp-card:hover { box-shadow: var(--sp-shadow-lg); transform: translateY(-1px); }
.sp-wizard.q-stepper {
  background: var(--sp-surface);
  color: var(--sp-text);
  border: 1px solid var(--sp-border);
  border-radius: .6rem;
  box-shadow: var(--sp-shadow);
}
.sp-section-header {
  /* flush under the app bar, whose height depends on font rendering and
     wrapping: `_APPBAR_HEIGHT_JS` publishes the measured height */
  top: var(--sp-appbar-h, 60px);
  background-color: color-mix(in srgb, var(--sp-bg) 86%, transparent);
  border-bottom: 1px solid var(--sp-border);
  -webkit-backdrop-filter: blur(7px);
  backdrop-filter: blur(7px);
}
.sp-section-header:hover { background-color: var(--sp-surface-2); }

/* segmented controls (ui.toggle) ----------------------------------------- */
.q-btn-group.q-btn-toggle {
  border: 1px solid var(--sp-border);
  background-color: var(--sp-surface);
  box-shadow: none;
  border-radius: .5rem;
  overflow: hidden;
}
.q-btn-toggle .q-btn { border-radius: 0; }
.q-btn-toggle .q-btn:not(.bg-primary) {
  background-color: var(--sp-surface) !important;
  color: var(--sp-text-dim) !important;
}
.q-btn-toggle .q-btn.bg-primary { background-color: var(--q-primary) !important; }

/* library toolbar: icon groups ------------------------------------------- */
.sp-toolgroup {
  border: 1px solid var(--sp-border);
  background-color: var(--sp-surface);
  border-radius: .6rem;
  padding: .1rem .2rem;
  min-height: 40px;
}
.sp-toolbtn { color: var(--sp-text-dim); }
.sp-toolbtn.text-primary {
  background-color: color-mix(in srgb, var(--q-primary) 12%, transparent);
}
.sp-toolbadge { font-size: .6rem; min-height: 14px; padding: 1px 4px; top: -2px; right: -4px; }
.sp-filterchip { margin: 0; }

/* settings: side rail + category grids ----------------------------------- */
.sp-settings-rail { position: sticky; top: 5rem; min-width: 12rem; }
.sp-settings-section { scroll-margin-top: 5rem; }
.sp-settings-grid {
  display: grid; gap: 1rem; width: 100%;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 26rem), 1fr));
}
.sp-settings-grid > .sp-wide { grid-column: 1 / -1; }
.sp-settings-card { padding: 1rem 1.1rem; min-width: 0; }
.sp-settings-card:hover { transform: none; box-shadow: var(--sp-shadow); }
.sp-tiles {
  grid-column: 1 / -1; display: grid; gap: .75rem;
  grid-template-columns: repeat(auto-fit, minmax(9.5rem, 1fr));
}
.sp-tile { padding: .75rem .9rem; }
.sp-tile:hover { transform: none; }
@media (max-width: 900px) {
  .sp-settings-rail { display: none; }
}

/* empty states --------------------------------------------------------------- */
.sp-empty {
  display: flex; flex-direction: column; align-items: center; gap: .5rem;
  text-align: center; margin: 4rem auto; max-width: 26rem; color: var(--sp-text-dim);
}
.sp-empty .sp-empty-title {
  font-size: 1.15rem; color: var(--sp-text);
}

/* inputs / menus / cards follow the season's surfaces */
.q-menu, .q-card, .q-dialog .q-card { background-color: var(--sp-surface); color: var(--sp-text); }
.q-field--outlined .q-field__control:before { border-color: var(--sp-border); }

/* settings: season swatches ------------------------------------------------ */
.sp-swatch {
  display: flex; flex-direction: column; gap: .35rem; cursor: pointer;
  padding: .5rem; border-radius: .6rem; border: 2px solid var(--sp-border);
  background-color: var(--sp-surface); min-width: 8.5rem;
  transition: border-color .12s, transform .12s;
}
.sp-swatch:hover { transform: translateY(-1px); }
.sp-swatch.sp-swatch-on { border-color: var(--q-primary); }
.sp-swatch-strip { display: flex; height: 1.1rem; border-radius: .3rem; overflow: hidden; }
.sp-swatch-strip > span { flex: 1; }
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
    indexer_stop: threading.Event | None = None
    indexer_thread: threading.Thread | None = None
    # Set by the desktop tray's "Pause indexing" (sortandpost.desktop): no new
    # scan starts and a running one stops after the file in hand. Per process.
    indexer_paused: threading.Event = threading.Event()
    shutting_down: bool = False
    manager: SyncManager | None = None
    # This machine hasn't finished the welcome wizard (sortandpost.onboarding):
    # every page redirects to /welcome and the indexer waits.
    onboarding_pending: bool = False
    theme: Theme = "system"  # per-machine UI theme (local/ui_prefs.json)
    season: SeasonChoice = "auto"  # per-machine seasonal palette (sortandpost.themes)
    # (trash batch id, photos deleted, time.monotonic()) of the most recent
    # delete on this machine -- drives the status bar's "Undo delete" button.
    last_delete: tuple[str, int, float] | None = None

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


def _apply_season(choice: SeasonChoice) -> str:
    """Put the season's class on <body> (dropping any other season's); returns
    the Material icon for the resolved season. The token CSS is keyed on it."""
    season = resolve_season(choice)
    ui.query("body").classes(
        add=body_class(season), remove=" ".join(body_class(s) for s in SEASONS)
    )
    return SEASON_ICONS[season]


# Publishes the app bar's rendered height as `--sp-appbar-h` so sticky elements
# (the group-by section headers) sit flush under it instead of at a guessed
# offset -- the bar is ~60px, not Tailwind's `top-16` 64px, and grows when its
# nav wraps on a narrow window. The header mounts with Vue, so wait for it.
_APPBAR_HEIGHT_JS = """
<script>
(function () {
  function watch() {
    const bar = document.querySelector('.q-header');
    if (!bar) { requestAnimationFrame(watch); return; }
    const publish = () => document.documentElement.style.setProperty(
      '--sp-appbar-h', bar.getBoundingClientRect().height + 'px');
    publish();
    new ResizeObserver(publish).observe(bar);
  }
  watch();
})();
</script>
"""


def _header(active: str, *, minimal: bool = False) -> ui.dark_mode:
    # primary / secondary / accent come from the season (sortandpost.themes)
    ui.colors(positive="#3f9d6d", negative="#c0392b")
    dark = ui.dark_mode(_THEME_TO_DARK[State.theme])  # per-machine; None = follow the OS
    glyph_icon = _apply_season(State.season)
    # right background on the first paint, before the body classes arrive via JS
    paint = first_paint_css(resolve_season(State.season), _THEME_TO_DARK[State.theme])
    ui.add_head_html(f"<style>{paint}</style>")
    ui.add_body_html(_APPBAR_HEIGHT_JS)
    with ui.header().classes("sp-appbar items-center justify-between gap-4"):
        with ui.row().classes("items-center gap-2 no-wrap"):
            ui.icon(glyph_icon).classes("sp-season-glyph text-lg").tooltip(
                f"{SEASON_LABELS[resolve_season(State.season)]} theme"
                + (" (auto)" if State.season == "auto" else "")
            )
            ui.link("sortandpost", "/").classes("sp-display text-xl font-semibold sp-nav")
            ui.label(__version__).classes("sp-faint text-[11px]")
        if minimal:  # the welcome wizard: nothing else to navigate to yet
            return dark
        with ui.row().classes("items-center gap-1 no-wrap"):
            for path, name, icon in _PAGES:
                with ui.link("", path).classes(
                    "sp-nav flex items-center gap-1" + (" sp-nav-active" if path == active else "")
                ):
                    ui.icon(icon)
                    ui.label(name)
        with ui.row().classes("items-center gap-2 no-wrap"):
            _inject_helper()
            if State.write_mode == WriteMode.writable:
                ui.label("writable").classes("sp-chip text-primary")
            else:
                ui.label(f"read-only · {State.lock_holder}").classes(
                    "sp-chip text-amber-600 dark:text-amber-400"
                )
    _status_bar()
    return dark


def _current_helper() -> HelperInfo | None:
    return read_helper_info(State.settings.paths) if State.settings is not None else None


def _inject_helper() -> None:
    """Hand the browser the running helper agent's ``{port, token}`` (if any) so
    the Reveal / Open / Browse buttons can ``fetch`` it directly. The agent is on
    the host; the server never talks to it."""
    hi = _current_helper()
    if hi is None:
        ui.run_javascript("window.__spHelper = null;")
        return
    ui.run_javascript(f"window.__spHelper = {json.dumps({'port': hi.port, 'token': hi.token})};")
    ui.label("🖥").classes("text-sm").tooltip("file-manager helper connected")


def _status_bar() -> None:
    """Persistent footer: index progress, CLIP queue, write-lock state. Polls
    every 5s; a transient DB lock just shows 'status unavailable' for one tick."""
    if State.engine is None:
        return
    client = ui.context.client  # valid slot context after the undo's await
    with ui.footer().classes("sp-statusbar px-4 py-1 items-center justify-between"):
        with ui.row().classes("items-center gap-3"):
            summary = ui.label("…")
            live = ui.label().classes("sp-chip text-sky-600 dark:text-sky-400")
            live.set_visibility(False)
            bar = ui.linear_progress(value=0, show_value=False, size="6px").classes("w-32")
            bar.set_visibility(False)
            undo = ui.button("Undo delete", icon="undo").props("dense no-caps color=amber-8")
            undo.set_visibility(False)
            undo.on_click(lambda: _undo_last_delete(client))

        def _undo_tick() -> None:
            last = State.last_delete
            show = (
                last is not None
                and State.write_mode == WriteMode.writable
                and time.monotonic() - last[2] < _UNDO_WINDOW_SEC
            )
            undo.set_visibility(show)
            if show and last is not None:
                undo.set_text(f"Undo delete ({last[1]})")

        def _live_tick() -> None:
            snap = PROGRESS.snapshot()
            live.set_visibility(snap.active)
            bar.set_visibility(snap.fraction is not None)
            if snap.active:
                live.set_text(_progress_text(snap))
                bar.set_value(snap.fraction or 0.0)

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
                paused = " · indexing paused" if State.indexer_paused.is_set() else ""
                summary.set_text(
                    f"{present}/{total} photos indexed · {total - present} missing · "
                    f"{pending} CLIP suggestions pending · {lock}{paused}"
                )
            except Exception:  # never let the status bar break a page
                summary.set_text("status unavailable")

        _tick()
        _live_tick()
        _undo_tick()
        ui.timer(5.0, _tick)
        ui.timer(1.0, _live_tick)
        ui.timer(1.0, _undo_tick)


_UNDO_WINDOW_SEC = 5 * 60


async def _undo_last_delete(client: Any) -> None:
    """Status-bar "Undo delete": put the most recent delete's batch back."""
    last = State.last_delete
    settings, engine = State.settings, State.engine
    if last is None or settings is None or engine is None:
        return
    State.last_delete = None  # one click; a failed put-back stays on /settings
    result = await nicegui_run.io_bound(put_back_batch, settings, engine, last[0])
    if result is None:
        return
    with client:
        ui.notify(_undelete_summary(result), type="warning" if result.failed else None)
        if result.photos_restored:
            ui.navigate.reload()


def _undelete_summary(result: UndeleteResult) -> str:
    msg = result.summary()
    if result.failed:
        msg += " — see Settings → Recently deleted"
    return msg


def _progress_text(snap: ProgressSnapshot) -> str:
    """``Indexing 120/4,300 · ~3 min left`` -- the status bar / settings label."""
    text = snap.label()
    eta = snap.eta_seconds()
    if eta is not None:
        text += f" · {_fmt_eta(eta)} left"
    return text


def _fmt_eta(seconds: float) -> str:
    if seconds < 90:
        return f"~{max(5, round(seconds / 5) * 5)} sec"
    if seconds < 90 * 60:
        return f"~{round(seconds / 60)} min"
    return f"~{seconds / 3600:.1f} h"


def _progress_panel() -> None:
    """`/settings` live view of the running scan / CLIP pass (polls 1s)."""
    with ui.column().classes("gap-1 w-full max-w-xl"):
        title = ui.label().classes("text-sm")
        bar = ui.linear_progress(value=0, show_value=False).classes("w-full")
        current = ui.label().classes("text-xs sp-faint truncate w-full")

        def _tick() -> None:
            snap = PROGRESS.snapshot()
            if not snap.active:
                title.set_text("Indexer idle.")
                bar.set_visibility(False)
                current.set_text("")
                return
            title.set_text(_progress_text(snap))
            bar.set_visibility(True)
            if snap.fraction is None:
                bar.props("indeterminate")
            else:
                bar.props(remove="indeterminate")
                bar.set_value(snap.fraction)
            current.set_text(snap.current)

        _tick()
        ui.timer(1.0, _tick)


def _thumb_src(pixel_hash: str, size: int) -> str:
    return f"{_THUMB_URL}/{pixel_hash[:2]}/{pixel_hash}_{size}.webp"


def _photo_url(pixel_hash: str, link_query: str = "") -> str:
    """A photo page link carrying the library view it was opened from (the
    grid's query string), so its prev/next follow that filtered order."""
    return f"/photo/{pixel_hash}?{link_query}" if link_query else f"/photo/{pixel_hash}"


def _add_tailwind_css() -> None:
    """Serve the prebuilt Tailwind stylesheet and link it from every page, with a
    content hash in the URL so a rebuilt file isn't masked by the browser cache."""
    css = resources.files("sortandpost.resources").joinpath(_TAILWIND_CSS)
    with resources.as_file(css) as path:
        nicegui_app.add_static_file(local_file=path, url_path=_TAILWIND_URL)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    ui.add_head_html(f'<link rel="stylesheet" href="{_TAILWIND_URL}?v={digest}">', shared=True)


def build_ui(settings: Settings) -> None:
    _add_tailwind_css()  # stands in for NiceGUI's in-browser Tailwind (see ui.run)
    ui.add_css(_GRID_KB_CSS, shared=True)  # grid keyboard-focus styling, all pages
    ui.add_css(_THEME_CSS, shared=True)  # design tokens + component polish, all pages
    ui.add_css(theme_css(), shared=True)  # seasonal token sets (sortandpost.themes)
    engine = make_engine(settings.paths.working_db)
    init_db(engine)  # ensure an empty DB still renders an empty grid
    State.engine = engine
    State.settings = settings
    State.theme = get_theme(settings.paths)
    State.season = get_season(settings.paths)

    manager = SyncManager(settings, engine=engine)
    try:
        manager.start()
    except Exception:  # browsing must work even if the sync layer is unhappy
        log.exception("sync layer failed to start; continuing read-only")
    State.apply(manager)
    State.manager = manager
    State.onboarding_pending = settings.onboarding_pending
    # One ordered hook -- NiceGUI runs on_shutdown callbacks in registration
    # order, and the final publish must come after the indexer's last write.
    nicegui_app.on_shutdown(lambda: _shutdown_all(manager))

    if (
        settings.config.indexer.run_in_app
        and State.write_mode == WriteMode.writable
        and not State.onboarding_pending
    ):
        _start_indexer_thread(settings, manager)
    _start_lock_watch(settings, manager)
    _install_onboarding_gate()
    _register_welcome(settings, engine, manager)

    settings.paths.thumbnails_dir.mkdir(parents=True, exist_ok=True)
    nicegui_app.add_static_files(_THUMB_URL, str(settings.paths.thumbnails_dir))

    @nicegui_app.get(_TRASH_THUMB_URL + "/{batch_id}/{pixel_hash}")
    def _trash_thumb(batch_id: str, pixel_hash: str) -> FileResponse:
        """Only a batch's saved ``thumbs/<pixel_hash>.webp`` -- never the
        trashed photo files themselves."""
        batch = find_batch(settings.paths.local, batch_id)
        thumb = batch.thumb(pixel_hash) if batch and _HEX64.fullmatch(pixel_hash) else None
        if thumb is None:
            raise HTTPException(status_code=404)
        return FileResponse(thumb, media_type="image/webp")

    thumb_small, thumb_large = _thumb_sizes(settings)

    @contextmanager
    def _session() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    register_post_pages(settings, engine, thumb_small)

    @ui.page("/")
    async def library(request: Request) -> None:
        _header("/")
        client = ui.context.client  # for a valid slot context after awaits / element deletes
        stack_cfg = settings.config.stacking
        lq = LibraryQuery.from_pairs(request.query_params.multi_items())
        mode = lq.mode
        data = await nicegui_run.io_bound(_load_library, settings, engine, lq)
        if data is None:  # io_bound cancelled / app shutting down
            return
        view = data.view
        stacks = view.stacks
        flag_n, cloud_n = data.flag_n, data.cloud_n
        rep_ids = view.ids
        total = len(rep_ids)
        noun = "stacks" if stack_cfg.enabled else "photos"
        # carried onto every photo link so /photo's arrows page through this view
        link_query = lq.query_string()

        # Group-by mode: `view` already put rep_ids in section order. Each section
        # renders as a clickable header + its own grid (so a section can be folded
        # away without disturbing the others). `rep_ids` stays the source of truth
        # for the keyboard index / selection / infinite scroll.
        groups: list[PhotoGroup] = view.groups
        key_hashes: dict[str, list[str]] = {}  # section key -> its pixel_hashes
        group_bounds: dict[str, tuple[int, int]] = {}  # key -> [start, end) in rep_ids
        _seg_starts: list[int] = []  # section start indices, ascending (for bisect)
        _seg_keys: list[str] = []  # section key at the matching _seg_starts entry
        collapsed: set[str] = set()  # section keys currently folded shut
        if mode is not None:
            at = 0
            for g in groups:
                key_hashes[g.key] = g.hashes
                group_bounds[g.key] = (at, at + len(g.hashes))
                _seg_starts.append(at)
                _seg_keys.append(g.key)
                at += len(g.hashes)

        def _key_for_index(i: int) -> str:
            """Which section rep_ids[i] belongs to ('' when not in group-by)."""
            if not _seg_starts:
                return ""
            return _seg_keys[bisect.bisect_right(_seg_starts, i) - 1]

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

        search_box = _filter_bar(
            settings, lq, data, on_collapse_all=lambda c: _set_all_collapsed(c)
        )

        # count · active-filter chips (each removable) · ⚑ marked link
        with ui.row().classes("w-full items-center justify-between px-4 pt-2 gap-2 no-wrap"):
            with ui.row().classes("items-center gap-2 flex-wrap min-w-0"):
                counter = ui.label(f"{total} {noun}").classes("text-sm sp-muted")
                if lq.has_filters():
                    ui.label(f"matching · {view.total} in library").classes("text-xs sp-faint")
                _filter_chips(lq)
            flag_box = ui.row().classes("items-center gap-2 no-wrap")

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
                    "text-xs sp-muted"
                )
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")

                    async def _go() -> None:
                        dialog.close()
                        await _do_clear_flags()

                    ui.button("Clear all", on_click=_go).props("color=negative")
            dialog.open()

        with flag_box:
            flag_link = ui.link("", LibraryQuery(mark=True).url()).classes(
                "text-sm text-amber-600 no-underline hover:underline"
            )
            if writable:
                ui.button("clear all", icon="clear", on_click=_confirm_clear_flags).props(
                    "flat dense size=sm color=amber-7"
                )

        _refresh_flag_link(flag_n)

        if cloud_n > 0:
            cloud_banner = ui.row().classes(
                "mx-4 mt-2 items-center gap-3 rounded-md border border-amber-300 "
                "bg-amber-50 px-3 py-2 text-sm text-amber-800 "
                "dark:border-amber-700 dark:bg-amber-950 dark:text-amber-200"
            )
            with cloud_banner:
                _n_txt = "1 photo is" if cloud_n == 1 else f"{cloud_n} photos are"
                ui.label(f"⛅ {_n_txt} online-only and weren't downloaded.").classes("flex-1")

                async def _download_cloud() -> None:
                    ui.notify("downloading online-only files…")
                    result = await nicegui_run.io_bound(_run_hydrate_pass, settings, engine)
                    ui.notify(result)
                    ui.navigate.reload()

                if writable:
                    ui.button(
                        "Download & index", icon="cloud_download", on_click=_download_cloud
                    ).props("flat dense size=sm color=amber-9")
                ui.button(icon="close", on_click=lambda: cloud_banner.set_visibility(False)).props(
                    "flat dense round size=sm"
                ).tooltip("dismiss")

        if view.notice:
            ui.label(view.notice).classes("mx-4 mt-2 text-sm text-amber-600")
        if total < _GRID_BATCH and not lq.has_filters():
            _reload_as_photos_arrive(engine)
        if total == 0:
            if lq.has_filters():
                _empty_state("search_off", "No matching photos", "Try loosening the filters.")
                with ui.row().classes("w-full justify-center"):
                    ui.button(
                        "Clear filters",
                        icon="filter_alt_off",
                        on_click=lambda: ui.navigate.to(
                            LibraryQuery(group=lq.group, folder_sort=lq.folder_sort).url()
                        ),
                    ).props("flat")
            else:
                scanning = State.indexer_started
                _empty_state(
                    "hourglass_top" if scanning else "photo_library",
                    "Looking for your photos…" if scanning else "No photos yet",
                    "They'll appear here as soon as the first ones are indexed."
                    if scanning
                    else "Add your photo folders in Settings → Library.",
                )
            return

        # group-by mode: one clickable header + its own grid per section; flat
        # mode: a single grid. Both dicts stay empty when not grouping.
        header_chevrons: dict[str, ui.icon] = {}
        group_grids: dict[str, ui.grid] = {}

        def _style_chevron(chev: ui.icon, key: str) -> None:
            # `replace=` (not the default merge) so the old transform is cleared
            # when a section re-opens, not left stuck at -90deg.
            deg = "-90deg" if key in collapsed else "0deg"
            chev.style(replace=f"transition: transform .15s; transform: rotate({deg})")

        def _group_header(g: PhotoGroup) -> None:
            hdr = (
                ui.element("div")
                .classes(
                    "sp-section-header sticky z-30 flex w-full items-baseline "
                    "gap-2 px-2 py-1.5 cursor-pointer"
                )
                .style("user-select: none")
            )
            with hdr:
                chev = ui.icon("expand_more").classes("sp-faint")
                _style_chevron(chev, g.key)
                ui.label(g.label).classes("sp-display text-base font-semibold")
                ui.label(f"({g.count})").classes("text-xs sp-faint")
                if g.sublabel and g.sublabel != g.label:
                    ui.label(g.sublabel).classes("truncate text-xs sp-faint").tooltip(g.sublabel)
            hdr.on("click", lambda k=g.key: _toggle_group(k))
            header_chevrons[g.key] = chev

        grid: ui.grid | None = None
        if mode is None:
            grid = _photo_grid()
        else:
            with ui.column().classes("w-full gap-1 p-0"):
                for g in groups:
                    _group_header(g)
                    group_grids[g.key] = _photo_grid()
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
        toolbar = (
            ui.element("div")
            .classes(
                "fixed bottom-12 left-1/2 z-50 flex -translate-x-1/2 items-center gap-2 "
                "rounded-full px-4 py-2 text-white shadow-lg backdrop-blur"
            )
            .style("background-color: rgba(32,27,21,.96)")
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
                    "text-xs sp-muted"
                )
                if settings.config.stacking.enabled:
                    ui.label(
                        "Whole stacks will be deleted."
                        if settings.config.deletion.stack_scope == "whole_stack"
                        else "Only the shown photo of each stack is deleted."
                    ).classes("text-xs sp-muted")
                ui.label("Their ratings, tags and thumbnails are removed too.").classes(
                    "text-xs sp-muted"
                )
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")

                    async def _go() -> None:
                        # close after the work so ui.notify still has a live slot context
                        await _do_bulk_delete()
                        dialog.close()

                    ui.button("Delete", on_click=_go).props("color=negative")
            dialog.open()

        async def _do_bulk_reveal_or_open(open_mode: bool, app: str | None = None) -> None:
            """Reveal every selected photo in the host file manager, or open it
            in *app* (or the OS default app when ``app`` is ``None``) -- via
            the native openers, or the host helper agent under Docker. Works
            read-only: it doesn't touch the DB."""
            hashes = list(sel.ordered())
            if not hashes:
                return
            resolved = await nicegui_run.io_bound(resolve_sources, engine, hashes)
            if resolved is None:  # io_bound cancelled / app shutting down
                return
            roots = settings.library_roots
            host_paths = [to_host_path(p, roots) for p in resolved.values() if p]
            missing = len(hashes) - len(host_paths)
            if not host_paths:
                ui.notify("No present copies to open on this machine.", type="warning")
                return
            helper = _current_helper()
            endpoint = "open" if open_mode else "reveal"
            app_for = app if open_mode else None
            if helper is not None:
                res = await ui.run_javascript(
                    _helper_action_js_multi(endpoint, host_paths, app_for), timeout=8.0
                )
                if not (res or {}).get("ok"):
                    ui.notify((res or {}).get("error") or "helper error", type="warning")
            elif reveal_supported():
                path_objs = [Path(p) for p in host_paths]
                if not open_mode:
                    reveal_multiple_in_file_manager(path_objs)
                elif app_for:
                    open_multiple_with_app(path_objs, app_for)
                else:
                    open_multiple_in_default_app(path_objs)
            else:
                ui.notify("Reveal/open isn't available in this environment.", type="warning")
                return
            if missing:
                ui.notify(
                    f"{missing} photo(s) have no present copy on this machine.", type="warning"
                )

        async def _bulk_reveal_or_open(open_mode: bool, app: str | None = None) -> None:
            count = len(sel.selected)
            if not count:
                return
            threshold = (
                _BULK_OPEN_CONFIRM_THRESHOLD if open_mode else _BULK_REVEAL_CONFIRM_THRESHOLD
            )
            if count <= threshold:
                await _do_bulk_reveal_or_open(open_mode, app)
                return
            verb = "Open" if open_mode else "Reveal"
            with ui.dialog() as dialog, ui.card().classes("gap-2"):
                ui.label(f"{verb} {count} photos?").classes("font-medium")
                ui.label(f"This opens {count} windows or apps at once.").classes("text-xs sp-muted")
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")

                    async def _go() -> None:
                        dialog.close()
                        await _do_bulk_reveal_or_open(open_mode, app)

                    ui.button(verb, on_click=_go).props("color=primary")
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
                ui.separator().props("vertical dark")
                ui.button(icon="folder_open", on_click=lambda: _bulk_reveal_or_open(False)).props(
                    "flat dense round"
                ).tooltip("reveal in file manager")
                _open_with_button(lambda app: _bulk_reveal_or_open(True, app))
                _collection_add_button(
                    settings, lambda: list(sel.selected), label=None, on_added=sel.clear
                )
                compose_button(
                    settings,
                    sel.ordered,
                    title=lambda: f"Post · {utcnow():%d %b %Y}",
                    label=None,
                )
                ui.button(icon="close", on_click=sel.clear).props("flat dense round").tooltip(
                    "deselect all"
                )

        if lq.mark and rep_ids:
            # the "marked for editing" view IS the flagged set -- pre-select
            # everything so bulk actions (rate, tag, open, delete, …) need no
            # manual checkbox clicking first.
            sel.selected = set(rep_ids)
            _render_toolbar()

        def _render_tiles(container: ui.element, hashes: list[str]) -> None:
            """Draw the tiles for *hashes* into *container* (one grid). Shared by
            the infinite-scroll batch and by re-expanding a folded section."""
            if not hashes:
                return
            with _session() as session:
                by_hash = {
                    p.pixel_hash: p
                    for p in session.exec(
                        select(Photo).where(col(Photo.pixel_hash).in_(hashes))
                    ).all()
                }
                stars_by_hash = {
                    r.pixel_hash: r.stars
                    for r in session.exec(
                        select(Rating).where(col(Rating.pixel_hash).in_(hashes), Rating.stars > 0)
                    ).all()
                }
                flagged_ids = edit_flagged_hashes(session, hashes)
            sent = posted_hashes(settings.paths)
            with container:
                for h in hashes:
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
                            link_query=link_query,
                            posted=any(m in sent for m in stacks.members(h)),
                        )

        def append_batch() -> None:
            nonlocal loaded, busy, done
            if busy or done:
                return
            busy = True
            try:
                slice_ids = rep_ids[loaded : loaded + _GRID_BATCH]
                if slice_ids and grid is not None:  # flat mode: one grid
                    _render_tiles(grid, slice_ids)
                elif slice_ids:  # group-by: split the slice by section, skip folded ones
                    i = 0
                    while i < len(slice_ids):
                        key = _key_for_index(loaded + i)
                        j = i
                        while j < len(slice_ids) and _key_for_index(loaded + j) == key:
                            j += 1
                        if key not in collapsed and key in group_grids:
                            _render_tiles(group_grids[key], slice_ids[i:j])
                        i = j
                loaded += len(slice_ids)
                counter.set_text(
                    f"{loaded} of {total} {noun}" if loaded < total else f"{total} {noun}"
                )
                if loaded >= total:
                    done = True
                    spinner.set_visibility(False)
            finally:
                busy = False

        def _repaint_header(key: str) -> None:
            chev = header_chevrons.get(key)
            if chev is not None:
                _style_chevron(chev, key)

        def _rebuild_section(key: str) -> None:
            """Sync one section's grid to its collapsed state: drop its tiles, and
            (when open) redraw the ones the scroll cursor has already reached."""
            gg = group_grids.get(key)
            if gg is None:
                return
            for h in key_hashes.get(key, ()):
                cell_els.pop(h, None)
                rating_setters.pop(h, None)
                flag_setters.pop(h, None)
                flag_toggles.pop(h, None)
            gg.clear()
            if key in collapsed:
                return
            start, end = group_bounds[key]
            upto = min(end, loaded)
            if upto > start:
                _render_tiles(gg, key_hashes[key][: upto - start])

        def _persist_collapsed() -> None:
            ui.run_javascript(f"window.__spCollapsed = {json.dumps(sorted(collapsed))}")

        def _toggle_group(key: str) -> None:
            if key in collapsed:
                collapsed.discard(key)
            else:
                collapsed.add(key)
                if 0 <= focus[0] < len(rep_ids) and _key_for_index(focus[0]) == key:
                    _clear_focus()
            _repaint_header(key)
            _rebuild_section(key)
            _persist_collapsed()

        def _set_all_collapsed(collapse: bool) -> None:
            collapsed.clear()
            if collapse:
                collapsed.update(group_bounds)
                _clear_focus()
            for key in group_bounds:
                _repaint_header(key)
                _rebuild_section(key)
            _persist_collapsed()

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
            if collapsed:  # step over any folded-away sections in the travel direction
                direction = 1 if delta >= 0 else -1
                while 0 <= target < len(rep_ids) and _key_for_index(target) in collapsed:
                    target += direction
                if not (0 <= target < len(rep_ids)):
                    return  # nothing open that way -- leave the ring where it is
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
                    ui.navigate.to(_photo_url(h, link_query))
            elif key == "/":
                search_box.run_method("focus")
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
        ui.run_javascript(_KEY_FOCUS_GUARD_JS)  # keep shortcuts alive after Esc
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
            want_n, want_y, want_c = await ui.run_javascript(_GRID_RESTORE_JS, timeout=3.0)
        except TimeoutError:
            _end_restore()
            return
        if mode is not None and want_c:  # restore which sections were folded shut
            reopen = {k for k in want_c if k in group_bounds}
            if reopen:
                collapsed.clear()
                collapsed.update(reopen)
                for key in reopen:
                    _repaint_header(key)
                    _rebuild_section(key)  # drop the first-screenful tiles now hidden
                _persist_collapsed()
        while loaded < int(want_n) and not done:
            append_batch()
        if want_y:
            ui.run_javascript(_scroll_to_js(int(want_y)))
            ui.timer(10.0, _end_restore, once=True)  # ceiling; the tick resumes sooner
        else:
            _end_restore()

    @ui.page("/photo/{pixel_hash}")
    async def photo_detail(pixel_hash: str, request: Request) -> None:
        _header("/")
        client = ui.context.client
        stacking_on = settings.config.stacking.enabled
        # the library view this photo was opened from (its query string rides on
        # the link); empty = the whole library, newest first
        lq = LibraryQuery.from_pairs(request.query_params.multi_items())
        link_query = lq.query_string()
        with _session() as session:
            photo = get_photo(session, pixel_hash)
            paths = get_paths(session, pixel_hash) if photo else []
            stars = get_rating(session, pixel_hash) if photo else None
            flagged = bool(edit_flagged_hashes(session, [pixel_hash])) if photo else False
            stacks = build_stacks(session, settings.config.stacking) if stacking_on else None
            members = stacks.members(pixel_hash) if stacks else [pixel_hash]

        if photo is None:
            _empty_state("image_not_supported", "Photo not found", pixel_hash)
            return

        # -- prev/next: the same order the grid showed ----------------------- #
        ordered = _cached_view(link_query)
        if ordered is None:
            view = await nicegui_run.io_bound(_resolve, settings, engine, lq)
            ordered = view.ids if view is not None else []
        anchor = stacks.representative(pixel_hash) if stacks is not None else pixel_hash
        prev_hash, next_hash = neighbors(ordered, anchor)

        ui.button("← Back", on_click=lambda: ui.navigate.back()).props("flat dense").classes(
            "mx-4 mt-2"
        )
        with ui.row().classes("w-full no-wrap gap-6 p-4"):
            # a plain <img>, not ui.image: q-img sizes itself by aspect-ratio
            # padding and ignores max-height, so a portrait photo overflowed the
            # window. This fits whole inside the viewport (minus header, back
            # button and status bar) and the width left beside the info column.
            ui.element("img").props(f'src="{_thumb_src(pixel_hash, thumb_large)}"').classes(
                "rounded shadow"
            ).style(
                "display:block; min-width:0; flex:0 1 auto; width:auto; height:auto;"
                " max-width:min(100%, 1280px); max-height:calc(100vh - 12rem - 20px);"
                " object-fit:contain"
            )
            with ui.column().classes("gap-1 text-sm shrink-0 max-w-sm"):
                star_widget = _star_widget(settings, engine, pixel_hash, stars or 0)
                flag_switch = _edit_flag_button(settings, engine, pixel_hash, flagged)
                ui.label("← → move · 1-5 rate · 0 clear · f flag").classes("text-[11px] sp-faint")
                _facts(photo)
                ui.separator()
                ui.label("Paths").classes("font-bold mt-2")
                native_reveal = reveal_supported()
                helper_info = _current_helper()
                for pp in paths:
                    _path_row(
                        pp.path,
                        present=pp.present,
                        roots=settings.library_roots,
                        helper=helper_info,
                        native_reveal=native_reveal,
                    )

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
                    ui.label("Not in any collection.").classes("text-xs sp-muted")
                    return
                with ui.row().classes("items-center gap-1 flex-wrap"):
                    for c in member_of:
                        with ui.row().classes(
                            "items-center gap-0 rounded-full sp-placeholder pl-2"
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
        with ui.column().classes("w-full gap-1 px-4"):
            posted_note(settings, pixel_hash)

        if len(members) > 1:
            _stack_filmstrip(pixel_hash, members, thumb_small, link_query)

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
                        ui.navigate.to(lq.url())

                def _confirm_delete() -> None:
                    with ui.dialog() as dialog, ui.card().classes("gap-2"):
                        ui.label(
                            f"Delete this stack ({len(members)} photos)?"
                            if whole_stack
                            else "Delete this photo?"
                        ).classes("font-medium")
                        ui.label(f"The file(s) will be {_delete_where_note()}.").classes(
                            "text-xs sp-muted"
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
                ui.run_javascript(
                    f"location.replace({json.dumps(_photo_url(prev_hash, link_query))})"
                )
            elif (key == "ArrowRight" or key == "j") and next_hash:
                ui.run_javascript(
                    f"location.replace({json.dumps(_photo_url(next_hash, link_query))})"
                )
            elif star_widget is not None and key.number is not None and 0 <= key.number <= 5:
                star_widget.set_value(key.number)  # fires the same write path as a click
            elif flag_switch is not None and key == "f":
                flag_switch.set_value(not flag_switch.value)

        ui.keyboard(on_key=_on_key)
        ui.run_javascript(_KEY_FOCUS_GUARD_JS)  # keep shortcuts alive after Esc

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

        progress = ui.label().classes("px-4 pt-2 text-sm sp-muted")
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
                    "w-full max-w-2xl rounded shadow sp-placeholder"
                ).style("max-height: 78vh").props("fit=contain")
                meta = [Path(photo.primary_path).name]
                if photo.taken_at:
                    meta.append(photo.taken_at.isoformat(sep=" ", timespec="minutes"))
                cam = " ".join(filter(None, (photo.camera_make, photo.camera_model)))
                if cam:
                    meta.append(cam)
                ui.label(" · ".join(meta)).classes("text-xs sp-muted")

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
                        "text-xs sp-muted self-center"
                    )
                ui.label("1-5 rate · f flag for editing · → / S skip · ← back").classes(
                    "text-[11px] sp-faint"
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
        ui.run_javascript(_KEY_FOCUS_GUARD_JS)  # keep shortcuts alive after Esc
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
                ).classes("text-xs sp-muted")
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
                with (
                    ui.element("div")
                    .classes("flex aspect-square w-full items-center justify-center sp-faint")
                    .style("background-color: var(--sp-surface-2)")
                ):
                    ui.icon("photo_library", size="2.5rem")
                return
            with ui.element("div").classes("grid w-full grid-cols-2 gap-0.5"):
                for i in range(4):
                    cell = (
                        ui.element("div")
                        .classes("aspect-square w-full")
                        .style("background-color: var(--sp-surface-2)")
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
                "sp-card group relative flex cursor-pointer flex-col overflow-hidden"
            )
            card.on("click", lambda c=c: ui.navigate.to(f"/collections/{c.id}"))
            with card:
                _mosaic(c)
                with ui.element("div").classes("px-3 py-2"):
                    ui.label(c.name).classes(
                        "block w-full truncate text-base font-semibold sp-display"
                    )
                    ui.label(f"{c.count} photo(s)").classes("block text-xs sp-muted")
                ui.button(icon="delete").props("flat dense round size=sm color=red-4").classes(
                    "absolute right-1 top-1 bg-black/40 opacity-0 group-hover:opacity-100"
                ).on("click.stop", lambda c=c: _confirm_delete(c))

        with ui.row().classes("w-full items-center justify-between px-4 pt-2"):
            ui.label(f"{len(colls)} collection(s)").classes("text-sm sp-muted")
            ui.button("New collection", icon="add", on_click=_new_collection).props("flat")

        if not colls:
            _empty_state(
                "bookmarks",
                "No collections yet",
                "Create one, then add photos from the library (select tiles → Add to "
                "collection) or from a photo page.",
            )
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
            _empty_state("bookmark_remove", "Collection not found")
            ui.link("← All collections", "/collections").classes("mx-auto")
            return

        ui.button("← All collections", on_click=lambda: ui.navigate.to("/collections")).props(
            "flat dense"
        ).classes("mx-4 mt-2")

        title = ui.label(coll.name).classes("px-4 text-2xl font-bold sp-display")
        meta = ui.label().classes("px-4 text-sm sp-muted")

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
                    "text-xs sp-muted"
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
                            "text-xs sp-faint"
                        )
                    count_lbl = ui.label("0 selected").classes("text-sm sp-muted")
                if len(candidates) > _PICKER_CAP:
                    ui.label(
                        f"Showing the first {_PICKER_CAP} of {len(candidates)} — add the rest "
                        "from the library's bulk toolbar."
                    ).classes("text-xs text-amber-600")
                if not shown:
                    ui.label("Every photo is already in this collection.").classes("sp-muted")

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
                                "aspect-square w-full sp-placeholder object-cover"
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
            compose_button(
                settings,
                lambda: _members_ids(),
                title=lambda: title.text,
                caption=lambda: notes_box.value or "",
                collection_id=coll_id,
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

        def _members_ids() -> list[str]:
            """Present members in collection order (what a new post starts with)."""
            return [p.pixel_hash for p in _members()[0]]

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
                        "w-full aspect-square object-cover sp-placeholder"
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
                    with ui.column().classes("m-4 items-start gap-2 sp-muted"):
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
                "text-xs sp-faint"
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
    def search_redirect(request: Request) -> RedirectResponse:
        """Search lives in the library now. Old ``/search`` links and bookmarks
        (``stars`` there meant "at least N") land on the equivalent ``/`` view."""
        return RedirectResponse(legacy_query(dict(request.query_params)).url(), status_code=307)

    @ui.page("/review")
    async def review() -> None:
        _header("/review")
        model_name = settings.config.clip.model
        vocab = load_vocab(settings.paths.tags_file)
        with _session() as session:
            groups = pending_groups(session, model=model_name, vocab=vocab)
        pending_total = sum(g.count for g in groups)
        sections = facet_sections(groups, vocab)

        totals = {"pending": pending_total}

        def _summary() -> str:
            return (
                f"{totals['pending']} pending CLIP suggestion(s) across "
                f"{len(groups)} tag(s) in {len(sections)} facet(s)"
            )

        summary = ui.label(_summary()).classes("px-4 pt-2 text-sm sp-muted")

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
            ).classes("m-4 sp-muted")
            return

        async def _bulk(tag_name: str, decision: str) -> None:
            done = await nicegui_run.io_bound(
                _apply_review_bulk, settings, engine, tag_name, decision
            )
            ui.notify(f"{decision}d {done} · {tag_name}")
            ui.navigate.reload()

        def _tag_panel(group: ReviewGroup) -> None:
            """One tag: header with a live count, bulk buttons, and per-photo tiles
            (loaded a page at a time, the first when the panel is opened)."""
            shown: dict[str, ui.element] = {}
            state = {"count": group.count, "loaded": False}
            with ui.expansion().classes("w-full") as panel:
                with panel.add_slot("header"):
                    title = ui.label().classes("text-base")
                with ui.row().classes("gap-2 p-2"):
                    ui.button(
                        "Approve all",
                        on_click=lambda: _bulk(group.tag_name, "approve"),
                    ).props("color=positive")
                    ui.button(
                        "Reject all",
                        on_click=lambda: _bulk(group.tag_name, "reject"),
                    ).props("color=negative flat")
                grid = _photo_grid(150)
                more = ui.button("Show more").props("flat")

            def _retitle() -> None:
                title.set_text(f"{group.tag_name}  ·  {state['count']}")

            def _load_page() -> None:
                with _session() as session:
                    page = pending_for_tag(
                        session,
                        model=model_name,
                        tag_name=group.tag_name,
                        limit=_REVIEW_PAGE,
                        offset=len(shown),
                    )
                with grid:
                    for pixel_hash, score in page:
                        _tile(pixel_hash, score)
                more.set_visibility(state["count"] > len(shown))

            async def _decide(pixel_hash: str, decision: str) -> None:
                tile = shown[pixel_hash]
                for btn in tile.descendants():
                    if isinstance(btn, ui.button):
                        btn.disable()
                applied = await nicegui_run.io_bound(
                    _apply_review_one, settings, engine, group.tag_name, pixel_hash, decision
                )
                shown.pop(pixel_hash, None)
                tile.delete()
                if applied:
                    state["count"] -= 1
                    totals["pending"] -= 1
                    summary.set_text(_summary())
                if state["count"] <= 0:
                    panel.delete()
                    return
                _retitle()
                more.set_visibility(state["count"] > len(shown))

            def _tile(pixel_hash: str, score: float) -> None:
                with ui.column().classes("gap-0 w-full") as tile:
                    with ui.link(target=f"/photo/{pixel_hash}").classes("block w-full"):
                        ui.image(_thumb_src(pixel_hash, thumb_small)).classes(
                            "w-full aspect-square object-cover sp-placeholder"
                        ).props("loading=lazy")
                    with ui.row().classes("w-full items-center justify-between no-wrap"):
                        ui.label(f"{score:.2f}").classes("text-xs sp-faint")
                        with ui.row().classes("gap-0 no-wrap"):
                            ui.button(
                                icon="close", on_click=lambda: _decide(pixel_hash, "reject")
                            ).props("flat dense round size=sm color=negative").tooltip("Reject")
                            ui.button(
                                icon="check", on_click=lambda: _decide(pixel_hash, "approve")
                            ).props("flat dense round size=sm color=positive").tooltip("Approve")
                shown[pixel_hash] = tile

            def _on_toggle(event: ValueChangeEventArguments[bool]) -> None:
                if event.value and not state["loaded"]:
                    state["loaded"] = True
                    _load_page()

            _retitle()
            more.set_visibility(False)
            more.on_click(_load_page)
            panel.on_value_change(_on_toggle)

        for section in sections:
            with ui.row().classes("items-baseline gap-2 px-4 pt-4"):
                ui.label(section.facet or "Other").classes("sp-display text-lg font-semibold")
                ui.label(f"{section.count} pending").classes("text-sm sp-faint")
            for group in section.groups:
                _tag_panel(group)

    @ui.page("/settings")
    async def settings_page() -> None:
        dark = _header("/settings")
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
            cloud_n = online_only_count(session)
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
        ]
        if manager.last_merge is not None:
            rows.append(("Last merge", str(manager.last_merge)))
        if manager.restored_from_snapshot is not None:
            rows.append(("Restored from", manager.restored_from_snapshot.name))

        writable = State.write_mode == WriteMode.writable
        tiles: list[tuple[str, str, str]] = [  # (icon, value, caption)
            ("photo_library", f"{present:,}", "photos indexed"),
            ("help_outline", f"{total - present:,}", "missing"),
            ("cloud_off", f"{cloud_n:,}", "online-only"),
            ("memory", f"{embeddings:,}", "CLIP embeddings"),
            ("rate_review", f"{pending:,}", "suggestions pending"),
            ("task_alt", f"{approved:,}", "suggestions approved"),
        ]

        def _needs_lock() -> None:
            ui.label(f"Read-only — {State.lock_holder or 'another machine'} holds the write lock.")
            ui.label("These settings unlock once this machine can write.").classes(
                "text-xs sp-faint"
            )

        with ui.row().classes("w-full max-w-7xl mx-auto p-4 gap-8 no-wrap items-start"):
            with ui.column().classes("sp-settings-rail gap-0.5 shrink-0"):
                ui.label("Settings").classes("sp-display text-2xl font-semibold pb-2 px-2")
                for key, title, icon in _SETTINGS_SECTIONS:
                    link = ui.link("", f"#sp-{key}").classes("sp-nav flex items-center gap-2")
                    with link:
                        ui.icon(icon)
                        ui.label(title)

            with ui.column().classes("flex-1 min-w-0 gap-10 pb-16"):
                with _settings_section("overview"):
                    tile_grid = ui.element("div").classes("sp-tiles w-full")
                    for icon, value, caption in tiles:
                        with tile_grid, ui.column().classes("sp-card sp-tile gap-0"):
                            ui.icon(icon).classes("text-lg text-primary")
                            ui.label(value).classes("sp-display text-2xl font-semibold")
                            ui.label(caption).classes("text-xs sp-muted")
                    with _settings_card("Indexing", "sync", wide=True):
                        _progress_panel()
                        if writable:
                            client = ui.context.client  # a real scan outlasts the page

                            async def _scan_now() -> None:
                                ui.notify("indexer pass started…")
                                result = await nicegui_run.io_bound(
                                    _run_index_pass, settings, engine
                                )
                                with client:  # the awaited gap can invalidate the slot
                                    ui.notify(result)

                            ui.button("Run indexer pass now", icon="play_arrow", on_click=_scan_now)
                        else:
                            ui.label("Read-only — indexer actions are disabled.").classes(
                                "text-amber-600 text-sm"
                            )

                with _settings_section("library"):
                    with _settings_card("Library folders", "folder_open", wide=True):
                        _library_roots_editor(settings, engine)
                    with _settings_card("Excluded folders", "folder_off"):
                        if writable:
                            _exclude_editor(settings, engine)
                        else:
                            _needs_lock()
                    with _settings_card("Online-only files", "cloud_download"):
                        if writable:
                            _online_only_editor(settings, engine, cloud_n)
                        else:
                            _needs_lock()

                with _settings_section("indexing"):
                    with _settings_card("Indexing speed", "speed"):
                        if writable:
                            _cpu_speed_editor(settings)
                        else:
                            _needs_lock()
                    with _settings_card("CLIP model files", "model_training"):
                        _clip_model_editor(settings)
                        ui.label(
                            "Rebuild the CLIP matrix from the CLI:  "
                            "python -m sortandpost.indexer --reembed"
                        ).classes("text-xs sp-faint")
                    with _settings_card("Image stacking", "layers"):
                        if writable:
                            _stacking_editor(settings)
                        else:
                            _needs_lock()

                with _settings_section("appearance"):
                    with _settings_card("Appearance", "palette", wide=True):
                        _theme_editor(settings, dark)
                    with _settings_card("Open with", "open_in_new"):
                        _open_with_app_editor(settings)

                with (
                    _settings_section("posting"),
                    _settings_card(f"Posting with {POSTING_SERVICE}", "send", wide=True),
                ):
                    posting_settings_panel(settings, writable=writable)

                with _settings_section("deleting"):
                    with _settings_card("Deleting photos", "delete_outline"):
                        if writable:
                            _deletion_editor(settings)
                        else:
                            _needs_lock()
                    with _settings_card("Recently deleted", "restore_from_trash", wide=True):
                        if writable:
                            _trash_panel(settings, engine)
                        else:
                            _needs_lock()

                with _settings_section("sync"):
                    with (
                        _settings_card("Sync status", "cloud_sync"),
                        ui.grid(columns="auto 1fr").classes("gap-x-6 gap-y-1 text-sm"),
                    ):
                        for key, value in rows:
                            ui.label(key).classes("sp-faint")
                            ui.label(value).classes("break-all")
                    with _settings_card("Shared library folder", "folder_shared"):
                        _app_data_editor(settings)


_SETTINGS_SECTIONS: list[tuple[str, str, str]] = [  # (anchor key, title, icon)
    ("overview", "Overview", "dashboard"),
    ("library", "Library", "photo_library"),
    ("indexing", "Indexing & AI", "memory"),
    ("appearance", "Appearance & apps", "palette"),
    ("posting", "Posting", "send"),
    ("deleting", "Deleting", "delete"),
    ("sync", "Sync & storage", "cloud_sync"),
]


@contextmanager
def _settings_section(key: str) -> Iterator[None]:
    """One `/settings` category: an anchored heading + a 2-column card grid
    (a single column on narrow windows)."""
    _, title, icon = next(s for s in _SETTINGS_SECTIONS if s[0] == key)
    with ui.element("section").props(f"id=sp-{key}").classes("sp-settings-section w-full"):
        with ui.row().classes("items-center gap-2 pb-3"):
            ui.icon(icon).classes("text-2xl text-primary")
            ui.label(title).classes("sp-display text-xl font-semibold")
        with ui.element("div").classes("sp-settings-grid"):
            yield


@contextmanager
def _settings_card(title: str, icon: str, *, wide: bool = False) -> Iterator[None]:
    """A titled `/settings` panel; ``wide`` spans both grid columns."""
    with ui.column().classes("sp-card sp-settings-card gap-2" + (" sp-wide" if wide else "")):
        with ui.row().classes("items-center gap-2 no-wrap"):
            ui.icon(icon).classes("text-lg sp-muted")
            ui.label(title).classes("font-semibold")
        yield


def _start_indexer_thread(settings: Settings, manager: SyncManager) -> None:
    """Run the continuous indexer (step 6) in-process while this machine is the
    writer. It stops on app shutdown; it never runs in read-only mode. Idempotent
    -- a later call (e.g. from ``_start_lock_watch`` on promotion) is a no-op."""
    if State.indexer_started or State.shutting_down:
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
            "paused": State.indexer_paused,
        },
        name="sortandpost-indexer",
        daemon=True,
    )
    State.indexer_stop, State.indexer_thread = stop, thread
    thread.start()


_INDEXER_JOIN_SEC = 60.0


def _shutdown_all(manager: SyncManager) -> None:
    """App shutdown (Quit, restart, Ctrl-C, SIGTERM), strictly in this order:
    stop the indexer and wait for it -- it stops between files, so this is
    quick -- then the final publish + lock release (``manager.stop``,
    constraints #4 and #9), then exiftool."""
    State.shutting_down = True  # the lock watch must not start a new indexer now
    thread = State.indexer_thread
    if State.indexer_stop is not None:
        State.indexer_stop.set()
    if thread is not None:
        thread.join(timeout=_INDEXER_JOIN_SEC)
        if thread.is_alive():
            log.warning("indexer still busy after %.0fs; publishing anyway", _INDEXER_JOIN_SEC)
    try:
        manager.stop()
    except Exception:
        log.exception("final publish / lock release failed")
    EXIFTOOL.close()
    log.info("shutdown complete")


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
                if (
                    mode is WriteMode.writable
                    and settings.config.indexer.run_in_app
                    and not State.onboarding_pending
                ):
                    _start_indexer_thread(settings, manager)  # no-op if already running
            except Exception:  # a bad tick must not kill the watch
                log.exception("lock-watch tick failed")

    thread = threading.Thread(target=_watch, name="sortandpost-lockwatch", daemon=True)
    thread.start()
    nicegui_app.on_shutdown(stop.set)


# The grid's resolved order per query string, so the photo page's prev/next
# walks exactly what the grid showed -- a snapshot, so rating a photo out of an
# "unrated" filter doesn't pull it out from under the arrow keys, and a text
# query isn't re-embedded on every arrow press. Small and time-bounded; a miss
# (reload, bookmark) just resolves again.
_VIEW_CACHE: OrderedDict[str, tuple[float, list[str]]] = OrderedDict()
_VIEW_CACHE_MAX = 16
_VIEW_CACHE_TTL = 30 * 60.0
_VIEW_CACHE_LOCK = threading.Lock()


def _remember_view(key: str, ids: list[str]) -> None:
    with _VIEW_CACHE_LOCK:
        _VIEW_CACHE[key] = (time.monotonic(), ids)
        _VIEW_CACHE.move_to_end(key)
        while len(_VIEW_CACHE) > _VIEW_CACHE_MAX:
            _VIEW_CACHE.popitem(last=False)


def _cached_view(key: str) -> list[str] | None:
    with _VIEW_CACHE_LOCK:
        hit = _VIEW_CACHE.get(key)
        if hit is None or time.monotonic() - hit[0] > _VIEW_CACHE_TTL:
            return None
        return hit[1]


def _resolve(settings: Settings, engine: Engine, lq: LibraryQuery) -> LibraryView:
    """Runs off the event loop (``run.io_bound``): a text query embeds with
    CLIP. Caches the order for the photo page's prev/next."""
    encoder = (lambda: State.get_clip_model(settings)) if clip_available() else None
    with Session(engine) as session:
        view = resolve_view(session, settings, lq, encoder=encoder)
    _remember_view(lq.query_string(), view.ids)
    return view


@dataclass
class _LibraryData:
    """Everything the ``/`` page needs from the DB, loaded in one off-loop call."""

    view: LibraryView
    cameras: list[tuple[str, int]]
    folders: list[tuple[str, str]]
    tags: dict[str, list[str]]
    flag_n: int
    cloud_n: int


def _load_library(settings: Settings, engine: Engine, lq: LibraryQuery) -> _LibraryData:
    view = _resolve(settings, engine, lq)
    try:
        vocab = load_vocab(settings.paths.tags_file)
    except Exception:  # a broken tags.yaml must not break the library
        log.exception("could not load the tag vocabulary")
        vocab = None
    with Session(engine) as session:
        return _LibraryData(
            view=view,
            cameras=camera_choices(session),
            folders=folder_choices(session, settings.library_roots),
            tags=tag_choices(session, vocab),
            flag_n=edit_flag_count(session),
            cloud_n=online_only_count(session),
        )


def _run_index_pass(settings: Settings, engine: Engine) -> str:
    stats = scan_once(settings)
    try:
        clip_stats = scan_clip_once(settings, model=State.get_clip_model(settings))
    except ModelMismatch as exc:
        return f"{stats}  |  CLIP: {exc}"
    except ClipModelUnavailable:
        return f"{stats}  |  CLIP: skipped (torch / open-clip not installed)"
    return f"{stats}  |  {clip_stats}"


def _run_hydrate_pass(settings: Settings, engine: Engine) -> str:
    """Off the event loop (``run.io_bound``): download every recorded online-only
    file, then run a normal index pass so they land in the library."""
    hy = hydrate_online_only(settings)
    if hy.hydrated == 0:
        return str(hy)
    return f"{hy}  |  {_run_index_pass(settings, engine)}"


def _exif(settings: Settings) -> AbstractContextManager[ExiftoolClient | None]:
    """The shared exiftool client (``None`` when exiftool isn't installed).
    Blocking: only use it off the event loop."""
    return EXIFTOOL.session(
        write_windows_compat_tags=settings.config.rating.write_windows_compat_tags
    )


def _apply_review_bulk(settings: Settings, engine: Engine, tag_name: str, decision: str) -> int:
    """Off the event loop (``run.io_bound``): approve/reject every pending
    prediction for *tag_name*. Approve embeds keywords via exiftool, so it must
    not touch the loop (hard constraint #7)."""
    model_name = settings.config.clip.model
    facet = load_vocab(settings.paths.tags_file).facet_of(tag_name)
    approving = decision == "approve"

    done = 0
    exif_ctx = _exif(settings) if approving else nullcontext(None)
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


def _apply_review_one(
    settings: Settings, engine: Engine, tag_name: str, pixel_hash: str, decision: str
) -> bool:
    """Off the event loop: approve/reject one photo's suggestion. ``False`` when it
    was no longer pending (already decided, e.g. by a bulk action in another tab)."""
    model_name = settings.config.clip.model
    facet = load_vocab(settings.paths.tags_file).facet_of(tag_name)
    with Session(engine) as session:
        if not is_pending(session, model=model_name, pixel_hash=pixel_hash, tag_name=tag_name):
            return False
        if decision == "approve":
            with _exif(settings) as exiftool:
                approve(
                    session,
                    device_id=settings.device_id,
                    model=model_name,
                    pixel_hash=pixel_hash,
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
                pixel_hash=pixel_hash,
                tag_name=tag_name,
                now=utcnow(),
            )
        session.commit()
    return True


def _apply_rating(settings: Settings, engine: Engine, pixel_hash: str, stars: int) -> bool:
    """Off the event loop (``run.io_bound``): set the rating (``ratings`` row +
    ``changes`` row) and mirror it into the present, embed-capable file copies via
    exiftool when available (hard constraints #4, #7). With stacking on, the
    rating is applied to every member of the stack (stack-level ratings)."""
    with Session(engine) as session, _exif(settings) as exiftool:
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
    with Session(engine) as session, _exif(settings) as exiftool:
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
    with Session(engine) as session, _exif(settings) as exiftool:
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
    batch_id = result.trash.batch_id if result.trash else None
    if batch_id is not None:
        State.last_delete = (batch_id, len(result.purged), time.monotonic())
    return result


def _delete_summary(result: DeleteResult) -> str:
    n = len(result.purged)
    msg = f"deleted {n} photo(s); {result.files_trashed} file(s) moved to the app trash"
    if result.files_failed:
        msg += f"; {result.files_failed} file(s) could not be moved"
    if result.files_trashed:
        msg += " — Undo is in the status bar"
    return msg


def _delete_where_note() -> str:
    return (
        "moved to the app's trash (APP_DATA/local/trash, never synced) — you can "
        "put them back from Settings until you move them to the system Trash"
    )


def _stacking_editor(settings: Settings, *, autosave: bool = False) -> None:
    """`/settings` toggles for image stacking. Read-time, so saving just persists
    to ``config.yaml`` + updates the live config -- no re-index; the next library
    or search view reflects it. ``autosave`` (the welcome wizard) saves on every
    change instead of showing a Save button."""
    cfg = settings.config.stacking
    ui.label(
        "Group near-duplicate photos in the same folder into one entry (the "
        "newest by modified time is shown). Ratings apply to the whole stack."
    ).classes("text-xs sp-faint max-w-xl")

    enabled = ui.switch("Enabled", value=cfg.enabled)
    raw_jpeg = ui.checkbox("Pair a RAW with its same-named JPEG", value=cfg.raw_jpeg)
    similar = ui.checkbox(
        'Group similar names — "img.jpg", "img copy.jpg", "img (1).jpg", "copy img.jpg"',
        value=cfg.similar_names,
    )

    def _store() -> None:
        cfg.enabled = bool(enabled.value)
        cfg.raw_jpeg = bool(raw_jpeg.value)
        cfg.similar_names = bool(similar.value)
        settings.config.dump(settings.paths.config_file)

    if autosave:
        for el in (enabled, raw_jpeg, similar):
            el.on_value_change(lambda _: _store())
        return

    def _save() -> None:
        _store()
        ui.notify("saved — reload the library to see it")

    ui.button("Save", on_click=_save).props("color=primary")


def _theme_editor(settings: Settings, dark: ui.dark_mode) -> None:
    """Light / Dark / System theme. Per-machine (``local/ui_prefs.json``), never
    synced -- each machine keeps its own. Applies to the current tab immediately;
    other open tabs pick it up on their next load."""
    ui.label("Light, Dark, or follow the operating system. Saved on this machine only.").classes(
        "text-xs sp-faint max-w-xl"
    )

    ui.label("Mode").classes("text-sm font-medium pt-1")
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

    ui.label("Season").classes("text-sm font-medium pt-2")
    ui.label(
        "A color palette per season. Auto follows today's date (Mar-May Spring, Jun-Aug "
        "Summer, Sep-Nov Fall, Dec-Feb Winter)."
    ).classes("text-xs sp-faint max-w-xl")
    swatches: dict[SeasonChoice, ui.element] = {}

    def _pick(value: SeasonChoice) -> None:
        State.season = value
        set_season(settings.paths, value)
        _apply_season(value)
        for key, el in swatches.items():
            if key == value:
                el.classes(add="sp-swatch-on")
            else:
                el.classes(remove="sp-swatch-on")

    with ui.row().classes("gap-2 flex-wrap"):
        for choice_key in SEASON_CHOICES:
            shown = resolve_season(choice_key)
            label = SEASON_LABELS[choice_key]
            if choice_key == "auto":
                label += f" · {SEASON_LABELS[shown]} now"
            with ui.element("div").classes(
                "sp-swatch" + (" sp-swatch-on" if choice_key == State.season else "")
            ) as sw:
                with ui.row().classes("items-center gap-1 no-wrap"):
                    ui.icon(SEASON_ICONS[choice_key]).classes("text-base sp-muted")
                    ui.label(label).classes("text-sm")
                with ui.element("div").classes("sp-swatch-strip"):
                    for color in PALETTES[shown]:
                        ui.element("span").style(f"background:{color}")
            sw.on("click", lambda c=choice_key: _pick(c))
            swatches[choice_key] = sw


def _open_with_app_editor(settings: Settings) -> None:
    """`/settings` control for the app the "Open" button launches instead of
    the OS default (#22). Per-machine (``local/ui_prefs.json``), never synced
    -- app paths differ across machines -- and doesn't touch the system's own
    file-type association, only what sortandpost itself launches."""
    ui.label(
        "The app sortandpost's Open button launches, instead of the "
        "operating system default. Saved on this machine only."
    ).classes("text-xs sp-faint max-w-xl")

    with ui.row().classes("w-full items-end gap-2 no-wrap"):
        app_in = (
            ui.input("App path", value=get_open_with_app(settings.paths) or "")
            .props("dense")
            .classes("flex-1")
        )
        if _app_browse_available():
            ui.button("Browse…", icon="folder_open", on_click=lambda: _browse_app(app_in)).props(
                "flat dense"
            )
        else:
            ui.label(
                "macOS: the .app in /Applications.  Windows: the .exe path.  "
                "Linux: an executable or .desktop file path."
            ).classes("text-xs sp-faint")

    def _save() -> None:
        value = clean_path(app_in.value or "")
        set_open_with_app(settings.paths, value or None)
        ui.notify("saved" if value else "cleared — using system default", type="positive")

    def _clear() -> None:
        app_in.value = ""
        set_open_with_app(settings.paths, None)
        ui.notify("cleared — using system default")

    with ui.row().classes("gap-2"):
        ui.button("Save", on_click=_save).props("color=primary")
        ui.button("Use system default", on_click=_clear).props("flat")


def _cpu_speed_editor(settings: Settings) -> None:
    """`/settings` control for ``clip.speed``: how much CPU indexing + CLIP may
    use. Applies from the next pass (the running one keeps its budget)."""
    ccfg = settings.config.clip
    slow_w = default_workers("slow")

    ui.label(
        "Indexing gathers your photos, stacks photos together that are the same, and "
        "uses machine learning to determine the contents of the photos without using "
        "any internet resources or uploading your photos anywhere."
    ).classes("text-xs sp-faint max-w-xl")
    ui.label(
        f"By default, indexing runs slowly using about {slow_w} cpu core(s), however "
        f"if you'd like to run the index quickly select fast and it will use all "
        f"available CPU cores. This may cause your computer to run slowly."
    ).classes("text-xs sp-faint max-w-xl")

    mode = ui.toggle(
        {"slow": "Slow (background)", "fast": "Fast"},
        value=ccfg.speed if ccfg.speed in ("slow", "fast") else "slow",
    ).props("no-caps dense")
    warning = ui.label("Fast uses all CPU cores and may slow down your computer.").classes(
        "text-amber-600 text-sm"
    )
    warning.set_visibility(ccfg.speed == "fast")

    def _save_speed(e: Any) -> None:
        ccfg.speed = "fast" if e.value == "fast" else "slow"
        settings.config.dump(settings.paths.config_file)
        warning.set_visibility(ccfg.speed == "fast")
        ui.notify(f"indexing speed: {ccfg.speed} (applies from the next pass)")

    mode.on_value_change(_save_speed)


def _deletion_editor(settings: Settings, *, autosave: bool = False) -> None:
    """`/settings` control for the "Delete photos" action (CLAUDE.md constraint
    #1, exception 2). Only the stack scope is user-configurable."""
    cfg = settings.config.deletion
    ui.label(
        "The bulk toolbar's Delete and the photo page's “Delete photo” move the "
        "files into the app's trash on this computer (see Settings → Recently deleted) and "
        "remove them from the library. When stacking is on, choose whether a delete "
        "takes only the shown photo or the whole stack."
    ).classes("text-xs sp-faint max-w-xl")

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

    def _store() -> None:
        cfg.stack_scope = "whole_stack" if scope.value == "whole_stack" else "selected"
        settings.config.dump(settings.paths.config_file)

    if autosave:
        scope.on_value_change(lambda _: _store())
        return

    def _save() -> None:
        _store()
        ui.notify("saved")

    ui.button("Save", on_click=_save).props("color=primary")


def _trash_panel(settings: Settings, engine: Engine) -> None:
    """`/settings` "Recently deleted": the managed trash batches on this
    machine, each with "Put back", plus "Move all to system Trash" (macOS /
    Windows) -- after which only the OS Trash's own Put Back can recover them."""
    client = ui.context.client
    ui.label(
        f"Deleted photos wait here, on this computer only, until you move them to the "
        f"system Trash. Put back returns the files to their original folders with their "
        f"ratings and tags. Folder: {settings.paths.local / 'trash'}"
    ).classes("text-xs sp-faint max-w-xl")
    box = ui.column().classes("w-full gap-2")

    def _render() -> None:
        box.clear()
        batches = list_batches(settings.paths.local)
        with box:
            if not batches:
                ui.label("Nothing here.").classes("text-sm sp-faint")
                return
            for b in batches:
                _trash_batch_row(b)
            if system_trash_available():
                ui.button(
                    "Move all to system Trash", icon="delete_sweep", on_click=_confirm_flush
                ).props("flat color=negative no-caps")

    def _trash_batch_row(b: TrashBatch) -> None:
        hashes = b.hashes
        when = _fmt_trash_time(b.created_at)
        with ui.card().classes("w-full max-w-3xl p-2 gap-1"):
            with ui.row().classes("w-full items-center justify-between"):
                label = f"{when} · {len(hashes)} photo(s), {len(b.items)} file(s)"
                if b.reason == "tombstone":
                    label += " · deleted on another computer"
                ui.label(label).classes("text-sm")
                ui.button(
                    "Put back", icon="restore_from_trash", on_click=lambda b=b: _put_back(b)
                ).props("dense flat no-caps color=primary")
            with ui.row().classes("gap-1"):
                for h in hashes[:12]:
                    if b.thumb(h) is not None:
                        ui.image(f"{_TRASH_THUMB_URL}/{b.id}/{h}").classes(
                            "w-16 h-16 rounded"
                        ).props("fit=cover")
                if len(hashes) > 12:
                    ui.label(f"+{len(hashes) - 12}").classes("text-xs sp-faint self-center")
            names = ", ".join(Path(str(i["original"])).name for i in b.items[:4])
            more = f" +{len(b.items) - 4}" if len(b.items) > 4 else ""
            ui.label(names + more).classes("text-xs sp-faint")

    async def _put_back(b: TrashBatch) -> None:
        result = await nicegui_run.io_bound(put_back_batch, settings, engine, b.id)
        if result is None:
            return
        if State.last_delete and State.last_delete[0] == b.id:
            State.last_delete = None
        with client:
            ui.notify(result.summary(), type="warning" if result.failed else None)
            _render()

    def _confirm_flush() -> None:
        with ui.dialog() as dialog, ui.card().classes("gap-2"):
            ui.label("Move everything here to the system Trash?").classes("font-medium")
            ui.label(
                "The app can no longer put these photos back afterwards — use the "
                "system Trash's own Put Back instead. Nothing is permanently deleted."
            ).classes("text-xs sp-faint max-w-sm")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")

                async def _go() -> None:
                    res = await nicegui_run.io_bound(flush_to_system_trash, settings.paths.local)
                    State.last_delete = None
                    with client:
                        if res is not None:
                            msg = f"moved {res.moved} file(s) to the system Trash"
                            if res.failed:
                                msg += f"; {len(res.failed)} could not be moved"
                            ui.notify(msg)
                        _render()
                        dialog.close()

                ui.button("Move to Trash", on_click=_go).props("color=negative")
        dialog.open()

    _render()


def _fmt_trash_time(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if dt.tzinfo is not None:
        dt = dt.astimezone()  # local time for display
    return dt.strftime("%Y-%m-%d %H:%M")


def _app_data_editor(settings: Settings) -> None:
    """`/settings` control for the shared folder -- where the database,
    thumbnails and CLIP embeddings are kept and synced (CLAUDE.md "App-data
    layout"). Saving writes the pointer file in the user's home directory
    (:mod:`sortandpost.launcher_pref`) and restarts: a clean shutdown (final
    publish, lock release) and a re-launch on the new folder. A genuine
    process/shell ``APP_DATA`` (Docker, or ``APP_DATA=... command``) always
    wins and can't be changed from here."""
    source = settings.env.paths_source()
    paths = settings.paths

    ui.label(
        "Where your library's database, thumbnails and search index are kept. Keep it "
        "in a cloud drive and pick the same folder on your other computers."
    ).classes("text-xs sp-faint max-w-xl")
    with ui.grid(columns="auto 1fr").classes("gap-x-4 gap-y-1 text-sm"):
        ui.label("Shared folder").classes("sp-faint")
        ui.label(str(paths.sync)).classes("break-all")
        ui.label("This computer only").classes("sp-faint")
        ui.label(str(paths.local)).classes("break-all")

    if source == "env":
        ui.label(
            "⚠ set for this launch by the APP_DATA environment variable (Docker, or "
            "APP_DATA=... on the command line) — change it there."
        ).classes("text-xs text-amber-600 dark:text-amber-400 max-w-xl")
        return
    if not paths.is_split:
        ui.label(
            "This setup keeps this computer's private files inside the data folder "
            "(local/). If that folder is synced, pick its sync/ folder below to move "
            "them onto this computer only — you'll confirm your photo folders once."
        ).classes("text-xs sp-faint max-w-xl")

    def _use(ins: FolderInspection) -> None:
        assert ins.sync_dir is not None
        target = ins.sync_dir
        if paths.is_split and target.resolve() == paths.sync.resolve():
            ui.notify("that's the folder this computer already uses")
            return

        def _go() -> None:
            dlg.close()
            target.mkdir(parents=True, exist_ok=True)
            remember_sync_dir(target)
            _restart_app("/", "Switching library…")

        with ui.dialog() as dlg, ui.card().classes("w-[32rem] max-w-full gap-2"):
            ui.label("Restart on this folder?").classes("font-semibold")
            ui.label(str(target)).classes("font-mono text-sm break-all")
            ui.label(ins.message).classes("text-sm sp-muted")
            ui.label("Your current library stays where it is; it isn't copied or moved.").classes(
                "text-xs sp-faint"
            )
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dlg.close).props("flat")
                ui.button("Save & restart", on_click=_go).props("color=primary")
        dlg.open()

    with ui.expansion("Change folder", icon="swap_horiz").classes("w-full"):
        _shared_folder_picker(str(paths.sync), on_use=_use, button="Save & restart")


def _clip_model_editor(settings: Settings) -> None:
    """`/settings` control to check whether the configured CLIP weights are
    fully cached under ``local/models/`` and, if not, force a download.

    Separate from the always-on indexer's own lazy load so a user who ran the
    app offline (or with ``HF_HUB_OFFLINE`` set, as the documented offline dev
    run does) can tell whether the weights actually finished downloading, and
    retry on demand rather than guessing from indexer error logs.
    """
    ccfg = settings.config.clip
    client = ui.context.client  # a real download outlasts the page; see _download

    ui.label(
        f"{ccfg.model} / {ccfg.pretrained} — cached under {settings.paths.models_dir}"
    ).classes("text-xs sp-faint max-w-xl")

    status = ui.label("not checked yet").classes("text-sm")

    async def _check() -> None:
        status.set_text("checking…")
        result = await nicegui_run.io_bound(clip_check_cached, ccfg, settings.paths.models_dir)
        ok, detail = result if result is not None else (False, "cancelled")
        with client:  # the awaited gap can invalidate the ambient slot
            status.set_text(
                "✓ fully cached — ready to run offline" if ok else f"✗ not fully cached: {detail}"
            )

    async def _download() -> None:
        status.set_text("downloading…")
        ui.notify("downloading CLIP model files — this can take a few minutes…")
        result = await nicegui_run.io_bound(clip_ensure_downloaded, ccfg, settings.paths.models_dir)
        ok, detail = result if result is not None else (False, "cancelled")
        with client:
            if ok:
                status.set_text("✓ fully cached — ready to run offline")
                ui.notify("CLIP model files downloaded")
            else:
                status.set_text(f"✗ download failed: {detail}")
                ui.notify(f"download failed: {detail}", type="negative")

    with ui.row().classes("gap-2"):
        ui.button("Check if downloaded", on_click=_check).props("flat dense")
        ui.button("Download / verify CLIP model", on_click=_download).props("color=primary")


async def _browse_folder(target: ui.input) -> None:
    """Folder picker that fills ``target`` from a real directory listing on this
    computer instead of a typed path. Under Docker it goes through the host
    file-manager helper agent (:mod:`sortandpost.helper`) straight from the
    browser via ``window.__spHelper`` -- the server never talks to it; on a
    native run it lists this process's own filesystem (:func:`_list_local`),
    starting from home, the cloud-sync drives and mounted volumes. Callers
    should only offer "Browse…" when :func:`_app_browse_available`."""
    helper = _current_helper()

    async def _list(path: str | None) -> dict[str, Any]:
        if helper is not None:
            js = (
                "(async () => { const h = window.__spHelper; if (!h) return {entries: []};"
                " const u = new URL('http://127.0.0.1:' + h.port + '/listdir');"
                f" const p = {json.dumps(path)}; if (p) u.searchParams.set('path', p);"
                " const r = await fetch(u, {headers: {'X-SP-Token': h.token}});"
                " return await r.json(); })()"
            )
            try:
                data = await ui.run_javascript(js, timeout=6.0)
            except Exception:  # helper stopped mid-browse
                data = None
        else:
            data = await nicegui_run.io_bound(
                _list_local, Path(path) if path else None, include_files=False, folders=True
            )
        return data if isinstance(data, dict) else {"entries": [], "parent": None}

    with (
        ui.dialog() as dlg,
        ui.card().classes("flex h-[70vh] w-[42rem] max-w-full flex-col gap-2"),
    ):
        crumb = ui.label("…").classes("truncate text-xs sp-faint")
        listing = ui.column().classes("w-full flex-1 gap-0 overflow-y-auto")
        state = {"path": (target.value or "").strip() or None}

        async def _load(path: str | None) -> None:
            data = await _list(path)
            if path and not data.get("entries") and data.get("path") is None:
                path = None  # a typed path that doesn't exist: start from the top
                data = await _list(None)
            state["path"] = path
            crumb.text = path or "Pick a starting point"
            listing.clear()
            with listing:
                if data.get("library"):
                    ui.label("✓ This folder is a sortandpost library").classes(
                        "text-sm text-primary px-2 pb-1"
                    )
                if data.get("parent"):
                    ui.button("⬆  parent", on_click=lambda p=data["parent"]: _load(p)).props(
                        "flat dense align=left no-caps"
                    ).classes("w-full justify-start")
                for entry in data.get("entries", []):
                    ui.button(
                        entry["name"],
                        icon="folder",
                        on_click=lambda p=entry["path"]: _load(p),
                    ).props("flat dense align=left no-caps").classes("w-full justify-start")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dlg.close).props("flat")

            def _use() -> None:
                if state["path"]:
                    target.value = state["path"]
                dlg.close()

            ui.button("Use this folder", on_click=_use).props("color=primary")
        await _load(state["path"])
    dlg.open()


def _list_local(
    path: Path | None, *, include_files: bool, folders: bool = False
) -> dict[str, object]:
    """Directories (and, when *include_files*, files) under *path* on *this
    process's own* filesystem. Only meaningful on a native run -- a
    container can't see the host, which is what the helper agent's
    ``/listdir`` is for. Deliberately duplicates
    ``tools/sortandpost_helper.py``'s ``list_dir``: that script must stay
    stdlib-only and importable without the ``sortandpost`` package
    installed, so it can't import this module either."""
    if path is None and folders:
        # picking a folder (library / shared data): home, the cloud-sync
        # drives, and mounted volumes / drives
        starts: list[Path] = [Path.home(), *cloud_folder_candidates()]
        if sys.platform == "darwin":
            volumes = Path("/Volumes")
            if volumes.is_dir():
                starts += sorted(v for v in volumes.iterdir() if v.is_dir())
        elif sys.platform.startswith("win"):
            starts += [Path(f"{c}:\\") for c in "CDEFGHIJ" if Path(f"{c}:\\").exists()]
        else:
            starts += [Path("/media"), Path("/mnt"), Path("/")]
        return {
            "path": None,
            "parent": None,
            "entries": [
                {"name": _start_label(p), "path": str(p), "dir": True}
                for p in dict.fromkeys(starts)
                if p.is_dir()
            ],
        }
    if path is None:
        starts = [Path.home()]
        if sys.platform == "darwin":
            apps = Path("/Applications")
            if apps.is_dir():
                starts.append(apps)
        elif sys.platform.startswith("win"):
            for letter in "CDEFGH":
                drive = Path(f"{letter}:\\")
                if drive.exists():
                    starts.append(drive)
            for env_var in ("ProgramFiles", "ProgramFiles(x86)"):
                pf = os.environ.get(env_var)
                if pf and Path(pf).is_dir():
                    starts.append(Path(pf))
        else:
            starts.append(Path("/"))
        return {
            "path": None,
            "parent": None,
            "entries": [
                {"name": str(p), "path": str(p), "dir": True} for p in dict.fromkeys(starts)
            ],
        }
    entries: list[dict[str, object]] = []
    try:
        for child in sorted(path.iterdir(), key=lambda c: c.name.casefold()):
            if child.name.startswith("."):
                continue
            try:
                is_dir = child.is_dir()
            except OSError:
                continue
            if is_dir:
                entries.append({"name": child.name, "path": str(child), "dir": True})
            elif include_files:
                entries.append({"name": child.name, "path": str(child), "dir": False})
    except (OSError, PermissionError):
        if not path.is_dir():
            return {"path": None, "parent": None, "entries": []}
        entries = []
    parent = str(path.parent) if path.parent != path else None
    library = folders and ((path / "config.yaml").is_file() or (path / "sortandpost.db").is_file())
    return {"path": str(path), "parent": parent, "entries": entries, "library": library}


def _start_label(p: Path) -> str:
    """A friendly name for a folder-picker starting point."""
    if p == Path.home():
        return f"Home ({p})"
    if p.name == "com~apple~CloudDocs":
        return "iCloud Drive"
    if p.parent.name == "CloudStorage":
        return p.name.replace("-", " · ", 1)
    return str(p)


def _app_browse_available() -> bool:
    """Can :func:`_browse_app` show a real listing? Either the host helper
    agent is connected (Docker), or this process itself runs natively on the
    user's machine and can list its own filesystem directly."""
    return _current_helper() is not None or not running_in_docker()


async def _browse_app(target: ui.input) -> None:
    """Picker for the "Open with" app: like :func:`_browse_folder` but also
    lists files (an app is often a single file -- an ``.exe`` on Windows, an
    executable or ``.desktop`` on Linux) and treats a macOS ``.app`` bundle as
    a selectable leaf rather than a folder to open. Goes through the host
    helper agent's ``/listdir`` under Docker, same as :func:`_browse_folder`;
    otherwise lists this (native) process's own filesystem directly -- no
    external helper needed on a native run."""
    helper = _current_helper()

    async def _list(path: str | None) -> dict[str, Any]:
        if helper is not None:
            js = (
                "(async () => { const h = window.__spHelper; if (!h) return {entries: []};"
                " const u = new URL('http://127.0.0.1:' + h.port + '/listdir');"
                f" const p = {json.dumps(path)}; if (p) u.searchParams.set('path', p);"
                " u.searchParams.set('files', '1');"
                " const r = await fetch(u, {headers: {'X-SP-Token': h.token}});"
                " return await r.json(); })()"
            )
            try:
                data = await ui.run_javascript(js, timeout=6.0)
            except Exception:  # helper stopped mid-browse
                data = None
        else:
            data = await nicegui_run.io_bound(
                _list_local, Path(path) if path else None, include_files=True
            )
        return data if isinstance(data, dict) else {"entries": [], "parent": None}

    with (
        ui.dialog() as dlg,
        ui.card().classes("flex h-[70vh] w-[42rem] max-w-full flex-col gap-2"),
    ):
        crumb = ui.label("…").classes("truncate text-xs sp-faint")
        listing = ui.column().classes("w-full flex-1 gap-0 overflow-y-auto")

        def _pick(path: str) -> None:
            target.value = path
            dlg.close()

        async def _load(path: str | None) -> None:
            data = await _list(path)
            crumb.text = path or "Pick a starting point"
            listing.clear()
            with listing:
                if data.get("parent"):
                    ui.button("⬆  parent", on_click=lambda p=data["parent"]: _load(p)).props(
                        "flat dense align=left"
                    ).classes("w-full justify-start")
                for entry in data.get("entries", []):
                    name = str(entry["name"])
                    is_app = bool(entry.get("dir")) and name.endswith(".app")
                    if entry.get("dir") and not is_app:
                        ui.button(
                            name,
                            icon="folder",
                            on_click=lambda p=entry["path"]: _load(p),
                        ).props("flat dense align=left").classes("w-full justify-start")
                    else:
                        ui.button(
                            name,
                            icon="apps" if is_app else "insert_drive_file",
                            on_click=lambda p=entry["path"]: _pick(p),
                        ).props("flat dense align=left").classes("w-full justify-start")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dlg.close).props("flat")
        await _load((target.value or "").strip() or None)
    dlg.open()


def _library_roots_editor(
    settings: Settings,
    engine: Engine,
    *,
    rescan: bool = True,
    suggestions: list[LibraryRoot] | None = None,
) -> None:
    """`/settings` editor for this device's library folders
    (``sync/devices/<name>/library.json`` -- see :mod:`sortandpost.library_roots`).

    A container cannot see the host filesystem or change its own bind-mounts, so
    "add a folder" here just records the entry and shows the ``docker-compose.yml``
    line to add; the folder goes live after ``docker compose up -d``. When the
    host helper agent is running, "Add folder" also gets a real native browser.
    """
    roots: list[LibraryRoot] = list(settings.library_roots)
    helper = read_helper_info(settings.paths)
    writable = State.write_mode == WriteMode.writable
    in_docker = running_in_docker()
    client = ui.context.client  # a real rescan outlasts the page; see _save_rescan

    ui.label(
        "Folders scanned for photos — specific to this computer. Under Docker a "
        "new folder only becomes active after you add its line to "
        "docker-compose.yml and run  docker compose up -d."
        if in_docker
        else "Folders scanned for photos — specific to this computer."
    ).classes("text-xs sp-faint max-w-xl")

    rows_box = ui.column().classes("w-full gap-1")

    def _persist() -> None:
        save_roots(settings.paths, settings.device_id, settings.hostname, roots)
        settings.library_roots = list(roots)

    def _drop(container: str) -> None:
        roots[:] = [r for r in roots if r.container != container]
        _persist()
        _render()

    def _adopt(root: LibraryRoot) -> None:
        roots.append(root)
        _persist()
        _render()

    def _render() -> None:
        rows_box.clear()
        with rows_box:
            have = {r.container for r in roots}
            offered = [s for s in suggestions or [] if s.container not in have]
            if offered:
                ui.label("Folders your other computer uses that also exist here:").classes(
                    "text-xs sp-muted"
                )
                for sug in offered:
                    ui.button(
                        sug.label or sug.host,
                        icon="add",
                        on_click=lambda _=None, r=sug: _adopt(r),
                    ).props("flat dense no-caps align=left").classes("justify-start")
            if not roots:
                ui.label("No folders yet — add one below.").classes("text-xs sp-faint")
            for r in roots:
                ok = Path(r.container).is_dir()
                if ok:
                    tip = "scanned"
                elif in_docker:
                    tip = "not mounted — add the compose line and run docker compose up -d"
                else:
                    tip = "folder not found on this computer"
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    ui.icon("check_circle" if ok else "error_outline").classes(
                        "text-emerald-500" if ok else "text-amber-500"
                    ).tooltip(tip)
                    with ui.column().classes("min-w-0 flex-1 gap-0"):
                        ui.label(r.label or r.host).classes("truncate text-sm")
                        sub = r.host if r.host == r.container else f"{r.host}  →  {r.container}"
                        if r.label or sub != r.host:
                            ui.label(sub).classes("truncate text-xs sp-faint")
                    ui.button(icon="close", on_click=lambda _=None, c=r.container: _drop(c)).props(
                        "flat dense round size=sm"
                    ).tooltip("remove")

    def _next_mount_point() -> str:
        used = {r.container for r in roots}
        if "/library" not in used:
            return "/library"
        i = 1
        while f"/library/{i}" in used:
            i += 1
        return f"/library/{i}"

    def _add_dialog() -> None:
        with ui.dialog() as dialog, ui.card().classes("w-[40rem] max-w-full gap-2"):
            ui.label("Add a library folder").classes("font-bold")
            with ui.row().classes("w-full items-end gap-2 no-wrap"):
                host_in = (
                    ui.input("Folder on this computer (host path)").props("dense").classes("flex-1")
                )
                if helper is not None or not in_docker:
                    ui.button(
                        "Browse…", icon="folder_open", on_click=lambda: _browse_folder(host_in)
                    ).props("flat dense")
            ui.label(
                "e.g.  ~/Library/CloudStorage/OneDrive-Personal/Photos  ·  "
                "~/Dropbox/Photos  ·  C:\\Users\\you\\OneDrive\\Photos"
            ).classes("text-xs sp-faint")
            cont_in = (
                ui.input("Mount point inside the container", value=_next_mount_point())
                .props("dense")
                .classes("w-full")
            )
            cont_in.set_visibility(in_docker)  # native: the folder is scanned directly
            label_in = ui.input("Label (optional)").props("dense").classes("w-full")
            snippet = ui.code("", language="yaml").classes("w-full text-xs")
            snippet.set_visibility(in_docker)

            def _entry() -> LibraryRoot | None:
                host = clean_path(host_in.value or "")
                if not host:
                    return None
                cont = clean_path(cont_in.value or "") if in_docker else ""
                return LibraryRoot(
                    container=cont or host, host=host, label=(label_in.value or "").strip()
                )

            def _refresh() -> None:
                r = _entry()
                if r is None or not in_docker:
                    snippet.content = "# fill in the folder path" if r is None else ""
                    return
                snippet.content = (
                    "# docker-compose.yml  →  services: app: volumes:\n"
                    f"      {compose_volume_line(r)}\n"
                    "# then:  docker compose up -d"
                )

            host_in.on_value_change(lambda _: _refresh())
            cont_in.on_value_change(lambda _: _refresh())
            _refresh()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")

                def _save_entry() -> None:
                    r = _entry()
                    if r is None:
                        ui.notify("enter the folder path", type="warning")
                        return
                    roots[:] = [x for x in roots if x.container != r.container]
                    roots.append(r)
                    _persist()
                    _render()
                    dialog.close()
                    ui.notify(
                        "saved — add the compose line, run docker compose up -d, then rescan"
                        if in_docker
                        else "saved — Save & rescan to index it"
                        if rescan
                        else "added"
                    )

                ui.button("Save", on_click=_save_entry).props("color=primary")
        dialog.open()

    with ui.row().classes("items-center gap-2"):
        ui.button("Add folder", icon="add", on_click=_add_dialog).props("flat dense")
        if helper is not None:
            ui.label("🖥 helper connected — Browse enabled").classes("text-xs text-emerald-600")

    if writable and rescan:

        async def _save_rescan() -> None:
            _persist()
            ui.notify("saved — rescanning…")
            result = await nicegui_run.io_bound(_run_index_pass, settings, engine)
            with client:  # the awaited gap can invalidate the ambient slot
                ui.notify(result)

        ui.button("Save & rescan", on_click=_save_rescan).props("color=primary")

    _render()


def _exclude_editor(settings: Settings, engine: Engine, *, autosave: bool = False) -> None:
    """`/settings` editor for ``indexer.exclude_dir_names``. Save persists to
    ``config.yaml``, updates the live config, and kicks a rescan -- the sweep
    then drops photos from newly-excluded folders (and revives un-excluded ones,
    hard constraint #8: nothing is purged)."""
    names: list[str] = list(settings.config.indexer.exclude_dir_names)
    client = ui.context.client  # a real rescan outlasts the page; see _save

    ui.label(
        "Folder names skipped while scanning — case-insensitive, matched at any "
        "depth. Photos already indexed from an excluded folder drop out on the "
        "next scan; removing an entry brings them back."
    ).classes("text-xs sp-faint max-w-xl")

    chips = ui.row().classes("items-center gap-2 flex-wrap min-h-8")

    def render() -> None:
        chips.clear()
        with chips:
            if not names:
                ui.label("none").classes("text-xs sp-muted")
            for name in names:
                ui.chip(name, removable=True).on_value_change(lambda _, n=name: _drop(n))

    def _changed() -> None:
        render()
        if autosave:  # the welcome wizard: nothing is indexed yet, so no rescan
            settings.config.indexer.exclude_dir_names = list(names)
            settings.config.dump(settings.paths.config_file)

    def _drop(name: str) -> None:
        if name in names:
            names.remove(name)
            _changed()

    def _add() -> None:
        value = (box.value or "").strip()
        if value and value.casefold() not in {n.casefold() for n in names}:
            names.append(value)
            _changed()
        box.set_value("")

    with ui.row().classes("items-center gap-2"):
        box = ui.input(placeholder="folder name, e.g. RAW").props("dense").classes("w-52")
        box.on("keydown.enter", _add)
        ui.button("Add", on_click=_add).props("flat dense")

    if autosave:
        render()
        return

    async def _save() -> None:
        settings.config.indexer.exclude_dir_names = list(names)
        settings.config.dump(settings.paths.config_file)
        ui.notify("saved — rescanning…")
        result = await nicegui_run.io_bound(_run_index_pass, settings, engine)
        with client:  # the awaited gap can invalidate the ambient slot
            ui.notify(result)

    ui.button("Save & rescan", on_click=_save).props("color=primary")
    render()


def _online_only_editor(settings: Settings, engine: Engine, cloud_n: int) -> None:
    """`/settings` editor for cloud "online-only" placeholder files. Toggles
    ``indexer.online_only`` (``skip`` / ``download``) in ``config.yaml`` and
    offers a one-shot "Download & index" for the files already recorded."""
    icfg = settings.config.indexer
    client = ui.context.client  # a real hydrate+rescan outlasts the page; see _download_now

    ui.label(
        "Cloud sync tools (OneDrive, Google Drive, Dropbox, iCloud) can leave "
        "placeholder stubs whose real bytes download only when opened. “Skip” "
        "records them without downloading; “Download” hydrates every one the "
        "scanner meets. Either way you can pull the recorded ones now."
    ).classes("text-xs sp-faint max-w-xl")

    mode = ui.toggle(
        {"skip": "Skip (don't download)", "download": "Download while scanning"},
        value=icfg.online_only if icfg.online_only in ("skip", "download") else "skip",
    ).props("no-caps dense")

    def _save_mode(e: Any) -> None:
        icfg.online_only = e.value or "skip"
        settings.config.dump(settings.paths.config_file)
        ui.notify(f"online-only mode: {icfg.online_only}")

    mode.on_value_change(_save_mode)

    status = ui.label(
        f"{cloud_n} file(s) currently recorded as online-only." if cloud_n else "None recorded."
    ).classes("text-xs sp-faint")

    async def _download_now() -> None:
        ui.notify("downloading online-only files…")
        result = await nicegui_run.io_bound(_run_hydrate_pass, settings, engine)
        with client:  # the awaited gap can invalidate the ambient slot
            ui.notify(result)
            status.set_text("Done — reload the library to see them.")

    btn = ui.button(
        "Download & index recorded files", icon="cloud_download", on_click=_download_now
    )
    btn.set_visibility(cloud_n > 0)


def _reload_as_photos_arrive(engine: Engine) -> None:
    """While the first scan is filling an empty (or not yet screen-full)
    library, reload the page whenever more photos have been indexed, so
    thumbnails show up without a manual refresh. Stops once the indexer goes
    idle with nothing new; a full page then continues via infinite scroll."""
    with Session(engine) as session:
        baseline = photo_count(session)
    idle_ticks = {"n": 0}

    def _tick() -> None:
        with Session(engine) as session:
            now = photo_count(session)
        if now > baseline:
            timer.deactivate()
            ui.navigate.reload()
            return
        idle_ticks["n"] = 0 if PROGRESS.snapshot().active else idle_ticks["n"] + 1
        if idle_ticks["n"] >= 3:  # indexer idle and nothing new: stop polling
            timer.deactivate()

    timer = ui.timer(4.0, _tick)


def _empty_state(icon: str, title: str, hint: str = "") -> None:
    """A centred placeholder for the "nothing here yet" pages."""
    with ui.element("div").classes("sp-empty"):
        ui.icon(icon, size="2.75rem").classes("sp-faint")
        ui.label(title).classes("sp-empty-title sp-display")
        if hint:
            ui.label(hint).classes("text-sm")


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
        caption = ui.label(_stars_caption(stars)).classes("text-xs sp-muted")
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


_STAR_CHOICES: dict[int, str] = {
    UNRATED: "Unrated",
    1: "1★",
    2: "2★",
    3: "3★",
    4: "4★",
    5: "5★",
}


def _with_selected(options: dict[Any, str], selected: Any) -> dict[Any, str]:
    """*options* plus any selected value it lacks (e.g. from an old saved
    search) -- ``ui.select`` rejects a value that isn't an option."""
    values = selected if isinstance(selected, list | tuple | set | frozenset) else [selected]
    extra = {v: str(v) for v in values if v not in options and v is not None}
    return {**options, **extra}


def _multi_select(
    options: dict[Any, str],
    value: list[Any],
    label: str,
    on_apply: Callable[[list[Any]], None],
    *,
    classes: str = "w-40",
    with_input: bool = False,
) -> ui.select:
    """A chip multi-select that applies each pick right away."""
    sel = (
        ui.select(
            _with_selected(options, value),
            value=value,
            label=label,
            multiple=True,
            with_input=with_input,
            clearable=True,
        )
        .props("dense outlined options-dense use-chips")
        .classes(classes)
    )
    sel.on_value_change(lambda _: on_apply(list(sel.value or [])))
    return sel


def _with_flag(lq: LibraryQuery, key: str, on: bool) -> LibraryQuery:
    """*lq* with one of its boolean filters (gps / dups / mark / missing) set."""
    flags: dict[str, bool] = {k: getattr(lq, k) for k in ("gps", "dups", "mark", "missing")}
    flags[key] = on
    return replace(
        lq, gps=flags["gps"], dups=flags["dups"], mark=flags["mark"], missing=flags["missing"]
    )


@contextmanager
def _filter_button(icon: str, tip: str, active: int, *, error: bool = False) -> Iterator[None]:
    """An icon-only filter-bar button whose popover the ``with`` body fills.
    Tinted + a count badge while its filter is on; red when it can't be read."""
    color = "negative" if error else ("primary" if active else None)
    btn = ui.button(icon=icon, color=color).props("flat dense round").classes("sp-toolbtn")
    with btn:
        _tip_above(tip)
        if active:
            ui.badge(str(active)).props("floating rounded color=primary").classes("sp-toolbadge")
        with ui.menu(), ui.column().classes("gap-1 p-2 min-w-56"):
            ui.label(tip).classes("text-xs sp-faint")
            yield


def _tip_above(text: str) -> None:
    """A tooltip above its element, so it never covers the menu that opens below."""
    ui.tooltip(text).props("anchor='top middle' self='bottom middle' :offset='[0, 6]'")


_GROUP_CHOICES: list[tuple[str, str, str]] = [  # (group param, label, icon)
    ("", "Month", "calendar_view_month"),  # DEFAULT_GROUP: no URL param
    ("day", "Day", "today"),
    ("folder", "Folder", "folder"),
    ("camera", "Camera", "photo_camera"),
    (FLAT, "Flat", "grid_view"),
]
_FOLDER_SORTS: list[tuple[str, str]] = [
    ("newest", "Newest first"),
    ("oldest", "Oldest first"),
    ("az", "A-Z"),
    ("za", "Z-A"),
]


def _group_by_control(lq: LibraryQuery, on_collapse_all: Callable[[bool], None]) -> None:
    """The toolbar's group-by menu button (+ collapse / expand all while grouped)."""
    current = "" if lq.group in ("", DEFAULT_GROUP) else lq.group
    _, label, icon = next((c for c in _GROUP_CHOICES if c[0] == current), _GROUP_CHOICES[0])

    def _to(group: str, folder_sort: str = "") -> None:
        ui.navigate.to(replace(lq, group=group, folder_sort=folder_sort).url())

    with ui.row().classes("sp-toolgroup items-center gap-0 no-wrap"):
        btn = ui.button(label, icon=icon, color=None)
        btn.props("flat dense no-caps icon-right=arrow_drop_down").classes("sp-toolbtn px-2")
        if lq.q:  # relevance order: sections would scatter the best matches
            btn.props("disable")
            with btn:
                _tip_above("Best match first — grouping is off for a text search")
            return
        with btn, ui.menu().classes("min-w-48"):
            for key, text, ic in _GROUP_CHOICES:
                with (
                    ui.menu_item(on_click=lambda k=key: _to(k)),
                    ui.row().classes("items-center gap-2 no-wrap w-full"),
                ):
                    ui.icon(ic).classes("text-lg sp-muted")
                    ui.label(text).classes("flex-1")
                    if key == current:
                        ui.icon("check").classes("text-primary")
            if current == "folder":
                fsort = parse_folder_sort(lq.folder_sort)
                ui.separator()
                ui.label("Sort sections").classes("text-xs sp-faint px-4 pt-1")
                for key, text in _FOLDER_SORTS:
                    with (
                        ui.menu_item(on_click=lambda k=key: _to("folder", k)),
                        ui.row().classes("items-center gap-2 no-wrap w-full"),
                    ):
                        ui.label(text).classes("flex-1 pl-7")
                        if key == fsort:
                            ui.icon("check").classes("text-primary")
        if lq.mode is not None:
            for ic, collapse, tip in (
                ("unfold_less", True, "Collapse every section"),
                ("unfold_more", False, "Expand every section"),
            ):
                with (
                    ui.button(icon=ic, color=None, on_click=lambda c=collapse: on_collapse_all(c))
                    .props("flat dense round")
                    .classes("sp-toolbtn")
                ):
                    _tip_above(tip)


def _filter_chips(lq: LibraryQuery) -> None:
    """The active filters as removable chips; removing one drops just it."""
    for chip in lq.chips():
        ui.chip(
            chip.label,
            icon=chip.icon,
            removable=True,
            on_value_change=lambda e, q=chip.without: (
                ui.navigate.to(q.url()) if not e.value else None
            ),
        ).props("dense outline color=primary").classes("sp-filterchip")


def _filter_bar(
    settings: Settings,
    lq: LibraryQuery,
    data: _LibraryData,
    *,
    on_collapse_all: Callable[[bool], None],
) -> ui.input:
    """The library's search + filter toolbar: a search box, then icon buttons
    whose popovers hold each filter, the group-by menu, saved searches and
    clear. Every change navigates to the new view's URL -- all state lives
    there (see :mod:`sortandpost.library_view`) -- keeping the group-by view as
    it is. Returns the search box (``/`` focuses it)."""
    sent = [False]

    def _go(new: LibraryQuery) -> None:
        if new == lq or sent[0]:  # blur after Enter, a menu closing unchanged, ...
            return
        sent[0] = True
        ui.navigate.to(new.url())

    def _keep_view(new: LibraryQuery) -> LibraryQuery:
        return replace(new, group=lq.group, folder_sort=lq.folder_sort)

    with ui.row().classes("w-full items-center gap-2 px-4 pt-3 flex-wrap"):
        box = (
            ui.input(placeholder="Search photos — e.g. dog on the beach", value=lq.q)
            .props("dense outlined clearable")
            .classes("min-w-64 flex-1")
        )
        with box.add_slot("prepend"):
            ui.icon("search")
        box.on("keydown.enter", lambda: _go(replace(lq, q=(box.value or "").strip())))
        box.on("clear", lambda: _go(replace(lq, q="")))

        with ui.row().classes("sp-toolgroup items-center gap-0 no-wrap"):
            with (
                _filter_button("star", "Rating", len(lq.stars)),
                ui.row().classes("gap-1 no-wrap"),
            ):
                for value, text in _STAR_CHOICES.items():
                    on = value in lq.stars
                    ui.button(
                        text,
                        color="primary" if on else None,
                        on_click=lambda v=value: _go(replace(lq, stars=lq.stars ^ {v})),
                    ).props("dense no-caps unelevated" + ("" if on else " flat"))

            with _filter_button("photo_camera", "Camera", len(lq.cameras)):
                if not data.cameras:
                    ui.label("No camera info yet.").classes("text-xs sp-muted")
                for cam, n in data.cameras:
                    on = cam in lq.cameras
                    picked = tuple(c for c in lq.cameras if c != cam) if on else (*lq.cameras, cam)
                    ui.checkbox(
                        f"{cam or 'Unknown camera'} ({n})",
                        value=on,
                        on_change=lambda _, p=picked: _go(replace(lq, cameras=p)),
                    ).props("dense")

            # Dates: a range calendar (click the first day, then the last; click one
            # day twice for a single day). Picks apply at once.
            date_tip = dates_label(lq) or "Date taken"
            with _filter_button(
                "event", date_tip, int(bool(lq.dates)), error=bool(lq.date_error())
            ):
                if lq.date_error():
                    ui.label(lq.date_error() or "").classes("text-xs text-red-600 max-w-xs")
                picker = ui.date(value=dates_to_picker(lq)).props("range today-btn minimal")
                picker.on_value_change(lambda e: _go(replace(lq, dates=picker_to_dates(e.value))))
                if lq.dates:
                    ui.button(
                        "Any date", icon="event_busy", on_click=lambda: _go(replace(lq, dates=""))
                    ).props("flat dense no-caps")

            with _filter_button("folder", "Folder", int(bool(lq.folder))):
                folder = (
                    ui.select(
                        _with_selected(dict(data.folders), lq.folder or None),
                        value=lq.folder or None,
                        label="Folder",
                        with_input=True,
                        clearable=True,
                    )
                    .props("dense outlined options-dense")
                    .classes("w-72")
                )
                folder.on_value_change(lambda e: _go(replace(lq, folder=e.value or "")))

            with _filter_button("sell", "Subject / tag", len(lq.tags)):
                tag_opts = {
                    name: f"{name} · {facet}"
                    for facet, names in data.tags.items()
                    for name in names
                }
                _multi_select(
                    tag_opts,
                    list(lq.tags),
                    "Subject / tag",
                    lambda v: _go(replace(lq, tags=tuple(v))),
                    classes="w-72",
                    with_input=True,
                )
                if len(lq.tags) > 1:
                    ui.toggle({"any": "Match any", "all": "Match all"}, value=lq.tagmode).props(
                        "no-caps dense"
                    ).on_value_change(lambda e: _go(replace(lq, tagmode=e.value or "any")))

            extras = sum(bool(x) for x in (lq.gps, lq.dups, lq.missing, lq.mark, lq.posted))
            with _filter_button("tune", "More filters", extras):
                for key, text, hint in (
                    ("gps", "Has GPS location", ""),
                    (
                        "dups",
                        "Detected duplicates",
                        "Photos that look almost identical to another "
                        "(resized, re-exported, lightly cropped copies)",
                    ),
                    ("mark", "⚑ Marked for editing", ""),
                    (
                        "missing",
                        "Show missing files",
                        "Also show photos whose file can't be found right now "
                        "(moved, deleted, or on a disconnected drive)",
                    ),
                ):
                    ui.checkbox(
                        text,
                        value=bool(getattr(lq, key)),
                        on_change=lambda e, k=key: _go(_with_flag(lq, k, bool(e.value))),
                    ).props("dense")
                    if hint:
                        ui.label(hint).classes("text-xs sp-faint pl-7 max-w-xs")
                ui.label(f"Posted with {POSTING_SERVICE}").classes("text-xs sp-faint pt-2")
                ui.toggle({"": "Any", "1": "Posted", "0": "Not posted"}, value=lq.posted).props(
                    "dense no-caps"
                ).on_value_change(lambda e: _go(replace(lq, posted=str(e.value or ""))))

        _group_by_control(lq, on_collapse_all)

        with ui.row().classes("sp-toolgroup items-center gap-0 no-wrap"):
            _saved_searches_menu(settings, lq, _go, _keep_view)
            clear = (
                ui.button(
                    icon="filter_alt_off",
                    color=None,
                    on_click=lambda: _go(LibraryQuery(group=lq.group, folder_sort=lq.folder_sort)),
                )
                .props("flat dense round")
                .classes("sp-toolbtn")
            )
            with clear:
                _tip_above("Clear search and filters")
            if not lq.has_filters():
                clear.props("disable")
    return box


def _saved_searches_menu(
    settings: Settings,
    lq: LibraryQuery,
    go: Callable[[LibraryQuery], None],
    keep_view: Callable[[LibraryQuery], LibraryQuery],
) -> None:
    """The filter bar's "Saved" dropdown: open / delete saved searches, and save
    the current search + filters under a name."""
    btn = ui.button(icon="bookmarks", color=None).props("flat dense round").classes("sp-toolbtn")
    with btn:
        _tip_above("Saved searches")
    with btn:
        menu = ui.menu()

    def _save_dialog() -> None:
        menu.close()
        with ui.dialog() as dialog, ui.card().classes("gap-2"):
            ui.label("Save this search").classes("font-medium")
            ui.label(lq.summary()).classes("text-xs sp-muted")
            name_in = ui.input(placeholder="name").props("dense autofocus").classes("w-64")

            def _do() -> None:
                name = (name_in.value or "").strip()
                if not name:
                    return
                add_search(settings.paths, SavedSearch.of(name, lq))
                dialog.close()
                ui.notify(f'saved "{name}"')
                _paint()

            name_in.on("keydown.enter", _do)
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Save", on_click=_do).props("color=primary")
        dialog.open()

    def _delete(s: SavedSearch) -> None:
        remove_search(settings.paths, s.name)
        ui.notify(f'deleted "{s.name}"')
        _paint()

    def _paint() -> None:
        menu.clear()
        saved = load_searches(settings.paths)
        with menu, ui.column().classes("gap-0 py-1 min-w-56"):
            if not saved:
                ui.label("No saved searches yet.").classes("px-4 py-2 text-xs sp-muted")
            for s in saved:
                with ui.row().classes("w-full items-center no-wrap gap-0 pr-1"):
                    ui.menu_item(
                        s.name, on_click=lambda s=s: go(keep_view(s.library_query))
                    ).classes("flex-1").tooltip(s.summary())
                    ui.button(icon="close", on_click=lambda s=s: _delete(s)).props(
                        "flat dense round size=xs"
                    ).tooltip(f'delete "{s.name}"')
            ui.separator()
            item = ui.menu_item("Save current search…", on_click=_save_dialog, auto_close=False)
            if not lq.has_filters():
                item.props("disable")

    _paint()


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


def _helper_action_js(endpoint: str, host_path: str, app: str | None = None) -> str:
    """Client-side ``fetch`` of the host helper agent, returning ``{ok, error}``
    so the Python handler can ``ui.notify`` the outcome. ``app`` (``/open``
    only) picks a specific app instead of the OS default -- see
    ``sortandpost.prefs.get_open_with_app``."""
    body: dict[str, object] = {"path": host_path}
    if app:
        body["app"] = app
    return (
        "(async () => { const h = window.__spHelper;"
        " if (!h) return {ok: false, error: 'helper not running'};"
        f" try {{ const r = await fetch('http://127.0.0.1:' + h.port + '/{endpoint}',"
        "   {method: 'POST', headers: {'X-SP-Token': h.token, 'Content-Type': 'application/json'},"
        f"   body: JSON.stringify({json.dumps(body)})}});"
        "   const j = await r.json().catch(() => ({}));"
        "   return {ok: r.ok, error: j.error}; }"
        " catch (e) { return {ok: false, error: 'helper not reachable'}; } })()"
    )


def _helper_action_js_multi(endpoint: str, host_paths: list[str], app: str | None = None) -> str:
    """Same as :func:`_helper_action_js` but for the bulk ``{paths: [...]}}``
    form of ``/reveal`` and ``/open``."""
    body: dict[str, object] = {"paths": host_paths}
    if app:
        body["app"] = app
    return (
        "(async () => { const h = window.__spHelper;"
        " if (!h) return {ok: false, error: 'helper not running'};"
        f" try {{ const r = await fetch('http://127.0.0.1:' + h.port + '/{endpoint}',"
        "   {method: 'POST', headers: {'X-SP-Token': h.token, 'Content-Type': 'application/json'},"
        f"   body: JSON.stringify({json.dumps(body)})}});"
        "   const j = await r.json().catch(() => ({}));"
        "   return {ok: r.ok, error: j.error}; }"
        " catch (e) { return {ok: false, error: 'helper not reachable'}; } })()"
    )


def _app_label(app: str) -> str:
    """Short display name for a saved "open with" app path."""
    return Path(app).stem or app


def _open_with_button(on_pick: Callable[[str | None], object], *, size: str = "") -> None:
    """The "open" button shared by the bulk toolbar and the photo page's Paths
    list. One click: opens with the app saved in Settings -> Open with, or the
    OS default when none is saved -- no extra menu step. Change or clear the
    app from Settings."""
    settings = State.settings
    saved = get_open_with_app(settings.paths) if settings is not None else None
    props = f"flat dense round{f' size={size}' if size else ''}"
    tooltip = f'open with "{_app_label(saved)}"' if saved else "open in default app"
    ui.button(icon="open_in_new", on_click=lambda: on_pick(saved)).props(props).tooltip(tooltip)


def _path_row(
    path: str,
    *,
    present: bool,
    roots: list[LibraryRoot],
    helper: HelperInfo | None,
    native_reveal: bool,
) -> None:
    """One row in the photo page's Paths list. Shows the **host** path (translated
    from the stored container path). Reveal / Open go through the host helper
    agent under Docker, or the local openers on a native run; otherwise the Copy
    button is the way in (paste into Finder's Go to Folder / Explorer's bar)."""
    host_path = to_host_path(path, roots)
    with ui.row().classes("items-center gap-1"):
        with ui.column().classes("min-w-0 gap-0"):
            ui.label(host_path + ("" if present else "  (missing)")).classes(
                "break-all text-xs " + ("sp-muted" if present else "text-red-500")
            )
            if host_path != path:
                ui.label(f"container: {path}").classes("break-all text-[10px] sp-faint")
        ui.button(icon="content_copy", on_click=lambda: _copy_path(host_path)).props(
            "flat dense round size=xs"
        ).tooltip("Copy path (paste into Finder's Go to Folder / Explorer's address bar)")
        if not present:
            return
        if helper is not None:

            async def _act(endpoint: str, app: str | None = None) -> None:
                res = await ui.run_javascript(
                    _helper_action_js(endpoint, host_path, app), timeout=6.0
                )
                if not (res or {}).get("ok"):
                    ui.notify((res or {}).get("error") or "helper error", type="warning")

            ui.button(icon="folder_open", on_click=lambda: _act("reveal")).props(
                "flat dense round size=xs"
            ).tooltip("Reveal in file manager")
            _open_with_button(lambda app: _act("open", app), size="xs")
        elif native_reveal:
            ui.button(
                icon="folder_open", on_click=lambda: reveal_in_file_manager(Path(path))
            ).props("flat dense round size=xs").tooltip("Reveal in file manager")

            def _open_native(app: str | None) -> None:
                if app:
                    open_with_app(Path(path), app)
                else:
                    open_in_default_app(Path(path))

            _open_with_button(_open_native, size="xs")


def _stack_filmstrip(current: str, members: list[str], size: int, link_query: str = "") -> None:
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
                        "w-full aspect-square object-cover sp-placeholder"
                    ).props("loading=lazy")
                    if i == 0:
                        ui.label("top").classes(
                            "absolute left-1 top-1 rounded bg-black/70 px-1 text-[10px] text-white"
                        )
                if not is_current:
                    cell.on(
                        "click",
                        lambda m=member: ui.run_javascript(
                            f"location.replace({json.dumps(_photo_url(m, link_query))})"
                        ),
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

    def tile_click(self, pixel_hash: str, shift: bool) -> None:
        """A shift / cmd / ctrl click anywhere on a tile (the JS handler in
        ``_thumb_cell`` already cancelled the link): shift extends the range,
        cmd / ctrl toggles that one photo."""
        self.click(pixel_hash, shift=shift)

    def click(self, pixel_hash: str, shift: bool | None = None) -> None:
        idx = self._pos.get(pixel_hash)
        if idx is None:
            return
        before = set(self.selected)
        if (self.shift_down if shift is None else shift) and self._anchor is not None:
            lo, hi = sorted((self._anchor, idx))
            self.selected.update(self._order[lo : hi + 1])
        else:
            self.selected.symmetric_difference_update({pixel_hash})
            self._anchor = idx
        self._repaint(self.selected ^ before)

    def ordered(self) -> list[str]:
        """The selection in grid order."""
        return [h for h in self._order if h in self.selected]

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
    link_query: str = "",
    posted: bool = False,
) -> None:
    target = _photo_url(photo.pixel_hash, link_query)
    with ui.link(target=target).classes("group relative block") as cell:
        if cell_registry is not None:
            cell_registry[photo.pixel_hash] = cell
        if on_hover is not None:
            cell.on("mouseenter", lambda: on_hover(photo.pixel_hash, True))
            cell.on("mouseleave", lambda: on_hover(photo.pixel_hash, False))
        ui.image(_thumb_src(photo.pixel_hash, size)).classes(
            "w-full aspect-square object-cover sp-placeholder"
        ).props("loading=lazy")
        if stack_size > 1:
            ui.label(f"⧉ {stack_size}").classes(
                "absolute left-1 bottom-1 rounded bg-black/70 px-1.5 py-0.5 "
                "text-xs font-medium text-white"
            )
        if posted:
            ui.icon("send", size="0.9rem").classes(
                "absolute right-1 bottom-1 rounded bg-black/60 p-0.5 text-emerald-400"
            ).tooltip("sent to robopost")
        if on_flag is not None:
            _grid_flag(photo.pixel_hash, flagged, on_flag, flag_registry, flag_toggle_registry)
        elif flagged:
            ui.icon("flag", size="1.1rem").classes(
                "absolute right-1 top-1 z-10 rounded bg-black/60 p-0.5 text-amber-400"
            ).tooltip("marked for editing")
        if selection is not None:
            selection.checkbox(photo.pixel_hash)
            # shift / cmd / ctrl-click anywhere on the tile selects instead of
            # following the link (or opening a new tab); plain clicks navigate.
            cell.on(
                "click",
                lambda e, h=photo.pixel_hash: selection.tile_click(h, bool(e.args["shift"])),
                js_handler="(e) => { if (e.shiftKey || e.metaKey || e.ctrlKey) "
                "{ e.preventDefault(); emit({shift: e.shiftKey}); } }",
            )
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
            ui.label(label).classes("sp-faint")
            ui.label(value)


# --------------------------------------------------------------------------- #
# Welcome wizard (first-run onboarding, V2 #23) -- see sortandpost.onboarding
# --------------------------------------------------------------------------- #
_WELCOME_URL = "/welcome"
# Pages that wait for the wizard; everything else (NiceGUI's own assets,
# thumbnails, the prebuilt CSS) is served as usual.
_GATED_PREFIXES = ("/photo", "/rate", "/collections", "/search", "/review", "/settings", "/posts")
_RESTART = threading.Event()


def _is_gated(path: str) -> bool:
    return path == "/" or any(path == p or path.startswith(p + "/") for p in _GATED_PREFIXES)


def _install_onboarding_gate() -> None:
    """Until this machine finishes the welcome wizard, every page redirects to
    it -- the library would be empty anyway, and the indexer is waiting."""

    @nicegui_app.middleware("http")
    async def _gate(request: Request, call_next: Callable[[Request], Any]) -> Any:
        if State.onboarding_pending and request.method == "GET" and _is_gated(request.url.path):
            return RedirectResponse(_WELCOME_URL)
        return await call_next(request)


def _restart_app(landing: str, message: str) -> None:
    """Restart the whole process (to open a different data folder): show a
    waiting dialog, have the browser poll until the new server answers, then
    shut down cleanly -- the usual on_shutdown hooks run (final publish, lock
    release, indexer stop) -- and :func:`_reexec_if_requested` re-launches."""
    with ui.dialog().props("persistent") as dlg, ui.card().classes("items-center gap-3 p-6"):
        ui.spinner(size="lg")
        ui.label(message).classes("font-medium")
        ui.label("sortandpost is restarting — this page reloads on its own.").classes(
            "text-xs sp-faint"
        )
    dlg.open()
    url = json.dumps(landing)
    ui.run_javascript(
        "(function () { let down = false; const t0 = Date.now();"
        " const go = () => fetch(" + url + ", {cache: 'no-store'})"
        "   .then(r => { if (r.ok && down) window.location.href = " + url + ";"
        "     else if (Date.now() - t0 < 180000) setTimeout(go, 700); })"
        "   .catch(() => { down = true; setTimeout(go, 700); });"
        " setTimeout(go, 700); })();"
    )
    _RESTART.set()
    asyncio.get_running_loop().call_later(0.8, nicegui_app.shutdown)


def _reexec_if_requested() -> None:
    """After ``ui.run`` returns: re-launch this same command in place if a
    restart was requested. The listening socket is already closed (and Python
    sockets aren't inherited across exec), so the new process can bind the
    port again."""
    if not _RESTART.is_set():
        return
    log.info("restarting sortandpost")
    logging.shutdown()
    if desktop_control.control_token() is not None:
        # The desktop tray supervises us: it starts the new server itself, so it
        # keeps its handle on the process (control API, parent watch, Quit).
        raise SystemExit(desktop_control.RESTART_EXIT_CODE)
    if getattr(sys, "frozen", False):  # a packaged app (P5)
        argv = [sys.executable, *sys.argv[1:]]
    else:
        argv = [sys.executable, *sys.orig_argv[1:]]
    if sys.platform == "win32":
        # Windows has no in-place exec: os.execv starts a new process and ends
        # this one, detaching the server from the terminal that started it.
        raise SystemExit(subprocess.call(argv))
    os.execv(sys.executable, argv)


@contextmanager
def _welcome_shell() -> Iterator[None]:
    with ui.column().classes("w-full max-w-3xl mx-auto p-4 gap-6 pb-16"):
        yield


def _welcome_intro(joined: bool = False) -> None:
    with ui.column().classes("gap-1 pt-4"):
        ui.label(
            "Welcome back — let's add this computer" if joined else "Welcome to sortandpost"
        ).classes("sp-display text-3xl font-semibold")
        ui.label(
            "Your library, ratings and tags are already here. Just tell sortandpost "
            "where your photos are on this computer."
            if joined
            else "Rate, tag and search your photos — privately, on your own computers. "
            "A few quick choices and you're done; nothing is scanned until you finish."
        ).classes("sp-muted max-w-2xl")


def _shared_folder_intro() -> None:
    ui.label(
        "Pick a folder in the cloud drive you already use (Synology Drive, OneDrive, "
        "Dropbox, Google Drive, iCloud Drive, …) so your ratings, tags and search index "
        "follow you to your other computers — or any folder if you only use this one."
    ).classes("text-sm sp-muted max-w-2xl")
    ui.label(
        "Already use sortandpost on another computer? Pick the same folder here and "
        "everything carries over. Your photos stay exactly where they are."
    ).classes("text-sm sp-muted max-w-2xl")


def _shared_folder_picker(
    initial: str, *, on_use: Callable[[FolderInspection], object], button: str
) -> None:
    """Path box + Browse… + a live verdict from :func:`inspect_folder` (new
    library / existing library / not usable) + the confirm button."""
    local_base = default_local_root()
    with ui.row().classes("w-full items-end gap-2 no-wrap"):
        path_in = (
            ui.input("Shared folder", value=initial, placeholder="e.g. ~/Dropbox/sortandpost")
            .props("dense")
            .classes("flex-1")
        )
        if _app_browse_available():
            ui.button(
                "Browse…", icon="folder_open", on_click=lambda: _browse_folder(path_in)
            ).props("flat dense")
    verdict = ui.row().classes("w-full items-start gap-2 no-wrap min-h-6")
    state: dict[str, Any] = {"n": 0, "ins": None}

    async def _check() -> None:
        state["n"] += 1
        n = state["n"]
        raw = path_in.value or ""
        if not raw.strip():
            verdict.clear()
            use_btn.disable()
            state["ins"] = None
            return
        ins = await nicegui_run.io_bound(inspect_folder, raw, local_root=local_base)
        if ins is None or n != state["n"]:  # superseded by a newer keystroke / browse
            return
        state["ins"] = ins
        verdict.clear()
        icon, tone = {
            "existing": ("check_circle", "text-primary"),
            "new": ("create_new_folder", "sp-muted"),
            "invalid": ("error_outline", "text-amber-600"),
        }[ins.kind]
        with verdict:
            ui.icon(icon).classes(f"text-xl {tone}")
            ui.label(ins.message).classes("text-sm")
        use_btn.set_enabled(ins.kind != "invalid")

    def _use() -> None:
        ins = state["ins"]
        if ins is not None and ins.kind != "invalid":
            on_use(ins)

    use_btn = ui.button(button, icon="arrow_forward", on_click=_use).props("color=primary")
    use_btn.disable()
    path_in.on_value_change(_check)
    if initial:
        ui.timer(0.05, _check, once=True)


def _register_welcome(settings: Settings, engine: Engine, manager: SyncManager) -> None:
    """``/welcome`` for a machine whose data folder is open but hasn't finished
    setup: confirm the shared folder, this computer's photo folders, then (a
    brand-new library only) scanning / stacking / delete choices, the look, and
    Finish -- which starts the indexer. A machine joining an existing library
    adopts that library's shared settings as they are."""

    @ui.page(_WELCOME_URL)
    def welcome_page() -> RedirectResponse | None:
        if not State.onboarding_pending:
            return RedirectResponse("/")
        dark = _header(_WELCOME_URL, minimal=True)
        joined = settings.joined_existing
        writable = State.write_mode == WriteMode.writable
        cfg = settings.config

        def _nav(back: bool = True, on_next: Callable[[], object] | None = None) -> None:
            with ui.stepper_navigation():
                ui.button("Next", on_click=on_next or stepper.next).props("color=primary")
                if back:
                    ui.button("Back", on_click=stepper.previous).props("flat")

        def _library_next() -> None:
            if not settings.library_roots:
                ui.notify("add at least one photo folder", type="warning")
                return
            if not any(Path(r.container).is_dir() for r in settings.library_roots):
                ui.notify(
                    "none of these folders can be found right now — they'll be "
                    "scanned once they're available",
                    type="warning",
                )
            stepper.next()

        def _read_only_note() -> None:
            ui.label(
                f"{State.lock_holder or 'Another computer'} is using this library right "
                "now, so shared settings can't change. Adjust them later in Settings."
            ).classes("text-sm text-amber-600")

        with _welcome_shell():
            _welcome_intro(joined)
            with (
                ui.stepper().props("vertical flat animated").classes("w-full sp-wizard") as stepper
            ):
                with ui.step("folder", title="Library data", icon="folder_shared"):
                    _welcome_folder_step(settings, joined)
                    _nav(back=False)

                with ui.step("library", title="Photo folders", icon="photo_library"):
                    ui.label(
                        "Folders sortandpost scans for photos. Nothing in them is moved "
                        "or renamed. Different computers can use different folders."
                    ).classes("text-sm sp-muted max-w-2xl")
                    _library_roots_editor(
                        settings,
                        engine,
                        rescan=False,
                        suggestions=suggested_roots(settings.paths, settings.device_id),
                    )
                    _nav(on_next=_library_next)

                if not joined:
                    with ui.step("scanning", title="Scanning", icon="speed"):
                        if writable:
                            _cpu_speed_editor(settings)
                            ui.label("Skip folders named").classes("text-sm font-medium pt-3")
                            _exclude_editor(settings, engine, autosave=True)
                        else:
                            _read_only_note()
                        _nav()

                    with ui.step("organizing", title="Stacks & deleting", icon="layers"):
                        if writable:
                            ui.label("Image stacking").classes("text-sm font-medium")
                            _stacking_editor(settings, autosave=True)
                            ui.label("Deleting photos").classes("text-sm font-medium pt-3")
                            _deletion_editor(settings, autosave=True)
                        else:
                            _read_only_note()
                        _nav()

                with ui.step("look", title="Look", icon="palette"):
                    _theme_editor(settings, dark)
                    _nav()

                with ui.step("finish", title="Ready", icon="rocket_launch"):
                    summary = ui.column().classes("gap-1 text-sm")
                    with ui.stepper_navigation():
                        ui.button(
                            "Start using sortandpost", icon="play_arrow", on_click=lambda: _finish()
                        ).props("color=primary")
                        ui.button("Back", on_click=stepper.previous).props("flat")

        def _render_summary() -> None:
            summary.clear()
            speed = "fast (all cores)" if cfg.clip.speed == "fast" else "slow (background)"
            rows = [
                ("Shared folder", str(settings.paths.sync)),
                (
                    "Photo folders",
                    ", ".join(r.label or r.host for r in settings.library_roots) or "none yet",
                ),
                ("Scanning", speed),
                ("Skipped folders", ", ".join(cfg.indexer.exclude_dir_names) or "none"),
                ("Stacking", "on" if cfg.stacking.enabled else "off"),
                (
                    "Deleting",
                    "the whole stack"
                    if cfg.deletion.stack_scope == "whole_stack"
                    else "only the selected photo",
                ),
            ]
            with summary:
                if joined:
                    ui.label(
                        "Shared settings come from your library — change any of them later "
                        "in Settings."
                    ).classes("sp-muted pb-1")
                with ui.grid(columns="auto 1fr").classes("gap-x-6 gap-y-1"):
                    for key, value in rows:
                        ui.label(key).classes("sp-faint")
                        ui.label(value).classes("break-all")
                ui.label(
                    "Scanning starts right away and runs in the background — you can "
                    "browse while it works."
                ).classes("sp-muted pt-2")

        stepper.on_value_change(lambda e: _render_summary() if e.value == "finish" else None)

        def _finish() -> None:
            mark_complete(settings.paths, how="joined" if joined else "new")
            State.onboarding_pending = False
            if settings.config.indexer.run_in_app and manager.mode is WriteMode.writable:
                _start_indexer_thread(settings, manager)
            log.info("onboarding finished (%s library)", "joined" if joined else "new")
            ui.navigate.to("/")

        return None


def _welcome_folder_step(settings: Settings, joined: bool) -> None:
    """Wizard step 1 once a folder is open: where the shared data is, what was
    found there, and (unless ``APP_DATA`` pins it) a way to pick another."""
    paths = settings.paths
    source = settings.env.paths_source()
    with ui.row().classes("items-center gap-2 no-wrap"):
        ui.icon("folder_shared").classes("text-xl text-primary")
        ui.label(str(paths.sync)).classes("font-mono text-sm break-all")
    if joined:
        ui.label(inspect_folder(str(paths.sync)).message).classes("text-sm max-w-2xl")
    else:
        ui.label(
            "A new library — its database, thumbnails and search index are kept here."
        ).classes("text-sm max-w-2xl")
    if paths.is_split:
        ui.label(f"This computer's private working files: {paths.local}").classes(
            "text-xs sp-faint break-all"
        )
    if source == "env":
        ui.label("Set by the APP_DATA environment variable (e.g. Docker).").classes(
            "text-xs sp-faint"
        )
        return
    with ui.expansion("Use a different folder", icon="swap_horiz").classes("w-full"):

        def _use(ins: FolderInspection) -> None:
            assert ins.sync_dir is not None
            ins.sync_dir.mkdir(parents=True, exist_ok=True)
            remember_sync_dir(ins.sync_dir)
            _restart_app(_WELCOME_URL, "Switching folder…")

        _shared_folder_picker("", on_use=_use, button="Use this folder")


def _desktop_status(settings: Settings | None) -> dict[str, Any]:
    """``GET /_desktop/status`` for the tray menu (cheap: no DB query)."""
    snap = PROGRESS.snapshot()
    trash_photos = 0
    if settings is not None:
        trash_photos = sum(len(b.hashes) for b in list_batches(settings.paths.local))
    return {
        "version": __version__,
        "setup": settings is None,
        "onboarding": State.onboarding_pending,
        "writable": State.write_mode == WriteMode.writable,
        "holder": State.lock_holder,
        "paused": State.indexer_paused.is_set(),
        "activity": _progress_text(snap) if snap.active else "",
        "trash_photos": trash_photos,
        "system_trash": system_trash_available(),
        "logs_dir": str(settings.paths.logs_dir) if settings is not None else "",
    }


def _desktop_set_paused(paused: bool) -> None:
    if paused:
        State.indexer_paused.set()
    else:
        State.indexer_paused.clear()
    log.info("indexing %s from the desktop tray", "paused" if paused else "resumed")


def _desktop_flush_trash(settings: Settings) -> dict[str, Any]:
    """Quit's "Move N deleted photos to the system Trash?" -> Move. A direct
    user click in the tray's dialog (constraint #1), never automatic."""
    res = flush_to_system_trash(settings.paths.local)
    State.last_delete = None
    return {"moved": res.moved, "failed": len(res.failed)}


def _install_desktop_control(settings: Settings | None) -> None:
    desktop_control.install(
        nicegui_app,
        desktop_control.ControlHooks(
            status=lambda: _desktop_status(settings),
            shutdown=nicegui_app.shutdown,
            set_paused=_desktop_set_paused if settings is not None else None,
            flush_trash=(lambda: _desktop_flush_trash(settings)) if settings is not None else None,
        ),
    )


def run(settings: Settings) -> None:
    build_ui(settings)
    _install_desktop_control(settings)
    ui.run(
        host=settings.env.host,
        port=settings.env.port,
        title="sortandpost",
        reload=False,
        show=False,
        storage_secret=settings.device_id,
        tailwind=False,  # prebuilt instead -- see _TAILWIND_URL
    )
    _reexec_if_requested()


def run_setup(env: Env) -> None:
    """First run on this machine, before any data folder is chosen
    (:meth:`Env.resolve_paths` is ``None``). Serves only the first welcome step
    -- pick the shared folder -- with no DB, no lock and no indexer, then
    restarts itself on the chosen folder (sortandpost.onboarding)."""
    _add_tailwind_css()
    ui.add_css(_THEME_CSS, shared=True)
    ui.add_css(theme_css(), shared=True)

    @ui.page("/")
    def _root() -> RedirectResponse:
        return RedirectResponse(_WELCOME_URL)

    @ui.page(_WELCOME_URL)
    def _setup_welcome() -> None:
        _header(_WELCOME_URL, minimal=True)
        with _welcome_shell():
            _welcome_intro()
            with _settings_card("Where should your library live?", "folder_shared", wide=True):
                _shared_folder_intro()

                def _use(ins: FolderInspection) -> None:
                    assert ins.sync_dir is not None
                    ins.sync_dir.mkdir(parents=True, exist_ok=True)
                    remember_sync_dir(ins.sync_dir)
                    _restart_app(_WELCOME_URL, "Setting up your library…")

                _shared_folder_picker("", on_use=_use, button="Continue")

    _install_desktop_control(None)
    ui.run(
        host=env.host,
        port=env.port,
        title="sortandpost — welcome",
        reload=False,
        show=False,
        storage_secret="sortandpost-setup",
        tailwind=False,
    )
    _reexec_if_requested()
