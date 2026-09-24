# sortandpost — build progress & resume notes

_Last updated: 2026-09-24 (V1 P1: search merged into the library + filter bar)._
_Working branch: `v1-plan`._

---

## 🔎 2026-09-24 — V1 P1: search merged into the library, filter bar

`/search` is gone as a page (it redirects); `/` is library + search. 403 tests pass
(+48); ruff / format / mypy clean; no schema change. User browser-tested + signed off.

- **`library_view.py` (new)**: `LibraryQuery` = every filter + view param, round-trips
  through the URL (`stars=u,4,5`, repeated `camera=` / `tag=`, `dates=`, `folder=`,
  `tagmode`, flags, `group`). `resolve_view()` → ordered representative hashes; used
  by both the grid and `/photo` prev/next (fixes the `Issues.md` arrow-key bug).
  `parse_date_range` accepts `2025`, `2025-2026`, `20260103-20260210`, `2025-06`, open
  ends. Choices helpers for camera / folder (root-relative labels) / tags by facet.
- **`PhotoFilter`** gained `stars_in` (0 = unrated), exact `cameras`, `folder` (prefix
  on present `photo_paths`, subfolders included), `tag_suggestions` (tags also match
  pending/approved predictions). `grouping._camera_label` → public `camera_label`.
- **Saved searches** now store `{name, query}`; legacy `/search` fields are converted on
  load (`stars` minimum → set). Dropdown in the filter bar.
- **UI**: selects apply on every pick; dates are a Quasar range calendar (applies once
  the range is complete). The photo page's order is a 30-min snapshot of what the grid
  showed (`_VIEW_CACHE` in `app.py`), so rating a photo out of an "unrated" filter
  doesn't break ←/→.
- **Gotcha:** restart the dev server by port (`lsof -ti :8080 | xargs kill`) — `pkill -f
  "python -m sortandpost"` misses the `Python` process and the new server dies on bind.

---

## 🗺️ 2026-09-24 — V1 scoped

Roadmap docs reorganized: `Features.md` → `InitialFeatures.md` (history), the old
`V2.md` → `V1Features.md` (trimmed to the V1 scope), the deferred items → a new `V2.md`
(original item numbers kept), and `Issues.md` for bugs found in use.
**`V1Plan.md` is the resume point:** phase order P0–P7, which Claude model to use for
each phase, and a status checklist. P1 (search merged into the library + filter bar)
is next.

---

## 🛠️ 2026-09-10 — Real-library test fixes: indexer concurrency, stuck-file retry, notify-after-navigate

**Found during the user's first real-world run** (their own Synology-synced photo
library, ~2,465 files, native `.venv` on a MacBook Air), not the sample library.
`339 tests pass` (+1); ruff + ruff format + mypy clean.

- **Concurrent `scan_once`/`scan_clip_once` corrupted nothing but spammed
  `IntegrityError: UNIQUE constraint failed` on `scan_state.path` /
  `embedding_rows.pixel_hash`.** The always-on background indexer thread and a
  manual "Run indexer pass now" / "Save & rescan" / "Download & index" click
  each open their own `Session` and do get-or-create writes; nothing serialized
  them against each other. Added a process-wide `_INDEX_LOCK`
  (`indexer/scan.py`) shared by both entry points — only one pass runs at a
  time in this process now. The per-file error handling already recovered
  gracefully, so no data was lost, just noisy logs.
- **A file that failed to decode 3 times was abandoned forever, even after it
  was fixed.** `needs_reindex` (`indexer/pipeline.py`) checked the
  `attempts >= _MAX_ATTEMPTS` cap *before* checking whether the file's
  size/mtime had changed, so a Synology-sync file caught mid-download (14 real
  photos hit this) would never be retried once the download finished and the
  bytes became valid. Reordered so a stat change always forces a retry
  regardless of error history. New regression test
  (`test_corrupt_file_recovers_once_content_changes_past_max_attempts`) fails
  on the old ordering.
- **`ui.notify(result)` after a multi-minute rescan threw `RuntimeError: The
  parent element this slot belongs to has been deleted`** if the user
  navigated away from `/settings` before the scan finished — the same
  ambient-slot-context gotcha as bulk-delete (see PR history). Applied the
  established fix (`client = ui.context.client` captured at page-build time,
  post-await work wrapped in `with client:`) to the four long-running
  handlers: "Run indexer pass now", the two "Save & rescan" buttons, and
  "Download & index recorded files".
- Verified live: killed and cold-started the real-instance process twice
  (once per fix batch) against the same real-library `APP_DATA` — lock
  reacquired cleanly both times, reconcile scan matched exactly where it left
  off (`0 new, 0 errors`), CLIP embeddings intact (`0 newly embedded`, row
  count unchanged), web UI back on `:8080` within seconds.
