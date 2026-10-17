
# sortandpost — V3 plan

_Drafted 2026-10-05. The feature list is in [`V3.md`](V3.md); this file gives the build order and the model for each phase.
Target: ship V3 in about two weeks (by ~2026-10-19). Anything below the cut line moves to V4._

## Context
V2 shipped (v2.0.0: macOS + Windows apps, onboarding, seasonal themes, robopost composer).
V3.md collects the next batch: an Instagram sizing pipeline, a CLIP off-switch,
achievements with an Angel/Devil persona, more posting back ends, a copy rewrite, Linux, and
a real Docker/NAS server mode. Each phase ends with the CLAUDE.md workflow: quality gate,
then boot on :8080 (or :8081 if prod is running), user click-through, sign-off, PROGRESS.md/V3.md update, then commit/PR.

How the model was chosen (same as V1/V2): **Opus** for work that is cross-cutting, risky or design-heavy
(sync and merge, the no-double-post send state machine, security, synced state).
**Sonnet** for everything else (bounded UI, one-module features, docs, packaging that
reuses a proven recipe). **No Haiku.**

## Decisions (agreed 2026-10-05)
- **CLIP off** is a **library-wide** switch (`clip.enabled` in shared `config.yaml`). When it's off:
  no embedding pass and no zero-shot predictions; `/review` and "more like this" are hidden;
  text search falls back to non-CLIP matching (tags, folders, camera, filename). The wizard (new library only) and
  Settings both offer it. Turning it off **deletes this machine's CLIP weights**
  (`<default_local_root>/models`). Each other machine deletes its own when it next sees the flag.
  The shared `sync/embeddings/` data is **kept**, so turning CLIP back on only needs the
  re-download, not a re-embed.
- **Achievements** are **synced, one profile**: the profile name, mode and unlocks travel in
  `sync/`. Thresholds count **existing totals**, so existing libraries unlock several right away.
  "Not on the first day" means the library's earliest `photos.first_indexed_at` is before today.
  **Mode Off** hides everything (nav link, header trophy, toasts), but progress keeps
  counting. The wizard defaults to **Angel**.
- **Instagram sizing**: Instagram channels get a **separate send** with their own sized
  uploads; other channels get the normal export. If every photo in the set already has an
  Instagram ratio (4:5, 1:1, 1.91:1, or 9:16 for a Story), the photos are used as they are. Otherwise the default is
  **Portrait 1080×1350, 0 px border**. In the composer the user can change the format, border and pad color.
  Every photo in one Instagram post gets the same size.
- **Posting back ends**: **one active back end** per library. robopost goes behind a provider
  interface, **Postiz** ships first, and **Buffer** comes only after its API access is confirmed (its public
  API has been closed to new apps).
- **Copy rewrite**: Claude exports a CSV (`id, page, location, current, correction`). The user fills in
  `correction`, then Claude applies each correction in the source and checks it in context. No i18n layer.
- **Docker login**: **one shared password** (env var / secret file, hashed), a signed session
  cookie with a random secret in `local/`, rate limiting, and binding to localhost by default.
- **Docker + desktop editing** (2026-10-14): request-based **checkout** of the write lock, through `sync/` only (no network link between instances), either direction (Docker can reclaim). Works for Docker on a Synology and for Docker Desktop on a Mac/PC with `sync/` in Google Drive / OneDrive. P6d.
- **pywebview native window**: last V3 priority (stretch). If it isn't reached, it goes to V4.

## Prioritized phases

