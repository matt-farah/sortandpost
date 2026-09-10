# sortandpost — build progress & resume notes

_Last updated: 2026-09-09 (Features.md: grid keyboard nav — uncommitted; bulk delete + photo-page shortcuts committed, PR #4 open)._
_Working branch: `scaffold`. PR #4 `scaffold → main` open (covers everything below)._

---

## ⌨️ 2026-09-09 — Features.md: grid keyboard nav (finishes the shortcuts item)

**Uncommitted** — `src/sortandpost/app.py` only. `231 tests pass`; ruff + format
+ mypy clean (`app.py` too). Verified in a real browser: ring moves with the
arrows, hover overlays appear on the target, Space selects without scrolling
(keyboard-focus and mouse-hover paths both), Shift+Space ranges, Esc clears,
digits/`f`/Enter act on the target.

- **"Target" tile** = the keyboard-focus ring if the arrows have been used, else
  the tile under the mouse (`hover_hash` tracked via per-tile
  `mouseenter`/`mouseleave`). `Space` toggles its selection (`sel.click`, so
  Shift+Space extends a range like shift-click); `1`–`5`/`0` rate, `f` flag,
  `Enter` open, `Esc` clears the ring + selection.
- **Ring**: `← →` step one tile, `↑ ↓` step a row — column count measured live
  from the grid DOM (`_GRID_COLS_JS`) so it follows the responsive `auto-fill`
  reflow. Moving the ring past the load cursor calls `append_batch()`; it never
  fights scroll-restore (`if restoring: return`, and the hover path paints with
  `scroll=False`).
- **Highlight**: one shared `ui.add_css(_GRID_KB_CSS, shared=True)` — `.kb-focus`
  on the tile `<a>` draws an amber border via `::after` (a `ring`/`outline` sits
  *behind* the opaque thumbnail) and forces every `.kbreveal` child (the stars /
  flag / checkbox overlays, which are otherwise `group-hover:opacity-100`) to
  full opacity. `_paint_focus` just toggles that one class.
- **`Space` page-scroll**: a one-time capture-phase `keydown` `preventDefault`
  for Space, skipped while an INPUT/TEXTAREA/SELECT/BUTTON is focused.
- **Gotcha fixed**: the page had two `ui.keyboard` elements (old shift-tracker +
  new nav) and NiceGUI only fires one — merged into a single `_grid_key` that
  also keeps `sel.shift_down` current.
- `_grid_rating` / `_grid_flag` gained registries (`set_value` / `toggle`) so the
  keys reuse each tile's own write path; `_thumb_cell` threads them + `on_hover`.
  `ui.run.io_bound`'s `R | None` now guarded in the two delete handlers, so
  `app.py` is mypy-clean.

---

## ⌨️ 2026-09-09 — Features.md: photo-page keyboard shortcuts

**Uncommitted** (same working tree as the bulk-delete change below). `231 tests
pass` (no new tests — pure UI glue, verified in a real browser: ←→/`j``k` change
the photo URL, `3` lights `★★★` + fires the write, `0` → "rating cleared", `f`
toggles the flag switch; no JS/server errors). `ruff` + `ruff format` + `mypy`
clean — and `app.py` is now **mypy-clean too** (added `if result is None:`
guards for `run.io_bound`'s `R | None`, which also cleared the delete-feature
warnings).

`/photo/{hash}` only (grid keyboard nav is deferred — it needs a focus system
that touches the fragile infinite-scroll / scroll-restore code):

- `←` / `k` → previous, `→` / `j` → next, in the **grid's representative order**
  (`list_photo_ids` minus hidden stack members); clamped at both ends; a
  filmstrip member maps back to its stack rep first. `ui.navigate.to` (Back
  retraces).
- `1`–`5` set the rating, `0` clears — via `star_widget.set_value(...)`, so it
  runs the exact same `_apply_rating` path as a click.
- `f` toggles the edit flag via `flag_switch.set_value(...)`.
- Ignores key-repeat and Ctrl/Alt/Meta chords; read-only mode leaves the
  rating/flag keys inert (the widgets are `None`). A `← → move · 1-5 rate · 0
  clear · f flag` hint sits under the star widget.

Implementation: `_star_widget` / `_edit_flag_button` now **return** their
`ui.rating` / `ui.switch` (or `None` in read-only) so the page's
`ui.keyboard(on_key=...)` handler can drive them. `KeyEventArguments` /
`KeyboardKey` from `nicegui.events`; `key.number` covers the digits (0 included).

---

## 🗑️ 2026-09-09 — Features.md: bulk delete (move-to-trash + cross-machine tombstones)

**Uncommitted.** `231 tests pass` (+14); `ruff` + `ruff format` + `mypy` clean;
`alembic --autogenerate` still empty (the new `tombstones` table is absorbed by
the `0001` baseline `create_all`, per the pre-release schema policy). Verified in
a real browser (headless Chromium against a throwaway 6-photo library + fresh
`APP_DATA`): bulk-toolbar Delete removes 2 tiles **in place** (no reload, no
scroll jump), files land in `local/trash/<ts>/` with a `manifest.json`, the
photo-page "Delete photo" navigates home; **0 server errors** after the
slot-context fix below.

**Decision — CLAUDE.md hard constraint #1 gained a second exception**: an
explicit, user-invoked "Delete photos" *moves* files to a recoverable location.
Never an `unlink`, never under a library root, never automatic. Also added a
"Deletion" subsection to the Sync model + a `tombstones` row to the schema list +
a `deletion:` block to the `config.yaml` template.

**New modules**

- `sortandpost.trash` — `move_to_trash(paths, *, local_dir, now) -> TrashResult`.
  OS Trash via `send2trash` on macOS/Windows; a managed
  `<APP_DATA>/local/trash/<ts>/` folder + `manifest.json` everywhere else (Linux
  desktop / headless / Docker — where `send2trash` would drop a `.Trash-<uid>`
  on the *library* filesystem). `restore_managed()` reverses it. `send2trash` is
  a new dep, guarded (not in the lightweight venv; managed backend is used in
  tests + on this Mac).
- `sortandpost.db.deletion` —
  - `delete_photos(session, *, pixel_hashes, paths, clip_model, thumb_sizes,
    device_id, now, trash_files=True, emit_change=True) -> DeleteResult`: trash
    present files → purge `photos/photo_paths/ratings/photo_tags/predictions/
    embedding_rows/scan_state` → delete content-addressed thumbnails → compact
    the CLIP matrix (`EmbeddingStore.remove_many`, new) + re-point
    `embedding_rows` → `record_change(unindex)` → upsert a `Tombstone`.
  - `apply_pending_tombstones(...)`: the lazy / cross-machine path — for every
    `Tombstone.applied == False`, purge this machine's derived rows and (if
    asked) move this machine's still-present copy of the file to trash. Called
    at the end of every `scan_once`.

