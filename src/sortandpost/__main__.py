"""``python -m sortandpost`` / ``sortandpost`` — launch the web app."""

from __future__ import annotations

from sortandpost import app as web_app
from sortandpost.config import Settings
from sortandpost.logging_setup import configure


def main() -> None:
    settings = Settings.resolve()
    configure(settings.paths.logs_dir)
    web_app.run(settings)


if __name__ == "__main__":
    main()
