# Docker deep test — findings and things to fix (V3.1, 2026-11-21)

From building and running the automated Docker suite (`tests/docker/`, see
`docs/docker.md#automated-tests-v31`) plus the full native gate. The manual,
real-hardware half is `dev-notes/DockerTestChecklist.md`.

## Test runs

| Run | Where | Result |
|---|---|---|
| Native gate | macOS arm64: ruff, ruff format, mypy, `uv audit`, `uv run pytest` | all clean; **1149 passed, 13 skipped** (74 s — was 286 s before F3) |
| Docker suite | Docker Desktop 29.9, macOS arm64 (virtiofs bind mounts) | **25 passed, 1 skipped** (the Linux-only custom-PUID test), 6.5 min |
| Docker suite | Lima VM, Ubuntu 24.04 arm64, Docker 29.9 (native bind mounts) | **25 passed, 1 skipped** (the RAW test: no sample RAW in the VM, see F9), 6.9 min |
| Docker suite | GitHub Actions amd64 + arm64 | not run yet (needs a push: run `docker.yml` from the Actions tab) |

The 26 Docker tests: image smoke (4), the normal suite in the container on its disk and on a
bind mount (2), guard (6), lifecycle (5), browser E2E (6), parity (1), handoff (2).

**Good news:**
- **Parity.** A native index and a Docker index of the same library agree byte for byte:
  pixel hashes (JPEG, PNG, HEIC, the CR2 sample), dates, cameras, dimensions, phash,
  ratings read from XMP, and every thumbnail. Constraint #3 and "thumbnails are
  byte-identical across machines" hold between macOS and Linux.
- **Everything else matches native too.** The normal suite passes inside the container
  (Debian exiftool, Linux wheels, uid 1000). Ratings and tags land in host files through
  the bind mount, a RAW is never touched, delete / Put back work, the watcher sees new files
  through virtiofs, and the guard refuses foreign Hosts / Origins and rate-limits logins.
  `docker stop` is a clean shutdown (final publish + lock release), a recreated container
  keeps its device id, and the editing handoff works both ways between a native app and a
  container on one shared folder.

## Fixed in this branch (each has a test)

