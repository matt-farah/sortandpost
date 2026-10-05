# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## What this project is

**sortandpost** is a locally-run photo organization app. It scans one or more existing photo
folders, lets the user rate photos 1–5 stars, auto-tags them with CLIP, and provides
natural-language semantic search — all in a browser, with **no cloud service**.

The whole app state (the SQLite database, generated thumbnails, and CLIP embeddings) lives in a
single **app-data folder** that the user drops into any third-party file-sync tool (OneDrive,
Google Drive, Synology Drive, Dropbox, …). Opening the app on another machine picks up the
existing index, ratings, tags, and embeddings with **no rebuild**.

This document is the architecture spec the code was built from (V1 + V2 shipped). Keep it
updated as decisions are made or changed.

---

## Hard constraints — do not violate these

1. **Never modify the user's photo files or folder structure**, with exactly two exceptions:
   1. writing embedded EXIF/XMP metadata (ratings, approved keywords) into formats that support
      it (JPEG, HEIC, TIFF, PNG);
   2. the **explicit user-invoked "Delete photos" action** (bulk toolbar / photo page, behind a
      confirm dialog) *moves* the selected photo files into the managed, never-synced
      `<APP_DATA>/local/trash/<batch>/` folder (with a `manifest.json`) on every platform — and
      its reverse, the explicit **"Put back" / "Undo delete"**, which moves them back to their
      exact original paths (refused if that path is now taken or its folder is gone; it never
      creates a folder). A separate user click, **"Move all to system Trash"** (macOS / Windows
      only, via `send2trash`), hands the managed batches to the OS Trash. The desktop app's
      Quit asks the same thing ("Move N deleted photos to the Trash?"), and only its
      **Move to Trash** button does it. None of these is ever
      an `unlink`, writes anything else under a library root (no `.Trash-*` dir on the library
      filesystem), or runs automatically — only from a user click. RAW files are moved by these
      actions too (constraint #2 governs *metadata writes*, not an explicit user delete).

   Outside those two: no renames, no moves, no other deletes, no new files, no new folders, no
   `.xmp` sidecars anywhere under a library root — ever.
2. **RAW files are read-only.** No metadata is written to them or beside them. Their ratings and
   tags live only in the database.
3. **Photo identity is the pixel hash**, never a file path and never a whole-file hash.
   `pixel_hash = sha256(decoded RGB bytes at native resolution)`. It is immune to our own EXIF
   writes and to re-saves that don't change pixels. Every cross-reference — thumbnails,
   embeddings, `changes` log, tags — keys on `pixel_hash`.
4. **The database is the source of truth and it travels with the sync folder.** It is never
   opened directly from the synced folder (see Sync model) and it must always be recoverable
   from the newest snapshot, but user decisions (ratings, tags, approvals) are not stored
   anywhere else. Any new user-authored state MUST also emit a row into the `changes` log or
   cross-machine conflict merges will silently drop it.
5. **CLIP predictions are never written to photo files.** Only tags the user explicitly approves
   in the UI are written to `XMP:Subject` / IPTC keywords.
6. **The indexer must be idempotent and resumable.** Assume it is killed mid-run constantly
   (Docker restarts, laptop sleep, sync churn).
7. **Never block the web event loop** with image decode, CLIP inference, or exiftool calls —
   all CPU-bound work goes through the process pool.
8. **The library may be partially offline** (unmounted network drive, mid-sync). Missing files
   are marked `missing_since`, never purged automatically.
9. **Only one machine writes at a time.** A second machine that opens the app while another
   holds the lock runs **read-only** until the lock goes stale (see Sync model).

---

## Stack

| Concern            | Choice |
|--------------------|--------|
| Language / runtime | Python 3.12–3.13 (`.python-version` pins **3.13**: dev venv + desktop builds) |
| Package manager    | `uv` + `pyproject.toml` + committed **`uv.lock`** (desktop builds sync `--locked`; Docker still `uv pip install .`) |
| Web UI             | NiceGUI (browser mode, not native), served by its bundled uvicorn |
| CSS                | Tailwind **prebuilt** (`tools/tailwind/build.sh` → `resources/tailwind.css`, run after changing classes); NiceGUI runs `tailwind=False` — its in-browser compiler froze the infinite-scroll grid |
| DB                 | SQLite via SQLModel/SQLAlchemy, migrations via Alembic |
| Embedded metadata  | `exiftool` as a persistent `-stay_open` subprocess |
| Image decode       | Pillow + pillow-heif (HEIC); `rawpy`/LibRaw for RAW (embedded preview) |
| ML                 | `open_clip_torch`, default `ViT-B-32` / `laion2b_s34b_b79k`; `device: auto` (MPS/CUDA/CPU) |
| Vector search      | NumPy brute-force cosine over a memory-mapped `float32` matrix (target ≤ ~25k photos) |
| Compression        | `zstandard` for DB snapshots |
| Filesystem watch   | `watchdog` |
| Lint / format / types | `ruff check`, `ruff format`, `mypy` |
| Tests              | `pytest` + `pytest-asyncio` |
| Deployment         | Docker Compose, single container; **macOS app** = PyInstaller onedir `.app` + DMG (`packaging/macos/build.sh`); **Windows app** = PyInstaller onedir + Inno Setup installer + zip (`packaging/windows/build.ps1`) |
| Desktop tray       | `pystray` (+ PyObjC on macOS) — `desktop` optional extra |

---

## Commands (target — implement to match)

```bash
# First run
cp .env.example .env                 # set PHOTO_LIBRARY, APP_DATA, PORT, CLIP_MODEL
docker compose up --build            # app at http://localhost:8080  (config.yaml auto-created in APP_DATA on first boot)

# Indexer (also runs continuously in the background inside the app container)
docker compose run --rm app python -m sortandpost.indexer --once      # one full scan pass, then exit
docker compose run --rm app python -m sortandpost.indexer --reembed   # recompute all CLIP embeddings (after model change)

# Maintenance
docker compose run --rm app python -m sortandpost.db snapshot         # write a snapshot now
docker compose run --rm app python -m sortandpost.db restore <path>   # restore from a snapshot, then reconcile-scan
docker compose run --rm app python -m sortandpost.db merge <path>     # fold a stray conflicted-copy DB into the current one

# Migrations
docker compose run --rm app alembic upgrade head
docker compose run --rm app alembic revision --autogenerate -m "message"

# Tests
docker compose run --rm app pytest
docker compose run --rm app pytest tests/test_hashing.py::test_pixel_hash_stable_across_exif_write -q

# Quality
docker compose run --rm app ruff check .
docker compose run --rm app ruff format .
docker compose run --rm app mypy src

# Desktop app (macOS)
uv run python -m sortandpost --tray    # menu-bar app from source (tray + server child)
packaging/macos/build.sh               # -> dist/sortandpost.app + dist/sortandpost-<ver>-arm64.dmg

# Desktop app (Windows, PowerShell)
pwsh packaging\windows\build.ps1       # -> dist\sortandpost-<ver>-setup.exe + -win-x64.zip

# Native dev (no Docker — requires exiftool + libraw on host)
uv sync --locked --extra dev --extra desktop   # exact: the venv == uv.lock
uv run python -m sortandpost           # web app
uv run python -m sortandpost.indexer --once
uv run pytest tests/path::test_name -q
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
   autogenerate-empty).
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
4. Wait for their sign-off. Only then update `PROGRESS.md` / `V1Plan.md` (+ `V1Features.md`) and commit.

Leave the app running until the user says they're done, then shut it down.

---

## App-data layout

The app's data has two roots: **`local/`** (machine-specific, never synced) and **`sync/`**
(the shared folder the user's file-sync tool carries between machines). They are placed in
one of two ways (`sortandpost.paths.Paths`):

- **Split layout** (what the first-run wizard sets up, `Paths.for_sync_dir`): the user
  picks only the shared folder, which *is* `sync/`. `local/` goes in a per-machine OS
  folder, **one per shared folder**: `<default_local_root>/libraries/<name>-<hash8>/`
  (`~/Library/Application Support/sortandpost`, `%LOCALAPPDATA%\sortandpost`,
  `$XDG_DATA_HOME/sortandpost`; `SORTANDPOST_LOCAL_DIR` overrides). CLIP weights are shared
  across libraries in `<default_local_root>/models`. So a second machine that picks the same
  shared folder never shares the first one's `device.json` or working DB, and switching
  libraries never carries a working DB from one library into another.
- **Single folder** (`Paths(app_data=X)`): `X/local` + `X/sync`. What a real `APP_DATA`
  env var (Docker, dev), a `.env` value, and pre-onboarding saved folders give. Here the user
  must point their sync tool at `sync/` only.

**Which folders?** In order (`Env.resolve_paths()` / `Env.paths_source()`,
`sortandpost.config`): a genuine process/shell `APP_DATA` env var (Docker's fixed container
mount, or `APP_DATA=... command`) — always wins, can't be changed from the UI; else the
shared folder picked in the wizard or **Settings → Shared library folder**, recorded outside
any data tree in `~/.sortandpost/app_data_path.json` as `{"sync_dir": …}` (see
`sortandpost.launcher_pref`; `SORTANDPOST_PREF_DIR` overrides the folder); else a legacy
`{"app_data": …}` pointer (single folder); else a bare value left in a `.env` file (a soft
default, ranked *below* a saved folder); else **nothing** — the app starts in first-run
setup mode (below) and the CLIs exit with "not set up yet". Telling a real env var from a
`.env`-file value apart means checking `os.environ` directly rather than trusting
pydantic-settings' own (order-collapsing) env/dotenv merge. Changing the folder from
Settings is **Save & restart**: a clean shutdown (final publish, lock release) and an
in-place re-exec (`app._restart_app` / `_reexec_if_requested`).

### First-run onboarding (`sortandpost.onboarding`, `/welcome`)

1. **Setup mode** — no folder chosen (`resolve_paths()` is `None`): `app.run_setup` serves
   only `/welcome`'s "Where should your library live?" step — no DB, no lock, no indexer.
   `inspect_folder` tells the pick apart: an existing sync tree (the folder itself, its
   `sync/` child for a legacy app-data folder, or a `sortandpost/` child) → **joined**; a
   missing/empty folder → new library there; a folder with unrelated files → new library in
   `<folder>/sortandpost`. The pointer is saved and the process re-execs.
2. **Wizard** — folders open but no `local/onboarding.json`: every page redirects to
   `/welcome` (HTTP middleware) and the indexer — in-app and `python -m
   sortandpost.indexer` — does not start. Steps: library data → this machine's photo
   folders (other devices' folders that exist here are offered one-click) → *new library
   only:* scanning speed + excluded folders, stacking (on by default) + delete scope → look
   (theme/season) → Ready → **Start** writes the marker and starts the indexer. A machine
   joining an existing library adopts the shared `config.yaml` as-is. New-vs-joined is
   remembered across a mid-wizard restart (`local/onboarding_started.json`).
3. **Upgrades** — a single-folder install whose `local/device.json` already exists is marked
   finished silently, so existing installs never see the wizard. A brand-new native machine
   doesn't get the template's Docker `/library` seeded as a library folder.
4. `/` reloads itself while the first scan fills an under-one-page library
   (`_reload_as_photos_arrive`), so photos appear without a manual refresh.

```
<APP_DATA>/                    # single-folder layout; in the split layout local/ and sync/ live apart
  local/                       # machine-specific — NEVER synced
    sortandpost.db (+ -wal/-shm/-journal while running)   # the working copy the app runs against
    device.json                # {"device_id": "<random>", "hostname": "..."}  created on first run
    publish_state.json         # last published DB generation counter + hash
    helper.json                # {port, token, ...} — written by the optional host file-manager agent
    robopost.json              # cached robopost channel list — NO secrets (the API key is in the OS keychain)
    post_exports/<post_id>/    # temporary clean JPEGs for an upload; removed after the send
    models/                    # downloaded CLIP weights
    logs/
  sync/                        # user points OneDrive / Drive / Synology / Dropbox HERE
    sortandpost.db             # published snapshot of the DB — the cross-machine source of truth
    .sortandpost.lock          # {device_id, hostname, pid, acquired_at, heartbeat_at, db_generation}
    config.yaml
    devices/<slug(hostname)>-<device_id[:8]>/library.json # PER-DEVICE library folders (see below)
    posts/<post_id>.json       # robopost posts, one file each (see "Posting" below)
    tags.yaml                  # user-editable zero-shot vocabulary (facet-grouped)
    snapshots/sortandpost-YYYYMMDD-HHMMSS.db.zst          # last N clean snapshots (corruption recovery)
    thumbnails/<hh>/<pixel_hash>_<size>.webp              # content-addressed, write-once, conflict-free
    embeddings/img_<model>.npy                            # append-mostly memmap, L2-normalized rows
    embeddings/text_<model>_<vocabhash>.npy               # cached vocabulary text embeddings
    embeddings/rowmap.json                                # row_index -> pixel_hash
```

### Library folders are per-device

`sync/devices/<slug(hostname)>-<device_id[:8]>/library.json` holds this machine's
library folders as `{"roots": [{container, host, label}, …]}`:

- `container` — the path the **scanner** walks (a Docker bind-mount target,
  usually `/library`; on a native run just the real folder).
- `host` — the real path on *this* computer, used for display, "copy path", and
  the file-manager helper. Same as `container` when running natively.

It rides in `sync/` so the sync tool backs it up, but a device only ever writes
its **own** subfolder, so two devices never collide (unlike `config.yaml`). The
folder is matched by the `-<device_id[:8]>` suffix, so a hostname change still
adopts the existing folder. `config.yaml`'s `library_roots` key is now only a
one-time seed for this file on first boot after upgrading. Edit roots in the
Settings page. See `sortandpost.library_roots`.

### File-manager helper (optional)

Under Docker the server is a Linux container with no host GUI, so "Reveal in
Finder / File Explorer" and the native folder browser need a small agent run on
the **host**, outside Docker: `python3 tools/sortandpost_helper.py --app-data
<host APP_DATA>`. It listens on `127.0.0.1`, writes `local/helper.json`
(`{port, token}`, seen by the container through the `/data` mount), and the
browser calls it directly. Token-gated, CORS-restricted to localhost origins,
and `/reveal` + `/open` are confined to registered `host` roots. Everything
degrades gracefully to a "copy host path" button when the agent isn't running.
See `sortandpost.helper` (server-side reader) + `tools/sortandpost_helper.py`.

`config.yaml` (created from a template on first boot):

```yaml
library_roots: ["/library"]        # one-time PER-DEVICE seed only (see above)
server: { host: 0.0.0.0, port: 8080 }
sync:
  publish_interval_sec: 60         # how often the working DB is published to sync/ when dirty & idle
  lock_heartbeat_sec: 20
  lock_stale_after_sec: 300        # a lock older than this (no heartbeat) may be taken over
  snapshot_every_publishes: 20
  snapshot_keep: 10
  sync_thumbnails: true            # thumbnails can be GBs; set false to keep them in local/ only
clip:
  model: ViT-B-32
  pretrained: laion2b_s34b_b79k
  device: auto        # auto -> cuda / Apple MPS / cpu; Docker on macOS always lands on cpu
  batch_size: 16
  speed: slow         # slow = ~30% of cores, batch <= 8, low priority; fast = cores - 1 (see sortandpost.cpu)
tagging:
  vocab_file: tags.yaml
  score_threshold: 0.22
  top_k: 8
  prompt_templates: ["a photo of {}", "{}"]
thumbnails: { sizes: [320, 1280], format: webp, quality: 80 }
indexer:
  full_scan_interval_min: 15
  workers: auto                    # follows clip.speed unless an int
  clip_deferred: true              # make photos browsable first, run CLIP after
  online_only: skip                # cloud placeholder stubs: "skip" (record, don't fetch) | "download"
rating: { write_windows_compat_tags: true }
deletion:
  stack_scope: selected             # "selected" = only the clicked photo; "whole_stack" = every stack member
```

---

## Sync model (the load-bearing design)

Goal: the index, thumbnails, tags, and CLIP data all ride along in `sync/` so a second machine
never rebuilds. Third-party sync tools corrupt SQLite files that are written in place, so:

### DB: local working copy + atomic publish

- The app runs against `local/sortandpost.db` with `PRAGMA journal_mode=DELETE`,
  `synchronous=FULL`, `foreign_keys=ON`. No `-wal`/`-shm` ever lands in `sync/`.
- **Publish**: every `publish_interval_sec` while the DB is dirty and no write transaction is
  open (and on clean shutdown): copy `local/sortandpost.db` → `sync/.sortandpost.db.tmp-<device>`,
  `fsync`, then `os.replace()` onto `sync/sortandpost.db`. The synced file is therefore always a
  complete, consistent snapshot. Bump a monotonic `db_generation` counter (stored in a
  `meta` row and in the lock file).
- **Snapshots**: every `snapshot_every_publishes` publishes, also write
  `sync/snapshots/sortandpost-<ts>.db.zst`; prune to `snapshot_keep`. This is the only
  recovery path that ever forces a rescan.
- **Startup**:
  1. Read `.sortandpost.lock`. If it has a fresh heartbeat from another `device_id` →
     **read-only mode** (see below). Otherwise write our own lock and start the heartbeat.
  2. Scan `sync/` for conflicted copies (`sortandpost*.db` other than the canonical name,
     names containing `conflict`/`conflicted`/`(1)`/`(2)`/hostname suffixes). If any →
     run the **merge** (below), then archive them under `sync/snapshots/merged/`.
  3. If `sync/sortandpost.db` `db_generation` > `local` generation → copy it to the working
     copy. If `local` is ahead (we had unpublished edits, no conflict) → keep local and
     publish.
  4. `PRAGMA quick_check`. On failure → restore newest `snapshots/*.db.zst`, then run a
     reconcile scan to catch up anything newer than the snapshot.

### Single-writer lock

- `sync/.sortandpost.lock` holds `{device_id, hostname, pid, acquired_at, heartbeat_at,
  db_generation}`. A heartbeat thread rewrites `heartbeat_at` every `lock_heartbeat_sec`.
- A lock whose `heartbeat_at` is older than `lock_stale_after_sec` is considered abandoned and
  may be taken over (the previous holder crashed or the machine slept).
- **Read-only mode**: the UI loads and browsing/search/viewing all work, but the star widget,
  tag editors, and CLIP approve/reject are disabled with a persistent banner naming the machine
  that holds the lock. The app re-checks the lock periodically and promotes itself to writable
  once the lock is free or stale.

### Conflict-copy merge

Two machines can still both edit if the sync tool is slow to propagate (machine A stops,
machine B starts before A's publish arrives), producing a "conflicted copy". Merge is
deterministic because every user-authored mutation also appends to an in-DB `changes` log:

```
changes(id, ts, device_id, op, pixel_hash, field, old_value, new_value)
        op ∈ { rate, tag_add, tag_remove, clip_approve, clip_reject, unindex, restore }
```

Merge algorithm:
1. Take the copy with the highest `db_generation` as the base.
2. Union every `changes` row from all copies; order by `(ts, device_id)`; replay onto the base.
   User-authored tables (`ratings`, manual `photo_tags`, `predictions.state`) become
   last-writer-wins per `(pixel_hash, field)`.
3. Union the derived tables by `pixel_hash` (`photos`, `photo_paths`, `embedding_rows`,
   `predictions` scores). Both machines scanned overlapping sets, so a union is safe; anything
   still missing is picked up by the next reconcile scan.
4. Publish the merged DB, archive the conflict copies, and log a summary to `/settings`.

Thumbnails and embeddings need no merge: thumbnails are content-addressed and write-once (two
machines generate byte-identical files), and the embeddings matrix is append-only with the row
count tracked in the DB.

### EXIF reconciliation

For embed-capable files, ratings and approved tags are also written into the file (portability,
Lightroom/Windows). On re-index, if a file's embedded `XMP:Rating` differs from the DB value:
if the file's mtime is newer than the DB row's `updated_at`, the file wins and we emit a
`changes` row; otherwise the DB wins and we rewrite the file.

### Deletion (explicit, user-invoked — constraint #1 exception 2)

The "Delete photos" action is the one destructive operation. On the machine that runs it:

1. Present file copies of each selected `pixel_hash` are **moved to the managed trash** (see
   constraint #1): one batch folder per delete, `<APP_DATA>/local/trash/<batch>/` with
   `files/<seq>/<original name>`, `thumbs/<pixel_hash>.webp` and a `manifest.json` that also
   saves each photo's user state (rating, tags, CLIP prediction states). RAW included.
   Duplicates (one `pixel_hash`, several present paths) all go together. A file is moved
   **only** if it resolves inside one of this machine's library roots **and** still holds
   the deleted pixels (unchanged `size`/`mtime_ns`, else re-hashed): `photo_paths` rides
   the shared DB, so a recorded path may name another machine's or a replaced file
   (Security.md H2). Refused files stay put and are reported as not moved.
2. The DB rows for that `pixel_hash` are purged — `photos`, `photo_paths`, `ratings`,
   `photo_tags`, `predictions`, `embedding_rows`, `scan_state` — the embedding matrix is
   compacted, and the content-addressed thumbnails are deleted.
3. A `changes` row `op=unindex` is appended (constraint #4) **and** a `tombstones` row is
   written: `(pixel_hash, deleted_at, deleted_by_device)`.

**Undo / Put back** (`sortandpost.undelete.put_back_batch`, from the status bar's "Undo delete"
for 5 minutes after a delete, or `/settings` → "Recently deleted"): move the batch's files back
(`trash.put_back`), drop each photo's tombstone and append a `changes` row **`op=restore`**,
re-index those paths immediately, then re-insert the saved rating / tags / prediction states. It
runs under the indexer's `_INDEX_LOCK`. The managed trash is a short-lived "oops" buffer, not a
history: **"Move all to system Trash"** (`trash.flush_to_system_trash`, macOS/Windows) forgets
the batches after handing their files to the OS Trash. The desktop app's Quit offers it too
(a dialog, never automatic). On Linux / Docker there is no flush (send2trash could create a `.Trash-<uid>` on a library
volume), so batches stay until put back or cleaned up by hand.

**Stack scope** is a config choice, `deletion.stack_scope` — `selected` (default; only the
clicked photo) or `whole_stack` (every stack member, like ratings fan out).

**Cross-machine.** Conflict-copy merge settles `unindex` vs `restore` per `pixel_hash`,
last-writer-wins by `(ts, device_id, op)` over the unioned `changes` log: a winning `unindex`
writes the same `tombstones` row on every machine (and sets `missing_since`); a winning
`restore` drops it, so the next scan re-indexes the file. A machine that already lazily trashed
its own copy keeps it in its own "Recently deleted" (`reason: tombstone`) for a manual put back. Each machine then applies its
tombstones on its next scan: purge the local DB rows / thumbnails / embedding row as above, and
**lazily move its own local copy of the file to trash** if the file is still present. The
scanner refuses to re-index a `pixel_hash` that has a tombstone, so a not-yet-deleted file
cannot resurrect a deleted photo. `tombstones` is append-only and travels in the DB; it is
never GC'd (a tiny row per deleted photo, bounded by real deletes).

**Caveat — this genuinely removes files from the library folder.** "Move to trash" is not a
soft-delete flag: the file leaves its folder for `<APP_DATA>/local/trash/`. Recovery is the
in-app Put back, until the batch is moved to the system Trash. After that, the OS Trash's own
Put Back only returns the file to `local/trash/<batch>/files/…`, and a file moved back into the
library by hand stays **tombstoned** (the scanner skips it) — there is no un-tombstone UI yet. If a **library root is itself inside a third-party sync folder** (OneDrive,
Drive, Dropbox, …), that sync tool then propagates the deletion to every other copy and to the
provider's own trash/version history, independently of our `tombstones` mechanism. The confirm
dialog must state plainly that files are moved out of the library; never widen this action to
skip the dialog, act on a hover, or run from anything other than a direct click.

---

## Indexer pipeline

Continuous background task in the app container (+ standalone entrypoint). Per file, keyed by
real path in `scan_state`, each stage idempotent and separately resumable:

1. **stat + fast path** — `(size, mtime_ns)` unchanged and fully indexed ⇒ skip. (Our own EXIF
   writes bump mtime; that only triggers a metadata refresh, not re-thumbnail/re-embed, because
   the pixel hash is unchanged.) A cloud **online-only placeholder** (OneDrive / Drive / Dropbox
   / iCloud stub whose bytes aren't on disk) is detected here from the `stat` alone
   (`sortandpost.indexer.placeholder.is_online_only`) and, when `indexer.online_only == "skip"`,
   recorded as `scan_state.online_only` and left untouched — `attempts` is **not** bumped (it is
   a deferral, not a failure). The `/` banner + `/settings` "Download & index" action
   (`sortandpost.indexer.hydrate`) does a bounded read to force the download, then a normal scan
   indexes it. `online_only: "download"` hydrates every stub the scan meets instead.
2. **decode** — Pillow / pillow-heif / rawpy. RAW uses the largest embedded preview for
   determinism and speed (full demosaic only if no preview exists).
3. **pixel hash** — `sha256(rgb_bytes)`. Upsert `photos` (unique on `pixel_hash`). Same hash at
   a new path ⇒ move; same hash at multiple present paths ⇒ duplicate (tracked in `photo_paths`).
4. **thumbnails** — configured sizes, WebP, written by hash. Skip if present.
5. **metadata read** — one batched exiftool call: `DateTimeOriginal`, orientation, GPS, camera
   make/model, existing `XMP:Rating`, existing `Subject`/`Keywords`.
6. **rating reconcile** — file vs DB, per the rule above.
7. **CLIP** — image embedding appended/replaced in the memmap matrix (+ `rowmap`); zero-shot
   scores vs the vocabulary text embeddings written to `predictions`. Deferred to a lower-
   priority queue when `clip_deferred: true` so browsing works before CLIP finishes.
8. **finalize** — set `scan_state.indexed_at`, clear dirty flags.

Deletions: file gone ⇒ set `photos.missing_since` and `photo_paths.present = false`. Never purge
without an explicit user action (see Sync model → Deletion). A pending `tombstones` row is the
one case the scanner acts on: it skips re-indexing that `pixel_hash` and, on the post-scan
sweep, trashes the still-present local file and purges the derived rows.

Concurrency: `ProcessPoolExecutor` for decode/hash/thumbnails, sized by `clip.speed` (`slow` ≈ 30% of
cores at lowered priority, the default; `fast` = `cpu_count - 1`); CLIP inference runs in the app/indexer
process with the same thread budget. `watchdog`
observers on the library roots, debounced into a **scoped** pass over just the changed paths
(`scan_once(scope=…)`: folders walked, files checked alone, presence swept only under them;
hidden / excluded / app-data / non-image events dropped, a symlinked root's resolved event
paths mapped back onto the root's spelling; > 500 paths → full pass), plus a periodic full
reconcile scan (`full_scan_interval_min`) as the safety net for missed events. The indexer only runs when this machine holds the write lock.

---

## Metadata engine (`sortandpost.metadata`)

The **only** module allowed to write under a library root. Wraps a persistent
`exiftool -stay_open True` process; batches reads and writes.

- **Rating** → `XMP-xmp:Rating` (integer 0–5; 0 clears). When `write_windows_compat_tags`,
  also mirror to `EXIF:Rating` / `EXIF:RatingPercent` and `XMP-microsoft:RatingPercent` for
  Windows Explorer / Lightroom parity.
- **Approved tags** → `XMP-dc:Subject` (+ `IPTC:Keywords`).
- Writes use `-overwrite_original_in_place` to preserve inode, permissions, and xattrs. Let
  mtime update so sync tools and other apps notice the change.
- **Never** writes CLIP predictions, UUIDs, or anything else. RAW paths are rejected at this
  layer as a safety net.
- **Stacks write one file.** Ratings fan out to every stack member in the DB; **tags live
  on one member only** (`app._tag_home`: add / approve / bulk tag go there, the photo page
  shows its tags, remove clears every member). Only the stack's `stacks.embed_target` (its
  newest non-RAW, embed-capable member — the top photo unless the top is a RAW) gets the
  file write (`app._file_exif`; reconcile's write-back too). Writing every member
  changed the originals' mtimes and flipped the stack's top photo.
  The photo page's **"Make this the top photo"** (`app._apply_make_top`) picks the top on
  purpose: it moves the stack's tags onto that photo (`db.tags.move_tags`), then
  `ExiftoolClient.restamp` rewrites that photo's rating + keywords (unrated as an explicit
  `XMP:Rating=0`, so the file always changes) and refreshes `photo_paths`.
- `-stay_open` reads **one argument per line**: `_execute` refuses any argument containing
  `\n` / `\r` / NUL, since a line break would inject exiftool options (`-Directory=` moves
  the file, `-if` runs Perl — Security.md C1). Tag names are normalized / validated by
  `sortandpost.tag_names` wherever they're created.

---

## CLIP tagging & search

- **Embeddings**: `open_clip` image embeddings, L2-normalized, stored as rows in
  `sync/embeddings/img_<model>.npy` (memmap). `embedding_rows` maps `pixel_hash -> row_index`.
  `embeddings_meta` records model/pretrained/dim; a mismatch triggers `--reembed`.
- **Vocabulary**: `sync/tags.yaml`, user-editable, facet-grouped (scene, subject, lighting,
  season, setting, activity, count, …), seeded with a sensible default set. For each term,
  average the normalized text embeddings of the `prompt_templates`, cache to
  `embeddings/text_<model>_<vocabhash>.npy`.
- **Zero-shot tagging**: cosine sim of each photo embedding to every vocab term; keep terms
  `>= score_threshold` and within `top_k`. Rows go to `predictions(pixel_hash, tag, score,
  model, run_at, state='pending')`. The user approves/rejects in `/review`; `approve` ⇒
  `photo_tags(source='clip')` + EXIF write + `changes` row; `reject` is recorded so re-runs
  don't re-surface it.
- **Semantic search**: query text ⇒ CLIP text embedding ⇒ dot product vs the memmap matrix ⇒
  top-N. Also image→image ("more like this"). Brute-force NumPy is the design target; if the
  library outgrows ~25k photos, add an on-disk ANN index (hnswlib / sqlite-vec) behind the same
  search interface — do not change the interface.

---

## Database schema (core tables)

- `photos` — `pixel_hash` (unique), `primary_path`, dimensions, `format`, `is_raw`,
  `orientation`, `taken_at`, camera make/model, GPS, `file_size`, `file_mtime_ns`,
  `first_indexed_at`, `last_indexed_at`, `missing_since`, `phash` (perceptual, near-dup).
- `photo_paths` — `pixel_hash`, `path`, `size`, `mtime_ns`, `present`. Handles duplicates/copies.
- `ratings` — `pixel_hash` (pk), `stars` (0–5), `updated_at`, `updated_by_device`, `source`
  (`app`|`file`).
- `tags` — `id`, `name` (unique), `facet`, `kind`.
- `photo_tags` — `pixel_hash`, `tag_id`, `source` (`manual`|`clip`), `score?`, `added_at`,
  `added_by_device`, `written_to_file`.
- `predictions` — `pixel_hash`, `tag_name`, `score`, `model`, `run_at`,
  `state` (`pending`|`approved`|`rejected`).
- `changes` — append-only user-authored mutation log (see Sync model). Powers conflict merge,
  audit, and undo.
- `tombstones` — `pixel_hash` (pk), `deleted_at`, `deleted_by_device`, `applied` (has this
  machine purged its derived rows + trashed its local file yet). Written by an explicit delete
  and by `unindex` merge replay; the scanner refuses to re-index a tombstoned `pixel_hash`.
- `embedding_rows` — `pixel_hash` (pk), `row_index`, `model`.
- `embeddings_meta` — `model`, `pretrained`, `dim`, `count`, `updated_at`.
- `scan_state` — `path` (pk), `pixel_hash`, per-stage progress, `error`, `attempts`.
- `meta` — key/value: `db_generation`, `schema_version`, etc.

---

## Frontend (NiceGUI pages)

- `/` — the library **and** search (merged 2026-09-24). Virtualized thumbnail grid,
  keyboard-first: arrows navigate, `1`–`5` rate, `0` clears, `f` flags, `/` focuses search.
  A compact toolbar: search box, then icon buttons whose popovers hold each filter (★ stars
  incl. unrated, camera, date-range calendar, folder, subject/tag any|all, ⋯ GPS / duplicates /
  ⚑ / missing / posted) — tinted with a count badge while active — then the group-by menu
  button (Month · Day · Folder (+ section sort) · Camera · Flat, with collapse/expand-all),
  saved searches and clear. Active filters also show as removable chips under the bar
  (`LibraryQuery.chips()`). All state lives in the URL (`sortandpost.library_view.LibraryQuery`).
  **Group-by defaults to Month** (no `group` param); `group=flat` is the ungrouped grid.
  Filters keep `taken_at` order + group-by; a text query ranks by CLIP relevance with group-by off.
  Subject/tag also matches un-rejected CLIP suggestions.
- `/photo/{pixel_hash}` — large preview, star widget, EXIF panel, a **Tags** section between
  the facts and Paths (applied pills; dashed ghost pills for pending CLIP suggestions with
  ✓ approve / ✕ dismiss; ✕ on every applied tag — on an approved CLIP tag it also rejects the
  suggestion so it isn't re-suggested; an add-tag input — in a stack, tags live on its top photo
  only, see "Stacks write one file"), a Collections row (rounded-square chips), "more like this". Links carry the grid's query string;
  prev/next walk the same resolved view (`resolve_view`, cached per query for 30 min).
- `/search` — redirects to `/` (old `stars=N` meant "≥ N" and is converted).
- `/review` — pending CLIP predictions grouped by tag for bulk approve/reject.
- `/welcome` — first-run onboarding wizard (see "First-run onboarding"); every other page
  redirects here until this machine has finished it.
- `/settings` — organized into categories (sticky side rail of anchors on wide windows; each
  category a responsive 2-column grid of `sp-card` panels via `_settings_section` /
  `_settings_card`): Overview (stat tiles + indexing progress), Library, Indexing & AI,
  Appearance & apps, Posting, Deleting, Sync & storage. Lock-gated panels still render in
  read-only mode with a note instead of disappearing. Contents: CLIP model & thresholds, re-index / re-embed actions, sync status (lock holder,
  last publish, `db_generation`, snapshots, last merge summary), the shared-folder picker with Save & restart (see
  "App-data layout" above), and a CLIP-weights cache check / force-download control (`Check if
  downloaded` / `Download / verify CLIP model`, `sortandpost.clip.model.check_cached` /
  `ensure_downloaded` — toggles `HF_HUB_OFFLINE` around the probe so it works even when the app
  itself was launched offline).
- `/posts` + `/posts/{id}` — the robopost composer (see "Posting (robopost.app)" below).
  Entry points: "Compose post" on a collection, ✈ in the library's bulk toolbar. The grid
  shows a ✈ badge on posted photos and `/` has a `posted=1|0` filter.
- Persistent status bar: index progress (`X/Y`, current stage), CLIP queue depth, write-lock
  state (writable / read-only + holder), last publish time.

---

## Theming (seasonal palettes)

Two independent **per-machine** appearance prefs in `local/ui_prefs.json` (never synced,
`sortandpost.prefs`): **mode** Light / Dark / System, and **season** Spring / Summer / Fall /
Winter / **Auto** (default — follows today's date, northern-hemisphere meteorological seasons:
Mar–May Spring, Jun–Aug Summer, Sep–Nov Fall, Dec–Feb Winter). Season × mode = 8 looks, picked
in `/settings` → Appearance (swatch cards show each palette).

The palettes (user-chosen — keep verbatim; source of truth is `sortandpost.themes.PALETTES`):

| Season | Palette |
|---|---|
| Spring | `#fec5bb #fcd5ce #fae1dd #f8edeb #e8e8e4 #d8e2dc #ece4db #ffe5d9 #ffd7ba #fec89a` |
| Summer | `#f7b267 #f79d65 #f4845f #f27059 #f25c54` |
| Fall   | `#003049 #d62828 #f77f00 #fcbf49 #eae2b7` |
| Winter | `#10002b #240046 #3c096c #5a189a #7b2cbf #9d4edd #c77dff #e0aaff` |

How they're applied:
- `sortandpost.themes.TOKENS[(season, dark)]` maps each look to CSS design tokens
  (`--sp-bg`, `--sp-surface`, `--sp-surface-2`, `--sp-border`, `--sp-text`, `--sp-text-dim`,
  `--sp-text-faint`, `--sp-primary`, `--sp-on-primary`, `--sp-secondary`, `--sp-accent`,
  `--sp-focus`, `--sp-statusbar-*`) plus Quasar's `--q-primary/secondary/accent`.
  `theme_css()` emits them keyed on `<body>` classes `sp-season-<s>` (+ Quasar's `body--dark`);
  the palette also shows as a hairline stripe under the app bar.
- **First paint**: those body classes only land when Vue mounts, but the theme CSS is already
  applied, so every full page load has a moment where `--sp-bg` is the `:root` light default.
  `first_paint_css()` (emitted per page in `<head>` by `_header`) sets the `<html>` background,
  `color-scheme` and `--sp-bg` for the resolved look, so Firefox / Safari don't flash white.
  Any token that paints large areas before mount must be set there too.
- Palette colors are used as-is where they read well; where a role needs more contrast
  (body text, a pastel primary used as text) the value is a **derived** shade, commented as such.
  Rough roles: Spring — blush bg, peach status bar, deepened coral primary (pastel `#fec5bb` as
  primary in dark). Summer — apricot tint bg, coral status bar, deepened `#f25c54` primary
  (`#f79d65` in dark). Fall — parchment bg, navy text + status bar, `#d62828` primary
  (`#f77f00` in dark). Winter — lavender tint bg, `#240046` status bar, `#5a189a` primary
  (midnight `#10002b` bg with `#c77dff` primary in dark).
- `tests/test_themes.py` enforces WCAG contrast for every look (text ≥ 7, dim ≥ 4.5, faint ≥ 3,
  primary ≥ 3 on bg/surface, on-primary ≥ 4.5, status bar ≥ 4.5).
- Use the tokens (`sp-muted`, `sp-faint`, `sp-placeholder`, `color=primary`) — not hard-coded
  Tailwind grays / hex colors — so new UI follows the season. Amber stays for warnings / the
  edit flag. Quasar's `!important` color utilities live in NiceGUI's `quasar_importants` cascade
  layer; to override one, put the `!important` rule in `@layer theme` (earliest layer wins).

## Posting (robopost.app)

`sortandpost.robopost` (HTTP client), `posts` (records, validation, payload),
`post_export` (clean JPEGs), `post_send` (orchestration), `posts_ui` (pages),
`secrets_store` (API key).

- **API key** — never in a file under `APP_DATA`. Native: saved from `/settings` into the OS
  keychain via `keyring`. Docker: `ROBOPOST_API_KEY` env var (always wins). No plaintext
  fallback. Never sent to the browser (write-only field, `••••` + last 4); `apikey=` is
  redacted from every log line, exception and stored error. **Keychain calls block while an
  OS prompt is open** (an ad-hoc-signed rebuild asks again), so they only run off the event
  loop, and the key is read once per process and cached (`secrets_store.cached_robopost_key`
  is the non-blocking view).
- **API facts** (verified live 2026-09-25): auth is an `?apikey=` query param. Exact paths
  matter — `GET /v1/auth`, `GET /v1/channels/`, `POST /v1/medias/upload`,
  `POST /v1/scheduled_posts`; the other slash form gets a **307 to plain http carrying the key**,
  so redirects are never followed. `image_object_ids` takes the upload's
  **`storage_object_id`**, not its `id` (robopost silently accepts a wrong id and posts no
  image). There is no read / edit / delete for a post and no alt text.
- **Posts** live in `sync/posts/<post_id>.json`, one file each (a deliberate exception to
  constraint #4, like collections: not `pixel_hash` state, not in `changes`, not merged). Status:
  `draft` → `sending` → `sent_draft` | `scheduled` (or `failed`, retryable). A sent post is
  frozen; "Duplicate" makes a new draft.
- **No double posts** — `sending` is written before the create call, which is never retried.
  A network error / timeout / 5xx on create leaves the post `sending` for the user to settle
  ("It was sent" / "It wasn't sent") after checking robopost; only a 4xx becomes `failed`.
  Uploads are cached per photo + export settings, so a retry doesn't re-upload.
- **Split Instagram send** (`posts.plan_send`) — Instagram channels get their own create with
  uploads fitted onto one Instagram canvas (`post_export.IgSpec` / `fit_and_pad`; format, border
  and pad colour in `network_settings["instagram"]`, never sent to robopost; cached as
  `media_cache["<hash>#ig"]`). Other channels go first with the normal export. If every photo
  already has an Instagram ratio (4:5, 1:1, 1.91:1; 9:16 for a Story) it is one create to every
  channel. Each create is a `Post.parts` entry (`sending → sent | failed` + its `channel_ids`):
  a failed / unknown first part stops the send, a retry only goes to channels no `sent` part
  reached, and "It was sent / wasn't" settles only the part in doubt. `failed` with a sent part
  = **Partly sent** (still editable; its sent channels are locked).
- **Schedule quick-pick**: the composer's "When" grid offers today + 6 days ×
  `posts.SLOT_HOURS` (9 / 12 / 17 / 19 local), one stable random minute per post + day
  (`slot_minute`); "Pick a date & time" opens free-form inputs.
- **Uploads are never the source file**: decoded like the indexer (RAW preview / HEIC), rotated
  upright, converted to sRGB, scaled to `posting.export_long_edge` (default 2048, 0 = full) at
  `posting.export_quality` (90), saved with no EXIF / GPS / ICC, in the process pool.
- **Guardrails**: sending needs the write lock and an explicit confirm dialog listing each photo,
  channel, and the local + UTC time. Instagram ≤ 10 photos and Pinterest's pin title block the
  send; other per-network limits are warnings.

---

## Docker

- One service `app` in `docker-compose.yml`. Base `python:3.12-slim` + `exiftool`, `libraw`,
  build deps.
- Mounts: `${PHOTO_LIBRARY}:/library:rw` (rw is required for EXIF writeback; only the metadata
  engine ever writes there), `${APP_DATA}:/data:rw`. Multiple libraries ⇒ more mounts
  (`/library/0`, `/library/1`, …) plus `library_roots` in config.
- Port `${PORT:-8080}:8080`.
- CLIP weights: mount `/data/local/models` as the cache so the image stays small and the model
  is swappable. (Baking weights into the image is an allowed alternative — document whichever is
  chosen.)
- Apple Silicon: the image is arm64 and CLIP runs on CPU (no MPS/CUDA inside Docker on macOS).
  A first full index of ~25k photos can take hours; that is expected — the indexer is background
  and resumable. Running the indexer natively is the escape hatch.

---

## Desktop app (V2 P5; `sortandpost.desktop`, `packaging/`)

The packaged app is **two processes from one executable**. The tray (AppKit / pystray) and
uvicorn each want the main thread.

- **Tray** (`desktop.tray`, the default when frozen; `--tray` from source):
  - Takes `app.lock` in `default_local_root()` (an OS file lock, so a second launch just
    opens the browser on the first one) and picks `127.0.0.1:8080` (next free up to 8099).
  - Starts the server child `<exe> --serve` (`desktop.supervisor`) with `HOST` / `PORT`,
    `SORTANDPOST_CONTROL_TOKEN` and `SORTANDPOST_PARENT_PID`.
  - Writes `runtime.json` (`{pid, host, port, token, version}`, 0600) and opens the browser
    at **`http://sortandpost.localhost:<port>`**. Any `*.localhost` name resolves to this
    computer with no DNS or hosts entry, so there's no setup and it works offline. The tray
    itself talks to the server on `127.0.0.1`. `https://sortandpost.local` (hosts entry +
    locally trusted certificate) and a public-domain certificate were scoped and rejected:
    admin prompts / a certificate-issuing service, and no security gain on loopback.
  - Menu: status, Open, Pause/Resume indexing, Start at login (macOS: a LaunchAgent that
    runs `open -a …app`; Windows: an `HKCU\…\Run` value; packaged app only), Show logs, Quit.
  - Menu-bar only (`LSUIElement`). Relaunching from Finder opens the browser
    (`applicationShouldHandleReopen`).
- **Server** = the normal web app. `desktop.control` mounts token-gated
  `/_desktop/{status,pause,resume,flush-trash,quit}` only when launched by the tray, and a
  parent watch shuts the server down cleanly if the tray dies. A source / Docker run has
  none of this.
- **Shutdown is one ordered hook** (`app._shutdown_all`): stop the indexer and join it, then
  `manager.stop()` (final publish + lock release, constraints #4 / #9), then exiftool.
  - The indexer stops **between files**: `scan_once` / `scan_clip_once` take an `interrupt`
    callable. An interrupted scan skips the presence sweep (it hasn't seen every path).
  - Pause uses the same mechanism (`State.indexer_paused`).
  - Tray Quit → `/quit` → wait 60 s → SIGTERM → SIGKILL only as a last resort. Logout goes
    through `applicationShouldTerminate` → the same stop, with no dialog.
- **Bundled exiftool**: `sortandpost.binaries.exiftool_path()` resolves in order:
  `$SORTANDPOST_EXIFTOOL`, then the bundle's `exiftool/exiftool` (the Perl distribution, run
  by macOS's `/usr/bin/perl`), then `PATH`. Never hard-code `"exiftool"`.
- **CLIP weights** are not bundled. They download on first use into
  `default_local_root()/models`, and the progress stage `downloading_model` labels it.
- **Build**: `packaging/macos/build.sh` pins the exiftool version + sha256, renders the icon
  from `desktop.icon`, runs PyInstaller (`sortandpost.spec`, onedir, arm64), signs, and
  makes the DMG.
  - Signing is ad-hoc unless `SORTANDPOST_SIGN_ID` is set; notarization runs when
    `NOTARY_PROFILE` is also set.
  - `multiprocessing.freeze_support()` is the first call in `__main__.main`, because the
    spawn pool re-runs the executable.
- **Restart** (a new data folder, the wizard's folder pick): under the tray the server exits
  with `control.RESTART_EXIT_CODE` (75) and the tray starts a fresh server on the same port
  (`TrayApp._watch_for_restart`). It never re-execs itself there: on Windows `os.execv`
  spawns a new process, and the tray would be left holding a dead one.

### Windows (V2 P6; `desktop._windows`, `packaging/windows/`)

- **Build**: `pwsh packaging\windows\build.ps1` → `dist\sortandpost\` (onedir, `console=False`,
  `.ico` + version info), `dist\sortandpost-<ver>-win-x64.zip`, and — with Inno Setup 6 —
  `dist\sortandpost-<ver>-setup.exe`. Bundles exiftool's standalone Windows zip
  (sha256-pinned; `exiftool(-k).exe` renamed to `exiftool.exe`). Unsigned.
- **Installer** (`sortandpost.iss`): per-user (`%LOCALAPPDATA%\Programs\sortandpost`, no
  admin prompt). It never touches the library, the shared folder or `%LOCALAPPDATA%\sortandpost`.
  Before replacing / removing files it finds the tray by its window title
  (`WINDOW_TITLE`), sends `WM_ENDSESSION` + `ENDSESSION_CLOSEAPP` (clean stop, then the tray
  exits), waits for the `APP_MUTEX` to go, and **refuses** if it's still held. Never let it
  replace files under a running app: the running copy loses its own files (it once broke Quit).
  Uninstall removes the Start-at-login Run value.
- **Tray specifics**: pystray answers every message it doesn't handle with 0 (not
  `DefWindowProc`), so `_windows.install_session_end` handles `WM_QUERYENDSESSION` (0 there
  would block logoff), `WM_ENDSESSION` (clean stop, synchronously, then exit) and
  `WM_SETTEXT`. The Quit question (`MessageBoxW`, topmost) runs on its **own thread**: from
  inside the menu callback it can't take input. Start at login = an `HKCU\…\Run` value.
- **Server specifics**: the parent watch holds a process handle on the tray (Windows never
  re-parents an orphan). `terminate()` is a hard kill there, so `/quit` is the only clean
  stop. The tray's loopback `httpx.Client` ignores the environment (`trust_env=False`,
  `verify=False`) so it never reads `SSL_CERT_FILE` / proxies; exiftool starts with
  `CREATE_NO_WINDOW` (`binaries.no_console_flags`).
- **File replacement**: Windows can't replace a file something holds open. The working DB's
  pool is disposed *before* a pull / restore / conflict swap, and `sync.publish.replace_file`
  retries sharing violations for ~3 s (publish, lock, embeddings matrix, rowmap).

---

## Suggested build order (to avoid rewrites)

1. Config load + app-data bootstrap (`local/` + `sync/`) + DB models + Alembic baseline +
   `meta.db_generation`.
2. Metadata engine (exiftool `-stay_open` wrapper): read + rating write + round-trip tests,
   including "pixel hash stable across an EXIF write".
3. Scanner + pixel hashing + thumbnails + `photos`/`photo_paths` + fast-path cache. Grid
   browsing works at the end of this step.
4. Sync layer: working-copy/atomic-publish, single-writer lock + heartbeat + read-only mode,
   snapshots, `changes` log, conflict-copy merge. Two-machine convergence test (two `sync/`
   dirs, hand-copy the DB + a conflict copy between them, assert identical merged state).
5. CLIP engine: embedding memmap store + semantic search; then zero-shot vocab + `predictions`
   + `/review` UI.
6. Watchers + periodic reconcile + EXIF reconciliation on re-index.
7. Polish: filters, duplicate detection (phash), settings, status bar.