- Added `V2.md` #20: live scan/CLIP progress counters for `/settings` (today's
  status bar only shows DB totals, not "X of Y scanned right now") — requested
  by the user off the back of this test.

---

## 🚀 2026-09-10 — Native-first runtime + real CLIP validated (PR #13)

**Merged to `main`.** `338 tests pass`; ruff/format/mypy clean; alembic empty.

**Decision.** Docker was the assumed runtime but never actually run in this
project — every test session has been a native `.venv` run. For a personal
desktop tool it's the wrong default: Docker Desktop is a heavy dependency,
bind-mount scans are slow on mac/Windows, CLIP can't use MPS in the container,
and "Reveal in Finder" needs the out-of-container helper. **Native is now the
primary runtime; Docker is reframed as the server / NAS / headless option**
(both still supported — the codebase already runs both). Full v2 backlog in
`V2.md` (repo root), including "flesh out Docker for NAS/server".

**CLIP device auto-detect.** `clip/model.py` `_resolve_device()`: `device:
"auto"` → `cuda` / Apple `mps` / `cpu`. Default flipped `cpu` → `auto` in
`ClipConfig` + `config.default.yaml` + `CLAUDE.md`. Docker still lands on cpu
(no passthrough). Encoders use the resolved `self.device`.

**First real CLIP run, ever.** Installed `torch` 2.14 + `open-clip-torch` +
`watchdog` into `.venv`; indexed the ~300-photo sample library on **MPS**:
model loads on `mps`, 0 errors, embeddings finite + L2-normalized, semantic
search returns differentiated sensible hits, `/review` + photo-page suggestion
chips populate. **Boots and serves fully offline** (`HF_HUB_OFFLINE=1`) from
the cached weights in `<APP_DATA>/local/models/` (~577 MB, ViT-B-32
laion2b_s34b_b79k). `watchdog.*` moved into the `follow_imports=skip` mypy
group (no stubs).

**Social media post composer → v2.** The one remaining `Features.md` item is
deferred; v1 is the "organize" half.

---

## ☁️ 2026-09-10 — Online-only (cloud placeholder) file handling (PR 2 of 2)

**Committed on `scaffold` (PR `scaffold → main`).** `330 tests pass` (+10);
ruff + ruff format + mypy clean; `alembic revision --autogenerate` empty on a
fresh DB (new `scan_state.online_only` column absorbed by `0001_baseline`).
Verified headless + a live native run against `browser_test_data` with a real
8 MB sparse stub (`st_blocks == 0`) in `~/Downloads/sampleImages` — banner,
`/settings` editor, skip↔download toggle, "Download & index", zero JS errors.
Finishes the plan `~/.claude/plans/recursive-doodling-frost.md` (Part C).

**Why.** The photo library lives in a cloud-sync folder; OneDrive / Drive /
Dropbox / iCloud leave placeholder stubs whose bytes download only on open. The
`decode` stage would silently hydrate every file on a first scan of a dehydrated
library (hundreds of GB).

- **`src/sortandpost/indexer/placeholder.py`** (new) — `is_online_only(st)`, a
  no-open check on the `os.stat_result` the scanner already has: macOS
  `st_flags & SF_DATALESS` (0x40000000); POSIX `st_blocks == 0 and st_size >
  4096`; Windows `st_file_attributes & (OFFLINE | RECALL_ON_OPEN |
  RECALL_ON_DATA_ACCESS)`. Any doubt → `False` (decode as before).
- **`IndexerConfig.online_only: Literal["skip", "download"] = "skip"`** (+
  `config.default.yaml`, CLAUDE.md). Threaded through `PipelineConfig` →
  `DecodeJob` → `decode_local` (`online_only_mode`); `decode_local` returns early
  with `DecodedFile(online_only=True)` and no decode when a stub is met in
  `skip` mode.
- **`pipeline.persist_decoded`** — new `IndexOutcome.online_only`; sets
  `scan.online_only=True`, `stages_done=STAT`, **does not bump `attempts`** (it's
  a deferral, not a failure). Success path clears the flag. `needs_reindex`
  returns `True` while the flag is set (cheap re-check each pass).
- **`ScanState.online_only: bool = Field(default=False, index=True)`** — no
  migration (baseline `create_all`). `ScanStats.online_only` counter +
  `__str__`.
- **`src/sortandpost/indexer/hydrate.py`** (new) — `hydrate_online_only(settings)`:
  bounded read (1 MiB chunks, 64 MiB cap) of every recorded stub under this
  device's `container_roots` to trigger the sync client's fetch, then clears the
  flag + stat fast-path; caller rescans. `HydrateStats(requested/hydrated/failed)`.