**Schema**: `Tombstone(pixel_hash pk, deleted_at, deleted_by_device, applied)`.
No migration (baseline `create_all`).

**Wiring**

- `sync/merge.py`: `unindex` replay now writes a `Tombstone(applied=False)` on
  every machine (still sets `missing_since` for interim visibility).
- `indexer/pipeline.persist_decoded`: a tombstoned `pixel_hash` is **not**
  re-indexed (`IndexOutcome.tombstoned`); the post-scan sweep trashes the file.
- `indexer/scan.scan_once`: runs `apply_pending_tombstones(trash_local_files=
  True)` after the presence sweep; `ScanStats` gained `tombstoned` /
  `tombstones_applied`.
- `app.py`: bulk-toolbar **Delete** (confirm dialog: count + trash destination +
  stack-scope note) and photo-page **Delete photo**; both read-only-aware.
  Deleted tiles are dropped from the grid via a new `cell_registry` on
  `_thumb_cell` (no reload → the tombstoned tiles are always above the
  infinite-scroll load cursor, so no re-render/dupe). `/settings` →
  `_deletion_editor` (stack-scope select). Config `DeletionConfig.stack_scope`
  (`"selected"` default | `"whole_stack"`).

**Bug found + fixed — NiceGUI "parent slot deleted" on the delete handler.**
After `await run.io_bound(...)` + `element.delete()` of the tiles, the ambient
slot context is stale, so `ui.notify` / `ui.navigate` inside the dialog handler
threw `RuntimeError: The parent element this slot belongs to has been deleted.`
Fix: capture `client = ui.context.client` at page build and wrap the post-await
UI work in `with client:`. Also reorder the dialog `_go` to `await work; then
dialog.close()`.

