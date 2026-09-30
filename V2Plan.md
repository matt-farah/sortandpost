# sortandpost — V2 plan

_Agreed 2026-09-28. Feature list lives in `V2.md`; this is the build order + model per phase._

## Context
V1 shipped (v1.1.0). V2.md was just reworked by the user: Docker/NAS items (#6–9) and
"desktop app feel" (#4) moved to V3.md; new V2 items added (multi-open / reveal, choose
the "open with" app, user onboarding, tray + startup/shutdown under packaging). Goal: an
ordered list of phases, each tagged with the model to run it on, so the user can
`/model <x>`, `/clear`, and do one phase per session. Each phase still ends with the
CLAUDE.md workflow: quality gate → boot app on :8080 (or :8081 if prod is running) →
user click-through → sign-off → PROGRESS.md/V2.md update → commit/PR.

Model rule of thumb (same as V1): **Opus** for cross-cutting / risky / design-heavy work
(sync, app-data resolution, packaging with native libs); **Sonnet** for everything else
(well-bounded UI, one-module features, docs). **No Haiku** (user preference).

## Finding from the code survey
- **#14 In-app indexer lock re-check is already done** — `_start_lock_watch()`
  (`src/sortandpost/app.py:2511`, started at `:661`) calls `manager.refresh()` on a timer
  and starts the indexer on promotion; logged in PROGRESS.md ~line 1064. Remove it from V2.md.
- Onboarding choices mostly map to things that already exist: theme
  (`prefs.get_theme/set_theme`, per-machine `local/ui_prefs.json`), excluded folders
  (`config.indexer.exclude_dir_names`, `config.py:143`), stacking (`StackingConfig`,
  `stacks.py`), scan speed (`clip.speed`), delete behaviour (`deletion.stack_scope`),
  library roots (`library_roots.py` + Settings UI), app-data folder
  (`launcher_pref.py` + Settings picker, restart-to-apply). Onboarding is mainly a
  first-run wizard over existing settings plus "don't index until finished".
- Reveal/open today: `reveal.py` — `reveal_in_file_manager(path)` / `open_in_default_app(path)`,
  single file only; wired on the photo page (`app.py:~3899`). Docker path goes through
  the host helper (`tools/sortandpost_helper.py` `/reveal`, `/open`).

## Prioritized phases

| # | Phase | Model | Why this model |
|---|-------|-------|----------------|
| P0 | ✅ Docs housekeeping: drop #14 from V2.md (done), renumber the new items (#21 open/reveal multiple, #22 open-with app, #23 onboarding), move #12/#17/#18 to a "fix if it bites" backlog section | **Sonnet** | Docs only (user: no Haiku — Sonnet or Opus only) |
| P1 | ✅ **Open / reveal multiple photos** — bulk-toolbar + "flagged for editing" actions: reveal all selected in Finder / Explorer / Linux file manager (with them selected), and open all in an app | **Sonnet** | Contained to `reveal.py` + toolbar. Platform details are known: macOS `open -R` takes one file → use AppleScript `tell Finder to reveal {…}` / `open -a`; Windows `SHOpenFolderAndSelectItems` via ctypes (grouped by folder); Linux `org.freedesktop.FileManager1.ShowItems` over dbus, fallback to opening the folder. Resolve duplicates to present paths; cap count with a confirm. Helper gets a multi-path `/reveal` for Docker. |
| P2 | ✅ **Choose the "open with" app** (per-device, doesn't change the system default) — Settings picker, one-click Open (no menu) | **Sonnet** | Per-machine pref in `local/ui_prefs.json` via `prefs.py` (not `sync/` — app paths differ per machine). macOS `open -a <App.app> files…`; Windows run the chosen `.exe` with the files; Linux runs the chosen executable/`.desktop` directly. Builds on P1's multi-open. A native OS "choose an application" dialog was tried and reverted — user wants the in-app web-based browser for consistency across native/Docker and simpler docs; don't re-propose it. |
| P3 | ✅ **Design pass — round 2 (#10)** — three stacked PRs: **A** seasonal themes (Spring/Summer/Fall/Winter/Auto × Light/Dark, `themes.py`, #28), **B** compact icon filter bar + group-by in the toolbar (default Month, `group=flat`, #29), **C** `/settings` in categories (side rail + 2-col cards) | **Sonnet** | User's feedback arrived 2026-09-28 (plan: seasons, compact icon filters, integrated group-by, organized settings); pure UI. Best done **before** onboarding so the wizard is built in the final style. If feedback isn't ready, skip and come back — nothing else depends on it hard. |
| P4 | ✅ **User onboarding** — first-run wizard: library folder(s), app-data location, slow/fast initial scan, stacking on/off, excluded folder names, theme, delete behaviour; indexer doesn't start until finished | **Opus** | Cross-cuts app-data resolution (`config.Env.resolved_app_data`, `launcher_pref`, restart-to-apply — the wizard runs *before* an app-data folder is final, so it must choose the folder first then bootstrap / adopt an existing synced `sync/` tree and skip steps whose answers already exist there), the per-device `library.json`, synced `config.yaml` vs per-machine prefs, lock/read-only mode, and indexer gating. Easy to get subtly wrong for the "second machine opens an existing sync folder" case. Sonnet can build the wizard screens once Opus has settled the design and state machine. _As built (#31): also split machine-local data out of the shared folder — see PROGRESS.md._ |
| P5 | ✅ **Packaging + tray + lifecycle (#5)** — macOS `.app` first: PyInstaller bundle, vendored `exiftool`, rawpy/pillow-heif/torch native libs, CLIP weights on first run; tray/menu-bar item (open UI, pause/resume indexer, quit); clean startup (single instance, open browser) and shutdown (stop indexer, final publish, release lock) | **Opus** | Highest-risk item: torch + native libs in a frozen bundle, codesign/notarization, a background process without a terminal, and a shutdown that must never skip the final publish / lock release (constraint #4, #9). Also where "move managed trash to system Trash on quit" lands (already designed as on-quit cleanup). |
| P6 | ✅ **Windows pass (#19) + Windows `.exe`** — Inno Setup installer + portable zip; tray logoff/installer close, parent watch, Run-key start at login; Windows fixes to sync publish/pull, hydrate, scanner junctions | **Sonnet** (Opus if packaging problems appear) | Needs a real Windows machine (ToDo.md "Test on Windows"). Exercise rawpy / pillow-heif / send2trash / Explorer reveal / Windows rating tags, P1–P2 Windows paths, then reuse the P5 packaging recipe. |
| P7 | **Linux AppImage** | **Sonnet** | Reuse P5 recipe; pairs with ToDo "Test on Linux". |
| P8 | **#15 Targeted subtree rescan on FS events** | **Sonnet** | Contained to `indexer/service.py` + `scan.py`: pass the debounced set of changed dirs to a scoped `scan_once`; the periodic full scan stays as a safety net (constraint #6). |
| Backlog | #12 group-by scroll race, #17 ANN index > 25k, #18 dHash on flat images | #12/#18 Sonnet, #17 Opus | All "fix if it bites" — leave until a real library hits them. #17 must keep the search interface (CLAUDE.md). |

### Ordering rationale
- P1/P2 first: small, high day-to-day value, low risk, and they exercise the
  per-platform code paths that the Windows/Linux passes later re-verify.
- Design pass before onboarding so the wizard isn't restyled twice.
- Onboarding before packaging: a double-clickable app with no terminal *needs* a
  first-run flow (no `APP_DATA` env var, no config editing), so P4 is a prerequisite for a
  usable P5.
- Packaging macOS first (the user's platform), then Windows/Linux once the recipe is proven.
- ToDo.md items (website, brew, winget, NAS) stay outside the app plan; brew/winget follow P5/P6.

## Per-phase workflow (unchanged from V1)
1. Plan in Opus (plan mode) → `/model <assigned>` to implement.
2. Quality gate: `ruff check .`, `ruff format .`, `mypy src`, `pytest`, alembic autogenerate-empty.
3. Boot for hands-on test (`APP_DATA=<repo>/browser_test_data PORT=8080 HF_HUB_OFFLINE=1 .venv/bin/python -m sortandpost`; use :8081 if prod is on :8080), tell the user what to click.
4. After sign-off: update PROGRESS.md + V2.md, commit, PR.

