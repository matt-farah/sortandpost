# Docker release — plan

Work to do before the Docker image is a supported way to run sortandpost (a
NAS / always-on server browsed from other devices). Today the Docker path works,
but it has no login and listens on the whole network. See [Security.md](Security.md).

## Must-have

- [x] **Login screen** (Security.md **H1**, V3 P6b, `sortandpost.auth`): one shared
      password from `SORTANDPOST_PASSWORD` or `SORTANDPOST_PASSWORD_FILE` (a Docker secret;
      the file wins, and an unreadable file stops the app rather than starting it open).
      Kept only as a scrypt hash in memory. A signed cookie (`sp_session_<port>`, HttpOnly,
      SameSite=Lax, Secure behind HTTPS, 30 days) with a random secret in
      `local/session_secret` (**L1**). Changing the password signs every browser out; Sign out
      (header) revokes that session. 5 wrong passwords per address in 5 minutes → wait
      (**L3**). One pure-ASGI middleware gates pages, `/thumbs`, `/trash-thumb`, uploads and
      socket.io (polling + websocket); only `/login`, `/healthz` and the tray's token-gated
      `/_desktop/*` stay open. The login page is plain HTML (no socket needed).
- [x] **Bind to localhost by default.** Compose: `${BIND:-127.0.0.1}:${PORT}:8080`
      (P6d); the source-run default is now `HOST=127.0.0.1` too (P6b). The image sets
      `HOST=0.0.0.0` inside the container.
- [x] **Host / Origin checks** (Security.md **M1**, P6b): without a password only
      `localhost`, `127.0.0.1`, `[::1]`, `*.localhost` and `SORTANDPOST_ALLOWED_HOSTS` are
      served; with one, any Host reaches the login page. A websocket, socket.io request or
      non-GET request from another Origin is refused.
- [x] **HTTPS story**: see [HTTPS](#https) below. Plain HTTP on a network lets anyone on it
      read the password and the session cookie.

## HTTPS

Over a network, put a reverse proxy in front and keep the app's own port on localhost.
Caddy gets and renews a certificate by itself for a public name; on a LAN, `tls internal`
uses its own local CA (install Caddy's root certificate on your devices once).

```caddyfile
photos.example.com {          # or: nas.local { tls internal ... }
    reverse_proxy 127.0.0.1:8080
}
```

- Keep `BIND=127.0.0.1` when Caddy runs on the same host (or put both in one compose
  network and proxy to `app:8080`).
- Caddy passes the `Host` header and websockets through unchanged, which the Origin check
  needs. With another proxy, make sure it forwards `Host` and `Upgrade` / `Connection`.
- It sets `X-Forwarded-Proto: https`, so the session cookie is marked `Secure`.
- A Synology's built-in reverse proxy (Control Panel → Login Portal → Advanced) works too;
  add the websocket custom headers there.

## Should-have

- [x] **Pinned, locked builds** (Security.md **L2**, P6a): `python:3.13-slim` and `uv` are
      pinned by digest; the image installs from `uv.lock` (`uv sync --locked`) in a
      deps-only layer. Linux torch / torchvision come from PyTorch's CPU index
      (`[tool.uv.sources]`), so the lock carries the CPU build; macOS / Windows are unchanged.
- [x] Dependency audit: `uv audit` is in the quality gate (no CI yet). Clean on 2026-10-14.
- [x] Runs as a non-root user (P6a): `docker-entrypoint.sh` re-owns only `/data/local`, then
      drops to `PUID:PGID` (default 1000) with `setpriv`. `/library` stays the only photo mount.
- [x] ~~Helper agent hardening (P6c)~~ — **shelved 2026-10-15**: the host file-manager helper
      (`tools/sortandpost_helper.py`, `packaging/helper/`) was removed for the first release as
      too much setup. Under Docker the photo page's copy button copies each file's **network
      path** instead (a per-folder `network` path in Settings → Library: `\\nas\photo` /
      `smb://nas/photo`, spelled for the viewer's OS). The helper is in git history before
      that commit if it comes back.
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

### Always-on NAS (Synology)

The NAS runs the container 24/7: it indexes new photos, runs CLIP and publishes the
database to the shared folder. Laptops and desktops run the desktop app on the same shared
folder (through Synology Drive) and borrow editing with **Take over editing** (P6d); when
they quit, the NAS takes it back. Browse the NAS's own web UI from a phone or another computer
only with a password and HTTPS ([HTTPS](#https)). There's no Reveal in Finder under Docker;
set each library folder's network path in Settings → Library and the copy button hands
your other computers a path they can open (`\\nas\photo\…` / `smb://nas/photo/…`).

Set `PUID` / `PGID` to the owner of the photo and shared folders (`id <user>` over SSH),
`DEVICE_NAME` to a stable name, and leave `LOCAL_DIR` unset (a Docker volume) or point it at
a folder that isn't synced.

### Bind-mount performance

- **Linux / Synology**: bind mounts are native; scans run at disk speed.
- **Docker Desktop (macOS / Windows)**: every file read crosses into the Linux VM
  (virtiofs on macOS, the WSL2 9P share on Windows). A first scan and CLIP pass are slower
  than a native run, and CLIP runs on the CPU only (no Apple GPU / MPS in Docker). For a
  large first index, the native app is faster; Docker is for the steady state after that.
- Thumbnails and the working DB live on Docker's side (`/data/local`, a volume by default),
  so browsing doesn't touch the bind mounts much.
- On Windows, keep the photos on an NTFS drive the WSL2 VM can reach; network drives
  (SMB) work but are slow and don't deliver file-change events, so new photos wait for the
  15-minute full scan.

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

- ~~Single-user or multi-user?~~ One shared password (V3 P6b). Accounts would tie into
  Security.md H3 (the shared folder is fully trusted) and are a V4+ question.
- Should the robopost composer be available on a network-exposed server at all,
  or only when logged in as an "owner"? (Today: anyone with the password.)