| # | Phase | Model | Why this model |
|---|-------|-------|----------------|
| P0 ✅ | **Docs housekeeping**: number V3.md items, link this plan, fix `Achievements/Achievements.md` (typo, empty row) | **Sonnet** | Docs only. |
| P1a ✅ | **Instagram resize + preview**: port `tools/reference/ResizePhoto.py` into `post_export.py` as a pure `fit_and_pad(img, w, h, border, rgb)` that runs after the existing decode → upright → sRGB steps, in the process pool. Add an `instagram_format` (portrait/square/landscape/story) + border + pad color to the post's `network_settings["instagram"]`; Story is only offered when postType=STORIES. Auto-detect "already IG ratio" (with a small tolerance). The composer previews the sized images with the real canvas and padding. The export cache key includes the format/border/color | **Sonnet** | Self-contained: pure image function + composer UI. Reuses `export_jpeg` / `_to_srgb` / `export_key`. |
| P1b ✅ | **Split Instagram send**: `post_send` makes two `scheduled_posts` calls (Instagram channels with IG uploads; other channels with normal uploads). Each sub-send gets its own `sending → sent/failed` record, so a retry or "It was sent / It wasn't sent" only settles the half that is in doubt. Confirm dialog shows both halves | **Opus** | Touches the no-double-post state machine (create is never retried, a 5xx leaves the post `sending`). A partial failure across two creates is easy to get wrong, and getting it wrong means double posts. |
| P2 ✅ | **Turn off CLIP**: `clip.enabled` in `ClipConfig`. Gate the deferred CLIP queue (`indexer/service.py`, `pipeline.py`, `scan_clip_once`), search (`library_view` text query → non-CLIP match), `/review` nav, "more like this" and the status-bar CLIP queue. Add a wizard step (new library) + Settings toggle with a confirm, and `clip.model.delete_cached()`. Machines that join a library also delete their weights on startup when the flag is off. Turning CLIP on again goes through the existing download/verify path | **Sonnet** | Many touchpoints, but each one is a simple gate. Sync is untouched (embeddings are kept). Escalate to Opus only if search fallback needs a new query path. |
| P3a ✅ | **Achievements engine + profile**: `achievements.py` holds the definition table, loaded from a bundled copy of `Achievements/Achievements.md` (name, trigger, points, shown, angel/devil text). Count-based triggers are **computed** from existing state: indexed photos, rated photos (`ratings.stars>0`, one per stack), collections, sent posts. Event triggers (Devil Mode, Konami, "github" search) and every unlock timestamp go in **per-device** `sync/devices/<slug>-<id8>/achievements.json`, and the union across devices is the truth, so there's no conflict merge (same idea as per-device `library.json`). Profile name + mode go in `sync/profile.json` (last-writer-wins, like collections). Wizard step (name, mode = Angel) and a Settings panel | **Opus** | New synced user state has to survive cross-machine edits without silently dropping unlocks (constraint #4). It also needs the "first day" rule, the retroactive computation, and the stack-aware counting. |
| P3b ✅ | **Achievements UI**: `/achievements` page of cards. Shown + locked: icon, name, `???` description. Shown + unlocked: description in the current mode's voice. Hidden + locked: a gray `?` card. Score = sum of unlocked points. Header top-right shows the profile name + 🏆 score. An unlock toast uses the Angel/Devil placeholder art (`Achievements/Angel.png` / `Devil.png`, served from resources). Konami listener lives on the achievements page and the "github" hook in the `/` search. Uses theme tokens | **Sonnet** | Pure UI on top of P3a's API. |
| P4a ✅ | **Posting provider interface + Postiz**: pull a `PostingProvider` protocol out of `robopost.py` / `posts.payload` / `post_send` (auth check, list channels, upload media, create post, network limits). robopost becomes the first implementation; Postiz is the second (base URL for self-hosted, key in the keychain via `secrets_store`, key redaction). Settings gets a back-end picker | **Opus** | Refactors the send path and secrets handling across 5 modules while keeping the robopost behavior verified live. Postiz API facts need checking live, the way robopost's were. |
| P4b ✅ | ~~Buffer~~ → **Zernio** (Buffer has no upload API, so it was skipped): a third provider; **P4c ✅ Upload-Post** a fourth | **Sonnet** | Fits behind P4a's interface. |
| P5 ✅ | **Copy rewrite**: (a) a script dumps every user-visible string from `/welcome` + `/settings` (`onboarding.py`, the settings code in `app.py`) to `copy/onboarding_settings.csv`. Run it **after P3** so the new wizard and settings text is included, and the user can edit while P4 is built. (b) Apply the corrections, then visually check both pages. (b ✅ 2026-10-14, `f741b0f`; with it, 2026-10-16: display name → **sortandpost.photos**, see architecture.md "Naming") | **Sonnet** | Mechanical extract + edit. |
| P9 ✅ | **Missing photos** (V3.md #10, added 2026-10-14): drop the Overview "missing" tile; status bar shows missing only while a library folder is offline. ⋯ filter becomes missing-**only**, with a per-photo reason (folder offline / file gone) on the tile and photo page. Missing counted **per computer**: first confirm with a two-machine test that a full scan's `sweep_presence` marks the other device's paths absent and sets the shared `missing_since`; if so, limit the sweep to paths under this machine's roots | **Opus** | The UI half is Sonnet-sized, but the sweep fix changes how presence is judged in the shared DB (constraint #8 + cross-machine state) and needs the two-machine check. |
| P10 ✅ | **Rebuild AI index button** (V3.md #11, added 2026-10-14): a lock-gated **Rebuild AI index** button with a confirm in Settings → AI search & tagging, run as a request the indexer service thread picks up (never a parallel thread: `--reembed` deletes the matrix). Status-bar progress; Pause / Quit stop it between batches and it resumes. A model-mismatch banner on `/` + Settings with the same button. Remove the CLI hint label (copy CSV row 103) and point the `--reembed` error texts at the button | **Sonnet** | Reuses `scan_clip_once(reembed=True)`, which already keeps approved / rejected suggestions. The only care point is routing it through the indexer thread. |
| #9 ✅ | **Start setup over** (V3.md #9, 2026-10-16): Settings button + confirm forgets this machine's pointer + onboarding marker and restarts into `/welcome`; shared folder untouched. Shared-folder card moved to the top of Library, linked from the Overview | **Opus** | Small, but it touches the startup / setup-mode path. |
| #12 ✅ | **Onboarding folder removal** (V3.md #12, 2026-10-16): a labelled Remove button + an inline "not found" note on each library-folder row (wizard + Settings); wizard Next names unreachable folders in a Go back / Continue anyway dialog | **Opus** | Small UI change on the shared folder editor. |
| ─ | **Two-week cut line**: everything above is the V3 must-ship. Phases below ship if time allows, otherwise they move to V4. | | |
| P6a ✅ | **Docker validate + harden**: first real `docker compose up --build` end to end (CLIP loads, mounts, EXIF writeback through a mount, managed trash in a Linux container). Add a compose `healthcheck`, a deps-only pre-layer, and a build from `uv.lock` with a CPU torch source. Run as non-root. Pin base images by digest (L2). Under Docker, "Download & index" can't hydrate online-only photos (Docker.md → Cloud folders): point it at "Always keep on this device" instead. Mount `local/` (named volume) and `sync/` (`SYNC_DIR`) **separately** so the working DB never sits in a sync folder; a stable `hostname:` (`DEVICE_NAME`). Test both shapes: Synology (native share) and Docker Desktop on a Mac/PC with `sync/` in Google Drive / OneDrive (File Provider / Files On-Demand bind mounts, placeholder detection through the mount) | **Sonnet** | Build plumbing. Mostly a matter of running it and fixing what breaks. |
| P6b ✅ | **Server-mode security** (Docker.md must-haves): shared-password login gating the pages, `/thumbs`, the trash-thumb route **and the socket.io connection**. Session secret in `local/` (L1), rate limiting (L3), bind to `127.0.0.1` by default, Host allowlist + websocket Origin check (M1), HTTPS reverse-proxy example | **Opus** | Security-critical. It's easy to leave a route or the websocket ungated. |
| P6d ✅ | **Editing checkout** (added 2026-10-14; design in [Docker.md](../docs/docker.md#editing-checkout)): an always-on Docker writer would keep every desktop read-only forever. Request → grant → take through `sync/` files only: the borrower writes `sync/locks/request-<id8>.json`; the holder shows a 30 s "handing over" toast with **Keep editing**, then stops the indexer, publishes, and rewrites its lock as a grant (`handoff_to`, `handoff_generation`); the borrower takes it only once its synced DB reaches that generation. Quit returns the lock (Docker reclaims a free lock unless another request is pending); Docker reclaims early with the same **Take over editing** button. Needs a writable → read-only **demotion** path in `SyncManager` + `_start_lock_watch`, and write handlers re-checking the mode at action time. Two-manager tests with a delayed, reordering `sync/` bridge | **Opus** | Changes the single-writer lock (constraint #9) across the sync layer, indexer lifecycle and UI. A wrong order hands over a stale DB. |
| P6c ✅ | **Helper + server docs**: launchd plist, Windows `.bat` + Task Scheduler note, a `/settings` helper-status panel with an install command, M2 helper fixes. Document the always-on NAS shape (read-only second machine) and bind-mount performance | **Sonnet** | Bounded tooling + docs. |
| P7 ✅ | **Linux AppImage + test on Linux**: reuse the P5 PyInstaller recipe. Linux VM ready 2026-10-16 (see **P7 prep** below). Done 2026-10-16: x86_64 + arm64 AppImages, tested headless in Lima + the user's click-through; real-desktop checks deferred (list below) | **Sonnet** (Opus if native-lib packaging fights back) | Recipe already proven on macOS/Windows. |
| P8 | **Stretch: pywebview native window** (`native=True`) as an option next to the tray + `*.localhost` browser | **Sonnet** | Small and isolated. Last priority; if not reached, it moves to V4. |

**P7 prep (2026-10-16): the Linux VM.** A Lima VM named `linux` on the Mac: Ubuntu 24.04.5,
arm64, `--vm-type=vz --rosetta`, 4 CPU / 6 GiB / 60 GiB, headless. Run commands from the Mac with
`limactl shell linux -- bash -lc '…'` (`-l` loads `~/.profile`); `limactl stop|start linux`.
- **Mounts** keep host paths: the repo `/Users/matt1125/Documents/GitHub/sortandpost` is writable,
  the rest of `~` read-only. The guest home is `/home/matt1125.guest`.
- **Venv outside the repo**: `~/.profile` sets `UV_PROJECT_ENVIRONMENT=~/venvs/sortandpost` and
  `UV_LINK_MODE=copy`, so the Mac `.venv` in the shared repo is never touched. Synced with
  `uv sync --locked --extra dev --extra desktop` (torch 2.14.1+cpu).
- **Installed**: Docker 29.9 + buildx/compose; uv + Python 3.13; exiftool 12.76; FUSE
  (`libfuse2t64`, `fuse3`), squashfs-tools, patchelf, desktop-file-utils (AppImage); Xvfb, xdotool,
  ImageMagick; GTK3 + AyatanaAppIndicator typelibs, `python3-gi`; Playwright Chromium.
- **Added 2026-10-16 (P7 step 1)**: `build-essential pkg-config libgirepository1.0-dev
  libcairo2-dev`, to build PyGObject 3.50.x + pycairo from source (the `desktop` extra on Linux).
  3.52+ needs GLib >= 2.80, and the AppImage builds on Ubuntu 22.04 (GLib 2.72), hence the `<3.52` pin.
- **x86_64**: Rosetta binfmt (flags `OCF`) runs x86_64 binaries in the VM and in
  `docker run --platform linux/amd64` containers (verified). Plan: arm64 AppImage natively,
  x86_64 AppImage in an amd64 container. A final x86_64 build can also run on GitHub Actions.
- **Status**: the full pytest suite passes on Linux arm64
  (`xvfb-run -a uv run --locked pytest -q -p no:cacheprovider`).
- **Open for P7:**
  - **Tray backend**: pystray needs a display (`xvfb-run -a`) and falls back to its `_xorg`
    backend. Ubuntu's GNOME hides X11 tray icons, so the app needs PyGObject in the venv / bundle
    to get the AppIndicator backend.
  - **Tray testing**: the VM has no desktop, so the tray can only be smoke-tested under Xvfb. A
    hands-on check needs a GUI VM (UTM) or a real Linux desktop.

**P7 plan (2026-10-16).** Decisions: ship **x86_64 + arm64** AppImages, bundle the
**AppIndicator** tray backend, and do all P7 testing in the headless Lima VM. The user clicks
through from the Mac browser over Lima's port forward. A GUI VM or real hardware comes later
(see the deferred list below). One PR, `feat/linux-appimage`:

1. **Deps** ✅: `pygobject>=3.50,<3.52; sys_platform == 'linux'` in the `desktop` extra (locked
   3.50.2 + pycairo 1.29.2). It's sdist-only, so the VM / build container needs
   `libgirepository1.0-dev libcairo2-dev`. `gi.*` is in mypy's ignore list.
2. **Desktop layer** ✅ (deviations from the plan below: pystray's GTK backends already queue
   `update_menu` onto their own loop, so there's no `GLib.idle_add`; `xorg` also needs an XEmbed
   host (`_NET_SYSTEM_TRAY_S<n>`), else headless; the no-keychain hint on a Linux desktop names
   GNOME Keyring / KWallet. Verified in Lima from source: headless + Settings Quit; `xorg`
   docked in stalonetray + SIGTERM; `appindicator` against a stand-in StatusNotifierWatcher
   (menu exported, Quit clicked over dbusmenu) -- all clean stops):
   - New `desktop/_linux.py`. `pick_backend()` sets `PYSTRAY_BACKEND` before pystray is
     imported: `appindicator` if gi + AyatanaAppIndicator3 import and a StatusNotifierWatcher
     is on the session bus, else `xorg` with a `DISPLAY`, else no tray. It also has a
     best-effort `notify()`.
   - Tray: `update_menu` goes through `GLib.idle_add` under GTK backends; SIGTERM/SIGHUP →
     `shutdown`.
   - **Settings → Quit sortandpost** (only when `desktop.control` is mounted) for a machine
     with no tray.
   - Start at login = an XDG autostart file
     `~/.config/autostart/app.sortandpost.desktop` → `$APPIMAGE`.
   - `binaries.host_env()` restores `LD_LIBRARY_PATH_ORIG` & co. for host tools (`xdg-open`,
     the browser, "Open with" apps, perl/exiftool), but not for the `--serve` child.
   - Check that a missing Secret Service shows the "use the env var" message, not an error.
3. **`packaging/linux/`** ✅ (build.sh + spec + make_icon.py + AppRun / .desktop / appdata + the
   builder `Dockerfile`). Pins: appimagetool 1.9.1, type2-runtime 20251108 (sha256 per arch,
   matching GitHub's asset digests). Output 2.0.0: aarch64 273 MB (875 MB bundle), x86_64
   305 MB (1017 MB). A cached rebuild takes ~1.5 min (arm64) / ~3 min (x86_64 under Rosetta).
   Gotchas met:
   - Importing pystray needs a display, so the build sets `PYSTRAY_BACKEND=dummy`.
   - Rosetta's binfmt refuses an AppImage (its magic bytes sit in the ELF header padding),
     so appimagetool runs from a copy with bytes 8-10 zeroed.
   - The x86_64 *app* can't run in the arm64 VM itself (no x86_64 glibc): test it in an amd64
     container (`ubuntu:24.04` + perl, `--network host`, `APPIMAGE_EXTRACT_AND_RUN=1`).
   - `--version` was added for the build's sanity check.
   - Build inside a digest-pinned `ubuntu:22.04` container (`--platform linux/arm64|amd64`;
     glibc 2.35 baseline), or `--no-container` for CI.
   - The same pinned exiftool Perl tarball as macOS, run by the system perl.
   - PyInstaller onedir (macOS spec's `collect_all` list + torchvision `*.so` glob +
     gi/pystray backends + secretstorage/jeepney hidden imports; lean `gi` hooksconfig).
   - Pinned `appimagetool` (type2 static runtime, zstd) →
     `dist/sortandpost-<ver>-{x86_64,aarch64}.AppImage`.
   - arm64 natively first, then x86_64 in an amd64 container (Rosetta).
4. **Test in Lima** ✅ (2026-10-16: all automated items, both arches as planned, plus the user's
   click-through). Extra fix found: `host_env` also drops `GDK_PIXBUF_MODULE_FILE` (PyInstaller's
   loader cache in /tmp, outside the bundle):
   - The gate (in the VM and on the Mac).
   - AppImage smoke under `xvfb-run`:
     - Launch, `app.lock` / `runtime.json` / `/healthz`, backend logged.
     - Second launch reuses the running instance.
     - Wizard on scratch pref/local dirs, the CLIP download, and an index of ~300 photos copied
       into the guest home (`~/Downloads` is a read-only mount). JPEG / HEIC / RAW, rating →
       `XMP:Rating` round trip, search, frozen pool workers.
     - Delete → put back, with no system-Trash offer and no `.Trash-*`.
     - Quit (tray / Settings / SIGTERM) → final publish + lock release.
     - Autostart file on/off; second-"machine" read-only → Take over editing.
     - Host tools get the restored env.
     - **Wayland-only** (no X server, `DISPLAY` unset): the AppImage under `weston
       --backend=headless` + a stand-in StatusNotifierWatcher shows the AppIndicator icon and
       quits cleanly, and with no host runs headless. This proves the bundled GTK carries its
       Wayland backend (verified from source 2026-10-16).
   - The user clicked through the arm64 AppImage (fresh first run on scratch pref/local dirs)
     at `http://localhost:8080` over Lima's forward (nothing on the Mac held 8080 then). Two
     bugs found and fixed:
     - **No xdg-utils** (the VM has none): the browser launch fell back to Python's
       `webbrowser`, which ran `gio open` with the bundle's `LD_LIBRARY_PATH` → `undefined
       symbol: g_string_free_and_steal`. `_linux.open_path` now tries xdg-open, `gio open`,
       `$BROWSER`, then a browser by name, always with `host_env()`.
     - **Restart moved ports** (all POSIX, macOS too): after the wizard's folder pick the old
       server's closed connections sat in TIME_WAIT, `runtime.port_free` (a plain `bind`)
       called 8080 busy and the new server came up on 8081, stranding the tab. `port_free`
       now binds with `SO_REUSEADDR` like uvicorn (not on Windows).
5. **Docs** ✅: an architecture.md "Desktop app → Linux" subsection (+ Stack table, Restart
   note), README "Linux app (AppImage)", CLAUDE.md Commands, CONTRIBUTING (Linux apt deps).

**Deferred: real Linux desktop checklist** (GUI VM or hardware, after P7):
- Tray icon + menu on Ubuntu GNOME (AppIndicator), KDE Plasma, and a Wayland session.
- No-tray fallback on stock Fedora GNOME.
- The Secret Service keychain (gnome-keyring / KWallet) prompt, plus the key surviving restarts.
- Start at login across a real logout / login; a clean stop on logout.
- Reveal / Open in Files / Dolphin.
- Double-click launch; AppImageLauncher / Gear Lever.
- The x86_64 AppImage on real Intel/AMD hardware (CLIP speed, `slow` / `fast`).

**Manual check (any time, on the Windows machine):** Ctrl-click grid selection (V2 #24),
which was never tried on Windows.

### Ordering rationale
- Instagram first (the user's pick, and the most day-to-day value). It comes before the provider refactor,
  so P4a abstracts a send path that already handles the split Instagram send rather than being built twice.
- CLIP-off before achievements: both add wizard steps, so the wizard gets reworked once in a stable order.
- Achievements engine (Opus) is separate from its UI (Sonnet), so the synced-state design is settled before
  any cards are built.
- Copy export runs after the onboarding/settings text stops changing (after P3), so the user can
  edit it while P4 is built.
- Docker/server mode, Linux and pywebview sit below the cut line. They're the biggest and least
  urgent for a two-week ship, and P6b (security) shouldn't be rushed.

## Per-phase workflow (unchanged)
1. Plan in Opus (plan mode), then `/model <assigned>` to implement.
2. Quality gate: `ruff check .`, `ruff format .`, `mypy src`, `pytest`, alembic autogenerate-empty, `uv audit`.
3. Boot for a hands-on test (`APP_DATA=<repo>/browser_test_data PORT=8080 HF_HUB_OFFLINE=1 .venv/bin/python -m sortandpost`;
   use :8081 if prod is on :8080) and tell the user what to click.
4. After sign-off: update PROGRESS.md + V3.md + this file, commit, PR.

