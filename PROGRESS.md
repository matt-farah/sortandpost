# sortandpost — build progress & resume notes

_Last updated: 2026-09-06 (end of build-order step 4)._
_Working branch: `scaffold` (pushed to `origin/scaffold`). `main` PR not opened yet._

---

## Where we are

Building from the architecture spec in `CLAUDE.md`, following the 7-step
**Suggested build order**. Steps 1–4 are complete, committed, and pushed.

| Step | What | Commit | Status |
|------|------|--------|--------|
| 1 | Config load + app-data bootstrap + DB models + Alembic baseline + `meta.db_generation` | `0aa8c65` / `8ab9e94` | ✅ done |
| 2 | Metadata engine (exiftool `-stay_open` wrapper): read + rating write + round-trip tests | `f1598f8` | ✅ done |
| 3 | Scanner + pixel hashing + thumbnails + `photos`/`photo_paths` + fast-path cache. Grid browsing works. | `94465f9` | ✅ done |
| 4 | Sync layer: working-copy/atomic-publish, single-writer lock + heartbeat + read-only mode, snapshots, `changes` log, conflict-copy merge. Two-machine convergence test. | `270e2b8` | ✅ done |
| 5 | **CLIP engine**: embedding memmap store + semantic search; then zero-shot vocab + `predictions` + `/review` UI. | — | ⏭️ **next** |
| 6 | Watchers + periodic reconcile + EXIF reconciliation on re-index. | — | pending |
| 7 | Polish: filters, duplicate detection (phash), settings, status bar. | — | pending |

### Current state of the codebase

- **90 tests pass.** `ruff check`, `ruff format --check`, `mypy` all clean on
  `src/sortandpost/{sync,indexer,db,hashing.py}`.
- `alembic upgrade head` works and `alembic revision --autogenerate` produces an
  **empty diff** (proves the create_all baseline matches the models).
- Grid browsing works end-to-end (`/` thumbnail grid, `/photo/{pixel_hash}`
  detail). `/search`, `/review`, `/settings` are still placeholders.
- The indexer runs stages 1–6 via `python -m sortandpost.indexer --once`.
  Stage 7 (CLIP) is a no-op until step 5.
- Sync layer is wired into `app.py` (read-only banner) and the
  `python -m sortandpost.db {snapshot,restore,merge}` CLI.

---

## Dev environment

- Python 3.12 target; local dev on **3.13** in a gitignored `.venv/`.
- **Lightweight test venv** — only lightweight deps are installed. Heavy deps
  (`nicegui`, `rawpy`, `pillow-heif`, `torch`, `open-clip-torch`, `watchdog`)
  are **NOT** installed. `exiftool` is on PATH via Homebrew. `zstandard` was
  added to the venv during step 4.
- **Step 5 will need `numpy` (already installed) and `open-clip-torch` + `torch`.**
  Torch is large (~200 MB+). Decide at the start of step 5 whether to:
  - install `torch` + `open-clip-torch` into `.venv` so the CLIP engine can be
    unit-tested for real, **or**
  - keep them out and guard the import like `rawpy`/`pillow-heif`
    (`try: import ... except ModuleNotFoundError`), unit-testing only the
    memmap store / search math with hand-made `float32` arrays and running the
    real model path in Docker. **Recommended: guard the import + test the
    numpy/memmap layer directly** (matches the pattern already used for
    `rawpy`, `pillow_heif`).

### Quality gate (run before every commit)

```bash
source .venv/bin/activate

ruff check .
ruff format --check .
mypy src/sortandpost/sync src/sortandpost/indexer src/sortandpost/db src/sortandpost/hashing.py
# add the step-5 packages once they exist:
#   mypy ... src/sortandpost/clip src/sortandpost/search
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

## Step 5 plan — CLIP engine + semantic search + review UI

Spec: `CLAUDE.md` → "CLIP tagging & search", "Indexer pipeline" stage 7,
"Database schema" (`embedding_rows`, `embeddings_meta`, `predictions`),
"Frontend" (`/search`, `/review`).

Do it in two sub-phases so browsing/search land before the vocab work:

### 5a. Embedding store + semantic search

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

### 5b. Zero-shot tagging + review

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
# one full browsable scan pass (stages 1-6)
python -m sortandpost.indexer --once

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

- `scaffold` → `main` PR not opened. `gh` CLI not installed. Compare URL:
  `https://github.com/matt-farah/sortandpost/compare/main...scaffold?expand=1`
- Docker not installed on this machine — `docker compose up --build` untested.
- Deferred by design: continuous indexer + watchers (step 6), EXIF
  reconciliation on re-index (step 6), grid virtualization / infinite scroll
  (step 7), process-pool concurrency for decode/hash/CLIP (later optimization —
  currently single-threaded sequential).
