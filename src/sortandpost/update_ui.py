"""The update prompt: a banner on ``/`` and Settings → Overview → Updates.

"Update & restart" downloads this computer's file from the release, verifies
it (:mod:`sortandpost.update.check`), then shuts the app down the usual clean
way and exits with ``UPDATE_EXIT_CODE``; the desktop tray installs it
(:mod:`sortandpost.update.apply`). Where the app can't replace itself (Docker,
a source run, a package manager's install) the banner says how to update
instead (:func:`sortandpost.update.source.manual_hint`).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import UTC, datetime

from nicegui import app as nicegui_app
from nicegui import run as nicegui_run
from nicegui import ui

from sortandpost import __version__
from sortandpost.update.check import RELEASES_URL, Download, PendingUpdate, Updater, write_pending
from sortandpost.update.manifest import Manifest, platform_key
from sortandpost.update.source import can_self_update, install_source, manual_hint

log = logging.getLogger(__name__)

# Set by "Update & restart" once the download is verified; app._reexec_if_requested
# then exits with UPDATE_EXIT_CODE.
INSTALL_REQUESTED = threading.Event()

_updater: Updater | None = None
# The installer's outcome is told once, not on every page load.
_result_told = False
_dismissed: set[str] = set()  # versions whose banner was closed (until a restart)

_BANNER = "mx-4 mt-2 items-center gap-3 rounded-md sp-card px-3 py-2 text-sm"


def start() -> Updater:
    """Create this process's :class:`Updater` and start the daily check."""
    global _updater
    if _updater is None:
        _updater = Updater()
        _updater.start()
        nicegui_app.on_shutdown(_updater.stop)
        if _updater.last_result is not None:
            ok, text = _updater.last_result
            log.info("last update: %s %s", "installed" if ok else "failed", text)
    return _updater


def updater() -> Updater | None:
    return _updater


def _release_url(m: Manifest) -> str:
    return m.notes_url or f"{RELEASES_URL}/tag/v{m.version}"


def _notes_dialog(m: Manifest) -> None:
    with ui.dialog() as dlg, ui.card().classes("w-[40rem] max-w-full gap-2"):
        ui.label(f"What's new in {m.version}").classes("font-semibold")
        with ui.scroll_area().classes("max-h-[60vh] w-full"):
            if m.notes.strip():
                ui.markdown(m.notes).classes("text-sm")
            else:
                ui.label("No notes came with this release.").classes("text-sm sp-muted")
        with ui.row().classes("w-full justify-between items-center"):
            ui.link("On GitHub", _release_url(m), new_tab=True).classes("text-sm")
            ui.button("Close", on_click=dlg.close).props("flat")
    dlg.open()


def _fmt_mb(n: int) -> str:
    return f"{n / 1_000_000:,.0f} MB"


def _update_dialog(up: Updater, m: Manifest) -> None:
    """Confirm → download with a progress bar → verified → shut down to install."""
    asset = m.asset_for(platform_key())
    size = f" ({_fmt_mb(asset.size)})" if asset else ""
    with ui.dialog().props("persistent") as dlg, ui.card().classes("w-[32rem] max-w-full gap-3"):
        ui.label(f"Update to sortandpost.photos {m.version}?").classes("font-semibold")
        info = ui.label(
            f"The new version{size} is downloaded and checked first. Then the app "
            "saves your library, closes, installs the update and opens again. "
            "This takes a minute or two."
        ).classes("text-sm sp-muted")
        bar = ui.linear_progress(value=0, show_value=False).classes("w-full")
        bar.set_visibility(False)
        status = ui.label("").classes("text-xs sp-faint")
        with ui.row().classes("w-full justify-end gap-2") as buttons:
            cancel = ui.button("Cancel", on_click=dlg.close).props("flat")
            go = ui.button("Update & restart", icon="system_update_alt").props("color=primary")

    timer: ui.timer | None = None
    dl: Download | None = None

    def _poll() -> None:
        assert dl is not None
        if dl.total:
            bar.set_value(dl.done / dl.total)
            status.set_text(f"{_fmt_mb(dl.done)} of {_fmt_mb(dl.total)}")
        if not dl.finished:
            return
        if timer is not None:
            timer.cancel()
        if dl.error or dl.path is None:
            info.set_text(f"The update couldn't be downloaded: {dl.error or 'unknown error'}")
            info.classes(replace="text-sm text-negative")
            bar.set_visibility(False)
            status.set_text("Nothing was changed. You can try again later.")
            cancel.set_text("Close")
            go.set_text("Try again")
            go.enable()
            cancel.enable()
            return
        _install(m)

    def _install(manifest: Manifest) -> None:
        assert dl is not None and dl.path is not None
        write_pending(PendingUpdate(path=str(dl.path), version=manifest.version, sha256=dl.sha256))
        buttons.set_visibility(False)
        bar.set_visibility(False)
        info.set_text(
            f"Installing {manifest.version}. sortandpost.photos closes now and opens "
            "again in a new tab when it's done. You can close this tab."
        )
        info.classes(replace="text-sm")
        status.set_text("")
        INSTALL_REQUESTED.set()
        asyncio.get_running_loop().call_later(0.8, nicegui_app.shutdown)

    def _start() -> None:
        nonlocal dl, timer
        go.disable()
        cancel.disable()
        info.set_text(f"Downloading sortandpost.photos {m.version}…")
        info.classes(replace="text-sm sp-muted")
        bar.set_value(0)
        bar.set_visibility(True)
        dl = up.start_download(m)
        timer = ui.timer(0.25, _poll)

    go.on_click(_start)
    dlg.open()


