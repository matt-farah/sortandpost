
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
| P4a | **Posting provider interface + Postiz**: pull a `PostingProvider` protocol out of `robopost.py` / `posts.payload` / `post_send` (auth check, list channels, upload media, create post, network limits). robopost becomes the first implementation; Postiz is the second (base URL for self-hosted, key in the keychain via `secrets_store`, key redaction). Settings gets a back-end picker | **Opus** | Refactors the send path and secrets handling across 5 modules while keeping the robopost behavior verified live. Postiz API facts need checking live, the way robopost's were. |
| P4b | **Buffer** (only if API access is available): a third provider | **Sonnet** | Fits behind P4a's interface. |
| P5 | **Copy rewrite**: (a) a script dumps every user-visible string from `/welcome` + `/settings` (`onboarding.py`, the settings code in `app.py`) to `copy/onboarding_settings.csv`. Run it **after P3** so the new wizard and settings text is included, and the user can edit while P4 is built. (b) Apply the corrections, then visually check both pages | **Sonnet** | Mechanical extract + edit. |
| ─ | **Two-week cut line**: everything above is the V3 must-ship. Phases below ship if time allows, otherwise they move to V4. | | |
| P6a | **Docker validate + harden**: first real `docker compose up --build` end to end (CLIP loads, mounts, EXIF writeback through a mount, managed trash in a Linux container). Add a compose `healthcheck`, a deps-only pre-layer, and a build from `uv.lock` with a CPU torch source. Run as non-root. Pin base images by digest (L2) | **Sonnet** | Build plumbing. Mostly a matter of running it and fixing what breaks. |
| P6b | **Server-mode security** (Docker.md must-haves): shared-password login gating the pages, `/thumbs`, the trash-thumb route **and the socket.io connection**. Session secret in `local/` (L1), rate limiting (L3), bind to `127.0.0.1` by default, Host allowlist + websocket Origin check (M1), HTTPS reverse-proxy example | **Opus** | Security-critical. It's easy to leave a route or the websocket ungated. |
| P6c | **Helper + server docs**: launchd plist, Windows `.bat` + Task Scheduler note, a `/settings` helper-status panel with an install command, M2 helper fixes. Document the always-on NAS shape (read-only second machine) and bind-mount performance | **Sonnet** | Bounded tooling + docs. |
| P7 | **Linux AppImage + test on Linux**: reuse the P5 PyInstaller recipe. Needs a Linux VM first | **Sonnet** (Opus if native-lib packaging fights back) | Recipe already proven on macOS/Windows. |
| P8 | **Stretch: pywebview native window** (`native=True`) as an option next to the tray + `*.localhost` browser | **Sonnet** | Small and isolated. Last priority; if not reached, it moves to V4. |

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
2. Quality gate: `ruff check .`, `ruff format .`, `mypy src`, `pytest`, alembic autogenerate-empty.
3. Boot for a hands-on test (`APP_DATA=<repo>/browser_test_data PORT=8080 HF_HUB_OFFLINE=1 .venv/bin/python -m sortandpost`;
   use :8081 if prod is on :8080) and tell the user what to click.
4. After sign-off: update PROGRESS.md + V3.md + this file, commit, PR.

