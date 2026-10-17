# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

The architecture spec — hard constraints, sync model, indexer, schema, UI — lives in
`docs/architecture.md` and is imported here. Keep it updated as decisions are made or changed.

@docs/architecture.md

---

## Where the docs live

- `docs/architecture.md` — the architecture spec (imported above). Code comments cite it
  as "docs/architecture.md constraint #N" / "→ <section>".
- `docs/` — also `docker.md` (Docker / NAS setup + plan) and `security-review.md`.
- `README.md` (users), `CONTRIBUTING.md` (dev setup + checks), `SECURITY.md` (reporting).
- `dev-notes/` — development history and planning: `PROGRESS.md` (log), `V1*`/`V2*`/`V3*`
  roadmaps + plans, `Issues.md`, `ToDo.md`, `achievements/` (source of the bundled
  achievements list + art), `copy/` (UI copy export from `tools/export_copy.py`).

---

## Commands

```bash
# Native dev (the main path; needs exiftool on the host — rawpy wheels bundle LibRaw)
uv sync --locked --extra dev --extra desktop   # exact: the venv == uv.lock
uv run python -m sortandpost                   # web app (first run: welcome wizard; or set APP_DATA)
uv run python -m sortandpost.indexer --once    # one full scan pass, then exit
uv run python -m sortandpost.indexer --reembed # recompute all CLIP embeddings (after model change)

# Maintenance
uv run python -m sortandpost.db snapshot         # write a snapshot now
uv run python -m sortandpost.db restore <path>   # restore from a snapshot, then reconcile-scan
uv run python -m sortandpost.db merge <path>     # fold a stray conflicted-copy DB into the current one

# Migrations
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "message"

# Tests + quality gate
uv run pytest
uv run pytest tests/test_hashing.py::test_pixel_hash_stable_across_exif_write -q
uv run ruff check . && uv run ruff format --check . && uv run mypy src
uv audit

# Tailwind (after changing utility classes in the UI)
tools/tailwind/build.sh                # -> src/sortandpost/resources/tailwind.css

# Desktop app (macOS)
uv run python -m sortandpost --tray    # menu-bar app from source (tray + server child)
packaging/macos/build.sh               # -> dist/sortandpost.app + dist/sortandpost-<ver>-arm64.dmg

# Desktop app (Windows, PowerShell)
pwsh packaging\windows\build.ps1       # -> dist\sortandpost-<ver>-setup.exe + -win-x64.zip

# Desktop app (Linux AppImage; needs Docker — builds in an ubuntu:22.04 container)
packaging/linux/build.sh [--arch x86_64|aarch64]   # -> dist/sortandpost-<ver>-<arch>.AppImage

# Docker (experimental; same CLIs with `docker compose run --rm app …`)
cp .env.example .env                   # set PHOTO_LIBRARY, SYNC_DIR, DEVICE_NAME, PORT
docker compose up --build              # http://localhost:8080
```

The indexer has a standalone entrypoint on purpose: CLIP on CPU inside Docker on macOS is slow,
so a user may run `python -m sortandpost.indexer` natively against the same `APP_DATA` for
speed. Nothing may assume the indexer is always in-container. (The single-writer lock still
applies — the native indexer and the container app must not both write.)

---

## Development workflow

**Every new feature gets a hands-on browser test by the user before it's considered done.**
Automated tests + a headless smoke are necessary but not sufficient. After building a feature:

1. Run the quality gate (ruff, ruff format, mypy on the gated paths, pytest, alembic
   autogenerate-empty, `uv audit`).
2. **Boot the app so the user can click through it themselves** — native run against the
   `browser_test_data` app-data tree on `http://localhost:8080`:

   ```
   APP_DATA=<repo>/browser_test_data PORT=8080 HF_HUB_OFFLINE=1 \
     .venv/bin/python -m sortandpost
   ```

   (`library_roots` there points at `~/Downloads/sampleImages`; ~300 photos already indexed;
   `.venv` has real torch + CLIP on MPS, and Playwright + Chromium for headless UI checks.)

   To test first-run onboarding as a **brand-new machine** without touching the real
   `~/.sortandpost` pointer: run from a directory with no `.env`, with no `APP_DATA`, and
   point `SORTANDPOST_PREF_DIR` + `SORTANDPOST_LOCAL_DIR` at empty scratch folders (symlink
   `browser_test_data/local/models` into `$SORTANDPOST_LOCAL_DIR/models` to skip the CLIP
   download). A second "machine" = another pair of scratch dirs picking the same shared folder.
3. Tell the user exactly which keys / buttons / flows to exercise, and what each should do.
4. Wait for their sign-off. Only then update `dev-notes/PROGRESS.md` and the
   current plan (`dev-notes/V3Plan.md` / `V3.md`), then commit.

Leave the app running until the user says they're done, then shut it down.

---
