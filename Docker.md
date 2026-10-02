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
- [ ] **Bind to localhost by default.** Compose: `127.0.0.1:${PORT}:8080`; opening
      it to the network should be an explicit `.env` choice, documented alongside the
      login. The source-run default `HOST=0.0.0.0` likewise.
- [ ] **Host / Origin checks** (Security.md **M1**): a `Host` header allowlist and
      an origin check on the websocket, configurable for a NAS hostname.
- [ ] **HTTPS story** for access over the network: a reverse proxy (Caddy / Traefik)
      example, or document that a password over plain HTTP on a network is sniffable.

## Should-have

- [ ] **Pinned, locked builds** (Security.md **L2**): pin `python:3.12-slim` and the
      `uv` image by digest; install from `uv.lock` (`uv sync --locked`) like the
      desktop builds.
- [ ] Dependency audit (`uv audit` / `pip-audit`) in the quality gate and CI.
- [ ] Run as a non-root user in the container; keep `/library` the only rw photo mount.
- [ ] Helper agent hardening (Security.md **M2**) — the helper is the Docker path's
      Reveal / Open, so ship it fixed: it ignores the request's `app`, and `/listdir`
      is limited.
- [ ] A compose healthcheck (curl is already in the image for this).

## Open questions

- Is the Docker deployment single-user (one password) or the start of multi-user?
- Should the robopost composer be available on a network-exposed server at all,
  or only when logged in as an "owner"?
