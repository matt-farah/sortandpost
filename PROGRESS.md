# sortandpost — build progress & resume notes

_Last updated: 2026-09-09 (Docker `compose up` integration test — image builds,
app runs, CLIP + search + review + watchers all work; one non-blocking finding)._
_Working branch: `scaffold` (pushed to `origin/scaffold`). `main` PR not opened yet._

---

## ✅ 2026-09-09 (later) — Docker `compose up --build` integration test

Docker Desktop (Apple Silicon, aarch64, 8 CPU / 8 GB) got installed mid-session.
Ran the real thing against the scratch 6-file JPEG library + a fresh
`APP_DATA`.

**Dockerfile fixes made this session** (both now committed as part of testing):
1. **Wheel-build ordering** — `RUN uv pip install .` ran before `COPY src/`, so
   hatchling had no package to build. Now `src/`, `migrations/`, `alembic.ini`
   are copied before a single combined install step. Also added
   `--torch-backend=cpu` (keeps the image off the ~2 GB CUDA wheel).
2. **`LICENSE` not copied** — `pyproject.toml` has `license = { file = "LICENSE" }`
   and hatchling reads it at wheel-build time → `OSError: License file does not
   exist: LICENSE`. Added `LICENSE` to the `COPY` line.

After those two, everything passed:

- **Image builds clean** — 1.61 GB, all 79 deps incl. `torch==2.14.0+cpu`,
  `nicegui==3.16.0`, `open-clip-torch==3.3.0`, `rawpy`, `pillow-heif`, `watchdog`.
  (Versions drifted well past the `pyproject` floors; the NiceGUI 3.x drift that
  was flagged as a risk did **not** bite — 0 errors anywhere.)
- **App boots**: bootstrap → `init_db` → `SyncManager` acquires the write lock
  (writable mode, lock file with live heartbeat, `pid 1`, container hostname) →
  `NiceGUI ready on :8080` → in-app indexer thread runs.
- **All 5 pages return HTTP 200** with valid NiceGUI SSR HTML: `/`, `/search`,
  `/review`, `/settings`, `/photo/{hash}` (5 photo links on `/`).
- **Index**: 6 JPEGs → 5 photos (byte-dup deduped), 10 thumbnails, `phash`.
- **EXIF reconcile** in-container imported `b.jpg`'s rating + keywords (written
  by the earlier native test) with `rate` / `tag_add` change rows.
- **CLIP**: downloaded `ViT-B-32 / laion2b_s34b_b79k` to the mounted cache
  (`/data/local/models`, via `OPEN_CLIP_CACHE_DIR` / `HF_HOME`), embedded all
  photos, wrote 40 zero-shot predictions (8/photo), 0 errors.
- **`sync/` published**: `sortandpost.db` (gen 2), `embeddings/img_*.npy` +
  `text_*_<vocabhash>.npy` + `rowmap.json`, `thumbnails/`, `.sortandpost.lock`;
  `local/publish_state.json` written; 60 s publish interval honored.
- **Semantic search** (real model, in-container): text→image cosine ranking with
  sane scores (~0.34 for on-topic, ~0.16 for absent concepts); `similar_to`
  ranks the other photos and excludes the query itself.
- **`/review` approve** (`app.approve`): `photo_tags(source='clip')` +
  **union** of manual + approved keywords embedded into the file (verified with
  host `exiftool`: `Subject: beach, landscape, sunset`) + `clip_approve` change
  row + `prediction.state → approved`.
- **`watchdog`**: dropping `/library/watch_test.jpg` triggered a debounced
  rescan within ~seconds → indexed + embedded (7 seen, 1 new).
- **`docker compose run --rm indexer --reembed`**: rebuilt 6 embeddings + 47
  predictions, 0 errors, kept the earlier approval (deletes pending only).

