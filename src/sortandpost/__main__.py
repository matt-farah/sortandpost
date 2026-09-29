"""``python -m sortandpost`` / ``sortandpost`` -- launch the web app.

``--tray`` runs the desktop app instead (menu-bar item + the server as a child
process, :mod:`sortandpost.desktop`); that is the default for the packaged app.
``--serve`` is how the desktop app starts its server child.
"""

from __future__ import annotations

import multiprocessing
import os
import sys


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _frozen_ca_bundle() -> None:
    """A packaged app has no system Python CA store: point HTTPS (the CLIP
    weights download, robopost) at the bundled certifi file."""
    try:
        import certifi
    except ImportError:  # pragma: no cover
        return
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())


def serve() -> None:
    from sortandpost import app as web_app
    from sortandpost.config import Env, Settings
    from sortandpost.desktop.control import control_token
    from sortandpost.logging_setup import configure
    from sortandpost.paths import default_local_root

    console = control_token() is None  # the desktop app's server child has no terminal
    env = Env()
    if env.resolve_paths() is None:
        # First run on this machine: no data folder chosen yet. Serve only the
        # welcome step that picks one, then the app restarts on it.
        configure(default_local_root() / "logs", console=console)
        web_app.run_setup(env)
        return
    settings = Settings.resolve()
    configure(settings.paths.logs_dir, console=console)
    web_app.run(settings)


def main() -> None:
    # First thing: in a packaged app a process-pool worker re-runs this
    # executable, and must turn into the worker here, not start a tray / server.
    multiprocessing.freeze_support()
    if _frozen():
        _frozen_ca_bundle()
    args = sys.argv[1:]
    if "--tray" in args or (_frozen() and "--serve" not in args):
        from sortandpost.desktop.tray import run_tray

        raise SystemExit(run_tray())
    serve()


if __name__ == "__main__":
    main()
