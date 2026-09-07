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

The codebase does not exist yet. This document is the architecture spec to build from. Keep it
updated as decisions are made or changed.

---

## Hard constraints — do not violate these

1. **Never modify the user's photo files or folder structure**, with exactly one exception:
   writing embedded EXIF/XMP metadata (ratings, approved keywords) into formats that support it
   (JPEG, HEIC, TIFF, PNG). No renames, no moves, no deletes, no new files, no new folders, no
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
| Language / runtime | Python 3.12 |
| Package manager    | `uv` + `pyproject.toml` |
| Web UI             | NiceGUI (browser mode, not native), served by its bundled uvicorn |
| DB                 | SQLite via SQLModel/SQLAlchemy, migrations via Alembic |
| Embedded metadata  | `exiftool` as a persistent `-stay_open` subprocess |
| Image decode       | Pillow + pillow-heif (HEIC); `rawpy`/LibRaw for RAW (embedded preview) |
| ML                 | `open_clip_torch`, default `ViT-B-32` / `laion2b_s34b_b79k`, CPU |
| Vector search      | NumPy brute-force cosine over a memory-mapped `float32` matrix (target ≤ ~25k photos) |
| Compression        | `zstandard` for DB snapshots |
| Filesystem watch   | `watchdog` |
| Lint / format / types | `ruff check`, `ruff format`, `mypy` |
| Tests              | `pytest` + `pytest-asyncio` |
| Deployment         | Docker Compose, single container |

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
docker compose run --rm app pytest tests/indexer/test_hashing.py::test_pixel_hash_stable_across_exif_write -q

# Quality
docker compose run --rm app ruff check .
docker compose run --rm app ruff format .
docker compose run --rm app mypy src

# Native dev (no Docker — requires exiftool + libraw on host)
uv sync
uv run python -m sortandpost           # web app
uv run python -m sortandpost.indexer --once
uv run pytest tests/path::test_name -q
```

The indexer has a standalone entrypoint on purpose: CLIP on CPU inside Docker on macOS is slow,
so a user may run `python -m sortandpost.indexer` natively against the same `APP_DATA` for
speed. Nothing may assume the indexer is always in-container. (The single-writer lock still
applies — the native indexer and the container app must not both write.)

---

## App-data layout

`APP_DATA` has two subtrees. The user points their file-sync app at **`sync/` only**.

```
<APP_DATA>/
  local/                       # machine-specific — NEVER synced
    sortandpost.db (+ -wal/-shm/-journal while running)   # the working copy the app runs against
    device.json                # {"device_id": "<random>", "hostname": "..."}  created on first run
    publish_state.json         # last published DB generation counter + hash
    models/                    # downloaded CLIP weights
    logs/
  sync/                        # user points OneDrive / Drive / Synology / Dropbox HERE
    sortandpost.db             # published snapshot of the DB — the cross-machine source of truth
    .sortandpost.lock          # {device_id, hostname, pid, acquired_at, heartbeat_at, db_generation}
    config.yaml
    tags.yaml                  # user-editable zero-shot vocabulary (facet-grouped)
    snapshots/sortandpost-YYYYMMDD-HHMMSS.db.zst          # last N clean snapshots (corruption recovery)
    thumbnails/<hh>/<pixel_hash>_<size>.webp              # content-addressed, write-once, conflict-free
    embeddings/img_<model>.npy                            # append-mostly memmap, L2-normalized rows
    embeddings/text_<model>_<vocabhash>.npy               # cached vocabulary text embeddings
    embeddings/rowmap.json                                # row_index -> pixel_hash
```

`config.yaml` (created from a template on first boot):

```yaml
library_roots: ["/library"]        # container paths of the mounted libraries
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
  device: cpu
  batch_size: 16
tagging:
  vocab_file: tags.yaml
  score_threshold: 0.22
  top_k: 8
  prompt_templates: ["a photo of {}", "{}"]
thumbnails: { sizes: [320, 1280], format: webp, quality: 80 }
indexer:
  full_scan_interval_min: 15
  workers: auto
  clip_deferred: true              # make photos browsable first, run CLIP after
rating: { write_windows_compat_tags: true }
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
        op ∈ { rate, tag_add, tag_remove, clip_approve, clip_reject, unindex }
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

---

## Indexer pipeline

Continuous background task in the app container (+ standalone entrypoint). Per file, keyed by
real path in `scan_state`, each stage idempotent and separately resumable:

1. **stat + fast path** — `(size, mtime_ns)` unchanged and fully indexed ⇒ skip. (Our own EXIF
   writes bump mtime; that only triggers a metadata refresh, not re-thumbnail/re-embed, because
   the pixel hash is unchanged.)
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
without an explicit user action.

Concurrency: `ProcessPoolExecutor` sized to `cpu_count - 1` for decode/hash/CLIP. `watchdog`
observers on the library roots, debounced, plus a periodic full reconcile scan
(`full_scan_interval_min`). The indexer only runs when this machine holds the write lock.

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
- `embedding_rows` — `pixel_hash` (pk), `row_index`, `model`.
- `embeddings_meta` — `model`, `pretrained`, `dim`, `count`, `updated_at`.
- `scan_state` — `path` (pk), `pixel_hash`, per-stage progress, `error`, `attempts`.
- `meta` — key/value: `db_generation`, `schema_version`, etc.

---

## Frontend (NiceGUI pages)

- `/` — virtualized thumbnail grid, keyboard-first: arrows navigate, `1`–`5` rate, `0` clears,
  `f` filter, `/` search. Infinite scroll ordered by `taken_at`.
- `/photo/{pixel_hash}` — large preview, star widget, EXIF panel, manual tag editor, CLIP
  suggestion chips (approve/reject), "more like this".
- `/search` — semantic text search + filters (stars ≥ N, tag AND/OR, date range, camera,
  has-GPS, missing, duplicates).
- `/review` — pending CLIP predictions grouped by tag for bulk approve/reject.
- `/settings` — CLIP model & thresholds, re-index / re-embed actions, sync status (lock holder,
  last publish, `db_generation`, snapshots, last merge summary).
- Persistent status bar: index progress (`X/Y`, current stage), CLIP queue depth, write-lock
  state (writable / read-only + holder), last publish time.

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