- **`db/browse.online_only_count()`**.
- **`app.py`** — dismissible `/` banner (count + "Download & index", writable
  only) before the empty-state check; `_online_only_editor` in `/settings`
  (skip/download `ui.toggle` → `config.yaml`, one-shot "Download & index recorded
  files"); "Online-only" info row; `_run_hydrate_pass` helper (hydrate +
  `_run_index_pass`).
- **Tests** — `test_placeholder.py` (7), `test_hydrate.py` (2: flag-clear +
  rescan indexes; `OSError` keeps the flag), `test_indexer.py`
  (`is_online_only` monkeypatched: recorded not indexed, `attempts == 0`, 2nd
  pass no `attempts++`, clearing the detector indexes it), `test_browse.py`
  (`online_only_count`).

**Known limits** (carried from the plan): the POSIX `st_blocks == 0` heuristic
can false-negative (just hydrates, as before); `online_only` `ScanState` rows
union across machines so the count can double-count (advisory only); a roots
change isn't *watched* until an indexer restart (same as `exclude_dir_names`).

---

## 📁 2026-09-10 — Per-device library folders + Settings picker + host file-manager helper (PR 1 of 2)

**Committed on `scaffold` (PR `scaffold → main`).** `320 tests pass` (+29);
ruff + ruff format + mypy (gated + `library_roots.py` + `helper.py` +
`tools/sortandpost_helper.py`) clean; alembic autogenerate empty (no schema
change). Verified headless + a live native run against `browser_test_data` +
the helper agent — add/remove folders, de-quoting, native-vs-Docker dialog,
`/health` + `/listdir` + host-path reveal buttons, Save & rescan — zero JS
errors. **PR 2 = online-only / placeholder files** (still to build; plan file
`~/.claude/plans/recursive-doodling-frost.md`).

**Why.** Library lives in a cloud-sync folder that mounts at a different path on
every machine; `library_roots` was one shared value in `sync/config.yaml`
(wrong cross-platform), hand-edited, and "Reveal in Finder" was dead under
Docker.

**Per-device roots** — new `src/sortandpost/library_roots.py`:
- `LibraryRoot(container, host, label)` — `container` = what the scanner walks
  (a bind-mount target under Docker; the real folder natively), `host` = the
  real per-machine path for display / copy / reveal.
- Stored in `sync/devices/<slug(hostname)>-<device_id[:8]>/library.json` (rides
  the sync tool, but a device only writes its own subfolder — no conflict copy).
  `device_dir()` matches by the `-<id8>` suffix so a hostname change still
  adopts the folder. `paths.devices_dir`.
- `load_roots` / `save_roots` (atomic), tolerant of corrupt / bare-list /
  legacy-string. `container_roots()`, `to_host_path()` (longest-prefix swap,
  Windows-separator aware), `compose_volume_line()`, `clean_path()` (strips
  wrapping quotes/space — users paste `'…'` from a shell), `running_in_docker()`
  (`/.dockerenv` / cgroup).
- `config.py`: `Settings.library_roots` + `hostname`; `resolve()` seeds the
  per-device file **once** from `AppConfig.library_roots` on first run after
  upgrade, then that file is canonical. `Settings.set_library_roots()` for the
  test fixtures. `load_device()` returns `(id, hostname)`.
- Consumers (`indexer/scan.py`, `indexer/service.py`, `/settings` row) →
  `container_roots(settings.library_roots)`.

**Host helper agent** — `tools/sortandpost_helper.py` (stdlib only, ~240 lines):
- `ThreadingHTTPServer` on `127.0.0.1` (`--app-data <host path>`, `--port`
  41847). **`_Server` overrides `server_bind`** to skip `socket.getfqdn()` —
  that reverse-DNS lookup hangs for many seconds on macOS.
- Writes `local/helper.json` `{port, token, os, heartbeat_at}` (rewritten every
  60 s); the container reads it via the `/data` mount. `src/sortandpost/helper.py`
  `read_helper_info()` (stale after 150 s).
- Endpoints (all need `X-SP-Token`; CORS reflects only `localhost`/`127.0.0.1`
  origins): `/health`, `/reveal`, `/open`, `/listdir` (native FS browser; dir
  names only, dotfiles hidden). `/reveal` + `/open` are confined to the `host`
  roots from `sync/devices/*/library.json`; subprocess = argv array, never a
  shell.

**`app.py`**:
- `_header` injects `window.__spHelper = {port, token}` (or `null`) + a 🖥 chip
  when connected (`_current_helper`, `_inject_helper`).