**Tests** (+14 files / cases): `test_trash.py` (managed move + manifest + missing
file + same-second append + restore), `test_deletion.py` (full purge, `unindex`
+ tombstone, embedding compaction + re-point, delete-of-missing, lazy
`apply_pending_tombstones` + idempotency, scanner resurrection guard, managed
trash is recoverable), `test_clip_store.py::remove_many` (compact + rowmap +
noop), `test_sync_merge.py` (`unindex` replay writes the tombstone).

**Not done / follow-ups**: an in-app "undo last delete" affordance (the managed
manifest already supports it; OS-Trash restore is manual); a `/settings` view of
the local trash folder; verifying the two-machine lazy-trash path in a live
two-container run (unit-tested only).

---

## 🎨 2026-09-09 — Features.md rounds 1–3: library UX, bulk editing, marked-for-editing

New user-facing feature work driven by `Features.md` (new file, untracked).
**All of this session's work is uncommitted** — working-tree edits to `app.py`
et al. plus untracked `src/sortandpost/{stacks,reveal,db/ratings,db/tags,db/flags}.py`
and their tests. `217 tests pass; ruff + ruff format + mypy clean.` Verified in a
real browser this time — Playwright headless Chromium against a live app
(`APP_DATA=<repo>/browser_test_data`, ~300 photos, `library_roots` →
`~/Downloads/sampleImages`); scratch repro scripts were in the session scratchpad
and have been removed.

**Backend modules added (each: DB write + `changes` row in one txn, hard
constraint #4; exiftool mirror off the event loop, hard constraint #7):**

- `sortandpost.db.ratings.set_rating` — the UI's single rating write path
  (`ratings` row + `rate` change + `XMP:Rating` / Windows-compat mirror). +6 tests.
- `sortandpost.db.tags.{add,remove}_manual_tag` — manual keyword add/remove
  (`photo_tags` source=`manual` + `tag_add`/`tag_remove` + `XMP:Subject` union).
  `RESERVED_TAG_NAMES` + `embeddable_keywords()` strip workflow-only tags before
  any file write (mirrored in `clip.tagging._embed_keywords`). +6 tests.
- `sortandpost.db.flags` — the **"marked for editing" queue**. Stored as a
  reserved manual tag `to-edit` so it rides the changes log / conflict merge and
  filters via `PhotoFilter.tags_all` for free, but is **never** embedded into a
  photo file. `set_edit_flag` / `edit_flagged_hashes` / `edit_flag_count` /
  `clear_all_edit_flags`. +8 tests.
- `sortandpost.stacks` — **read-time image stacking** (no schema change, no
  re-index). Same-parent-folder only; two modes (`raw_jpeg` pairs,
  `similar_names` — `img.jpg` / `img copy.jpg` / `img (1).jpg` / `copy img.jpg`);
  newest mtime is the representative. `build_stacks(session, cfg) -> StackIndex`.
  Config `stacking.{enabled,raw_jpeg,similar_names}` (default off), toggle on
  `/settings`. Grid + `/search` collapse to the representative with a `⧉ N`
  badge; the photo page shows a stack filmstrip; ratings/flags fan to every
  member. +25 tests.
- `sortandpost.reveal` — `reveal_in_file_manager` / `open_in_default_app`
  (macOS `open -R`, Windows `explorer /select,`, Linux `xdg-open`), native runs
  only. +6 tests.

**Config:** `IndexerConfig.exclude_dir_names` (case-insensitive, any depth — the
scanner prunes matching dirs; excluded photos go `missing`, not purged, and
revive when un-excluded) and `StackingConfig`. `db/browse.list_photo_ids()`.

**`app.py` (the big diff — ~950 lines):**

- `/` grid rebuilt: **infinite scroll** (80/batch, appends near the bottom, live
  header counter, no more `?page=`) replacing the pager; **full-bleed responsive
  grid** (`repeat(auto-fill, minmax(220px, 1fr))`) on `/`, `/search`, `/review`.
- **Scroll-position restore across back/forward** (hard-won — see the dedicated
  note below): position lives in `history.state.spGrid`, `scrollRestoration =
  'manual'`, a retry-loop `_scroll_to_js`, and the per-tick save must keep
  running even after all tiles are loaded.
- **Bulk select + toolbar**: hover checkbox per tile + shift-click ranges
  (`_GridSelection`), floating toolbar with ★1–5 / clear / tag input / flag /
  unflag; every write fans to stack members.
- **Clickable star widget on each grid tile** — top row by the checkbox,
  hover-only, desaturated amber; rate straight from the grid.
- **Marked-for-editing UI**: clickable ⚑ in each tile's top-right corner (solid
  amber + persistent when set, dim + hover-only when not); bulk flag/unflag that
  repaint visible tiles live (a `pixel_hash -> set(on)` registry threaded through
  `_thumb_cell`); a "Mark for editing" switch on the photo page; a live
  "⚑ N marked for editing" quick link on the library header →
  `/search?mark=true`, with a **"clear all"** button (confirm dialog) shown
  whenever ≥1 is flagged; a "⚑ to edit" checkbox on `/search`.
