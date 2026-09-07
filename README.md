# sortandpost

Locally-run photo organization app. Scans existing photo folders, lets you rate photos 1–5
stars, auto-tags them with CLIP, and provides natural-language semantic search — in the
browser, no cloud service. All state lives in one app-data folder you can put in any file-sync
tool (OneDrive, Google Drive, Synology Drive, Dropbox) to work across machines with no rebuild.

**Architecture and design decisions live in [CLAUDE.md](CLAUDE.md). Read it first.**

## Quick start

```bash
cp .env.example .env          # set PHOTO_LIBRARY, APP_DATA, PORT
docker compose up --build     # http://localhost:8080
```

On first boot the app creates `APP_DATA/local/` and `APP_DATA/sync/` and writes a default
`sync/config.yaml` and `sync/tags.yaml`. Point your file-sync tool at `APP_DATA/sync/`.

## One-shot index

```bash
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
