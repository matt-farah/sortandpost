"""macOS-only bits of the tray, via PyObjC (which pystray already uses there).

Everything here must be called on the main thread (AppKit), except
:func:`call_on_main`, which is how the tray's worker threads get back there.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import AppKit
import Foundation
from PyObjCTools import AppHelper


def call_on_main(fn: Callable[[], object]) -> None:
    """Run ``fn`` on the AppKit main thread (from any thread)."""
    AppHelper.callAfter(fn)


def use_menu_bar_only() -> None:
    """No Dock icon, no app menu -- the packaged app sets ``LSUIElement`` in its
    Info.plist; this does the same when the tray runs from source."""
    AppKit.NSApplication.sharedApplication().setActivationPolicy_(
        AppKit.NSApplicationActivationPolicyAccessory
    )


def ask(title: str, message: str, *, yes: str, no: str) -> bool:
    """A modal alert with two buttons; True for ``yes`` (the default button)."""
    app = AppKit.NSApplication.sharedApplication()
    app.activateIgnoringOtherApps_(True)  # a menu-bar app's alert must come to the front
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(message)
    alert.addButtonWithTitle_(yes)
    alert.addButtonWithTitle_(no)
    return bool(alert.runModal() == AppKit.NSAlertFirstButtonReturn)


def notify(title: str, message: str) -> None:
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(message)
    AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    alert.runModal()


class _AppDelegate(Foundation.NSObject):  # type: ignore[misc]
    on_reopen: Callable[[], None] | None = None
    on_terminate: Callable[[], None] | None = None

    def applicationShouldHandleReopen_hasVisibleWindows_(self, app: Any, flag: bool) -> bool:
        # Launching the app again (Finder, Spotlight, Dock) while it's running.
        if self.on_reopen is not None:
            self.on_reopen()
        return True

    def applicationShouldTerminate_(self, app: Any) -> int:
        # Logout / restart / shutdown, `osascript -e 'quit app "sortandpost"'`,
        # Ctrl-C in a terminal: stop the server cleanly first (final publish +
        # lock release), off the main thread, then let AppKit finish quitting.
        if self.on_terminate is None:
            return int(AppKit.NSTerminateNow)
        handler = self.on_terminate

        def _work() -> None:
            try:
                handler()
            finally:
                AppHelper.callAfter(app.replyToApplicationShouldTerminate_, True)

        threading.Thread(target=_work, name="sortandpost-terminate", daemon=True).start()
        return int(AppKit.NSTerminateLater)


_delegate: _AppDelegate | None = None


def install_app_delegate(
    *, on_reopen: Callable[[], None], on_terminate: Callable[[], None]
) -> None:
    global _delegate
    _delegate = _AppDelegate.alloc().init()
    _delegate.on_reopen = on_reopen
    _delegate.on_terminate = on_terminate
    AppKit.NSApplication.sharedApplication().setDelegate_(_delegate)
