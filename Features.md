# Features to add
## Library Management
- [x] Star rating widget
    - Make the star rating widget functional
    - Done: interactive 1–5 widget on /photo; writes ratings row + changes log + EXIF; read-only aware
- [x] Browser back button navigation
    - Ensure browser back button goes to last page rather than reloading to home page
    - Done: grid remembers loaded count + scroll position, restored on back/forward nav; photo page back buttons use history
- [x] Have previews span the whole page rather than just one column and dynamically scale by browser window size
    - Done: full-bleed responsive grid (auto-fill, minmax 220px) on /, /search, /review
- [x] Dark/light mode with an option in the settings to choose manually or go based off system settings
    - Done: `/settings` → Appearance → System / Light / Dark toggle. `ui.dark_mode`
      set from `State.theme` in `_header` (every page). Persisted per-machine in
      `local/ui_prefs.json` (never synced) via new `src/sortandpost/prefs.py`;
      `paths.ui_prefs_file`. Applies to the current tab live, other tabs on reload.
      +7 tests.
- [x] Bulk rating/tagging
    - Selection: checkbox on tile hover + shift-click range (decided)
    - Done: hover checkbox per tile, shift-click ranges, floating toolbar (★1-5 / clear / tag input); writes fan to stack members; new db/tags.py (add/remove manual tag + changes row + EXIF keywords)
- [x] Bulk delete files
    - Confirm dialog stating files will be deleted from the actual folder
    - Done: CLAUDE.md constraint #1 gained a 2nd exception (explicit user delete). Files are *moved* — OS Trash on macOS/Windows (`send2trash`), else a recoverable `APP_DATA/local/trash/<ts>/` folder + `manifest.json` (never a `.Trash-*` under a library root). Bulk-toolbar Delete + photo-page "Delete photo", both behind a confirm dialog + read-only aware; tiles drop from the grid in place (no reload). Purges every derived row + thumbnails, compacts the CLIP matrix, logs `ChangeOp.unindex` + a `tombstones` row. Cross-machine: `unindex` merge replay writes the tombstone on every machine; each purges its rows and lazily trashes its own copy on the next scan; the scanner refuses to re-index a tombstoned hash. `deletion.stack_scope` on /settings ("selected" default vs "whole_stack"). New `src/sortandpost/{trash.py,db/deletion.py}`, +26 tests.
- [x] "Marked for editing" tag
    - A quick flag/tag to compile a list of images to edit; bulk toolbar button + a way to view the list
    - Done: reserved `to-edit` manual tag (rides the changes log + conflict merge, never embedded into files); new src/sortandpost/db/flags.py; bulk toolbar flag/unflag buttons (repaint the visible tiles live, no refresh); clickable ⚑ in each grid tile's top-right corner (solid amber + always visible when set, dim + hover-only when not); "Mark for editing" switch on the photo page (stack-level, like ratings); live "⚑ N marked for editing" quick link on the library header → /search?mark=true, with a "clear all" button (confirm dialog) that appears whenever ≥1 is flagged; "⚑ to edit" checkbox on /search
- [x] Show star rating on hover (if already rated)
    - Grid tile shows its ★ rating as an overlay on hover when the photo has one
    - Done: clickable star widget on every grid tile, top row by the checkbox, hidden until the tile is hovered; rate directly from the grid; desaturated amber; read-only aware
- [x] exclude specific subfolders based on name
    - i.e. exclude all subfolders called RAW or af from scanning
    - use settings page to specify what folders to exclude
    - Done: indexer.exclude_dir_names config (case-insensitive, any depth); chip editor on /settings with Save & rescan; excluded photos go "missing" (not purged), revive on removal
- [x] Image stacking
    - I typically import all my images and name them 'img copy.jpg' and have the final image named 'img.jpg' I would like to have them stacked into one entry with the latest modified being the image that shows
    - Decided: same parent folder only; user picks mode(s): RAW+JPEG pairs and/or similar-name JPEG+JPEG; newest mtime is the top; ratings/tags are stack-level (shared)
    - Done: read-time grouping (no re-index), config toggle on /settings; grid + search collapse to representative w/ ⧉ badge; photo page shows a stack filmstrip; rating fans out to all members; (N) counts as a copy marker, bare digits never merge
