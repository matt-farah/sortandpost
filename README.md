# sortandpost

Locally-run photo organization app. Scans existing photo folders, lets you rate photos 1–5
stars, auto-tags them with CLIP, and provides natural-language semantic search — in the
browser, no cloud service. Your library's state (database, thumbnails, search index) lives in
one shared folder you keep in any file-sync tool (OneDrive, Google Drive, Synology Drive,
Dropbox) — pick the same folder on another computer and it works there with no rebuild.

**Architecture and design decisions live in [CLAUDE.md](CLAUDE.md). Read it first.**

> [!WARNING]
> **Only run sortandpost on a network you trust.** There is no login yet. Run from source
> (or in Docker with `BIND=0.0.0.0`), the web UI listens on every network interface, so anyone on
> the same network can browse your photos, delete them (moved to the app's trash), change
> settings, and send robopost posts. The macOS / Windows desktop app only listens on this
> computer. To keep a source run local, start it with `HOST=127.0.0.1`. A login screen is
> planned for the Docker release ([Docker.md](Docker.md)). See [Security.md](Security.md).

## macOS app (easiest)

Download `sortandpost-<version>-arm64.dmg` (Apple Silicon), drag **sortandpost** to
Applications and open it. No Python or exiftool needed. It lives in the **menu bar**: the ★
icon opens the library in your browser and lets you pause or resume indexing, start
sortandpost at login, show logs, and quit. Quitting finishes the current photo, saves the
library to your shared folder and releases it for your other computers. If photos you
deleted are still waiting in *Recently deleted*, Quit asks whether to move them to the
Trash.

The app isn't notarized yet, so the first time you open it, **right-click → Open**, or go to
System Settings → Privacy & Security → *Open Anyway*. It opens at
`http://sortandpost.localhost:8080` and only listens on this computer. To build it yourself, run `packaging/macos/build.sh` (see CLAUDE.md →
"Desktop app").

## Quick start (native — from source)

Runs directly on your machine: native filesystem speed, GPU CLIP (Apple MPS /
CUDA), and "Reveal in Finder / Explorer" works with no helper process.

```bash
# 1. one external tool
brew install exiftool           # macOS   (Linux: apt install libimage-exiftool-perl; Windows: winget install exiftool)

# 2. install
uv sync                         # or: pipx install .

# 3. run it
uv run python -m sortandpost                # http://localhost:8080
```

The first launch opens a short **welcome wizard**: pick a shared folder (ideally inside
your cloud drive), your photo folders, and a few preferences. Nothing is scanned until you
finish. On another computer, pick the same shared folder — your library, ratings, tags and
settings carry over, and you only confirm that computer's photo folders. Machine-only files
(working database, logs, trash) stay in `~/Library/Application Support/sortandpost`
(`%LOCALAPPDATA%` on Windows, `~/.local/share` on Linux). The CLIP model (~600 MB) downloads
once on first scan — do that before going offline.

Setting `APP_DATA=<folder>` instead keeps the older single-folder layout
(`<folder>/local` + `<folder>/sync`; sync only `sync/`) and skips the folder step.

One-shot index (also runs continuously inside the app):

```bash
uv run python -m sortandpost.indexer --once
```

## Docker (server / NAS / headless)

For an always-on box that indexes 24/7 while you browse from a laptop. CLIP runs
CPU-only in the container and bind-mount scans are slower on macOS/Windows — use
the native path for a desktop.

```bash
cp .env.example .env          # set PHOTO_LIBRARY, SYNC_DIR, DEVICE_NAME, PORT
docker compose up --build     # http://localhost:8080 (this computer only; BIND=0.0.0.0 opens it up)
docker compose run --rm indexer --once
```

`SYNC_DIR` is the shared library folder, the same one your desktop apps pick. The
container's own working files go in a Docker volume (or `LOCAL_DIR`), never in the
synced folder. Two common setups:

- **Synology NAS**: run it in Container Manager with `SYNC_DIR` set to the shared
  folder on the NAS. Your computers reach the same folder through Synology Drive.
- **An always-on Mac or PC**: run Docker Desktop with `SYNC_DIR` inside Google Drive or
  OneDrive. Set that folder to **Always keep on this device** (OneDrive) or **Mirror
  files** (Google Drive). Docker can't download online-only files, so the app shows a
  warning if it finds any.

**Editing from a desktop app while Docker runs.** Only one computer edits at a time, and
Docker usually holds editing. In the desktop app, click **Take over editing** in the top
bar. Docker hands over in about 30 seconds, unless someone using Docker clicks **Keep
editing**. When you quit the desktop app, Docker takes editing back. Docker's own page
has the same button.

## Development

```bash
uv sync --locked --extra dev --extra desktop
uv run python -m sortandpost            # web app (first run: welcome wizard; or set APP_DATA)
uv run pytest
uv run ruff check . && uv run mypy src
```

Build order and status are tracked in CLAUDE.md → "Suggested build order".

# How to run locally for testing

.venv/bin/python -m sortandpost