- Photo page: `← Back` / `← Back to library` use `history.back()`; **Paths**
  panel with Copy path always + Reveal / Open (native).
- `/settings`: excluded-folders chip editor (Save & rescan) + stacking toggles.

**Features.md status:** done — rating widget, back-nav, responsive grid, infinite
scroll, bulk rate/tag, marked-for-editing, grid hover rating, exclude
subfolders, image stacking, file-path exposure. Not started — dark/light mode,
bulk **delete** (needs a `CLAUDE.md` hard-constraint-#1 carve-out + `send2trash`;
decisions already taken: move to system Trash, full cleanup incl. embedding
compaction), design pass, keyboard nav on the photo/grid pages, saved searches,
swipe-triage page, robopost social composer.

---

### Grid scroll-restore mechanism (don't regress this)

Position for the `/` grid lives in **`history.state.spGrid` = `{n: loaded, y:
scrollY}`**, written every 0.3 s by the page tick — which **must keep writing
even after `done`** (all tiles loaded); stopping there was the "jumps to a fixed
mid-page spot near the bottom" bug. On back/forward, `_GRID_RESTORE_JS` reads it,
rebuilds `n` batches, then `_scroll_to_js` re-applies `scrollTo(0, y)` on a 50 ms
loop for up to 8 s (grid rows stream in over the websocket, so the document is
still growing — a single `scrollTo` lands short), stopping once on-target or once
the height has been stable a while. `history.scrollRestoration = 'manual'` so the
browser doesn't also guess. Filmstrip cells navigate with `location.replace` (not
real links) so hopping stack members never grows history.

---

### Local dev / browser testing

`.venv` carries only light deps (added `nicegui`, `rawpy`, `pillow-heif`,
`playwright` + `playwright install chromium`) — **no torch**, so CLIP degrades
gracefully (`clip_available()` / `ClipModelUnavailable`; semantic search +
auto-tag are inert locally, filters still work). Run the app natively:

```
APP_DATA=<repo>/browser_test_data PHOTO_LIBRARY=~/Downloads/sampleImages \
  .venv/bin/python -m sortandpost        # http://localhost:8080
```

`browser_test_data/` is git-excluded (`.git/info/exclude`) and holds ~300
indexed photos + thumbnails. No browser tool in-session — drive the running app
with headless Playwright scripts when a UI change needs real-browser
verification. `.env` in the repo points `APP_DATA` at a stale scratchpad and a
`/library` root that doesn't exist on this Mac — ignore it, pass env explicitly.