- [x] File path exposure
    - The image page shows a link that will open the file in the native file viewer to be able to edit the image or copy it to a new folder
    - Done: Copy path always; Reveal in file manager + Open in default app on native runs
    - Done (2026-09-10): under Docker, "Reveal / Open" now work via an optional
      host helper agent (`tools/sortandpost_helper.py`, run outside Docker on
      127.0.0.1) — token-gated, CORS-locked to localhost, `/reveal` + `/open`
      confined to registered folders. The photo page shows and copies the
      **host** path (translated from the container path); degrades to
      "copy host path" when the agent isn't running.
- [x] Choose library folders from the Settings UI (per computer)
    - The folders scanned should be pickable in Settings, not hand-edited YAML,
      and must work across Mac/Windows/Linux + Docker where paths differ
    - Done (2026-09-10): library folders moved out of the shared `config.yaml`
      into a **per-device** file, `sync/devices/<hostname>-<id8>/library.json`
      (new `src/sortandpost/library_roots.py`; each root is `{container, host,
      label}`; folder matched by the id suffix so a hostname change still adopts
      it; seeded once from the old `config.yaml` on upgrade). Settings →
      **Library folders**: add / remove folders with a live "scanned / not found"
      badge. Native run → just pick the folder (a real filesystem browser when
      the helper is up). Docker run → the dialog generates the exact
      `docker-compose.yml` volume line + `docker compose up -d` (the app never
      mutates compose or restarts itself). Pasted paths are de-quoted; the
      scanner now skips an unreadable dir (macOS data-vault / locked mount)
      instead of aborting the whole pass. +29 tests.
- [x] Move from paginated version to infinite scroll that loads images as needed
    - Done: 80/batch, appends near bottom, header counter, no more ?page= URLs
- [x] Keyboard navigation & shortcuts
    - Photo page — DONE: ←/`k` prev, →/`j` next (representative order, clamped at
      ends; maps a filmstrip member back to its stack rep; `location.replace` so
      Back returns to the grid), `1`–`5` rate, `0` clear, `f` toggle the edit
      flag. Rating/flag keys drive the same widgets as a click (one write path);
      read-only aware; ignores Ctrl/Alt/Meta chords and key-repeat. Hint line.
    - Grid — DONE: a "target" tile = the keyboard-focus ring (moved by ← → ↑ ↓;
      ↑/↓ step a full row via a live column count) OR, if you haven't used the
      arrows, the tile under the mouse. Amber ring + the hover overlays (stars,
      flag, checkbox) shown on the target via one shared `ui.add_css` block.
      `Space` toggles selection (Shift+Space extends a range; its page-scroll is
      suppressed over the grid), `1`–`5`/`0` rate, `f` flag, `Enter` open, `Esc`
      clears ring + selection. Additive over infinite scroll (moving the ring
      loads more batches); one `ui.keyboard` per page (two don't both fire).
- [x] Add a mode to library to group by with a header for each item dividing the library
    - I would liike to be able to divide by date and folder and if there are any other categories that you think of, add those
    - I'm imagining something similar to how Google Photos website works where each day has its own photos divided
    - Done: new `src/sortandpost/grouping.py` (pure logic, +19 tests) +
      `list_group_rows` in `db/browse.py`; no schema change (read-only
      projection). `/` takes `?group=day|month|folder|camera` (+
      `?folder_sort=newest|oldest|az|za`) via a `Flat/Day/Month/Folder/Camera`
      segmented control by the photo count. Day/Month keep the newest-first
      order with Today/Yesterday labels and a trailing "No date" group;
      Folder = leaf name + full-path sublabel, sorted by newest file mtime or
      A-Z/Z-A; Camera = make+model (de-duped), newest-first, "Unknown camera"
      last. Each section renders as a sticky clickable header + its own grid;
      **collapse/expand** per section (chevron), Collapse-all / Expand-all
      buttons, keyboard ring skips folded sections, collapsed set + scroll pos
      persist in `history.state` for back/forward. URL-param only — the nav
      link and a fresh visit start flat. Infinite scroll / scroll-restore /
      bulk toolbar unchanged.
- [x] Collections
    - I would like a feature separate from tagging where users could create custom collections separate from tagging.
    - Collections should live in json files like the saved searches
    - I think this would be a good base for the social media composer
    - I would want the collection to display the photos in the collection
    - Users should be able to bulk add to the collection from the bulk add screen or add to a collection from a specific photo's page
    - I would also like there to be notes field for each collection that will be used in the future for the social media post content
    - Done: new `src/sortandpost/collections.py` (`sync/collections.json`, synced
      like `tags.yaml`, not conflict-merged; tolerant loader that backs up a
      corrupt file to `collections.json.corrupt-<ts>` instead of losing it).
      `Collection` = id / name / notes / ordered de-duped `photos` (pixel_hash).
      `clean_name()` strips control chars, collapses whitespace, caps 120.
      New nav entry + `/collections` (big square 2×2 preview-mosaic cards, name
      + count underneath, hover-delete) and `/collections/{id}` (photos in
      membership order at the top with per-tile remove / move-earlier /
      move-later; editable notes at the bottom; rename / delete). "Add to
      collection" menu on the bulk toolbar and the photo page (+ member chips on
      the photo page). "Add photos" picker = maximized dialog reusing the
      library's `_GridSelection` checkbox model (hover checkbox, click /
      shift-click ranges) **plus** keyboard nav: arrows move a gold focus ring
      (native `scrollIntoView` inside an `overflow-y-auto` container), Space /
      Shift+Space select, Enter adds, Esc cancels. "Create & add photos" opens
      that picker straight away (`?add=1`). Collections editing works in
      read-only mode (like saved searches). `paths.collections_file`. +24 tests.
