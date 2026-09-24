# sortandpost — v1 roadmap

v.5 is "organize photos locally, index/ratings/tags/embeddings travel in a sync
folder, run it natively." v1 is the list below. Build order, per-phase model
choice, and status live in `V1Plan.md`; everything deferred is in `V2.md`.

_Created 2026-09-10. Scoped 2026-09-24._

---

## Features

### Filters
- I would like to be able to filter from the main library based on several different categores
        - Star Rating (including a category for unrated)
        - Camera
        - Date range i.e. 2025-2026 or 20260103-20260210
        - Folder
        - Subject type (from tags/CLIP)

**Decided 2026-09-24:** merge `/search` into the main library, so there's no separate
tab. One filter bar on `/`, with all state in the URL query string:
- A text query ranks by CLIP relevance and turns group-by off. Filters on their
  own keep the normal `taken_at` order, and group-by still works.
- Saved searches become a "Saved" dropdown in the bar. `/search` redirects to `/`.
- Photo-page arrows page through the filtered set (fixes the `Issues.md` bug).

### Control for CLIP CPU/memory usage — ✅ shipped (V1 P2)
- Add a setting for CLIP for slow/fast scan that shows a warning that fast scan will use all CPU cores and may slow down your computer
- Limit CLIP scan by default to use 30% CPU/Memory

### 1. Social media post composer (robopost.app)
The last unchecked `Features.md` item, and the app's namesake half.

- Group photos into a post with caption text; reorder; per-image alt text.
- Push to the **robopost.app API** — upload media, create a post, schedule for a
  future time.
- API key lives in `.env` (per-machine, never synced), same pattern as
  `PHOTO_LIBRARY` / `APP_DATA`.
- Build on **Collections** — a collection is already an ordered, de-duplicated
  photo group with a notes field. A "post" is a collection + schedule metadata +
  a publish log.
- New page `/compose` (or an action on a collection). Draft state stored like
  collections (`sync/posts.json` or extend `collections.json`).
- Guardrails: never auto-post; explicit "Schedule" button behind a confirm;
  show what will be uploaded and where.

### 2. `/search` filter UI completion
_Folded into **Filters** above (search is merged into `/`)._ The `PhotoFilter` model and `allowed_hashes` plumbing already support these —
only the widgets are missing.

- Date-range picker (taken_at from/to).
- Tag AND / OR selector (multi-tag).
- Currently wired: stars ≥ N, camera, has-GPS, duplicates, missing.

### 3. Per-photo `/review` controls + facet grouping — ✅ shipped (V1 P4)
Today `/review` is per-tag bulk approve/reject only.

- Per-prediction approve/reject on individual photos.
- Group pending predictions by facet (scene / subject / lighting / …), not just
  a flat tag list.

## Polish

### 11. Bulk-delete follow-ups — ✅ shipped (V1 P5)
The delete action itself is done (move-to-trash + tombstones + cross-machine
lazy purge). P5 moved every OS to the managed trash, added status-bar "Undo
delete", `/settings` → "Recently deleted" (Put back, Move all to system Trash)
and the `restore` change op. Still open: the live two-container test below, and
an un-tombstone control for files recovered after a system-Trash flush.

- In-app **"undo last delete"** using the managed
  `local/trash/<ts>/manifest.json`.
- A `/settings` view of the local trash folder (list, "reveal", "put back").
- A live two-container test of the cross-machine lazy-trash path (unit-tested
  only so far).

### 13. `/review` exiftool reuse — ✅ shipped (V1 P4)
Each bulk approve/reject spawns its own `exiftool -stay_open` process. Share
one serialized client across a review session.

---

## Robustness & scale

### 21. Full re-index from Settings
_Added 2026-09-24; not scheduled, lower priority._ A `/settings` action to clear the
index and rebuild it from scratch, as a recovery path if the database gets corrupted
or out of step with the library.

- Needs clear warnings, in a confirm dialog, of what a rebuild can lose. Anything that
  lives only in the DB and can't be re-derived from the photo files: ratings on RAW
  files (never written to the file), un-approved CLIP suggestions and their
  approve/reject decisions, flags, collections, saved searches, and manual tags on
  formats that can't hold embedded metadata. Ratings and approved keywords embedded in
  JPEG / HEIC / TIFF / PNG files are re-imported from EXIF.
- Design questions to settle first: take a snapshot before clearing (so it's undoable);
  what happens to synced thumbnails / embeddings and to the `changes` log and
  tombstones; how a second machine picks up the rebuilt DB (`db_generation`); and
  whether to offer a lighter "re-scan only" alternative.
- Only when this machine holds the write lock; runs through the normal indexer.

### 16. RAW + HEIC end-to-end audit — ✅ shipped (V1 P6)
Decode real `.CR2` / `.NEF` / `.HEIC` via embedded preview and **verify
constraint #2**: RAW files are never written to or beside (check mtimes + logs
before/after a rating write). Partly exercised (a full native index incl.
`.CR2` + `.HEIC` ran clean) but the no-write property isn't formally audited.

### 20. Live scan/CLIP progress in `/settings` — ✅ shipped (V1 P3)
The status bar (`_status_bar` in `app.py`) polls DB totals every 5s, so it
shows *outcome* (`present/total indexed`, pending predictions) but not
*in-progress* counts — during a big real-world scan there's no "X of Y files
scanned" / "X of Y embedded" while `scan_once` / `scan_clip_once` are actually
running. Surfaced 2026-09-10 during a real-library test that took 10+ minutes
with no feedback beyond the log.

- `scan_once`/`scan_clip_once` (`indexer/scan.py`) only return a stats object
  when the whole pass finishes; need a live counter updated per-file/per-batch
  during the loop (a shared `State`-held progress object, or a small in-memory
  struct the indexer writes to and `/settings` polls) — total-to-do has to be
  known up front (`len(todo)` / candidate count) for an X/Y display.
  `_INDEX_LOCK` (added 2026-09-10) already serializes passes, so there's only
  ever one in-flight progress to report.
- Two separate counters: indexing (decode/hash/thumbnail stage) and CLIP
  (embedding stage) — they run as separate passes (`_run_index_pass` calls
  `scan_once` then `scan_clip_once`) and can have very different remaining
  counts.
- Consider reusing the same plumbing for the CLAUDE.md-specified persistent
  status bar ("index progress X/Y, current stage, CLIP queue depth") — that's
  spec'd for v1 but the live-counter part was never built, only the
  DB-totals version above.
