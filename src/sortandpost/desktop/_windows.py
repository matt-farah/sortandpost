"""Windows-only bits of the tray, via ctypes (no pywin32).

pystray's win32 backend runs a message loop on the thread that called
``Icon.run`` and dispatches its hidden window's messages through
``Icon._message_handlers``; menu callbacks run on that same thread.
"""

from __future__ import annotations

import ctypes
import logging
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

log = logging.getLogger(__name__)

WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_SETTEXT = 0x000C
# The tray's hidden window title; packaging/windows/sortandpost.iss looks for it.
WINDOW_TITLE = "sortandpost tray"
# Held while the tray runs; the installer refuses to replace files while it
# exists (sortandpost.iss, CheckForMutexes).
APP_MUTEX = "sortandpost-app"

_mutex: int | None = None


def hold_app_mutex() -> None:
    """Create :data:`APP_MUTEX` and keep it until the process exits (Windows
    drops it then, however the process ends)."""
    global _mutex
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined,unused-ignore]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    _mutex = kernel32.CreateMutexW(None, False, APP_MUTEX)


_MB_YESNO = 0x04
_MB_ICONQUESTION = 0x20
_MB_SETFOREGROUND = 0x10000
_MB_TOPMOST = 0x40000
_IDYES = 6


def ask(title: str, message: str, *, yes: str, no: str) -> bool:
    """A modal Yes / No box in front of everything (a tray app has no window
    of its own to sit on); True for ``yes``. MessageBoxW can't relabel its
    buttons, so the message says which is which."""
    text = f"{message}\n\nYes = {yes}    No = {no}"
    flags = _MB_YESNO | _MB_ICONQUESTION | _MB_SETFOREGROUND | _MB_TOPMOST
    user32 = ctypes.windll.user32  # type: ignore[attr-defined,unused-ignore]
    return bool(user32.MessageBoxW(None, text, title, flags) == _IDYES)


def name_window(icon: Any) -> None:
    """Title the tray's hidden window :data:`WINDOW_TITLE`, so the installer
    can find a running copy (``FindWindowByWindowName``) and close it with
    ``WM_ENDSESSION`` / ``ENDSESSION_CLOSEAPP`` before replacing its files."""
    hwnd = getattr(icon, "_hwnd", None)
    if hwnd:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined,unused-ignore]
        user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
        user32.SetWindowTextW(hwnd, WINDOW_TITLE)


def install_session_end(icon: Any, on_end: Callable[[], None]) -> None:
    """Stop cleanly when Windows logs off / restarts / shuts down, or when an
    installer (Restart Manager, ``packaging/windows/sortandpost.iss``) asks the
    app to close.

    pystray answers every message it doesn't know with 0, and 0 to
    ``WM_QUERYENDSESSION`` means "block the logoff" -- so answer it ourselves.
    ``WM_ENDSESSION`` (``wParam`` TRUE) is the last chance: at logoff the
    session ends once this handler returns, taking the server child with it,
    so ``on_end`` (final publish + lock release) runs *here*, synchronously.
    The block reason is what Windows shows if that takes more than a few
    seconds. Then the tray exits: an installer waits for the process to go,
    and the server child has no window of its own for it to close."""
    user32 = ctypes.windll.user32  # type: ignore[attr-defined,unused-ignore]
    # Explicit types: a 64-bit HWND doesn't fit ctypes' default C int.
    user32.ShutdownBlockReasonCreate.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    user32.ShutdownBlockReasonDestroy.argtypes = [wintypes.HWND]

    def _query(_wparam: int, _lparam: int) -> int:
        return 1

    def _end(wparam: int, _lparam: int) -> int:
        if not wparam:
            return 0  # the logoff was cancelled after all
        hwnd = getattr(icon, "_hwnd", None)
        if hwnd:
            user32.ShutdownBlockReasonCreate(hwnd, "Saving your photo library…")
        log.info("Windows session ending; stopping the server")
        try:
            on_end()
        finally:
            if hwnd:
                user32.ShutdownBlockReasonDestroy(hwnd)
            icon.stop()  # posts WM_STOP: the message loop ends after this returns
        return 0

    def _settext(wparam: int, lparam: int) -> int:
        # pystray swallows messages it doesn't know; let Windows store the
        # title name_window sets.
        return int(user32.DefWindowProcW(icon._hwnd, WM_SETTEXT, wparam, lparam))

    user32.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.DefWindowProcW.restype = wintypes.LPARAM
    icon._message_handlers[WM_QUERYENDSESSION] = _query
    icon._message_handlers[WM_ENDSESSION] = _end
    icon._message_handlers[WM_SETTEXT] = _settext
