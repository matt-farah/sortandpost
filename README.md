<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/brand/logo-dark.svg">
    <img src="docs/brand/logo-light.svg" alt="" height="110">
  </picture>
</p>

# sortandpost.photos

**Take your photos from 'living on my hard drive' to organized and posted to social media all locally and open sourced.**

sortandpost.photos helps you sort through all the photos you have taken, edit them using your favorite editing app, and post them to social media. 

sortandpost.photos has a privacy by design focus that can be run completely locally and offline, and never sends your data to anyone else. You can choose to sync your photos between computers using OneDrive, Google Drive, or another file-syncing service. 

sortandpost.photos believes that you should be in charge of your files. sortandpost.photos will never lock you into using our product. Most data is stored in the EXIF data of the files and can be moved to another organization software. Also sortandpost.photos never change your file structure that you already have. 

![The library grid](docs/images/library.png)


## Features
- **Your file system** sortandpost.photos never moves your folders from your existing structure. Organize your files how you like and let sortandpost help you find the best ones
- **Works fully offline** sortandpost.photos works fully offline. You can rate and sort your photos and even create draft posts offline.
- **Keyboard shortcuts.** Arrow keys to move, `1`–`5` to rate, `0` to clear, `f` to flag. This can help you move through your library quickly
- **AI tagging you approve.** A local machine learning model called CLIP is used to suggest tags for your photos that you can approve. This feature can be disabled at any time.
- **Ratings and tags stay with your files** Ratings and tags are applied to the EXIF data on your photos so it can be imported into other software and you won't lose data
- **Multi-computer, no server.** Sync your library using your own method *i.e. Google Drive, OneDrive, Dropbox* and sortandpost.photos keeps your information in sync between your PC, Mac, and Linux computers
- **Stacks** RAW+JPEG pairs and similarly-named files stack together. The files share ratings and tags and are recognized by having similar pixels in the images.
- **Collections** Create collections to put together a project images for a book, a social media post, or just for fun
- **Post to social media** through a scheduling service (robopost, Postiz, Zernio, Upload-Post). These apps all have free tiers to try out and their free tiers may meet your needs. Try them all and see what fits best.
- **Safe deletes.** "Delete" moves files into a recoverable trash with one-click undo.
- **Rate Photos** A rating page that chooses random photos for you to look through and rate quickly using your keyboard or mouse
- **Achievements** An optional achievements feature to help motivate you to organize and sort your photos

> [!WARNING]
> **Opening sortandpost.photos to other devices needs a password.** By default it only answers on
> this computer (`HOST=127.0.0.1`; Docker publishes on `127.0.0.1`). To use it from a phone
> or another computer, set `SORTANDPOST_PASSWORD` (or `SORTANDPOST_PASSWORD_FILE`) and, for
> Docker, `BIND=0.0.0.0`. On a network, plain HTTP lets others on that network read the
> password: put it behind HTTPS (see [docs/docker.md](docs/docker.md#https)). The macOS /
> Windows / Linux desktop app only listens on this computer. See [SECURITY.md](SECURITY.md).

## Install

### macOS app
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

#### Advanced
To build it yourself: `pwsh packaging\windows\build.ps1`.

### Linux app (AppImage)

Download `sortandpost-<version>-x86_64.AppImage` (or `-aarch64` for ARM) from
[Releases](https://github.com/matt-farah/sortandpost/releases), make it executable and run it:

```bash
chmod +x sortandpost-*.AppImage
./sortandpost-*.AppImage
```

No Python or exiftool to install (it uses the system `perl`, which every desktop distro has).
Needs glibc 2.35 or newer: Ubuntu 22.04+, Debian 12+, Fedora 36+. It opens the library in your
browser at `http://sortandpost.localhost:8080` and only listens on this computer.

- **Tray icon**: shown on KDE, most other desktops, and GNOME with the *AppIndicator* extension
  (Ubuntu ships it). Where there's no tray, the app still runs. Quit it from
  **Settings → Overview → Quit sortandpost.photos**.
- **FUSE**: AppImages need `fusermount` (`libfuse2` or `fuse3`). Without it, run
  `./sortandpost-*.AppImage --appimage-extract-and-run`.
- **API keys for posting** go in your Secret Service keyring (GNOME Keyring / KWallet). Without
  one, set the provider's env var (e.g. `ROBOPOST_API_KEY`) before starting the app.
- *Start at login* adds `~/.config/autostart/app.sortandpost.desktop`. If you move the
  AppImage, turn it off and on again.

To build it yourself: `packaging/linux/build.sh` (needs Docker; `--arch x86_64|aarch64`).

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

## Updates

The desktop apps check GitHub once a day for a new release (one small file, nothing about
you or your photos is sent) and show a banner when there is one. **Update & restart**
downloads it, checks its signature, saves your library, installs it and reopens the app.
Turn the check off in **Settings → Overview → Updates**, or with
`SORTANDPOST_NO_UPDATE_CHECK=1`. Docker and source installs only get the banner: update them
with `docker compose pull` / `git pull`.

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
| `packaging/` | Docker entrypoint, macOS `.app` / DMG, Windows installer and Linux AppImage builds |
| `tools/` | Dev helpers (Tailwind CSS build, UI copy export) |
| `docs/` | Architecture spec, Docker / NAS setup, security review |
| `dev-notes/` | Planning notes, roadmaps, progress log and issue history from development |
| `CLAUDE.md` | Instructions for Claude Code (imports `docs/architecture.md`) |

## License

[MIT](LICENSE)