## Search features
- [x] Ability to save searches for future review
    - Done: `/search` bookmark button saves the full state (query + every filter)
      under a name; a "Saved:" pill row (click to re-run, × to delete) always
      visible. Stored in `sync/searches.json` — synced like `tags.yaml`, sorted by
      name, re-save overwrites, not conflict-merged. New `src/sortandpost/searches.py`
      + `paths.saved_searches_file`. +12 tests.
## Design
- [x] Flesh out website to move from scaffold to production looks
    - Done (first pass — restyle, layouts unchanged): new `_THEME_CSS` design-token
      layer in `app.py` (`--sp-bg/-surface/-surface-2/-border/-text/-text-dim/
      -text-faint/-shadow`, flipped on Quasar's `body--dark`) — "warm editorial":
      paper `#faf8f4` light / warm near-black `#1b1815` dark. `ui.colors()` sets an
      **indigo** primary (`#4f46e5`) that flows into every `color=primary` button /
      selection ring / active nav link / segmented-control active state; amber stays
      reserved for the keyboard-focus ring + edit flag. `.sp-display` = a system
      old-style serif stack (Iowan/Palatino → Georgia, no webfont fetch) on the
      wordmark, group-by section headers, page titles, collection names. Reworked
      `_header` (warm translucent blurred bar + hairline border, serif wordmark,
      quiet pill nav links w/ indigo active, read-only/writable as an uppercase
      chip) and the status-bar footer (warm dark). `.q-btn-toggle` restyled so the
      dark-mode buttons aren't stark white (warm surface / dim text inactive, solid
      indigo active, clean edges). `.sp-card` (collections), `.sp-section-header`
      (group-by), and a shared `_empty_state(icon, title, hint)` helper replacing
      the bare "No …" one-liners. No layout/structure changes, no schema change,
      no new deps. More detailed polish deferred to a later pass.
## Other pages
- [x] Tinder style swipe through of unrated images with star ratings 
    - I would like to have random images pulled and I can quickly rate them
    - Done: new `/rate` page (nav "Rate"). A random batch of unrated photos (no
      `ratings` row, or 0★), one big card at a time — `1`–`5` rates and
      auto-advances, `→`/`S`/`Space` skip, `←` steps back, `f` toggles the
      "marked for editing" flag beside the stars. Rating/flag reuse the shared
      `_apply_rating` / `_apply_flag` paths (fan to stack members, EXIF mirror,
      `changes` row). Fresh random batch per visit (skipped photos resurface
      later); "N rated this session · ~M unrated left" counter; end state when
      caught up. Read-only aware. New `browse.unrated_count` /
      `unrated_photo_ids`. +2 tests.
- [ ] Social media post composer
    - I would like to be able to group images into a social media post with some text
    - I want to be able to use the robopost.app api to upload these images and create future postings
    - I want this to be easy to help me be able to post more images