def _actions(up: Updater, m: Manifest, *, dense: bool) -> None:
    flat = "flat dense no-caps" if dense else "flat no-caps"
    ui.button("What's new", on_click=lambda: _notes_dialog(m)).props(flat)
    if can_self_update() and m.asset_for(platform_key()) is not None:
        ui.button(
            "Update & restart", icon="system_update_alt", on_click=lambda: _update_dialog(up, m)
        ).props(("dense " if dense else "") + "no-caps color=primary")
    else:
        url = _release_url(m)
        ui.button("Release page", on_click=lambda: ui.navigate.to(url, new_tab=True)).props(flat)


def banner() -> None:
    """On ``/``: the last update's outcome (once), else a newer version."""
    global _result_told
    up = _updater
    if up is None:
        return
    if up.last_result is not None and not _result_told:
        _result_told = True
        ok, text = up.last_result
        with ui.row().classes(_BANNER) as row:
            if ok:
                ui.icon("task_alt").classes("text-lg text-primary")
                ui.label(f"Updated to sortandpost.photos {__version__}.").classes("flex-1")
            else:
                ui.icon("error_outline").classes("text-lg text-negative")
                ui.label(
                    f"The update couldn't be installed: {text}. You're still on {__version__}."
                ).classes("flex-1")
            ui.button(icon="close", on_click=lambda: row.set_visibility(False)).props(
                "flat dense round size=sm"
            ).tooltip("dismiss")
    m = up.available()
    if m is None or m.version in _dismissed:
        return
    with ui.row().classes(_BANNER) as row:
        ui.icon("system_update_alt").classes("text-lg text-primary")
        text = f"sortandpost.photos {m.version} is available (you have {__version__})."
        if not can_self_update():
            text += " " + manual_hint(install_source())
        ui.label(text).classes("flex-1")
        _actions(up, m, dense=True)

        def _skip() -> None:
            up.skip(m.version)
            row.set_visibility(False)

        ui.button("Skip this version", on_click=_skip).props("flat dense no-caps")

        def _close() -> None:
            _dismissed.add(m.version)
            row.set_visibility(False)

        ui.button(icon="close", on_click=_close).props("flat dense round size=sm").tooltip(
            "hide until the app restarts"
        )


def _ago(ts: datetime | None) -> str:
    if ts is None:
        return "never"
    secs = (datetime.now(UTC) - ts).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{secs / 60:.0f} minutes ago"
    if secs < 129600:
        return f"{secs / 3600:.0f} hours ago"
    return f"{secs / 86400:.0f} days ago"


def settings_card() -> None:
    """The body of Settings → Overview → Updates."""
    up = _updater
    if up is None:
        ui.label("Update checks aren't running in this process.").classes("text-sm sp-muted")
        return

    @ui.refreshable
    def _body() -> None:
        ui.label(f"This computer has sortandpost.photos {__version__}.").classes("text-sm")
        m = up.latest()
        if m is not None:
            with ui.row().classes("items-center gap-2"):
                ui.label(f"{m.version} is available.").classes("text-sm font-medium")
                _actions(up, m, dense=True)
            if not can_self_update():
                ui.label(manual_hint(install_source())).classes("text-xs sp-faint")
        elif up.last_check_at is not None:
            ui.label("It's up to date.").classes("text-sm sp-muted")
        line = f"Last checked {_ago(up.last_check_at)}."
        if up.last_error:
            line += f" The last try failed: {up.last_error}"
        ui.label(line).classes("text-xs sp-faint")

    _body()

    async def _check() -> None:
        btn.disable()
        try:
            await nicegui_run.io_bound(up.check_now)
        finally:
            btn.enable()
        _body.refresh()
        m = up.latest()
        if m is not None:
            ui.notify(f"{m.version} is available")
        elif up.last_error:
            ui.notify(f"Couldn't check: {up.last_error}", type="warning")
        else:
            ui.notify("You're up to date")

    with ui.row().classes("items-center gap-3"):
        btn = ui.button("Check now", icon="refresh", on_click=_check).props("flat dense no-caps")
        sw = ui.switch("Check once a day", value=up.enabled)
        if up.disabled_by_env:
            sw.disable()
            sw.tooltip("Turned off by SORTANDPOST_NO_UPDATE_CHECK")
        else:
            sw.on_value_change(lambda e: up.set_enabled(bool(e.value)))
    ui.label(
        "The check fetches one small file from github.com. It sends nothing about "
        "you or your photos."
    ).classes("text-xs sp-faint")


WELCOME_NOTE = (
    "sortandpost.photos checks github.com once a day for a new version and asks "
    "before installing it. You can turn this off in Settings."
)
