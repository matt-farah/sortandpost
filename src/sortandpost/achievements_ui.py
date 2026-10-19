"""Achievements UI (V3 #2, P3b): the ``/achievements`` page, the header's
name + 🏆 score chip, unlock toasts, and the profile editor used by Settings
and the welcome wizard. The engine is :mod:`sortandpost.achievements`.

A background thread re-checks the count achievements every few seconds (and
right after a rating / new collection via :func:`poke`), so unlocks happen no
matter which page -- or which tab -- caused them. Each open tab polls the
tracker's in-memory feed and toasts what's new. Mode Off hides the chip, the
toasts and the page, but the checks keep running.

Like ``posts_ui`` this reuses ``app``'s page chrome through a lazy import.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime
from functools import partial

from nicegui import app as nicegui_app
from nicegui import ui
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlmodel import Session

from sortandpost import profile_ui
from sortandpost.achievements import (
    EVENT_KONAMI,
    EVENT_PERCIVAL_MODE,
    Achievement,
    AchievementTracker,
    Unlock,
    compute_metrics,
    resource_path,
)
from sortandpost.config import Settings
from sortandpost.profiles import MODE_LABELS, NAME_MAX_LEN, Mode, load_profile

log = logging.getLogger(__name__)

PAGE_URL = "/achievements"
_ART_URL = "/_achievements_art"
_CHECK_SEC = 10.0  # background re-check interval (a poke runs one sooner)
_FEED_SEC = 2.0  # per-tab toast poll -- in-memory only, cheap

TRACKER: AchievementTracker | None = None
_SETTINGS: Settings | None = None
_WAKE = threading.Event()
_STOP = threading.Event()

_CSS = """
.sp-ach-grid { display: grid; gap: .75rem; width: 100%;
  grid-template-columns: repeat(auto-fill, minmax(15rem, 1fr)); }
.sp-ach-card { display: flex; flex-direction: column; gap: .35rem; padding: .9rem 1rem;
  border-radius: .75rem; border: 1px solid var(--sp-border);
  background-color: var(--sp-surface); min-height: 8.5rem; }
.sp-ach-card.sp-ach-on { border-color: var(--q-primary);
  box-shadow: 0 0 0 1px var(--q-primary) inset; }
.sp-ach-card.sp-ach-locked .sp-ach-icon { color: var(--sp-text-faint); }
.sp-ach-card.sp-ach-hidden { align-items: center; justify-content: center;
  border-style: dashed; background-color: var(--sp-surface-2); color: var(--sp-text-faint); }
.sp-ach-mystery { font-size: 2.6rem; font-weight: 700; line-height: 1; }
.sp-ach-icon { font-size: 1.6rem; color: var(--q-primary); }
.sp-ach-desc p { margin: 0; }
.sp-ach-persona { width: 4.5rem; height: 4.5rem; border-radius: 50%; object-fit: cover;
  border: 2px solid var(--sp-border); background-color: var(--sp-surface); }
.sp-trophy { text-decoration: none; color: inherit; }
.sp-ach-badge { font-size: .65rem; font-weight: 700; padding: 0 .4rem; border-radius: 999px;
  background-color: var(--q-primary); color: var(--sp-on-primary); }
.sp-ach-card.sp-ach-sealed { align-items: center; justify-content: center; cursor: pointer;
  text-align: center; border: 2px solid var(--q-primary);
  animation: sp-ach-glow 1.8s ease-in-out infinite; transition: transform .12s; }
.sp-ach-card.sp-ach-sealed:hover { transform: translateY(-2px) scale(1.02); }
.sp-ach-sealed .sp-ach-icon { font-size: 2.2rem; }
@keyframes sp-ach-glow {
  0%, 100% { box-shadow: 0 0 0 0 transparent; }
  50% { box-shadow: 0 0 14px 2px var(--q-primary); } }
@media (prefers-reduced-motion: reduce) { .sp-ach-card.sp-ach-sealed { animation: none; } }
.sp-ach-reveal { width: min(28rem, 92vw); padding: 1.5rem; text-align: center;
  animation: sp-ach-pop .28s ease-out; }
/* the cats' art, shown big on /achievements (head + reveal dialog) */
.sp-ach-persona-xl { width: min(9rem, 30vw); height: min(9rem, 30vw); }
.sp-ach-persona-lg { width: 14rem; height: 14rem; }
@keyframes sp-ach-pop {
  from { transform: scale(.85); opacity: 0; } to { transform: none; opacity: 1; } }
