"""Linux-only bits of the tray (V3 P7).

pystray picks its backend from ``PYSTRAY_BACKEND`` when it is first imported,
so :func:`pick_backend` runs before that:

* ``appindicator`` -- a StatusNotifier host is on the session bus (KDE, GNOME
  with the AppIndicator extension, Ubuntu's default, XFCE's plugin) and the
  AyatanaAppIndicator3 typelib loads. Stock GNOME hides X11 tray icons, so this
  is the only way an icon shows there.
* ``xorg`` -- otherwise, when the X display has an XEmbed tray host (the
  ``_NET_SYSTEM_TRAY_S<n>`` selection: XFCE, MATE, older desktops). GNOME on X11
  has none, and pystray would just log "Failed to dock icon".
* no tray at all -- no display, or no host. The tray process still runs (it
  supervises the server); :class:`HeadlessIcon` stands in for the icon and the
  app is quit from Settings → Desktop app → Quit (the server exits with
  ``QUIT_EXIT_CODE`` and the tray follows).

pystray's GTK backends hop onto the GTK main loop by themselves (its
``@mainloop`` decorator), so the tray can call ``update_menu`` from any thread.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import warnings
from collections.abc import Callable
from typing import Any

from sortandpost.binaries import host_env

log = logging.getLogger(__name__)

BACKEND_ENV = "PYSTRAY_BACKEND"
SNI_WATCHER = "org.kde.StatusNotifierWatcher"


def choose_backend(
    env: dict[str, str], *, appindicator: bool, sni_host: bool, xembed_host: bool
) -> str | None:
    """The pystray backend for this session, or ``None`` for no tray icon.
    A ``PYSTRAY_BACKEND`` the user set wins."""
    forced = env.get(BACKEND_ENV, "").strip()
    if forced:
        return forced
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        return None
    if appindicator and sni_host:
        return "appindicator"
    if env.get("DISPLAY") and xembed_host:
        return "xorg"
    return None  # nowhere to show an icon


def _appindicator_available() -> bool:
    try:
        import gi

        gi.require_version("AyatanaAppIndicator3", "0.1")
    except (ImportError, ValueError):
        return False
    return True


def _sni_host_present(timeout: float = 1.0) -> bool:
    """Is a StatusNotifierWatcher (the tray host) on the session bus?"""
    try:
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.blocking import open_dbus_connection

        bus = DBusAddress(
            "/org/freedesktop/DBus",
            bus_name="org.freedesktop.DBus",
            interface="org.freedesktop.DBus",
        )
        with open_dbus_connection(bus="SESSION") as conn:
            msg = new_method_call(bus, "NameHasOwner", "s", (SNI_WATCHER,))
            reply = conn.send_and_get_reply(msg, timeout=timeout)
        return bool(reply.body[0])
    except Exception:  # no session bus, no jeepney, a timeout: no host
        log.debug("no StatusNotifier host", exc_info=True)
        return False


def _xembed_host_present() -> bool:
    """Does the X display have a system-tray manager (XEmbed host)?"""
    try:
        from Xlib import X, display

        d = display.Display()
        try:
            atom = d.intern_atom(f"_NET_SYSTEM_TRAY_S{d.get_default_screen()}")
            return bool(d.get_selection_owner(atom) != X.NONE)
        finally:
            d.close()
    except Exception:  # no display, no python-xlib
        log.debug("no XEmbed tray host", exc_info=True)
        return False


def pick_backend() -> str | None:
    """Choose the backend (see the module docstring), export it for pystray and
    return it; ``None`` means run without a tray icon."""
    env = dict(os.environ)
    forced = bool(env.get(BACKEND_ENV, "").strip())
    backend = choose_backend(
        env,
        appindicator=not forced and _appindicator_available(),
        sni_host=not forced and _sni_host_present(),
        xembed_host=not forced and bool(env.get("DISPLAY")) and _xembed_host_present(),
    )
    if backend is not None:
        os.environ[BACKEND_ENV] = backend
    # pystray's GTK helper still calls the deprecated GObject.idle_add.
    warnings.filterwarnings("ignore", message=r"GObject\.idle_add is deprecated")
    log.info("tray backend: %s", backend or "none (quit from Settings)")
    return backend


class HeadlessIcon:
    """Stands in for ``pystray.Icon`` when there's nowhere to show one: ``run``
    blocks the main thread until ``stop`` (so signals still reach Python)."""

    visible = False

    def __init__(self) -> None:
        self._stopped = threading.Event()

    def run(self, setup: Callable[[Any], None] | None = None) -> None:
        if setup is not None:
            threading.Thread(target=setup, args=(self,), name="sortandpost-tray-setup").start()
        while not self._stopped.wait(0.5):
            pass

    def stop(self) -> None:
        self._stopped.set()

    def update_menu(self) -> None:
        pass


def install_session_end(on_end: Callable[[], None]) -> None:
    """Logout / ``kill`` send SIGTERM (a closed terminal, SIGHUP): stop cleanly
    -- final publish + lock release -- instead of dying with the server
    orphaned. ``on_end`` blocks, so it runs on a thread of its own."""

    def _handler(signum: int, _frame: object) -> None:
        log.info("signal %d: quitting", signum)
        threading.Thread(target=on_end, name="sortandpost-signal-quit").start()

    signal.signal(signal.SIGTERM, _handler)
    if sys.platform != "win32":  # no SIGHUP there (mypy checks this module on Windows too)
        signal.signal(signal.SIGHUP, _handler)


# Tried in order when there's no xdg-open (xdg-utils isn't on every minimal
# install). Not :mod:`webbrowser`: it starts its programs with this process's
# environment, i.e. the bundle's LD_LIBRARY_PATH, and a host `gio` then fails
# with "undefined symbol" against the bundle's older GLib.
_FALLBACK_OPENERS: tuple[tuple[str, ...], ...] = (
    ("gio", "open", "--"),
    ("sensible-browser",),
    ("x-www-browser",),
    ("firefox",),
    ("chromium",),
    ("chromium-browser",),
    ("google-chrome",),
)


def _opener_command(target: str, env: dict[str, str]) -> list[str] | None:
    """The command that opens ``target``: xdg-open, then ``$BROWSER`` (a URL
    only), then the first of :data:`_FALLBACK_OPENERS` on ``PATH``."""
    path = env.get("PATH")
    xdg = shutil.which("xdg-open", path=path)
    if xdg:
        return [xdg, target]
    if "://" in target:
        for entry in env.get("BROWSER", "").split(os.pathsep):
            parts = entry.split()
            if parts and (exe := shutil.which(parts[0], path=path)):
                args = [a.replace("%s", target) for a in parts[1:]]
                return [exe, *args] if "%s" in entry else [exe, *args, target]
    for cmd in _FALLBACK_OPENERS if "://" in target else _FALLBACK_OPENERS[:1]:
        if exe := shutil.which(cmd[0], path=path):
            return [exe, *cmd[1:], target]
    return None


def open_path(target: str) -> None:
    """Open a folder / URL with the desktop's handler, outside the bundle's
    libraries (:func:`sortandpost.binaries.host_env`)."""
    env = host_env()
    cmd = _opener_command(target, env)
    if cmd is None:
        log.warning("nothing here can open %s (install xdg-utils); open it by hand", target)
        return
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )


def notify(title: str, message: str) -> None:
    """A desktop notification (best effort; there's no dialog toolkit here)."""
    sender = shutil.which("notify-send")
    if sender is None:
        log.warning("%s: %s", title, message)
        return
    try:
        subprocess.run(
            [sender, "--app-name=sortandpost.photos", title, message],
            env=host_env(),
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        log.warning("%s: %s", title, message)
