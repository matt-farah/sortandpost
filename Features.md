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
- [ ] Dark/light mode with an option in the settings to choose manually or go based off system settings
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
- [x] Move from paginated version to infinite scroll that loads images as needed
    - Done: 80/batch, appends near bottom, header counter, no more ?page= URLs
- [~] Keyboard navigation & shortcuts
    - Photo page — DONE: ←/`k` prev, →/`j` next (representative order, clamped at
      ends; maps a filmstrip member back to its stack rep), `1`–`5` rate, `0`
      clear, `f` toggle the edit flag. Rating/flag keys drive the same widgets as
      a click (one write path); read-only aware; ignores Ctrl/Alt/Meta chords and
      key-repeat. Hint line on the page.
    - Grid — TODO: arrows move a focused tile, number keys rate it, `f` flags it,
      Enter opens it. Needs a focus system that touches the infinite-scroll /
      scroll-restore code — deferred to its own change.
## Search features
- [ ] Ability to save searches for future review
## Design
- [ ] Flesh out website to move from scaffold to production looks
## Other pages
- [ ] Tinder style swipe through of unrated images with star ratings 
    - I would like to have random images pulled and I can quickly rate them
- [ ] Social media post composer
    - I would like to be able to group images into a social media post with some text
    - I want to be able to use the robopost.app api to upload these images and create future postings
    - I want this to be easy to help me be able to post more images