"""

# ↑ ↑ ↓ ↓ ← → ← → B A, anywhere on the page except while typing in a field.
_KONAMI_JS = """
<script>
(function () {
  const code = ['arrowup','arrowup','arrowdown','arrowdown','arrowleft','arrowright',
                'arrowleft','arrowright','b','a'];
  let at = 0;
  document.addEventListener('keydown', (e) => {
    const t = e.target;
    if (t && (t.isContentEditable || /^(input|textarea|select)$/i.test(t.tagName))) return;
    const k = (e.key || '').toLowerCase();
    at = (k === code[at]) ? at + 1 : (k === code[0] ? 1 : 0);
    if (at === code.length) { at = 0; emitEvent('sp-konami'); }
  });
})();
</script>
"""


# --------------------------------------------------------------------------- #
# engine plumbing
# --------------------------------------------------------------------------- #
def start(settings: Settings, engine: Engine) -> None:
    """Create the tracker, serve the persona art, start the background check."""
    global TRACKER, _SETTINGS
    _SETTINGS = settings
    TRACKER = AchievementTracker(settings.paths, settings.device_id, settings.hostname)
    for mode in ("tugs", "percival"):
        nicegui_app.add_static_file(
            local_file=resource_path(f"{mode.capitalize()}.png"), url_path=art_url(mode)
        )
    ui.add_css(_CSS, shared=True)
    nicegui_app.on_shutdown(stop)
    threading.Thread(
        target=_watch, args=(settings, engine), name="sortandpost-achievements", daemon=True
    ).start()


def stop() -> None:
    _STOP.set()
    _WAKE.set()


def poke() -> None:
    """Re-check soon (after a rating, a new collection, …)."""
    _WAKE.set()


def _watch(settings: Settings, engine: Engine) -> None:
    from sortandpost import app as A  # lazy: app imports this module

    while not _STOP.is_set():
        tracker = TRACKER
        if tracker is not None and not A.State.onboarding_pending:
            try:
                with Session(engine) as session:
                    metrics = compute_metrics(
                        session,
                        settings.paths,
                        settings.config.stacking,
                        rated_floor=tracker.rated_floor(),
                    )
                tracker.check(metrics)
            except OperationalError as exc:  # the indexer holds the DB for a moment
                log.debug("achievements check skipped: %s", exc)
            except Exception:
                log.exception("achievements check failed")
        _WAKE.wait(_CHECK_SEC)
        _WAKE.clear()


def record_event(event: str) -> list[Unlock]:
    if TRACKER is None:
        return []
    try:
        return TRACKER.record_event(event)
    except OSError as exc:
        log.warning("could not record achievement event %s: %s", event, exc)
        return []


def current_mode() -> Mode:
    return load_profile(_SETTINGS.paths).mode if _SETTINGS is not None else "off"


def art_url(mode: str) -> str:
    return f"{_ART_URL}/{'percival' if mode == 'percival' else 'tugs'}.png"


# --------------------------------------------------------------------------- #
# header chip + toasts
# --------------------------------------------------------------------------- #
def header_chip() -> None:
    """Profile picture + name + 🏆 score (+ how many are waiting to be
    revealed), top right; toasts new unlocks in this tab. With achievements
    Off, just the picture (when there is one), linking to the profile."""
    tracker, settings = TRACKER, _SETTINGS
    if tracker is None or settings is None:
        return
    store = profile_ui.store(settings)
    profile = store.get()
    if profile.mode == "off":
        url = store.avatar_url(profile)
        if url:
            with ui.link("", "/settings#sp-profile").classes("sp-chip sp-trophy flex items-center"):
                ui.tooltip("Profile")
                profile_ui.avatar(profile, url)
        return
    with ui.link("", PAGE_URL).classes("sp-chip sp-trophy flex items-center gap-1 no-wrap"):
        ui.tooltip("Achievements")

        @ui.refreshable
        def _face() -> None:
            p = store.get()
            url = store.avatar_url(p)
            if url:
                profile_ui.avatar(p, url)

        _face()
        if profile.name:
            ui.label(profile.name).classes("font-medium")
        score = ui.label()
        badge = ui.label().classes("sp-ach-badge")
    last_seq, last_version, face_version = tracker.seq, -1, profile_ui.VERSION

    def _refresh() -> None:
        nonlocal last_version
        last_version = tracker.version
        unlocked = tracker.unlocked()
        score.set_text(f"🏆 {tracker.score(unlocked):,}")
        sealed = len(tracker.sealed(unlocked))
        badge.set_text(f"{sealed} new")
        badge.set_visibility(sealed > 0)

    def _feed() -> None:
        nonlocal last_seq, face_version
        new = tracker.since(last_seq)
        if new:
            last_seq = new[-1].seq
            mode = load_profile(settings.paths).mode
            if mode != "off":
                _toast(new, mode)
        if new or tracker.version != last_version:
            _refresh()
        if face_version != profile_ui.VERSION:  # the picture changed in another tab
            face_version = profile_ui.VERSION
            _face.refresh()

    _refresh()
    ui.timer(_FEED_SEC, _feed)


def _toast(new: list[Unlock], mode: Mode) -> None:
    """Announce, don't spoil: the name and text wait for the reveal."""
    n = len(new)
    ui.notify(
        "🏆 Achievement unlocked!" if n == 1 else f"🏆 {n} achievements unlocked!",
        caption="Open your trophy case to reveal " + ("it." if n == 1 else "them."),
        avatar=art_url(mode),
        position="top-right",
        timeout=8000,
        multi_line=True,
        # a QBtn with href -- Quasar's notify actions can't call back into Python
        actions=[
            {"label": "Reveal", "href": PAGE_URL, "color": "white", "noCaps": True},
            {"label": "Later", "color": "white", "flat": True, "noCaps": True},
        ],
    )


