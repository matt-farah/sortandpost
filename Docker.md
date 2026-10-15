# Docker release — plan

Work to do before the Docker image is a supported way to run sortandpost (a
NAS / always-on server browsed from other devices). Today the Docker path works,
but it has no login and listens on the whole network. See [Security.md](Security.md).

## Must-have

- [ ] **Login screen** (Security.md **H1**). Any device that can reach the port
      currently has full control: browse, delete, rate/tag (writes into files),
      robopost posts, settings. Decide:
  - single local password vs. per-user accounts (accounts tie into H3,
    multi-user);
  - where the password hash lives: per machine in `local/`, never synced and never
    plaintext. Under Docker, likely an env var / secret file, like
    `ROBOPOST_API_KEY` (see the `secrets_store` rules);
  - session cookie signed with a random secret from `local/`, **not** the device
    id (Security.md **L1**);
  - login rate limiting / lockout (Security.md **L3**);
  - gating NiceGUI's pages, the static `/thumbs` mount, the trash-thumbnail route
    and the socket.io connection, not just page routes.
- [x] **Bind to localhost by default.** Compose: `${BIND:-127.0.0.1}:${PORT}:8080`
      (P6d); opening it to the network is `BIND=0.0.0.0` in `.env`. Still to do: the
      source-run default `HOST=0.0.0.0`, and documenting `BIND` alongside the login.
- [ ] **Host / Origin checks** (Security.md **M1**): a `Host` header allowlist and
      an origin check on the websocket, configurable for a NAS hostname.
- [ ] **HTTPS story** for access over the network: a reverse proxy (Caddy / Traefik)
      example, or document that a password over plain HTTP on a network is sniffable.

## Should-have

- [x] **Pinned, locked builds** (Security.md **L2**, P6a): `python:3.13-slim` and `uv` are
      pinned by digest; the image installs from `uv.lock` (`uv sync --locked`) in a
      deps-only layer. Linux torch / torchvision come from PyTorch's CPU index
      (`[tool.uv.sources]`), so the lock carries the CPU build; macOS / Windows are unchanged.
- [x] Dependency audit: `uv audit` is in the quality gate (no CI yet). Clean on 2026-10-14.
- [x] Runs as a non-root user (P6a): `docker-entrypoint.sh` re-owns only `/data/local`, then
      drops to `PUID:PGID` (default 1000) with `setpriv`. `/library` stays the only photo mount.
- [ ] Helper agent hardening (Security.md **M2**) — the helper is the Docker path's
      Reveal / Open, so ship it fixed: it ignores the request's `app`, and `/listdir`
      is limited.
- [x] Healthcheck (P6a): `GET /healthz` → `ok`, in the image and in compose.

## Validated (P6a, 2026-10-14, Docker Desktop on macOS arm64)

First full `docker compose up --build` on scratch folders: the image builds (1.9 GB, torch
`2.14.1+cpu`), turns `healthy`, the wizard runs, 39 photos index, CLIP downloads into
`/data/local/models` and embeds on CPU, a rating lands in the host file's `XMP:Rating`
through the bind mount (as uid 1000), delete → managed trash → put back works, a watchdog
event through the mount triggers a scoped pass, and a sparse stub reads as online-only
(`st_blocks` 0) with the Docker banner. Not tested yet: a Synology (PUID/PGID), Windows /
WSL2 mounts.

## Deployment shapes

- **Synology NAS**: Docker (Container Manager) on the NAS; `sync/` and the photos are native
  NAS folders; desktop apps reach them through Synology Drive.
- **Mac / PC host**: Docker Desktop on an always-on computer; `sync/` lives in Google Drive or
  OneDrive; other machines reach it through that sync tool. Keep `sync/` and the library
  **always downloaded** (OneDrive: Always keep on this device; Google Drive: Mirror files).

### Cloud folders and Docker Desktop (tested 2026-10-14, OneDrive on macOS)

- Bind-mounting a `~/Library/CloudStorage/OneDrive*` folder works with no extra Docker
  setup (virtiofs). Files the container writes are uploaded by OneDrive as usual.
- **An online-only file can't be read from inside the container.** After Free Up Space, a
  read fails with `Input/output error` (`st_blocks` 0); the read does not make OneDrive
  download it. The Mac app's own read does download it, and Docker can read it after that.
- So under Docker, `sortandpost.sync.online_only` checks `sync/` at startup and every 5
  minutes, and shows a banner on `/` + a Settings row naming the host folder
  (`SORTANDPOST_HOST_SYNC_DIR`) and the setting to change.
- The library's own online-only photos (P6a): under Docker the scan always records stubs
  (`online_only: download` is ignored there, since the read would only fail), and the `/`
  banner + Settings row explain "Always keep on this device" instead of offering
  "Download & index".
- Not tested: Google Drive, Windows (WSL2 mounts), and whether the placeholder heuristic
  sees online-only files there.

In both, `local/` must never sit in a sync folder, so compose mounts the halves separately
(`Paths(app_data=/data)` already resolves `/data/local` + `/data/sync`):

```yaml
volumes:
  - "${PHOTO_LIBRARY}:/library:rw"
  - "${SYNC_DIR}:/data/sync:rw"          # the shared folder
  - "sortandpost-local:/data/local"      # working DB, device.json, trash — never synced
hostname: "${DEVICE_NAME:-sortandpost-server}"   # stable name in banners + devices/ folder
```

## Editing checkout

An always-on Docker instance renews the write lock forever, so every desktop app would stay
read-only. V3 P6d adds a checkout through `sync/` files only (no network link between the
instances, no new auth), usable in either direction:

1. **Request** — the borrower writes `sync/locks/request-<device_id[:8]>.json`
   (`{device_id, hostname, requested_at}`, expires after `sync.handoff_request_ttl_sec`, 600).
   Only the holder ever writes the lock file; requesters write only their own file.
2. **Grace** — the holder's heartbeat tick sees it and shows "<requester> wants to edit —
   handing over in 30 s [Keep editing]" (`sync.handoff_grace_sec`). Keep editing = deny.
   An unattended holder (Docker) hands over when the grace ends.
3. **Yield** — UI goes read-only first, the indexer stops between files and is joined, a forced
   publish, then the lock is rewritten as a **grant** (`handoff_to`, `handoff_generation`) and
   still heartbeated.
4. **Take** — the borrower promotes only once its own `sync/sortandpost.db` has
   `db_generation >= handoff_generation` (the sync tool may deliver the lock before the DB),
   then runs the normal promotion (merge, reconcile, verify, indexer) and deletes its request.
5. **Return** — quit releases as today; a read-only instance reclaims a free lock unless
   another device's request is pending. Docker reclaims early with the same **Take over
   editing** button.
6. **Safety nets** — a stale grant / lock is taken over as today; a slow sync that lets both
   write is settled by the conflict-copy merge; after acquiring, read the lock back one
   heartbeat later and step down if another device won.

## Open questions

- Is the Docker deployment single-user (one password) or the start of multi-user?
- Should the robopost composer be available on a network-exposed server at all,
  or only when logged in as an "owner"?