---

## ✅ 2026-09-09 — cross-machine Docker test: read-only promotion + stale-engine fix

Ran the real thing across containers: **machine 1** fresh-indexed 286 photos
(scan 46 s + CLIP 89 s), published to `sync/` at `db_generation 5`, clean stop.
A `cp -Rp m1/sync → m2/sync` stood in for the third-party sync tool. **Machine 2**
(own fresh `local/`, weights pre-seeded like a new install) came up **operational
in ~2 s, zero rebuild**: `0 indexed / 286 unchanged`, `0 newly embedded`, working
DB `gen 5` identical to m1, thumbnails + semantic search served straight off the
synced `sync/`. Single-writer lock verified: a 2nd instance on the same `sync/`
boots read-only and does not steal a live lock.

**Bug found + fixed — stale DB engine after a file swap (`27841a3`).**
`pull_published` / conflict-base swap / snapshot restore all `os.replace()` the
working DB. The long-lived SQLAlchemy engine (shared with the web app) kept
`QueuePool` connections bound to the old, now-unlinked inode — so a read-only
instance that pulled a newer DB on start **browsed stale/empty data**, and
**crashed with `attempt to write a readonly database`** the instant it promoted
to writable and published. `SyncManager._reset_working_engine()` now
`engine.dispose()`s after each of those four replacements (pool recycles, engine
object stays valid, next connection opens the current file). +1 regression test
driving read-only → pull → promote → publish through a shared engine.

**Read-only → writable auto-promotion (`3fc13f1`).** The web app captured
`manager.mode` once at boot and never called `manager.refresh()`, so an instance
that started read-only stayed read-only (and never indexed) until restart, even
after the lock freed. New `_start_lock_watch()` daemon thread calls
`manager.refresh()` every `lock_heartbeat_sec`, re-applies the mode to `State`,
and starts the in-process indexer on promotion (`_start_indexer_thread` is now
idempotent). Verified live: on machine 2's clean shutdown, machine 3 logs
`write-lock state changed: read-only -> writable` within the heartbeat interval,
starts indexing, and writes ratings — no restart.

161 tests pass; ruff + format clean. (Pre-existing: 5 mypy `untyped-decorator`
warnings on the NiceGUI `@ui.page` handlers.)

---

## ✅ 2026-09-09 — CLIP model reuse + two-machine sync test

**CLIP per-cycle reload — fixed (`e204775`).** `run_indexer` rebuilt the CLIP
architecture and re-read the safetensors weights on *every* loop cycle,
including no-op cycles (the loop wakes every few seconds behind a Docker bind
mount from phantom `watchdog` events) — ~2–4 s of wasted CPU per idle wake.
`run_indexer` now builds one `ClipModel` and threads it through
`scan_clip_once(..., model=…)` (`ClipModel.load()` was already a no-op after the
first call). The app passes its **process-wide** `State.get_clip_model(settings)`
so search + the indexer share a single ~600 MB copy instead of two; the
standalone `python -m sortandpost.indexer` builds its own. The `/settings`
"Run indexer pass now" button reuses the shared model too. +1 regression test
(`test_loop_loads_the_clip_model_once_across_many_cycles`).

**Two-machine sync — end-to-end integration test (`bdcd38b`).** `test_sync_merge`
already covered `merge_all` at the function level; the new
`test_two_machines_with_separate_sync_dirs_converge` drives the full
`SyncManager` startup path: two independent `APP_DATA` trees, each with its own
`local/` + `sync/`, a fake sync tool that cross-delivers each side's DB into the
other's `sync/` as a conflicted copy, then a restart on each side. Asserts both
machines converge to identical ratings (LWW picks the newer device's edits +
imports the rating that existed on only one side), an identical unioned
`changes` log (3 rows), an equal `db_generation`, and that the conflict copies
are consumed + archived (not re-merged). A second sync round asserts
`change_key` de-dup holds across rounds — no double-applied mutations.