# --------------------------------------------------------------------------- #
# /achievements
# --------------------------------------------------------------------------- #
def register_pages(settings: Settings) -> None:
    from sortandpost import app as A  # lazy: app imports this module

    @ui.page(PAGE_URL)
    def achievements_page() -> None:
        A._header(PAGE_URL)
        tracker = TRACKER
        if tracker is None:
            return
        profile = load_profile(settings.paths)
        mode = profile.mode
        with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):
            if mode == "off":
                ui.label("Achievements").classes("sp-display text-2xl font-semibold")
                ui.label(
                    "Achievements are turned off. They still count in the background — "
                    "turn them back on in Settings to see them."
                ).classes("text-sm sp-muted")
                ui.link("Open Settings", "/settings#sp-profile").classes("text-sm")
                return
            dialog = ui.dialog()

            def _reveal(a: Achievement) -> None:
                tracker.reveal(a.key)
                _reveal_dialog(dialog, a, mode)
                body.refresh()

            @ui.refreshable
            def body() -> None:
                unlocked = tracker.unlocked()
                sealed = set(tracker.sealed(unlocked))
                _page_head(tracker, mode, profile.name, unlocked, len(sealed))
                with ui.element("div").classes("sp-ach-grid"):
                    for a in tracker.defs:
                        at = unlocked.get(a.key)
                        if a.key in sealed:
                            _sealed_card(a, on_click=partial(_reveal, a))
                        else:
                            _card(a, mode, at)

            body()
        ui.add_body_html(_KONAMI_JS)

        def _konami() -> None:
            konami = next((a for a in tracker.defs if a.trigger.event == EVENT_KONAMI), None)
            if not record_event(EVENT_KONAMI) or konami is None:
                ui.notify("↑↑↓↓←→←→BA — you already found this one", position="top-right")
                return
            _reveal(konami)  # found right here: open it straight away

        ui.on("sp-konami", _konami)


def _page_head(
    tracker: AchievementTracker, mode: Mode, name: str, unlocked: dict[str, str], sealed: int
) -> None:
    done = sum(1 for a in tracker.defs if a.key in unlocked)
    with ui.row().classes("w-full items-center gap-4 no-wrap"):
        ui.image(art_url(mode)).classes("sp-ach-persona sp-ach-persona-xl shrink-0")
        with ui.column().classes("gap-0 min-w-0"):
            ui.label("Achievements").classes("sp-display text-2xl font-semibold")
            who = f"{name} · " if name else ""
            waiting = f" · {sealed} waiting to be revealed" if sealed else ""
            ui.label(
                f"{who}{MODE_LABELS[mode]} mode · {done} of {len(tracker.defs)} unlocked{waiting}"
            ).classes("text-sm sp-muted")
        ui.space()
        with ui.column().classes("items-end gap-0"):
            ui.label(f"🏆 {tracker.score(unlocked):,}").classes("sp-display text-2xl font-semibold")
            ui.label(f"of {tracker.total_points():,} points").classes("text-xs sp-faint")


def _local_date(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).astimezone().date().isoformat()
    except ValueError:
        return ts[:10]


