# Security review — 2026-10-02

Findings from a security review of the whole app (web UI, sync folder, file
writes/moves, helper agent, dependencies). There are no accounts, payments or uploads.
The attack surfaces are: who can reach the web server, who can write to the shared
sync folder, and which files the app writes or moves.

Status: **fixed** · **planned** (scheduled, see the linked doc) · **deferred** (later
release) · **accepted** (a known, deliberate trade-off for now).

| ID | Severity | Finding | Status |
|----|----------|---------|--------|
| C1 | Critical | Tag name with a line break → exiftool argument injection (move files, run code) | **fixed** |
| H2 | High | Delete / tombstone sweep moves any DB-listed path, unchecked | **fixed** |
| H1 | High | Web UI on `0.0.0.0` with no login (source + Docker runs) | **fixed** (V3 P6b) |
| H3 | High | The shared sync folder is fully trusted | **accepted** — single-user by design |
| M1 | Medium | DNS rebinding / cross-site websocket to the local server | **fixed** (V3 P6b) |
| M2 | Medium | Helper `/open` runs any program named in the request | **fixed** (V3 P6c); helper removed 2026-10-15 |
| L1 | Low | NiceGUI `storage_secret` is the device id, which is visible in `sync/` | **fixed** (V3 P6b) |
| L2 | Low | Docker build unpinned; no dependency audit in the quality gate | **fixed** (V3 P6a) |
| L3 | Low | No rate limiting | **fixed** (V3 P6b, login attempts) |

---

## C1 — exiftool argument injection through tag names · fixed

`ExiftoolClient` drives one `exiftool -stay_open` process and sends **one argument
per line** on its stdin. `write_keywords` built `-XMP:Subject=<name>` from tag names
that were only `.strip()`-ed, so a name containing a newline became extra exiftool
options. Both were reproduced on a scratch file:

