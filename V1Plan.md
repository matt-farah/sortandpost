# sortandpost — V1 build plan

_Agreed 2026-09-24. This is the working plan for V1: scope, phase order, and which Claude model
to use for each phase. `V1Features.md` has the detailed feature notes; `V2.md` has everything
deferred. Tick phases off here as they merge._

## Status
- [x] P0 Housekeeping: this file, split V1Features / V2 (Haiku)
- [ ] P1 Search merged into the library + filter bar + photo-nav follows filter (Opus)
- [ ] P2 CLIP CPU control: slow/fast (Sonnet)
- [ ] P3 Live scan/CLIP progress (Sonnet)
- [ ] P4 `/review` per-photo + facets + shared exiftool (Sonnet)
- [ ] P5 Undo last delete + trash view (Opus)
- [ ] P6 RAW/HEIC no-write audit (Haiku)
- [ ] P7 robopost composer (Opus for design + API, Sonnet for UI)

## Context
v.5 shipped: indexing, ratings, tags, CLIP, collections, group-by, bulk delete, sync. `V1Features.md`
lists many candidates. This plan picks the V1 scope from your answers, orders it, and assigns the
cheapest Claude model that can do each phase safely. Each phase is **one branch → PR → your
hands-on browser test → commit**, following CLAUDE.md.

**V1 scope (your picks):** filters merged into the library, search merged into `/` (new), CLIP
CPU control, Issues.md photo-nav bug, #20 live progress, #3 per-photo/facet `/review`, #11
undo-delete + trash view, #13 `/review` exiftool reuse, #16 RAW/HEIC no-write audit, #1 robopost
composer.
**Moves to V2.md:** #4 native window, #5 packaging, #6–9 Docker/NAS, #10 design pass 2, #12, #14,
#15, #17, #18, #19.

## Model guide (API price per million tokens, input/output)
| Model | Price | Use for |
|---|---|---|
| **Opus 5.5** | $4 / $20 | Cross-cutting changes in `app.py` (3.6k lines), sync/tombstone/`changes` semantics, new external integrations, design decisions |
| **Sonnet 5** | $2 / $10 | Well-scoped features that follow existing patterns, a settings panel, a new page section |
| **Haiku 4.5** | $1 / $5 | Tests with a clear spec, doc updates (PROGRESS/V1Features/CLAUDE.md), fixing ruff/mypy nits, mechanical moves |

Opus 5.5 costs only 2× Sonnet 5, so use it wherever a mistake would cost a redo. The cheapest
pattern: **plan each phase in Opus (plan mode), then `/model sonnet` to implement** the approved
plan. Use Haiku for docs and test-only phases.

---

## Phase 1: Merged library + search + filters (fixes the Issues.md nav bug) · **Opus 5.5**
Biggest UX change; touches the `/` page (`app.py:472–1140`), `/search` (`app.py:1972–2113`),
`/photo` prev/next (`app.py:1158`), stacks, group-by, infinite scroll, and keyboard focus.

- **Filter state lives in URL query params on `/`**: `q`, `stars` (multi, incl. `u`=unrated),
  `camera`, `from`/`to` (accepts `2025`, `2025-2026`, `20260103-20260210`), `folder`, `tags`
  + `tagmode=all|any`, plus the existing `gps`, `dups`, `missing`, `mark`, `group`.
- **Extend `PhotoFilter`** (`search/filters.py`): `stars_in: set[int]` with 0 = unrated (via an
  outer join on `Rating`), `folder` (prefix match on `photo_paths.path` where present, subfolders
  included). Add a date-range parser helper plus unit tests.
- **Shared filter-bar component** on `/`. Choices come from the DB: distinct cameras, folders
  (grouped by library root), tags grouped by facet (approved/manual tags).
- **Text query** → `_run_search` (`app.py:2325`) with relevance order and group-by hidden;
  filters only → `allowed_hashes` intersected with `list_photo_ids` in `taken_at` order, group-by
  still works. Stack collapse applies in both cases.
- **Saved searches** → a "Saved" dropdown in the bar (reuse `searches.py` load/save/delete).
  `/search` redirects to `/` with the same params. The header drops its Search tab.
- **Photo-page arrows follow the filtered set**: `/photo/{h}` receives the grid's query string
  and rebuilds the same ordered list (a shared `resolve_view(params) -> list[hash]` helper used
  by both pages).
- Tests: filter clauses (unrated, folder, date parsing), `resolve_view` ordering, redirect.

