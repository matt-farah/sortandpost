"""``python -m sortandpost`` / ``sortandpost`` — launch the web app."""

from __future__ import annotations

from sortandpost import app as web_app
from sortandpost.config import Env, Settings
from sortandpost.logging_setup import configure
from sortandpost.paths import default_local_root


def main() -> None:
    env = Env()
    if env.resolve_paths() is None:
        # First run on this machine: no data folder chosen yet. Serve only the
        # welcome step that picks one, then the app restarts on it.
        configure(default_local_root() / "logs")
        web_app.run_setup(env)
        return
    settings = Settings.resolve()
    configure(settings.paths.logs_dir)
    web_app.run(settings)


if __name__ == "__main__":
    main()