- [x] **F1 — `indexer --once` / `--reembed` ignored the write lock (constraint #9).**
  - **Problem:** `docker compose run --rm indexer --once` beside the running app (or
    `python -m sortandpost.indexer --once` beside the native app) wrote the same working DB.
  - **Why it slipped through:** the sync layer treats a lock from the same device as its own,
    and the one-shot container shares the app's `local/`, so it has the same `device.json`.
  - **Fix:** the one-shot modes now take the lock through `SyncManager` (exit 3 while another
    machine holds it), publish, then release. Both CLI modes also refuse a lock from their
    own device that has a fresh heartbeat, i.e. another live process: no pid check, since two
    containers sharing a `local/` can have the same pid.
  - **Tests:** `test_onboarding.py::test_indexer_once_*`,
    `tests/docker/test_lifecycle.py::test_one_shot_indexer_*`. This was the open memory note
    "indexer --once ignores lock".
- [x] **F2 — No init process: the app was PID 1.**
  - **Problem:** an orphaned process (a pool worker whose parent died) became a zombie that
    still looked alive, and PID 1 gets no default signal handling.
  - **Fix:** `tini` is now the image's entrypoint (`ENTRYPOINT ["tini", "--",
    "docker-entrypoint.sh"]`, also the compose `indexer` service). It works for compose,
    plain `docker run` and Synology alike.
  - **Found by:** `test_procwatch` / `test_relocate` failing in the container.
  - **Test:** `test_image.py` asserts PID 1 is tini.
- [x] **F3 — On a Mac, the template's `library_roots: [/library]` could seed `/Library`.**
  - **Problem:** an existing native device with no `library.json` (the "seed from
    config.yaml once" upgrade path in `config.Settings.resolve`) took the Docker mount point
    as a photo folder. The Mac filesystem is case-insensitive, so it scanned 100k+ system
    files.
  - **Fix:** `/library` is only seeded inside Docker.
  - **Test:** `test_native_upgrade_never_seeds_the_docker_mount_point`. It also made the native suite
    ~4× faster (other tests were walking `/Library` too).
- [x] **F4 — The test suite assumed it never runs in a container.**
  - **Fix:** an autouse fixture pins `running_in_docker()` to False for every module that
    imports it, so tests describe a native run unless they patch it back.
  - **Fix:** permission tests now probe the real filesystem (`needs_permission_bits`) instead
    of checking for root.

## To fix (open)

- [ ] **F5 — The Docker update hint is wrong** (`update/source.py` `manual_hint["docker"]`).
  - **Problem:** it says `docker compose pull && docker compose up -d`, but no image is
    published to a registry: compose builds `sortandpost:local` from the checkout, so `pull`
    does nothing.
  - **Options:**
    - (a) change the hint to `git pull && docker compose up -d --build`; or
    - (b) publish the image to GHCR from the release workflow and point compose's `image:` at
      it, keeping `build:` for source users.
  - **Recommendation:** (a) now; (b) when Docker leaves "experimental".
- [ ] **F6 — `docker stop` exits 143, not 0.**
  - **Problem:** uvicorn re-raises SIGTERM after its graceful shutdown. Natively the process
    shows -15.
  - **Impact:** the shutdown itself is clean (publish + release happen), but Container
    Manager and `docker ps -a` show "Exited (143)", which reads like a crash.
  - **Fix:** exit 0 after a graceful SIGTERM shutdown, for example a SIGTERM handler that
    sets uvicorn's `should_exit` instead of the default re-raise. Then tighten
    `test_stop_is_a_clean_shutdown` to require 0.
- [ ] **F7 — A bad password file restart-loops forever.**
  - **Problem:** with `restart: unless-stopped`, an unreadable `SORTANDPOST_PASSWORD_FILE`
    makes the container exit 2, restart, and repeat. Refusing to start open is correct, but
    the loop fills the log, and on a NAS the only sign is "restarting".
  - **Options:** `restart: on-failure:5` in compose, or sleep ~30 s before exiting on a
    config error so the loop is slow and the log readable.
- [ ] **F8 — The healthcheck is slow to say "healthy".**
  - **Problem:** `interval: 30s` means `docker compose up --wait` and Container Manager show
    "starting" for about 30 s although the app is up in about 3.
  - **Fix:** add `start_interval: 2s` (Docker ≥ 25) next to `start_period` in the Dockerfile
    and compose. The tests override the interval already.
- [ ] **F9 — No RAW fixture in CI.**
  - **Problem:** the RAW checks (parity hash of a CR2, "a RAW is never written", the RAW/HEIC
    audit in the container) only run where `~/Downloads/sampleImages` exists, which means
    this Mac and not CI.
  - **Fix:** commit one small, freely licensed RAW (e.g. a DNG from raw.pixls.us, CC0) under
    `tests/fixtures/` and use it when the sample folder is missing.
- [ ] **F10 — Torch makes the image 1.9 GB.** That's ~1.5 GB of torch/open_clip for a feature
  the plan wants to move to the desktop endpoint (V3.1 "CLIP on the endpoint"). When that
  lands, a slim image without torch (`--no-extra clip`, or a build arg) would cut pull / build
  time on a NAS a lot. Note only, not a bug.
- [ ] **F11 — Put back across filesystems shows a half-written file for a moment.**
  - **Problem:** `trash.put_back` uses `shutil.move`. When `local/trash` and the library are
    on different filesystems, the move is a copy then a delete, so the photo appears at its
    original path before all its bytes are there. That's always the case in Docker (separate
    mounts) and natively with a library on an external drive or NAS.
  - **Impact:** nothing is lost, since the trash copy is deleted only after the copy finishes.
    But the watcher can index the partial file (a decode error that's retried later). A
    crash mid-copy also leaves a truncated photo at the original path, with the full one
    still in the trash.
  - **Found by:** CI amd64, `test_delete_then_put_back` read the file while it was empty.
    The test now waits for the full bytes.
  - **Options:**
    - (a) copy to a hidden temp name beside the destination, then `os.replace` it into
      place. This is atomic, but it briefly writes a new file under a library root, which
      needs a constraint #1 carve-out.
    - (b) keep it as is and have the scanner skip a file whose size or mtime is still
      changing.

## Notes (no fix needed)

- **N1 — Permission bits through Docker Desktop's virtiofs.** A `chmod` made *inside* the
  container is cosmetic: the container sees the new mode, but the host file keeps its own,
  and writes still succeed. A host-side `chmod 444` and a Finder lock (`uchg`) **are**
  enforced, and exiftool refuses both. So a user's read-only photo stays protected; only
  tests that chmod from inside a container can't rely on it (they now skip there).
- **N2 — The one-shot `indexer` service shares the app's identity** (same `LOCAL_DIR`, same
  hostname). That's intended: it's the same machine. It's exactly why F1 needed the
  heartbeat check.
- **N3 — The handoff test has no sync delay:** both apps see the same folder instantly. The
  "waiting for the library to sync" path (grant arrives before the DB) is covered by the
  two-manager unit tests with a delayed bridge (`test_sync_handoff.py`), and for real by the
  Synology checklist item.
