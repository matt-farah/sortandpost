# sortandpost

Locally-run photo organization app. Scans existing photo folders, lets you rate photos 1–5
stars, auto-tags them with CLIP, and provides natural-language semantic search — in the
browser, no cloud service. All state lives in one app-data folder you can put in any file-sync
tool (OneDrive, Google Drive, Synology Drive, Dropbox) to work across machines with no rebuild.

**Architecture and design decisions live in [CLAUDE.md](CLAUDE.md). Read it first.**

## Quick start (native — recommended)

Runs directly on your machine: native filesystem speed, GPU CLIP (Apple MPS /
CUDA), and "Reveal in Finder / Explorer" works with no helper process.

```bash
# 1. one external tool
brew install exiftool           # macOS   (Linux: apt install libimage-exiftool-perl; Windows: winget install exiftool)

# 2. install
uv sync                         # or: pipx install .

# 3. point it at your photos + an app-data folder
export PHOTO_LIBRARY=~/Pictures
export APP_DATA=~/sortandpost-data          # put the `sync/` subfolder in OneDrive/Drive/Dropbox later
uv run python -m sortandpost                # http://localhost:8080
```

First boot creates `APP_DATA/local/` (machine-only) and `APP_DATA/sync/` (writes a
default `config.yaml` + `tags.yaml`) and downloads the CLIP model (~600 MB, once —
do this before going offline). Point your file-sync tool at `APP_DATA/sync/`.

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
uv run python -m sortandpost            # web app against ./appdata by default
uv run pytest
uv run ruff check . && uv run mypy src
```

Build order and status are tracked in CLAUDE.md → "Suggested build order".
