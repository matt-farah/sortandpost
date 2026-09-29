"""PyInstaller entry script for the packaged desktop app (every platform).

Frozen, ``sortandpost.__main__.main`` defaults to the tray; the tray starts the
server as ``<this executable> --serve``.
"""

from sortandpost.__main__ import main

if __name__ == "__main__":
    main()