- `_library_roots_editor` in `/settings` — rows with a scanned / not-found
  badge + remove; **Add folder** dialog: native run shows just the host-path
  field (+ **Browse** via the helper's `/listdir` when up); Docker run adds the
  mount-point field + a live `ui.code` compose snippet. Save persists to the
  per-device file; **Save & rescan** (writable only) also runs `_run_index_pass`.
- `_path_row` shows/copies `to_host_path(pp.path, roots)` (+ a `container: …`
  caption); Reveal / Open call the helper client-side (`_helper_action_js` →
  `{ok, error}` → `ui.notify`), falling back to the native openers, then to
  copy-only.

**Bug fixed (pre-existing) — `indexer/scanner.py._walk` aborted the whole scan
on one unreadable entry.** Only `iterdir()` was guarded; a per-entry
`is_symlink()` / `is_dir()` raising `PermissionError` (a macOS SIP data-vault
under `/Library`, a locked mount) propagated out and killed `scan_once`. Now
each entry is in its own `try/except OSError` → skip it, keep walking. Surfaced
when a native user accidentally set a root's `container` to `/library` (→
macOS `/Library`).

**Docs**: `CLAUDE.md` (app-data layout + a "Library folders are per-device" +
"File-manager helper" section), `docker-compose.yml` (multi-mount pattern +
helper note), `.env.example`, `config.default.yaml`.

**Tests** (+29): `test_library_roots.py` (round-trip, corrupt/legacy, dedupe,
`device_dir` stale-hostname adopt, `to_host_path` POSIX+Windows+passthrough,
`clean_path`), `test_helper.py` (in-process server: `/health` no-auth, token
401/403, `/reveal` argv + outside-roots 403 + relative 400, `/listdir` dirs-only
+ dotfiles + PermissionError, `read_helper_info` staleness), `test_scanner.py`
(unstatable entry skipped not fatal), fixture switch in 4 indexer test files.

**Not done / follow-ups**: PR 2 online-only files; the Browse dialog is tall
with lots of empty space; `run_indexer` still fixes the `watchdog` roots at
startup (a roots change triggers an immediate `scan_once` + the timer, but new
roots aren't *watched* until restart — same as `exclude_dir_names`).

---

## 🎨 2026-09-10 — Features.md: Design pass (first cut)

**Committed on `scaffold` (PR `scaffold → main`).** `291 tests pass` (no new —
CSS/UI strings only); ruff + ruff format + mypy (41 gated files incl. `app.py`)
clean; alembic autogenerate empty (no schema change). Verified with headless
Chromium against `browser_test_data` — `/`, `/?group=day|folder`, `/collections`
(+ detail), `/search`, `/settings`, `/rate`, `/photo/{hash}` in **both** light
and dark — zero JS/console errors.

**Decisions** (asked): aesthetic = **warm editorial** (paper backgrounds, an
old-style serif for display type); scope = **restyle, keep layouts** (no sidebar,
no page restructure — lower risk to the fragile scroll-restore / keyboard code);
accent = **indigo / violet** (`#4f46e5`), amber kept only for the keyboard-focus
ring + edit flag. User signed off on the first cut ("looks good, ship it"),
detailed feedback to come later.

**`app.py` only:**

- New module constant **`_THEME_CSS`**, added via
  `ui.add_css(_THEME_CSS, shared=True)` next to `_GRID_KB_CSS`. Design tokens on
  `:root` / `body.body--dark`: `--sp-bg` (`#faf8f4` / `#1b1815`), `--sp-surface`,
  `--sp-surface-2`, `--sp-border`, `--sp-text` + `-dim` + `-faint`, `--sp-shadow`
  + `-lg`. Component classes: `.sp-display` (serif stack — Iowan Old Style →
  Palatino → Georgia; **system fonts only**, no webfont fetch), `.sp-muted` /
  `.sp-faint`, `.sp-appbar`, `.sp-nav` / `.sp-nav-active`, `.sp-chip`,
  `.sp-statusbar`, `.sp-card`, `.sp-section-header`, `.sp-empty`, plus a
  `.q-btn-toggle` restyle (warm surface / dim text inactive, solid indigo active,
  `border-radius: 0` on the inner buttons so the active one doesn't overflow the
  rounded group — fixes the "stark white toggle in dark mode" note).
- **`ui.colors(...)`** now called at the top of `_header` (page context): indigo
  `primary` + matching `secondary`/`accent`/`positive`/`negative`. Flows into
  every `color=primary` button, `ring-primary`, `bg-primary`, `.sp-nav-active`.
  Quasar's `.q-layout__section--marginal` (which paints header + footer
  `bg-primary`) is overridden by `.sp-appbar` / `.sp-statusbar` (specificity
  `(0,2,0)` + `!important`).
- **`_header`** rebuilt: warm translucent blurred bar + hairline bottom border
  (was solid `bg-primary` blue), serif wordmark that links to `/`, tiny dim
  version, nav as quiet `.sp-nav` pill links with an indigo active state,
  read-only/writable rendered as an uppercase `.sp-chip`.
