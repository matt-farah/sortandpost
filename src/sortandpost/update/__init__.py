"""In-app updates from GitHub Releases (docs/architecture.md → "Updates").

* :mod:`.manifest` -- the signed ``latest.json`` every release carries.
* :mod:`.source` -- who installed this copy, and so whether it may replace itself.
* :mod:`.check` -- the once-a-day check, the download, and the per-machine state.
* :mod:`.apply` -- hands a verified download to the platform's installer once
  the desktop app has shut down cleanly.
"""
