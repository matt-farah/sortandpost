# sortandpost

Locally-run photo organization app. Scans existing photo folders, lets you rate photos 1–5
stars, auto-tags them with CLIP, and provides natural-language semantic search — in the
browser, no cloud service. Your library's state (database, thumbnails, search index) lives in
one shared folder you keep in any file-sync tool (OneDrive, Google Drive, Synology Drive,
Dropbox) — pick the same folder on another computer and it works there with no rebuild.

**Architecture and design decisions live in [CLAUDE.md](CLAUDE.md). Read it first.**

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
cp .env.example .env          # set PHOTO_LIBRARY, APP_DATA, PORT
docker compose up --build     # http://localhost:8080
docker compose run --rm indexer --once
```

## Development

```bash
uv sync --extra dev
uv run python -m sortandpost            # web app (first run: welcome wizard; or set APP_DATA)
uv run pytest
uv run ruff check . && uv run mypy src
```

Build order and status are tracked in CLAUDE.md → "Suggested build order".

# How to run locally for testing

.venv/bin/python -m sortandpost