- `"sunset\n-Directory=/elsewhere"` moved the photo out of its library folder
  (a hard-constraint #1 violation);
- `"sunset\n-if\nsystem('…')"` ran a shell command (`-if` evaluates Perl), i.e.
  code execution as the app's user, or in the container with `/library` writable.

Entry points: the tag editor (any web client can send any string over the
websocket), `sync/tags.yaml` (approving the suggestion writes it), and tag rows in
a synced / conflicted-copy DB (written on the next keyword write for that photo).

**Fix:**
- `ExiftoolClient._execute` refuses any argument containing `\n`, `\r` or NUL.
  Every exiftool call goes through it, so this is the guarantee.
- `sortandpost.tag_names`: `normalize_tag_name` collapses every whitespace run,
  line breaks included, to one space. `is_safe_tag_name` rejects control characters
  and line or paragraph separators.
- Applied in `add_manual_tag` (normalizes; a `ValueError` on control characters)
  and `load_vocab` (normalizes; skips unsafe terms with a warning).
- `embeddable_keywords` drops unsafe names already in a DB (older or merged rows)
  before any keyword write.
- Tests: `test_metadata.py::test_a_line_break_in_a_keyword_cannot_inject_exiftool_options`,
  `test_tags.py`, `test_clip_vocab.py`.

## H2 — deletes moved any path the DB listed · fixed

`_trash_with_state` moved every present `photo_paths.path` that existed on disk.
That table travels in the shared DB, so:

- a path from another machine, or a hostile synced DB row, could name any file
  (`~/Documents/…`), and the tombstone sweep would move it into `local/trash/`;
- the file was never re-checked against the deleted photo. Machine B with a
  *different* `IMG_0001.JPG` at the same absolute path as machine A, or a file
  replaced since the last scan, would be moved when A's delete was applied.

**Fix:** `delete_photos` / `apply_pending_tombstones` now take `roots` (this
machine's library folders), and a file is moved only if:
1. its resolved path is inside a resolved root, so a symlink can't lead out of
   the library; and
2. it is still the same photo: an unchanged `(size, mtime_ns)` is trusted, as in
   the scanner's fast path; otherwise it is decoded and re-hashed, and it moves
   only on a matching `pixel_hash`.

A refused file is reported as a failed item (it shows as "could not be moved")
and left where it is. The delete itself (DB purge, tombstone, `unindex`)
still happens. Tests: `test_deletion.py` (outside root, symlink escape, replaced
file, touched-but-same-pixels file, another machine's path in the tombstone sweep).

## H1 — no login, listens on all interfaces · fixed

`HOST` defaults to `0.0.0.0` on source and Docker runs, and compose publishes the
port on every network interface of the computer. Anyone on the network can browse and delete photos, rate or tag
(EXIF writes), send robopost posts, change settings, and, on a native run, set
**Open with** to any program and click Open (`reveal.open_with_app` runs it on the
host). The packaged desktop app binds `127.0.0.1` and is not affected.

**Fixed in V3 P6b** (`sortandpost.auth`): `HOST` now defaults to `127.0.0.1` (the Docker
image sets `0.0.0.0` inside the container, and compose publishes on `127.0.0.1` unless
`BIND` says otherwise). Setting `SORTANDPOST_PASSWORD` / `SORTANDPOST_PASSWORD_FILE` turns
on a shared-password login (scrypt hash in memory only) that one pure-ASGI middleware applies
to every route: pages, `/thumbs`, `/trash-thumb`, uploads and the socket.io connection
(HTTP polling and websocket). Without a password, a non-loopback `Host` is refused (M1),
so a source run reachable on the network still can't be used from another device.

## H3 — the shared sync folder is fully trusted · accepted

The synced DB is pulled / merged as-is, as are `tags.yaml`, `config.yaml` and every
`devices/*/library.json`. That's fine for one person's own machines, which is the
design today. With C1 and H2 fixed, a hostile sync folder can no longer run code or
move files outside the library folders. It can still change ratings and tags,
tombstone photos (files go to the recoverable app trash), and widen the helper's
allowed folders (M2). **Revisit if multi-user / shared libraries become a feature.**

## M1 — DNS rebinding / cross-site websocket · fixed

NiceGUI's socket.io server allows any origin (`cors_allowed_origins='*'`) and the
app doesn't check the `Host` header, so a malicious site that rebinds its domain to
`127.0.0.1` gets full control of the UI, even for the desktop app. Fix: a `Host` header
allowlist (`localhost`, `127.0.0.1`, `*.localhost`) and an origin check on the
websocket.

**Fixed in V3 P6b**: `sortandpost.auth.ServerGuard` refuses a non-loopback `Host` unless a
password is set or the name is in `SORTANDPOST_ALLOWED_HOSTS`, and refuses any websocket,
socket.io request or non-GET request whose `Origin` isn't the same host:port.

## M2 — helper `/open` runs any program · fixed

**2026-10-15:** the helper agent was shelved and removed from the repo, so this surface no
longer exists. Kept for the record.

`tools/sortandpost_helper.py` takes `app` from the request body and runs
`[app, path]`. Only the *path* is confined to library roots. The token is put in
`window.__spHelper` on every page, so anyone who can load the UI has it. `/listdir`
lists any folder on the computer. Fix: the helper reads the configured open-with app
itself (or an allowlist) and ignores the request's `app`; consider limiting `/listdir`.

**Fixed in V3 P6c**: the request's `app` is only a flag. The helper reads the saved
Open-with app from `<local>/ui_prefs.json` and refuses (403) any other name. `/listdir`
only lists under the registered roots, home, and the usual drive / app folders, with no
"up" link above them; `/health` (no token) no longer lists folders. Remaining by design:
whoever can use the UI can still *set* the Open-with app in Settings, the same as on a native
run; with the P6b login that's only someone with the password.

## L1 — `storage_secret` is the device id · fixed

`ui.run(storage_secret=settings.device_id)`, and the full device id appears in
`sync/` (lock file, `changes.device_id`). Nothing relies on `app.storage.user` for
access control today. Use a random per-machine secret in `local/` before anything does.

**Fixed in V3 P6b**: a random 32-byte secret in `local/session_secret` (0600) signs both
NiceGUI's storage cookie and the login session cookie.

## L2 — dependency hygiene · fixed

The Docker image uses `uv:latest`, the `python:3.12-slim` tag, and
`uv pip install .` without `uv.lock`, unlike the locked desktop builds. Pillow,
pillow-heif, LibRaw and exiftool all parse untrusted image files, so keep them current.
Add `uv audit` / `pip-audit` to the quality gate.

**Fixed in V3 P6a**: base images pinned by digest, the image installs from `uv.lock`, and
`uv audit` is part of the quality gate.

## L3 — no rate limiting · fixed

Not meaningful for a local single-user app until H1's login exists. Then
rate-limit login attempts. The robopost send already has an in-flight guard.

**Fixed in V3 P6b**: at most 5 wrong passwords per client address in 5 minutes, then HTTP
429 until the oldest one ages out. Each check costs a scrypt hash (~50 ms).

## Checked and fine

- No secrets in the repo or git history. The robopost key is in the OS keychain / env
  var, never sent to the browser, and `apikey=` is redacted in logs and errors.
- All DB access is parameterized SQLModel; there is no raw SQL.
- The trash-thumbnail route checks its batch id and hash. Post ids are checked
  against `^[0-9a-f]{16}$`. Put back refuses to overwrite or create folders.
- The desktop control API is token-gated with a constant-time compare.
- Subprocesses are always argument lists, never a shell.
- Not applicable: payments, Stripe, row-level security, file uploads.