- **`_status_bar`** footer: `.sp-statusbar` (warm dark `#201b15`) replacing
  `bg-gray-800 text-gray-200`.
- **`_empty_state(icon, title, hint="")`** helper (centred icon + serif title +
  hint) replaces the bare `ui.label("No …").classes("m-4 text-gray-500")` at:
  library `total == 0`, `/collections` empty, `photo_detail` not-found,
  `collection_detail` not-found.
- Targeted class swaps (no structure change): group-by `_group_header` →
  `.sp-section-header` + serif label; collections `_card` → `.sp-card`, `_mosaic`
  placeholders → `var(--sp-surface-2)`; bulk toolbar pill → warm `rgba(32,27,21,.96)`;
  `collection_detail` title + `settings_page` ("Settings" h2 added) → `.sp-display`;
  a handful of `text-gray-400/500` counters → `.sp-muted` / `.sp-faint`.

**NiceGUI notes (this round):**

- `ui.header` / `ui.footer` get their default colour from Quasar's
  `.q-layout__section--marginal { background: var(--q-primary); color:#fff }` (in
  `quasar.unimportant.css`, specificity `(0,1,0)`) — **not** from a class NiceGUI
  adds. Override with a `(0,2,0)` selector like `.sp-appbar.q-header`.
- `ui.colors()` needs a page/slot context — call it inside a per-page function
  (`_header`), not in `build_ui` (which runs once at startup, no context).
  `ui.add_css(..., shared=True)` is the opposite — fine in `build_ui`, global.
- NiceGUI 3.16 dark mode = Quasar's `body.body--dark` class (also drives the
  UnoCSS `dark:` variant). Token overrides key off `body.body--dark`.
- The deep-in-the-grid `bg-gray-100/200` tile placeholders were left alone
  (behind photos, near-invisible) — `_THEME_CSS` does **not** retint Tailwind/
  UnoCSS utility classes wholesale (specificity war, fragile); it swaps at call
  sites and adds semantic `.sp-*` classes instead.

**Not done / follow-ups:** the user will send detailed feedback later — likely
card definition strength, how far the serif leans, header density, accent
weight, input/field styling, `/search` + `/review` + `/photo` fit. Photo-page
layout + `/review` grid density untouched this pass.

---

## 🧭 2026-09-10 — Features.md: Library group-by mode

**Committed on `scaffold` (PR `scaffold → main`).** `291 tests pass` (+19);
ruff + ruff format + mypy (gated paths + `grouping.py` + `app.py`) clean;
alembic autogenerate empty (read-only projection, no schema change). Verified
across many headless-Chromium runs against a throwaway full index of the sample
library (RAW + HEIC included, no dir excludes) so every mode has multiple
sections — all four modes, all four folder sorts, per-section + all collapse /
expand, keyboard skip-collapsed, sticky offset, back/forward persistence — zero
JS/server errors.

**Decisions** (asked): facets = **day / month / folder / camera**; folder
sub-sort = **newest / oldest / A-Z / Z-A** (user-picked); persistence =
**URL param only** (`?group=…&folder_sort=…`) — nav link + fresh visit start
flat, back/forward keeps the grouped view. Collapse was added after review;
its state rides in `history.state` (not the URL).

**New module** `src/sortandpost/grouping.py` (pure logic, no DB/UI imports):

- `GroupRow` NamedTuple (`pixel_hash`, `taken_at`, `primary_path`,
  `camera_make/model`, `file_mtime_ns`) + `PhotoGroup` (`key`, `label`,
  `sublabel`, `hashes`).
