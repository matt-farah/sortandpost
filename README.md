# sortandpost.photos

**Rate, tag, search and post your photos — locally, in your browser, with no cloud service.**

sortandpost.photos scans the photo folders you already have and gives you a fast, keyboard-first
library on top of them: rate photos 1–5 stars, let CLIP suggest tags, and find anything by
describing it ("dog on a beach at sunset"). It never reorganizes your files. Your library's
state (database, thumbnails, search index) lives in one shared folder you keep in any
file-sync tool (OneDrive, Google Drive, Synology Drive, Dropbox), so another computer picks
it up with no rebuild.

<!-- Screenshots: add docs/images/library.png, photo.png, search.png and uncomment.
![The library grid](docs/images/library.png)
-->

## Features

- **Keyboard-first rating.** Arrow keys to move, `1`–`5` to rate, `0` to clear, `f` to flag.
- **AI tagging you approve.** CLIP (open_clip, runs on your machine — Apple MPS / CUDA / CPU)
  suggests tags from an editable vocabulary; only tags you approve are kept.
- **Semantic search.** Natural-language search and "more like this", plus filters for stars,
  camera, date range, folder, tag, GPS, duplicates and more. Group by month, day, folder or
  camera.
- **Your files stay yours.** Photos are never renamed or moved. Ratings and approved tags are
  written into the file's standard XMP / IPTC metadata (JPEG, HEIC, TIFF, PNG) so Lightroom
  and Windows Explorer see them. RAW files are read-only.
- **Multi-computer, no server.** Pick the same shared folder on another computer and your
  ratings, tags and search index are there. One computer edits at a time; the others can hand
  editing over with one click. Conflicting copies from a slow sync are merged automatically.
- **Stacks, duplicates, collections.** RAW+JPEG pairs and "copy" files stack together;
  identical photos at several paths are recognised by their pixels; build ordered collections.
- **Post to social media** through a scheduling service you already use (robopost, Postiz,
  Zernio, Upload-Post), with clean exported JPEGs (no EXIF / GPS) and Instagram-ready crops.
- **Safe deletes.** "Delete" moves files into a recoverable trash with one-click undo.
- Seasonal light / dark themes, and optional achievements to make sorting a bit of a game.

> [!WARNING]
> **Opening sortandpost.photos to other devices needs a password.** By default it only answers on
> this computer (`HOST=127.0.0.1`; Docker publishes on `127.0.0.1`). To use it from a phone
> or another computer, set `SORTANDPOST_PASSWORD` (or `SORTANDPOST_PASSWORD_FILE`) and, for
> Docker, `BIND=0.0.0.0`. On a network, plain HTTP lets others on that network read the
> password: put it behind HTTPS (see [docs/docker.md](docs/docker.md#https)). The macOS /
> Windows desktop app only listens on this computer. See [SECURITY.md](SECURITY.md).

## Install

### macOS app (easiest)

Download `sortandpost-<version>-arm64.dmg` (Apple Silicon) from
[Releases](https://github.com/matt-farah/sortandpost/releases), drag **sortandpost** to
Applications and open it. No Python or exiftool needed. It lives in the **menu bar**: the ★
icon opens the library in your browser and lets you pause or resume indexing, start
sortandpost.photos at login, show logs, and quit. Quitting finishes the current photo, saves the
library to your shared folder and releases it for your other computers. If photos you
deleted are still waiting in *Recently deleted*, Quit asks whether to move them to the
Trash.

The app isn't notarized yet, so the first time you open it, **right-click → Open**, or go to
System Settings → Privacy & Security → *Open Anyway*. It opens at
`http://sortandpost.localhost:8080` and only listens on this computer. To build it yourself,
run `packaging/macos/build.sh` (see
[docs/architecture.md → Desktop app](docs/architecture.md#desktop-app-v2-p5-sortandpostdesktop-packaging)).

### Windows app

Download `sortandpost-<version>-setup.exe` from
[Releases](https://github.com/matt-farah/sortandpost/releases) and run it. It installs for
your user only (no admin prompt) and lives in the system tray, like the macOS app. It isn't
code-signed yet, so Windows SmartScreen may ask you to confirm (**More info → Run anyway**).
To build it yourself: `pwsh packaging\windows\build.ps1`.

### From source (any OS)

Runs directly on your machine: native filesystem speed, GPU CLIP (Apple MPS /
CUDA), and "Reveal in Finder / Explorer" works.

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

### Docker (experimental — server / NAS / headless)

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
  warning if it finds any (in the shared folder or among your photos).

The container runs as user `1000:1000`, not root. On a Linux host or a Synology, set
`PUID` / `PGID` in `.env` to the owner of your photo and shared folders (`id <user>`), or
rating writes and the shared folder will fail with "permission denied". Docker Desktop
on a Mac or PC works with the default. `docker ps` shows the container as `healthy` once
the web UI answers.

**Opening photos under Docker.** A container can't open Finder or File Explorer, so the
photo page's copy button copies the file's path instead. Give each library folder its
**network path** in **Settings → Library** (e.g. `\\nas\photo` or `smb://nas.local/photo`)
and it copies a path your other computers can open: `\\nas\photo\…` on Windows (paste in
Explorer's address bar), `smb://nas/photo/…` on a Mac (Finder → Go → Connect to Server).
[docs/docker.md](docs/docker.md) covers the always-on NAS setup and bind-mount speed.

**Editing from a desktop app while Docker runs.** Only one computer edits at a time, and
Docker usually holds editing. In the desktop app, click **Take over editing** in the top
bar. Docker hands over in about 30 seconds, unless someone using Docker clicks **Keep
editing**. When you quit the desktop app, Docker takes editing back. Docker's own page
has the same button.

## How it works

Every photo is identified by a hash of its decoded pixels, so moves, renames, copies and
metadata edits never lose its ratings or tags. The app runs against a local working copy of
its SQLite database and atomically publishes a snapshot into the shared folder, which is what
makes plain file-sync tools safe to use. The full design — constraints, sync model, indexer,
schema — is in [docs/architecture.md](docs/architecture.md).

## Contributing

Bug reports and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the dev
setup and checks, and [SECURITY.md](SECURITY.md) to report a vulnerability privately.

## Repository layout

| Path | What's there |
|------|--------------|
| `src/sortandpost/` | The app (web UI, indexer, sync layer, CLIP, posting, desktop tray) |
| `tests/` | pytest suite |
| `migrations/` | Alembic migrations for the SQLite schema |
| `packaging/` | Docker entrypoint, macOS `.app` / DMG and Windows installer builds |
| `tools/` | Dev helpers (Tailwind CSS build, UI copy export) |
| `docs/` | Architecture spec, Docker / NAS setup, security review |
| `dev-notes/` | Planning notes, roadmaps, progress log and issue history from development |
| `CLAUDE.md` | Instructions for Claude Code (imports `docs/architecture.md`) |

## License

[MIT](LICENSE)