def _sealed_card(a: Achievement, *, on_click: Callable[[], None]) -> None:
    """Unlocked, not yet seen: a glowing card to click open (one at a time)."""
    with ui.element("div").classes("sp-ach-card sp-ach-sealed") as card:
        ui.icon("redeem").classes("sp-ach-icon")
        ui.label("New achievement!").classes("font-semibold")
        ui.label("Click to reveal").classes("text-xs sp-muted")
    card.on("click", on_click)


def _card(a: Achievement, mode: Mode, unlocked_at: str | None) -> None:
    if unlocked_at is None and not a.shown:
        with ui.element("div").classes("sp-ach-card sp-ach-hidden"):
            ui.label("?").classes("sp-ach-mystery")
            ui.label("Hidden achievement").classes("text-xs")
        return
    state = "sp-ach-on" if unlocked_at else "sp-ach-locked"
    with ui.element("div").classes(f"sp-ach-card {state}"):
        with ui.row().classes("items-center gap-2 no-wrap w-full"):
            ui.icon(a.icon).classes("sp-ach-icon")
            ui.label(a.name).classes("font-semibold flex-1 min-w-0")
            ui.label(f"+{a.points}").classes("text-xs sp-muted")
        if unlocked_at:
            ui.markdown(a.description(mode)).classes("sp-ach-desc text-sm")
            ui.label(f"Unlocked {_local_date(unlocked_at)}").classes("text-xs sp-faint mt-auto")
        else:
            ui.label("???").classes("text-sm sp-faint")


def _reveal_dialog(dialog: ui.dialog, a: Achievement, mode: Mode) -> None:
    """The reveal moment: the persona, the name, the points, the line."""
    dialog.clear()
    with dialog, ui.card().classes("sp-ach-reveal items-center gap-2"):
        ui.image(art_url(mode)).classes("sp-ach-persona sp-ach-persona-lg")
        ui.label("Achievement unlocked").classes("text-xs sp-muted uppercase tracking-wide")
        with ui.row().classes("items-center gap-2 no-wrap"):
            ui.icon(a.icon).classes("sp-ach-icon")
            ui.label(a.name).classes("sp-display text-2xl font-semibold")
        ui.label(f"+{a.points} points").classes("text-sm text-primary font-semibold")
        ui.markdown(a.description(mode)).classes("sp-ach-desc text-center")
        ui.button("Nice!", on_click=dialog.close).props("color=primary unelevated")
    dialog.open()


# --------------------------------------------------------------------------- #
# profile editor -- Settings + the welcome wizard
# --------------------------------------------------------------------------- #
def profile_editor(settings: Settings, *, notify: bool = True) -> None:
    """Profile picture, name + Tugs / Percival / Off. Saved as it changes to the
    shared profile (caller gates on the write lock)."""
    profile = profile_ui.store(settings).get()
    state = {"name": profile.name, "mode": profile.mode}
    with profile_ui.avatar_editor(settings):
        name_in = (
            ui.input("Profile name", value=profile.name)
            .props(f"outlined dense maxlength={NAME_MAX_LEN}")
            .classes("w-64")
        )
    ui.label(
        "Achievements turn sorting and posting into a game. Your name and score show "
        "at the top right. Tugs, the sweet cat, cheers you on; Percival, the grumpy cat… "
        "doesn't. Shared by every "
        "computer that uses this library."
    ).classes("text-xs sp-faint max-w-xl pt-2")
    with ui.row().classes("items-center gap-4 no-wrap"):
        persona = ui.image(art_url(profile.mode)).classes("sp-ach-persona shrink-0")
        persona.set_visibility(profile.mode != "off")
        mode_in = ui.toggle(
            {m: label for m, label in MODE_LABELS.items()}, value=profile.mode
        ).props("no-caps")

    def _save(what: str) -> None:
        mode: Mode = mode_in.value if mode_in.value in MODE_LABELS else "tugs"
        name = name_in.value or ""
        if name.strip() == state["name"] and mode == state["mode"]:
            return
        was = state["mode"]
        try:
            saved = profile_ui.store(settings).save(name=name, mode=mode)
        except OSError as exc:
            ui.notify(f"couldn't save: {exc}", type="negative")
            return
        state["name"], state["mode"] = saved.name, saved.mode
        persona.set_source(art_url(saved.mode))
        persona.set_visibility(saved.mode != "off")
        if saved.mode == "percival" and was != "percival":
            record_event(EVENT_PERCIVAL_MODE)
        if notify:
            ui.notify(f"{what} saved", position="bottom")

    name_in.on("blur", lambda: _save("Name"))
    name_in.on("keydown.enter", lambda: _save("Name"))
    mode_in.on_value_change(lambda: _save("Mode"))