- `parse_mode` / `parse_folder_sort` (tolerant query-param → enum).
- `group_photos(rows, mode, *, folder_sort, today)` — assumes `rows` already in
  the page's display order (newest-first); day/month insert headers in place
  with Today/Yesterday labels + trailing "No date"; camera groups by make+model
  (label de-dupes a repeated make) ordered by each group's newest photo,
  "Unknown camera" last; folder keyed on the parent dir (handles `\` and `/`),
  header = leaf name + full-path sublabel, ordered by max `file_mtime_ns`
  (newest/oldest) or name (az/za).
- `db/browse.list_group_rows(session)` — `GroupRow`s in `list_photos` order
  (reuses it with a large limit; no multi-column `select()` typing pain).

**`app.py` — `/` page (`library(group="", folder_sort="")`):**

- `if mode`: `list_group_rows` → `group_photos` → `rep_ids` re-ordered into
  section order; `group_bounds` / `key_hashes` / `_seg_starts`+`_seg_keys` +
  `_key_for_index(i)` (bisect) map a flat index back to its section.
- Scaffold: flat mode keeps the one `grid`; grouped mode builds, per section, a
  **sticky clickable header** (`_group_header`, chevron via `_style_chevron`
  using `.style(replace=…)` so the rotate clears on expand) + its own
  `_photo_grid()` in `group_grids`.
- `_render_tiles(container, hashes)` factored out of `append_batch`; the batch
  loop splits its slice by section and skips folded ones (still advances
  `loaded`, so scroll walks past them).
- Collapse: `collapsed: set[str]`, `_toggle_group` / `_rebuild_section`
  (drops the section's tiles + registry entries, redraws the already-reached
  ones on expand) / `_repaint_header` / `_set_all_collapsed`. Collapse-all /
  Expand-all buttons (`unfold_less` / `unfold_more`) by the group toggle.
- `_move_focus` steps the keyboard ring over folded sections in the travel
  direction (bails if nothing open that way).
- `_GRID_SAVE_JS` / `_GRID_RESTORE_JS` carry `c` = sorted collapsed keys
  (via `window.__spCollapsed`); the restore block re-folds them before the
  catch-up loop so back/forward lands right.
- `_GRID_COLS_JS` now reads the resolved `grid-template-columns` track count of
  the fullest `.nicegui-grid` (was a first-row DOM walk — broke on full-width
  headers / per-section grids).
- New: `import bisect`, `import json`. `_GRID_KB_CSS` +
  `scroll-margin-top: 4.5rem` on grid cells so a keyboard-focused tile isn't
  parked under the app header / a sticky section header.

**NiceGUI gotchas (new this round):**

- `Element.style("…")` **merges** properties — a later call can't unset an
  earlier `transform`. Use `.style(replace="…")` to fully swap.
- `getComputedStyle(el).transform` is `matrix(1,0,0,1,0,0)` (not `"none"`) for
  `rotate(0deg)` — test the matrix `a` term, not `!== 'none'`.
- `ui.header` (Quasar `q-header`) renders **60 px** tall here → sticky section
  headers use `top-16` (64 px), not `top-12`.

**Not done / follow-ups:** the infinite-scroll "race to the end" when a big
section is collapsed (see NEXT SESSION #4); a design pass on the white
`ui.toggle` buttons in dark mode.

---

## 🗂️ 2026-09-10 — Features.md: Collections

**Committed on `scaffold` (PR `scaffold → main`).** `276 tests pass` (+24);
ruff + ruff format + mypy (gated paths + `collections.py`/`app.py`) clean;
alembic autogenerate empty (collections are JSON, no schema change). Verified
across many headless-Chromium runs against `browser_test_data` (create + notes +
add via photo pages + detail reorder/remove + rename + delete + bulk-toolbar
add + the picker's checkbox model + keyboard nav + scrolling + the square
mosaic) — zero JS/server errors.

**Decisions** (asked): storage = **`sync/collections.json`** (rides along like
`tags.yaml`, not conflict-merged — a two-machine simultaneous edit yields a
sync-tool conflict copy); photo order = **manual / insertion order**; surface =
**dedicated `/collections` index + `/collections/{id}` detail pages + nav
entry**. Collections editing is **not** write-lock-gated (same as saved
searches).

**New module** `src/sortandpost/collections.py` (mirrors `searches.py`):

- `Collection` dataclass — `name`, `id` (`secrets.token_hex(8)`), `notes`,
  ordered de-duplicated `photos` (pixel_hash list), `created_at`/`updated_at`.
  `__post_init__` de-dupes + `clean_name`s + stamps timestamps.
- `clean_name(raw)` — strips C0/C1/DEL control chars, collapses whitespace runs,
  trims, caps `NAME_MAX_LEN = 120`. Applied on create / rename / load. (A
  semicolon etc. is fine — `json.dumps` escapes everything; the point is control
  chars + length.)
- `load_collections` — tolerant (bad JSON → **moves the file to
  `collections.json.corrupt-<ts>`** and returns `[]`, never silently
  overwrites; bare-list JSON, nameless/unknown-key rows all handled).
  `save_collections` = atomic `.json.tmp` → replace, `sort_keys`, `indent=2`.
- CRUD: `get_collection` / `collections_containing` / `create_collection` /
  `rename_collection` / `set_collection_notes` / `delete_collection` /
  `add_to_collection` (append, skip dupes, keep order, returns #added) /
  `remove_from_collection` / `reorder_collection` (permutation; unknowns
  ignored, leftovers kept in old relative order). All name-sort the list.
- `paths.collections_file` → `sync/collections.json`.

**`app.py`**

- Nav: `("/collections", "Collections")` (2nd, after Library).
- `/collections` — `_card` per collection: a **clickable `<div>`** (NOT
  `ui.link` — NiceGUI's `<a>` collapses to ~2 px around a block child, which was
  clipping the preview), a **square 2×2 mosaic** of the first up-to-4 members
  built from plain `aspect-square` divs with CSS `background-image` (q-img
  ignores an outer `aspect-ratio`; muted filler for empty slots; icon
  placeholder at 0), then the **name + count underneath** in a white footer.
  Hover-delete button uses `.on("click.stop", …)` so it opens the confirm
  dialog instead of navigating. "New collection" dialog → **Create** /
  **Create & add photos** (the latter → `/collections/{id}?add=1`).
- `/collections/{coll_id}` (optional `add: int = 0`) — back link, title + meta,
  **[Add photos · Rename · Delete]**, then the **photo grid at the top**
  (`_member_tile`: order badge + hover remove / move-earlier / move-later,
  re-renders in place), then a "N missing or deleted — Remove them" row, then
  the **Notes section at the bottom** (`ui.textarea`, save on blur + button).
  `?add=1` auto-opens the picker via `ui.timer(0.4, …, once=True)`.
- **Add-photos picker** (`_add_photos_dialog`) — maximized dialog, card
  `flex h-full flex-col`, grid inside a plain **`overflow-y-auto` div**
  (`min-height:0` so the flex child can scroll — NOT `ui.scroll_area`, which
  moves content with a transform and defeats native `scrollIntoView`).
  Candidates = `list_photo_ids` minus current members, capped `_PICKER_CAP =
  500`. Reuses the library's `_GridSelection` (hover checkbox, click /
  shift-click ranges) + a live "N selected" + a primary ring on selected cells.
  Keyboard: `_key` on a `ui.keyboard(ignore=["input","select","textarea"])`
  (dropping `"button"` from the default — otherwise a button keeping focus after
  the click kills every shortcut), arrows move a gold `.kb-focus` ring
  (`_GRID_KB_CSS` extended to `.sp-pick.kb-focus::after`, same `#fbbf24` border
  as the library), `↑/↓` jump a row via `_cols()` **measured once and cached**
  (per-press JS roundtrips timed out under fast presses → fell back to 1),
  `scrollIntoView` targets the **raw DOM node**
  (`document.querySelectorAll('.q-dialog .sp-pick')[idx]` — `getElement(id).$el`
  isn't the `<div>` for a plain `ui.element`), Space/Shift+Space select,
  Enter adds, Esc cancels. After `dialog.open()` a JS snippet blurs the trigger
  `<button>` so focus lands on the dialog.
- Bulk toolbar: icon-only **Add to collection** button (`_collection_add_button`,
  `label=None`) → menu of collections + "New collection…"; adds the selected
  representatives (no stack fan-out — collections are hand-curated), clears the
  selection. Shown in read-only mode too.
- Photo page: a **Collections** panel — chips for every collection containing
  the photo (click → detail, ✕ → `remove_from_collection`) + the same
  `_collection_add_button` (labelled).

**Tests** — `tests/test_collections.py` (+24): CRUD round-trips, order
preservation, dedupe, reorder-permutation-with-leftovers, `collections_containing`,
name-sort, `clean_name` (control chars / whitespace / length / keeps `;`),
create+rename sanitize, semicolon/quotes/backslash round-trip through the file,
corrupt-file backup + recovery, bare-list JSON, coerce drops nameless / non-str
photos, add-to-missing is a noop.

**Not done / follow-ups**: drag-to-reorder on the detail page (buttons only for
now); a picker filter box for very large libraries (capped at 500, falls back to
the bulk toolbar); Collections is the intended base for the **robopost.app
social composer** (next).

---

## 🎯 2026-09-09 — Features.md: `/rate` swipe-triage page (first of "Other pages")

**Uncommitted.** `252 tests pass` (+2); ruff + format + mypy (gated paths) clean;
alembic autogenerate empty (no model changes). Verified in a real browser
(headless Chromium against `browser_test_data`): card renders the large preview +
meta + star widget, `1`–`5` rates and auto-advances with the counter updating,
`f` toggles the flag (on/off, "added/removed from edit list"), advancing shows
the next photo's own flag state, no JS errors.

**Decisions** (asked): interaction = **keyboard + big buttons** (no drag-swipe
gesture); card source = **fresh random batch per visit** (skipped photos can
resurface on a later visit; no persistent skip list).

- New `db/browse.py` helpers: `unrated_count(session)` and
  `unrated_photo_ids(session, *, limit=60, exclude=())` — present photos with no
  `ratings` row or `stars == 0`, `ORDER BY random()`, `exclude` drops
  hashes already pulled this page load.
- `app.py`: `("/rate", "Rate")` in `_PAGES`; `@ui.page("/rate")` `rate_page()`.
  In-memory `queue` / `seen` / `idx`, `_load_more()` pulls another batch on
  demand (excluding `seen`), `_current()` skips hashes that went missing/deleted
  since the pull. `_render()` rebuilds one card: `ui.image` needs a concrete
  width (`w-full max-w-2xl` + `max-height: 78vh` + `fit=contain`) or Quasar's
  `q-img` collapses to zero height. `1`–`5` → `_rate()` (`io_bound(_apply_rating)`
  → `unrated_count` re-query → advance on `stars >= 1`), `→`/`S`/`Space` →
  `_step(1)`, `←` → `_step(-1)` (disabled at idx 0), `f` → toggles the
  `_edit_flag_button` switch held in `flag_holder[0]`. Space page-scroll
  suppressed via the shared one-time capture-phase guard (same as the grid).
- Read-only: banner, `rating` readonly, `_edit_flag_button` returns `None`;
  skip/back still work.

---

## 🔖 2026-09-09 — Features.md: saved searches

**Uncommitted.** `250 tests pass` (+12); ruff + format + mypy clean; alembic
autogenerate empty. Verified in a real browser: save with filters → runs +
persists across reload; pill re-runs; delete removes it in place + from the file;
sorts by name.

**Decisions** (asked): storage = **synced `sync/searches.json`** (rides along
like `tags.yaml`, not conflict-merged — a simultaneous two-machine edit yields a
sync-tool conflict copy, kept for manual recovery; deliberately lighter than the
DB + `changes` log). Scope = **full `/search` state** (query + all filters).

- New `src/sortandpost/searches.py` — `SavedSearch` dataclass (fields == the
  `/search` query params) with `query_string()` / `summary()` / `is_empty()`;
  `load_searches` / `save_searches` / `add_search` (replace-by-name,
  case-insensitive; keeps the list name-sorted) / `remove_search`. Tolerant of a
  corrupt file, a bare-list JSON, nameless/unknown-key rows.
  `paths.saved_searches_file` → `sync/searches.json`.
- `/search` (`app.py`): a `bookmark_add` button → name dialog → `add_search` →
  navigate to the new query. A `saved_row` (always rendered, even on the empty
  landing) of pill rows: a name button (`ui.navigate.to(query_string())`, tooltip
  = `summary()`) + a `×` delete button; `_delete` re-renders in place. `_apply`
  was refactored to build a `SavedSearch` and reuse `query_string()` (one
  URL-builder). `_current(name)` snapshots the live widget values.

---

## 🌗 2026-09-09 — Features.md: dark / light / system theme

**Uncommitted.** `238 tests pass` (+7); ruff + format + mypy clean. Verified in a
real browser: toggling Dark flips `<body>`, persists across navigation and a full
reload, writes `local/ui_prefs.json`; System follows the OS.

- `/settings` → **Appearance** → a `System / Light / Dark` `ui.toggle`
  (`_theme_editor`, works in read-only too).
- New `src/sortandpost/prefs.py` — `local/ui_prefs.json` (a small dict; never
  synced, so a laptop and desktop each keep their own). `get_theme` /
  `set_theme`, corrupt/unknown → `"system"`. `paths.ui_prefs_file`.
- `State.theme` loaded once in `build_ui`; `_header` (called by every page) now
  creates `ui.dark_mode(_THEME_TO_DARK[State.theme])` and **returns** it so
  `settings_page` can drive it live. `None` = follow the OS.
- Not a full dark-theme design audit (that's the "Design pass" item) -- the
  Quasar `ui.toggle`'s unselected buttons render white, a bit stark on dark.

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

**All `Features.md` items are done or deferred.** The social composer and the
full backlog are in **`V2.md`** (repo root). What's left before calling v1
"production":

1. **RAW + HEIC no-write audit** — index real `.CR2` / `.NEF` / `.HEIC`, then
   rate one and confirm the RAW file's mtime / bytes are untouched (hard
   constraint #2). `.venv` has `rawpy` + `pillow-heif` + `torch` now. A full
   native index incl. `.CR2` + `.HEIC` has run clean (0 errors) — the no-write
   property just isn't formally audited.
2. **In-app indexer lock re-check** — a web session that starts read-only never
   promotes itself to indexing until restart (`manager.refresh()` never called
   in-app; the standalone `python -m sortandpost.indexer` does re-check).
   Harmless single-machine; matters with two machines on one sync folder.
3. **Browser smoke** of the non-feature surface: `/review` bulk approve/reject,
   `/settings` "run indexer pass now", the status-bar footer, a live
   read-only→writable header promotion.

Everything else (design pass 2, bulk-delete undo, group-by scroll race,
Dockerfile caching, `/search` filter widgets, Docker/NAS hardening) → `V2.md`.

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