159 tests pass; ruff + format clean. (Pre-existing: 5 mypy `untyped-decorator`
warnings on the NiceGUI `@ui.page` handlers in `app.py` — unchanged by this work.)

---

## ✅ 2026-09-09 — 286-photo Docker index (timing) + `/settings`/pages check

Ran the real app (`docker run`, source overlaid, `HF_HUB_OFFLINE=1`, CLIP
weights pre-seeded) against `~/Downloads/sampleImages` (286 × 26 MP Fujifilm
X-S10 JPEGs), **fresh DB**.

**First-index timing** (8-core Docker Desktop VM, warm library cache):

| phase | wall time | outcome |
|-------|-----------|---------|
| scan (decode/hash/thumbnail/EXIF, 7 workers) | **63 s** | 286 photos, 572 thumbnails, 0 errors — browsable |
| CLIP pass (embed + zero-shot score) | **97 s** | 286 embeddings, 739 predictions, 0 errors |
| **total first index** | **~160 s** | |

Next indexer cycle: 286 unchanged via fast path in ~1 s; CLIP 0 new. Steady.

**App surface** (HTTP + in-container calls — no browser, extension declined):

- `/` 200 (120 tiles + pager: 120/120/46 = 286), `/photo/{hash}` 200 with EXIF
  panel (camera `X-S10`, `6240×4160`), `/search` 200, `/review` 200 (108 KB,
  sample thumbs for 19 pending-tag groups), `/settings` 200.
- Semantic search (real ViT-B-32): ranked distinct results per query, scores
  ~0.1–0.24 (real photos, flatter than the synthetic set).
- Zero-shot: 739 pending predictions, 19 tags (portrait ×208, night ×99,
  one person ×81, …) — plausible for a portrait-heavy Fuji set.
- `HF_HUB_OFFLINE=1` → CLIP loads from the local cache with **no** HF network
  call (mitigates the offline half of the per-cycle-reload finding; the reload
  itself still happens — ~1 s from disk now).

**Bug found + fixed — `Rating=0` mass-import.** Every Fuji JPEG embeds a literal
`Rating: 0`. Old `_reconcile_rating` treated `0` as "a rating" and, with no DB
row, imported it → **286 spurious `ratings` rows + 286 `rate` `changes` rows** on
a clean index of unrated files (bloats the merge-driving `changes` log and the
`/settings` audit count). Fix (`indexer/reconcile.py`): `not file_stars` (None
*or* 0) means "unrated" — no import / no change row when there's no DB rating.
An explicit `0` from a file *newer* than an existing DB rating still imports (=
user cleared the star in Lightroom); an older `0` still pushes the DB rating
back out. +3 reconcile tests. 157 pass; ruff + mypy clean.

---

## ✅ 2026-09-09 (later still) — process-pool indexer (spec gap #1)

CLAUDE.md hard constraint #7 / the "Indexer pipeline" section want a
`ProcessPoolExecutor` for decode/hash/CLIP; the scan was single-threaded. Done
for the `scan_once` path (the genuine bottleneck — decode + `pixel_hash` +
`dhash` + thumbnail-encode of full-res JPEGs):

- **New `sortandpost.indexer.workers`** — `decode_local()` / `decode_file()`
  (worker entry) run stages 2-4 and return a picklable `DecodedFile` (hash,
  dims, phash, thumbnails written). No DB, no exiftool handle. `DecodeJob`
  carries only primitives so a `spawn` worker has zero shared state.
- **`pipeline.persist_decoded()`** — the DB half (stage-3 identity, stage-5
  metadata read, stage-6 reconcile, `scan_state` bookkeeping). Runs on the main
  process only (owns the `Session` + the `-stay_open` exiftool). `index_file()`
  is now `fast-path check → decode_local → persist_decoded` for the serial path.
- **`scan_once(settings, *, workers=None)`** — main thread does the fast-path
  filter (one `scan_state` query) then feeds a bounded pool
  (`_decode_stream`, ≤ `2·workers` in flight so peak memory is flat); results
  are persisted + committed serially as they land. `workers=1` (or `< 8` files
  needing work) → inline, no pool. `workers` overrides `config.indexer.workers`
  (`auto` = `cpu-1`).