## Phase 2: CLIP CPU control · **Sonnet 5**
- Config `clip.speed: slow|fast` (default `slow`) in `config.py` + `config.default.yaml`.
- slow: torch threads and indexer workers = `max(1, round(0.3 × cores))`, CLIP `batch_size` 8,
  and `os.nice(10)` (Windows: `psutil` BELOW_NORMAL, or skip) inside the worker/CLIP process.
  fast: current behavior (`cores − 1`).
- Replace the hard-coded `torch.set_num_threads` at `clip/model.py:175` and
  `Config.worker_count()` (`config.py:191`) with a speed-aware helper.
- Settings toggle with a warning: "Fast uses all CPU cores and may slow down your computer."
  It takes effect on the next pass, not in the middle of a pass.

## Phase 3: Live scan/CLIP progress (#20) · **Sonnet 5**
- A thread-safe in-memory `Progress` struct (stage, done, total, current path) written by
  `scan_once`/`scan_clip_once` (`indexer/scan.py`). Safe because `_INDEX_LOCK` already
  serializes passes.
- `_status_bar` (`app.py:401`) and `/settings` show `Indexing 120/4,300` and
  `Embedding 40/4,300`.

## Phase 4: `/review` upgrade (#3 + #13) · **Sonnet 5**
- Group pending predictions by facet (from `tags.yaml` via `clip/vocab.py`), then by tag.
  Per-photo approve/reject chips next to the existing bulk buttons.
- A single shared, lock-serialized exiftool client per review session instead of one per bulk
  action (`_apply_review_bulk`, `app.py:2394`).

## Phase 5: Undo last delete + trash view (#11) · **Opus 5.5**
Sync-sensitive: undoing has to reverse a tombstone that may already have been published and
replayed on other machines.
- Managed trash (`local/trash/<ts>/manifest.json`): list the batches in `/settings`, plus
  "Put back" and "Reveal". For OS Trash (macOS/Windows), show the list only and say "restore
  via Finder/Explorer Put Back".
- Put back = move the files back to their original paths (refuse if a file now exists there),
  delete the tombstone, append a `changes` row `op=restore` (a new op: merge replay removes the
  tombstone last-writer-wins by ts), then rescan those paths. Update CLAUDE.md constraint #1
  and the Deletion section to cover the reverse move.
- "Undo last delete" toast/button right after a bulk delete.

## Phase 6: RAW/HEIC no-write audit (#16) · **Haiku 4.5**
- Test: a real `.CR2`/`.NEF` fixture (or one from `~/Downloads/sampleImages`): record
  bytes-hash, mtime and a directory listing → rate it through `_apply_rating` → assert nothing
  changed and no sidecar was created. Same for tag approval. HEIC: rating round-trips and
  `pixel_hash` stays stable.
- If it finds a bug, move the fix to Sonnet.

## Phase 7: robopost composer (#1) · **Opus 5.5** for design + API client, **Sonnet 5** for UI
- Prerequisite: you have a robopost.app account + API key. Fetch their API docs at the start of
  the phase (don't guess endpoints).
- `sync/posts.json` (or extend `collections.json`): a post = collection id, caption, per-image
  alt text, order, schedule time, status, publish log. Any user-authored state emits `changes`
  rows (constraint #4).
- `sortandpost/robopost.py` client: upload media → create post → schedule. Key in `.env` as
  `ROBOPOST_API_KEY`. Calls run off the event loop.
- `/compose` page, or a "Compose post" action on a collection. The confirm dialog lists
  exactly what will be uploaded and when. Never auto-post.
- Tests with the HTTP layer mocked.

## Phase 0 / housekeeping · **Haiku 4.5**
- Rewrite `V1Features.md` to the scope above, move the deferred items into a new `V2.md`, and
  update CLAUDE.md "Frontend" (the `/search` merge) at the end of phase 1. Commit the pending
  rename (`Features.md`/`V2.md` deletions + new files).

---

## Verification (every phase)
1. Quality gate: `ruff check .`, `ruff format .`, `mypy src`, `pytest`, alembic autogenerate
   produces no changes.
2. Boot for a hands-on test:
   `APP_DATA=<repo>/browser_test_data PORT=8080 HF_HUB_OFFLINE=1 .venv/bin/python -m sortandpost`
   and give an exact click-list, e.g. for phase 1: filter to unrated + one camera + `2025-2026`,
   open a photo, arrow through it (it stays inside the filter), Back restores the grid, save and
   reload a search, `/search?q=x` redirects.
3. Your sign-off → update PROGRESS.md / V1Features.md → commit → PR.