**Finding (non-blocking) — CLIP model reloaded every indexer cycle.**
`indexer/service.py::_run_cycle` calls `scan_clip_once(settings)` with no cached
`model=`, so `scan_clip_once` builds a fresh `ClipModel` and calls `.load()`
**every cycle** (~1 s weights-from-disk + an HTTP `HEAD` to huggingface.co), even
on idle cycles with nothing to embed — observed 7 loads / 7 HF requests in
~3.5 min. The `model=` param on `scan_clip_once` exists for exactly this. Impact:
needless CPU/network churn, and the indexer effectively **requires network to HF
every cycle** even with weights cached locally (works against the "locally-run,
no cloud service" goal; would fail air-gapped). Fix: build the `ClipModel` once
in `run_indexer`, thread it through `_run_cycle` → `scan_clip_once(..., model=…)`;
optionally set `HF_HUB_OFFLINE=1` once weights are present. The web-app path
already caches on `State.clip_model` and is fine.

**Not exercised this pass** (no assets / needs a second machine / needs a real
browser): RAW read-only + HEIC decode (library was JPEG-only), two-machine sync
+ read-only banner, real browser interaction (button clicks, keyboard nav,
infinite scroll), snapshot/restore recovery in-container.

The app is currently **running** (`docker compose up -d app`, port 8080).
`docker compose down` to stop it. `.env` points at the scratch library.

---

## ✅ 2026-09-09 — native (no-Docker) integration test

Ran the non-heavy-dep paths end to end against a real ~6-file JPEG library and a
scratch `APP_DATA`, using the real `exiftool` 13.55 on PATH. Quality gate still
clean: **151 tests pass**, `ruff check`, `ruff format --check`, `mypy` all green,
`alembic upgrade head` + `--autogenerate` diff still empty.

Verified live (not just unit tests):

- **Step 1** — `Settings.resolve()` bootstraps `local/` + `sync/`
  (`device.json`, `config.yaml`, `tags.yaml`); `AppConfig` round-trips.
- **Step 2** — metadata engine: `write_rating(4)` → `XMP:Rating` + `IFD0:Rating`
  + `IFD0:RatingPercent 75` + `XMP-microsoft:RatingPercent 75`;
  `write_keywords` → `XMP:Subject` + `IPTC:Keywords`. **`pixel_hash` stable
  across the EXIF write** (hard constraint #3); mtime bumps as intended.
- **Step 3** — `indexer --once`: 6 files → 5 `photos` / 6 `photo_paths` (a
  byte-identical copy at a second path deduped to one `pixel_hash`, both
  `present`), 10 thumbnails written content-addressed under
  `sync/thumbnails/<hh>/`, `phash` populated, `scan_state.stages_done = 63`
  (BROWSABLE complete, CLIP bit clear). Second run: 5 unchanged via fast-path.
- **Step 4** — `SyncManager.start()` → writable + `.sortandpost.lock` (device,
  heartbeat, pid, `db_generation`); `publish()` → atomic `sync/sortandpost.db`,
  `db_generation` bumped in the `meta` row **and** the lock; `stop()` releases
  the lock. `python -m sortandpost.db snapshot` → `sync/snapshots/*.db.zst`.
- **Step 6** — re-index after the exiftool write imported `b.jpg`'s rating as a
  `ratings` row (`source='file'`) + a `rate` change row, and the two keywords as
  `manual` tags + `tag_add` change rows (file mtime newer ⇒ file wins, additive).

Still **unverified** (need Docker or the heavy deps): CLIP embed / semantic
search / zero-shot tagging (`torch`, `open-clip-torch`), all NiceGUI pages,
RAW decode (`rawpy`), HEIC decode (`pillow-heif`), `watchdog` FS events, and the
image build itself.

**Dockerfile bug spotted by inspection (fix before `docker compose up --build`):**
the first `RUN uv pip install --system --no-cache .` runs *before* `COPY src/`,
so hatchling has no `src/sortandpost` to build the wheel from and the layer
fails. Install deps only first (e.g. `uv pip install --system --no-deps`-style
or a deps-only manifest / `--no-install-project`), then `COPY src/`, then
`uv pip install --system --no-cache -e . --no-deps`.

---

## ▶️ NEXT SESSION — start here

The build is feature-complete (151 unit tests green), steps 1–4 + 6 are verified
natively, and a full `docker compose up --build` pass works (see the two
2026-09-09 sections above). Remaining work:

1. **Fix the CLIP per-cycle reload** (finding above): cache one `ClipModel` in
   `run_indexer` and pass it to `scan_clip_once(..., model=…)`; consider
   `HF_HUB_OFFLINE=1` once weights are cached. Small, self-contained.
2. **Browser smoke** at `http://localhost:8080` (only HTTP-GET + in-container
   Python calls were exercised so far — not a real browser): keyboard nav +
   `1`–`5` rating on `/`, the `/search` box + filter row + dupes toggle,
   `/review` bulk approve/reject buttons, `/settings` "run indexer pass now",
   the status-bar footer ticking, read-only banner behaviour.
3. **RAW + HEIC**: point `PHOTO_LIBRARY` at a folder with a few real `.CR2`/`.NEF`
   + `.HEIC` files; confirm RAW decodes via embedded preview and is **never**
   written (check mtimes / logs), HEIC decodes + thumbnails.
4. **Two-machine sync** (optional): copy `APP_DATA/sync/` to a second dir, point a
   second container at it, confirm no rebuild + the read-only banner while the
   first holds the lock; then a conflict-copy merge.
5. **Open the PR:** `scaffold → main`
   (`https://github.com/matt-farah/sortandpost/compare/main...scaffold?expand=1`).
   `gh` is not installed — install it or open via the web UI.

Full caveat list is in **"Open threads / not blocking"** near the bottom.

---

## Where we are

Building from the architecture spec in `CLAUDE.md`, following the 7-step
**Suggested build order**. **All 7 steps are complete, committed, and pushed**
to `origin/scaffold`. Remaining work is a `scaffold → main` PR and testing the
real (torch / Docker) paths — see "Open threads".

| Step | What | Commit | Status |
|------|------|--------|--------|
| 1 | Config load + app-data bootstrap + DB models + Alembic baseline + `meta.db_generation` | `0aa8c65` / `8ab9e94` | ✅ done |
| 2 | Metadata engine (exiftool `-stay_open` wrapper): read + rating write + round-trip tests | `f1598f8` | ✅ done |
| 3 | Scanner + pixel hashing + thumbnails + `photos`/`photo_paths` + fast-path cache. Grid browsing works. | `94465f9` | ✅ done |
| 4 | Sync layer: working-copy/atomic-publish, single-writer lock + heartbeat + read-only mode, snapshots, `changes` log, conflict-copy merge. Two-machine convergence test. | `270e2b8` | ✅ done |
| 5a | **CLIP embedding store + semantic search**: memmap matrix + rowmap, `ClipModel` wrapper (guarded import), deferred CLIP indexer pass (`scan_clip_once`) + `--reembed`, `search_by_vector`/`semantic_search`/`similar_to`, `/search` page. | `b9f9cfb` | ✅ done |
| 5b | **Zero-shot vocab + predictions + `/review`**: `tags.yaml` → cached text-embedding matrix (keyed by vocab hash), per-photo scoring folded into `scan_clip_once`, `predictions` rows, `/review` bulk approve/reject (approve ⇒ `photo_tags` + EXIF keyword union + `changes`; reject sticky). | `95abfd9` | ✅ done |
| 6 | **Continuous indexer + EXIF reconciliation**: pipeline stage 6 real (rating file↔DB with mtime tie-break + `rate` change rows; additive keyword import), `run_indexer` loop (timer + debounced `watchdog` FS events, gated on the write lock), standalone `python -m sortandpost.indexer` (no flag) + in-app thread. | `405efbc` | ✅ done |
| 7 | **Polish**: dHash near-duplicate detection (`sortandpost.dedup`), structured `PhotoFilter` (stars / GPS / camera / date / tags / duplicates) wired into `/search` + query-less filtered browse, real `/settings` page (sync + CLIP status, "run pass now"), persistent status-bar footer. | — | ✅ done |

### Current state of the codebase

- **151 tests pass.** `ruff check`, `ruff format --check`, `mypy` all clean on
  `src/sortandpost/{sync,indexer,db,hashing.py,clip,search,dedup.py}`.
- `alembic upgrade head` works and `alembic revision --autogenerate` produces an
  **empty diff** (steps 5–7 used only pre-existing tables — no model changes;
  `photos.phash` was already in the baseline, step 7 just populates it).
- **All pages are real**: `/` grid, `/photo/{hash}`, `/search` (semantic +
  structured filters + query-less filtered browse), `/review` (bulk
  approve/reject), `/settings` (sync + CLIP status, "run indexer pass now"),
  plus a persistent status-bar footer on every page.
- **Indexer entrypoints** (`python -m sortandpost.indexer`):
  - no flag → **continuous**: `init_db`, then `SyncManager.start()` (acquire the
    write lock + start publish/heartbeat), then `run_indexer` — a full reconcile
    `scan_once` + CLIP pass on the `full_scan_interval_min` timer and on
    debounced `watchdog` FS events. Skips every cycle while another machine holds
    the lock. Ctrl-C → clean stop, lock released. `--no-watch` for timer-only.
  - `--once` → one `scan_once` + one `scan_clip_once`, exit.
  - `--reembed` → rebuild the CLIP matrix + re-score (keeps approve/reject).
  - The web app runs the same loop in a daemon thread when
    `indexer.run_in_app: true` (default) **and** it started as the writer.
- **Pipeline stage 6 is real** (`sortandpost.indexer.reconcile`): on re-index,
  file `XMP:Rating` vs the `ratings` row — file `mtime` newer ⇒ file wins
  (import + `rate` change row), else DB wins (rewrite the file); a DB-only rating
  is written out. `XMP:Subject` keywords not yet on the photo are imported as
  `manual` tags (+ `tag_add` change rows); import is additive, never deletes.
  RAW files are never read back or written (hard constraint #2).
- Step-6 modules: `sortandpost.indexer.{reconcile,service}`; `index_file` gained
  a `device_id` arg + an `IndexOutcome.reconcile` field; `make_engine` sets
  `PRAGMA busy_timeout=5000`; `IndexerConfig` gained `run_in_app` / `watch` /
  `debounce_sec`.
- **Step 7 modules**:
  - `sortandpost.dedup` — `dhash(image)` (64-bit row difference hash, hex),
    `hamming`, `find_duplicate_groups(session, max_distance=8)` (vectorised SWAR
    popcount + union-find). The indexer computes `photos.phash` from the decoded
    image (idempotent; backfilled on any later re-decode).
  - `sortandpost.search.filters` — `PhotoFilter` (min_stars, has_gps, camera
    substring, date range, tags_all / tags_any, only_duplicates, include_missing)
    → `allowed_hashes()` (a `pixel_hash` set, or `None` = "no constraint");
    `browse_photos()` for the query-less `/search` path. `search_by_vector` /
    `semantic_search` / `similar_to` all take `flt=`.
  - `app.py` — `/search` filter row (+ URL params), real `/settings`,
    `_status_bar()` footer polled every 5s, `State.engine` / `State.settings`.
- Sync layer is wired into `app.py` (read-only banner disables `/review`
  actions; in-app indexer thread) and the
  `python -m sortandpost.db {snapshot,restore,merge}` CLI.

---

## Dev environment

- Python 3.12 target; local dev on **3.13** in a gitignored `.venv/`.
- **Lightweight test venv** — only lightweight deps are installed. Heavy deps
  (`nicegui`, `rawpy`, `pillow-heif`, `torch`, `open-clip-torch`, `watchdog`)
  are **NOT** installed and are guarded imports (`clip.model`, `indexer.decode`,
  `indexer.service`). `exiftool` is on PATH via Homebrew. `numpy` + `zstandard`
  are in the venv. The continuous indexer runs timer-only without `watchdog`;
  FS-event debouncing is unit-tested with an injected clock.
### Quality gate (run before every commit)

```bash
source .venv/bin/activate

ruff check .
ruff format --check .
mypy src/sortandpost/sync src/sortandpost/indexer src/sortandpost/db src/sortandpost/hashing.py \
     src/sortandpost/clip src/sortandpost/search src/sortandpost/dedup.py
pytest

# alembic must stay clean (use a throwaway DB URL, then delete the generated file):
SORTANDPOST_DB_URL="sqlite:////tmp/sp_alembic_check.db" alembic upgrade head
SORTANDPOST_DB_URL="sqlite:////tmp/sp_alembic_check.db" alembic revision --autogenerate -m "should be empty"
#   -> confirm `def upgrade(): pass`, then:
rm migrations/versions/*should_be_empty.py /tmp/sp_alembic_check.db
```

### Commit convention

Branch off nothing — stay on `scaffold`. Commit message trailer:

```
Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UE3CvoD3KNFFAVPjhja7cv
```

Then `git push` (updates `origin/scaffold`).

---

## Step 5 plan — CLIP engine + semantic search + review UI — ✅ DONE (5a `b9f9cfb`, 5b this commit)

Spec: `CLAUDE.md` → "CLIP tagging & search", "Indexer pipeline" stage 7,
"Database schema" (`embedding_rows`, `embeddings_meta`, `predictions`),
"Frontend" (`/search`, `/review`).

### 5b. Zero-shot tagging + review — ✅ DONE

Landed as planned, with these notes:

- `clip/vocab.py`: `load_vocab` preserves facet-then-term order, de-dupes a term
  that appears under two facets (first facet wins), and derives a 12-char
  `vocab_hash` from the ordered `(facet, name)` list. `ensure_text_matrix`
  averages the L2-normalized encodings of every `prompt_templates` entry per
  term, re-normalizes, and caches to `text_<model>_<vocabhash>.npy` (atomic
  write; a wrong-row-count cache is rebuilt). The `<vocabhash>` in the filename
  means editing `tags.yaml` transparently invalidates the cache.
- Scoring is folded into `scan_clip_once`'s existing batch loop
  (`_score_photo` → `pick_terms` → `upsert_prediction`). `ClipStats` gained a
  `predictions` counter. `--reembed` deletes **pending** predictions only;
  `approved`/`rejected` survive so a rejection stays sticky across re-runs
  (`upsert_prediction` never flips an existing row's state).
- `/review` (`app.py`): pending predictions grouped by tag (most-suggested
  first) with sample thumbnails; **Approve all** / **Reject all** per tag run
  off the event loop via `run.io_bound` (approve embeds keywords via exiftool).
  Read-only mode disables the buttons with a banner.
- `approve()` sets `predictions.state='approved'`, get-or-creates the `Tag`
  (`kind=clip-approved`) + `PhotoTag(source='clip')`, writes the **union** of
  the photo's manual + approved-clip keywords into every present, embed-capable,
  non-RAW copy (`written_to_file` reflects whether any file was touched), and
  emits `ChangeOp.clip_approve`. `reject()` sets `state='rejected'` (creating
  the row if the pending one was pruned) + `ChangeOp.clip_reject`. Merge replay
  in `sync/merge.py` already consumes both — no merge changes needed.
- Tests: `test_clip_vocab.py` (order/dedup/hash, cache build + hit + stale
  rebuild), `test_clip_tagging.py` (`pick_terms` threshold/top_k,
  `upsert_prediction` insert/refresh/sticky), `test_review.py` (predictions land
  from the pass, `pending_groups` shape, approve writes tag+change+EXIF union,
  RAW approve is DB-only, reject sticky across `--reembed`). A `FakeClipModel`
  with aligned image/text encoders stands in for torch.

### 5a. Embedding store + semantic search — ✅ DONE

Landed as described below, with these notes:

- `torch` + `open-clip-torch` are **not** in the venv. `clip/model.py` guards the
  import (`clip_available()` / `ClipModelUnavailable`); `scan_clip_once` returns
  `ClipStats(skipped_no_model=True)` when it's missing, and `/search` shows a
  banner. The memmap store + ranking are tested directly with hand-made
  `float32` vectors (`tests/test_clip_store.py`, `tests/test_search.py`); the
  indexer pass is tested with a `FakeClipModel` (`tests/test_clip_scan.py`).
- The `.npy` matrix is allocated with spare capacity (`_GROWTH_ROWS`, geometric
  grow via tmp-file + `os.replace`); `len(store)` / `rowmap.json` are the
  authoritative row count, `matrix()` slices to it.
- `--once` now runs `scan_once` then `scan_clip_once`. `--reembed` clears
  `embedding_rows` + `embeddings_meta` + the `Stage.CLIP` bits + the matrix
  files, then re-runs.
- Query text embedding is offloaded off the event loop via
  `nicegui.run.io_bound`; the model is cached on `State.clip_model`.

### 5a. Embedding store + semantic search (original plan)

1. **`src/sortandpost/clip/` module** (currently just `__init__.py` stub):
   - `model.py` — thin wrapper around `open_clip` (`ViT-B-32` /
     `laion2b_s34b_b79k`, CPU). Guarded import. Exposes
     `encode_images(list[PIL.Image]) -> np.ndarray` (L2-normalized `float32`)
     and `encode_text(list[str]) -> np.ndarray`. Model/pretrained/dim read from
     `settings.config.clip`.
   - `store.py` — the memmap matrix at
     `sync/embeddings/img_<model>.npy` (see `paths.image_embeddings_file`).
     Append-mostly, L2-normalized rows. `embedding_rows` table maps
     `pixel_hash -> row_index`; `embeddings_meta` records
     `model/pretrained/dim/count`. Handle: append new row, replace existing
     row in place, grow the file, rebuild `rowmap.json`
     (`paths.embeddings_rowmap_file`). A model/dim mismatch vs
     `embeddings_meta` must trigger `--reembed` (wire up the flag in
     `indexer/__main__.py`, currently `raise NotImplementedError`).
2. **Pipeline stage 7** in `src/sortandpost/indexer/pipeline.py`:
   - Add `Stage.CLIP` handling (the flag already exists;
     `Stage.COMPLETE = BROWSABLE | CLIP`).
   - When `clip_deferred: true` (default), browsing must not wait on CLIP —
     run it as a second pass / lower-priority queue after the main scan.
     Simplest first cut: a separate `scan_clip_once()` in `indexer/scan.py`
     that walks photos with `scan_state.stages_done & CLIP == 0` and embeds
     them. `--once` can run it after the browsable pass.
   - Respect hard constraint #7: no CLIP inference on the web event loop.
3. **`src/sortandpost/search/` module** (currently stub):
   - `semantic_search(query: str, *, limit, filters) -> list[pixel_hash]`:
     text embed → dot-product vs the memmap matrix → top-N. Brute-force NumPy
     is the design target (≤ ~25k photos). Keep the interface stable so an ANN
     index can slot in later.
   - image→image ("more like this") using a stored row.
4. **`/search` page** in `app.py`: text box + results grid (reuse
   `_thumb_cell`). Filters (stars ≥ N, has-GPS, date range, camera, missing,
   duplicates) can be minimal here and fleshed out in step 7.
5. Tests: memmap store append/replace/grow/rowmap round-trip; search ranking
   with hand-made unit-norm vectors (no torch needed); `embeddings_meta`
   mismatch detection.

### 5b. Zero-shot tagging + review (original plan)

6. **`sync/tags.yaml`** is already seeded (`resources/tags.default.yaml`).
   Load it, facet-grouped. For each term, average the normalized text
   embeddings of `config.tagging.prompt_templates`; cache to
   `paths.text_embeddings_file(model, vocab_hash)`.
7. **Zero-shot scoring**: cosine sim of each photo embedding vs every vocab
   term; keep terms `>= score_threshold` within `top_k`. Write rows to
   `predictions(pixel_hash, tag_name, score, model, run_at, state='pending')`
   (unique on `(pixel_hash, tag_name, model)` — already in the model).
8. **`/review` page**: pending predictions grouped by tag, bulk approve/reject.
   - **approve** ⇒ `photo_tags(source='clip')` + EXIF write (via the metadata
     engine, embed-capable files only, never RAW) + **`record_change(...)` row**
     (`ChangeOp.clip_approve`). Use `sortandpost.sync.changelog.record_change`
     — hard constraint #4.
   - **reject** ⇒ `predictions.state='rejected'` + `ChangeOp.clip_reject` row so
     re-runs don't resurface it.
9. The merge replay in `sync/merge.py` **already handles** `clip_approve` /
   `clip_reject` / `tag_remove` / `unindex` — just make sure the UI emits those
   change rows and that `photo_tags` / `tags` get written the way
   `_merge_tags` expects.
10. Tests: vocab embedding cache keyed by vocab hash; threshold/top_k
    filtering; approve path writes photo_tag + change row + (mocked) EXIF call;
    reject path is sticky across re-runs.

### Watch out for (carried constraints)

- **Photo identity is `pixel_hash`** everywhere — embeddings, predictions,
  tags, thumbnails all key on it.
- **CLIP predictions are never written to photo files.** Only user-approved
  tags go to `XMP:Subject` / IPTC keywords.
- **RAW files are read-only** — the metadata engine rejects them; ratings/tags
  for RAW live only in the DB.
- **Every user-authored mutation emits a `changes` row** (`record_change`).
- **Indexer is idempotent & resumable** — assume killed mid-run; use the
  `scan_state.stages_done` bitmask.
- **Pre-release schema policy**: do NOT write incremental Alembic migrations.
  New model columns/tables are absorbed by the `0001_baseline` `create_all`.
  After any model change, re-verify the autogenerate diff is empty.
- `from sqlmodel import col` to wrap columns in `.where()` / `.order_by()`
  (avoids ruff E712 and satisfies strict mypy): `col(X).is_(None)`,
  `col(X).desc()`.

---

## Handy commands

```bash
# continuous: acquire the write lock, then scan + CLIP on a timer and on FS events
python -m sortandpost.indexer                   # Ctrl-C to stop; --no-watch for timer only

# one full scan pass (stages 1-6) + deferred CLIP pass (embed + zero-shot score)
python -m sortandpost.indexer --once
python -m sortandpost.indexer --reembed   # rebuild the matrix + re-score (keeps approve/reject)

# DB maintenance
python -m sortandpost.db snapshot
python -m sortandpost.db restore <path-to-.db-or-.db.zst>
python -m sortandpost.db merge <path-to-conflict-copy.db>

# web app (needs nicegui — Docker, or install into .venv)
python -m sortandpost

# single test
pytest tests/test_sync_merge.py::test_two_machine_merge_converges -q
```

## Open threads / not blocking

The 7-step build order is done. What's left is verification + a PR, not features:

- **`scaffold` → `main` PR not opened.** `gh` CLI not installed. Compare URL:
  `https://github.com/matt-farah/sortandpost/compare/main...scaffold?expand=1`
- **Docker / real-model paths untested here.** Docker isn't installed on this
  machine, and `torch` / `open-clip-torch` / `nicegui` / `rawpy` / `pillow-heif`
  / `watchdog` aren't in the venv. Unexercised: the real CLIP load + inference,
  the NiceGUI pages rendering, RAW decode, HEIC decode, `watchdog` FS events.
  All are behind guarded imports with tested fallbacks; a `docker compose up`
  pass is the acceptance test.
- **Not built (deliberately):** grid virtualization / infinite scroll (`/` still
  paginates), process-pool concurrency for decode/hash/CLIP (single-threaded
  sequential), per-photo (not just per-tag) `/review` controls + facet grouping,
  a shared/serialized exiftool client for `/review` approvals (one `-stay_open`
  process per bulk action), FS events triggering a *targeted* subtree rescan
  (currently a debounced full `scan_once` — correct, just not minimal),
  `PhotoFilter` date-range + tag AND/OR widgets in the `/search` UI (the model
  and `allowed_hashes` support them; only stars/camera/GPS/dupes/missing are
  wired to inputs).
- The in-app indexer thread reads `manager.mode` without re-checking the lock
  (the app never calls `manager.refresh()`); a session that starts read-only
  stays non-indexing until restarted. The standalone entrypoint does re-check.
- dHash is weak on near-flat images (a solid-colour frame hashes to all-zero, so
  solid frames cluster together). Real photos have gradients; unit tests use
  them. Not worth a fix.