- **Resilience**: a `BrokenProcessPool` (spawn can't re-import `__main__`,
  sandbox limits, …) is caught — the scan finishes the remaining files inline
  instead of dying. Single-worker crashes on one file are recorded as that
  file's `scan_state.error` and retried next pass (≤ 3 attempts), pool intact.
- Idempotency/resumability preserved: fast-path cache unchanged; thumbnails are
  still content-addressed + write-once (safe from N workers); a worker-written
  thumbnail with no committed DB row is just re-confirmed next pass.
- **Tests**: +3 in `test_indexer.py` (parity vs inline, corrupt-file isolation
  under the pool, `BrokenProcessPool` → inline fallback). 154 pass; ruff +
  mypy + `alembic --autogenerate`-empty all still clean.

**Measured** (Docker Desktop, 8-core VM, 286 × ~17 MB / ~40 MP JPEGs, fresh
index, CLIP deferred so not counted):

| workers | `scan_once` wall time |
|---------|-----------------------|
| 1 (serial) | 146 s |
| auto (7) | 102 s — 1.44×, 0 errors, identical output (286 photos / 572 thumbs) |

Modest in Docker because (a) 7 workers contend on the library read over
virtiofs, and (b) the now-serial tail — 286 × (`exiftool` round-trip + rating
reconcile + `synchronous=FULL` commit) — dominates. Native (no virtiofs) should
widen the gap. **Next optimisation** (separate change): batch stage-5 into one
`exiftool.read_batch` per wave, and commit every N files instead of per-file
(still resumable — re-scan redoes ≤ N idempotent files).

Still open from the earlier findings: CLIP model reloaded every indexer cycle
(`scan_clip_once` not handed a cached model); parallelising the CLIP-pass decode
(torch already multithreads inference, so lower ROI — left for later).

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

**First: the bulk-delete work (this session) is uncommitted.** `git status` shows
working-tree edits to `CLAUDE.md`, `Features.md`, `pyproject.toml`, `app.py`,
`config.py`, `resources/config.default.yaml`, `db/models.py`, `clip/store.py`,
`sync/merge.py`, `indexer/{scan,pipeline}.py`, plus untracked
`src/sortandpost/trash.py`, `src/sortandpost/db/deletion.py`,
`tests/test_{trash,deletion}.py`. `231 tests pass; ruff + format + mypy clean;
alembic autogenerate empty.` Not reviewed or pushed.

Then, remaining `Features.md` items (unchecked, roughly in priority order):

1. **Dark / light mode** with a `/settings` manual/system choice (persist
   per-machine in `local/`, never synced).
2. **Saved searches** (Search features).
3. **Design pass** — scaffold → production looks.
4. Other pages — swipe-style triage of unrated photos; robopost.app social
   composer (API key → `.env`, per-machine, never synced).
5. **Bulk-delete follow-ups** (feature itself is done): an in-app "undo last
   delete" using the managed `local/trash/<ts>/manifest.json`; a `/settings`
   view of the local trash folder; a live two-container check of the
   cross-machine lazy-trash path (unit-tested only so far).

Older still-open verification work (from before this session):

7. **Browser smoke** of the non-feature surface at `http://localhost:8080`:
   `/review` bulk approve/reject, `/settings` "run indexer pass now", the
   status-bar footer, read-only banner. Confirm the `_header` banner (not just
   the footer) reflects a live read-only→writable promotion — it currently only
   re-renders on page load.
8. **RAW + HEIC** end-to-end with real `.CR2`/`.NEF`/`.HEIC` files — decode via
   embedded preview, RAW **never** written (check mtimes / logs). (The `.venv`
   now has `rawpy` + `pillow-heif`, so this can be done natively.)
9. **Dockerfile caching** (optional): a deps-only pre-layer to cache the ~150 MB
   torch wheel across code changes.

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
