# Docker — manual test checklist (V3.1)

The automated suite (`uv run pytest --docker tests/docker`, and `.github/workflows/docker.yml`
on amd64 + arm64) covers what one computer can check by itself: the image, the normal test
suite inside it, the browser flows, the server guard, lifecycle, native-vs-Docker parity and
the native ↔ Docker handoff on a shared folder. What's left below needs real hardware, a real
sync tool, or a second device. Tick each with the date and anything odd.

Set-up for every rig: `cp .env.example .env`, set `PHOTO_LIBRARY`, `SYNC_DIR`, `DEVICE_NAME`,
`docker compose up -d --build`, open `http://localhost:8080`.

## Synology NAS (Container Manager)

- [ ] **Import the compose project** in Container Manager → Project, with `PUID` / `PGID` set to
      the owner of the photo and shared folders (`id <user>` over SSH). Expect: the container
      turns *healthy*; the app's log has no `Permission denied`.
- [ ] **Rate a photo** (press `4` on the photo page). Expect: Synology Photos / File Station
      shows the file with a new modified time, and its owner and ACL are unchanged (File
      Station → Properties → Permission).
- [ ] **Desktop app on the same shared folder** through Synology Drive: Mac app's banner says
      "read-only · <DEVICE_NAME>". Click **Take over editing**. Expect: within ~30 s plus
      Synology Drive's delay the Mac becomes the editor ("waiting for the library to sync"
      first if the DB lags); the NAS UI says read-only.
- [ ] Rate on the Mac, **quit the Mac app**. Expect: the NAS takes editing back and shows that
      rating after Synology Drive delivers the publish.
- [ ] **Keep editing** on the NAS UI while the Mac asks. Expect: the Mac says "<NAS> kept
      editing".
- [ ] **Reverse proxy + HTTPS** (Control Panel → Login Portal → Advanced → Reverse Proxy, with
      the websocket custom headers), and `SORTANDPOST_PASSWORD` set. Expect: the login page over
      `https://`; after login the page stays connected (no reload loop = websocket works);
      browser dev tools show the `sp_session_*` cookie as `Secure`.
- [ ] **Reboot the NAS.** Expect: the container comes back by itself (`restart:
      unless-stopped`), keeps its device name, and takes the lock again (or stays read-only if a
      desktop app is editing).
- [ ] **New photos over SMB** (copy from another computer onto the share). Expect: they appear
      within the 15-minute full scan (network shares don't deliver file events). Note the time.
- [ ] **First-index timing** for a real library (N photos, minutes) vs the Mac app.

## Docker Desktop on a Mac

- [ ] `SYNC_DIR` inside **OneDrive** (Always keep on this device). Run, rate, quit/restart.
      Expect: OneDrive uploads `sortandpost.db` after each publish; no "conflicted copy" files.
- [ ] Same with **Google Drive** (Mirror files). Not tested at all yet.
- [ ] **Free up space** on one file in `SYNC_DIR` (or on a photo in a cloud library folder).
      Expect: within 5 minutes the "online-only" banner names the host folder and says to keep it
      downloaded; for a library photo the banner says "Always keep on this device", and there's
      no "Download & index" button.
- [ ] **Sleep the Mac** with a desktop app on another computer waiting. Expect: after
      `lock_stale_after_sec` (5 min) the other computer can take over; on wake the container
      notices and goes read-only (no two writers).
- [ ] **Read-only photo file**: in Finder, Get Info → Locked on one photo, then rate it in the
      container. Expect: the "couldn't write to 1 file" warning toast; the DB keeps the rating
      (exiftool alone refused both a host `chmod 444` and a Finder lock through virtiofs, see
      DockerTestFindings.md N1).

## Docker Desktop on Windows (WSL2)

- [ ] `PHOTO_LIBRARY` on an **NTFS drive** (e.g. `D:\Photos`). Expect: indexing works, a new
      photo copied in appears within seconds (file events reach WSL2).
- [ ] `PHOTO_LIBRARY` on a **mapped SMB drive**. Expect: indexing works but slowly; new photos
      wait for the 15-minute scan. Note the speed.
- [ ] Settings → Library → set the folder's **network path** (`\\nas\photo`). On the photo
      page, click copy. Expect: pasting into Explorer's address bar opens the file's folder.
- [ ] Default `PUID=1000` works (no permission errors writing ratings).
- [ ] **Windows desktop app ↔ container handoff** on the same shared folder (OneDrive): Take
      over editing, rate, quit, container reclaims.
- [ ] `docker compose down` then `up -d` (e.g. after a Docker Desktop update). Expect: same
      device name, nothing re-indexed.

## Linux host (Lima VM or a real box)

- [ ] Library and shared folder owned by a **non-1000 user**; set `PUID`/`PGID` to it.
      Expect: ratings write; `ls -ln` shows the photo files' owner unchanged.
- [ ] **inotify**: copy a photo in. Expect: it appears within a few seconds.
- [ ] **Reboot the host.** Expect: the container starts by itself.
- [ ] `docker compose run --rm indexer --once` **while the app runs**. Expect: it refuses
      ("holds the write lock", exit 3). With the app stopped it indexes, then the app starts
      without re-indexing.

## Phone / another device on the LAN

- [ ] `BIND=0.0.0.0`, **no password**. From the phone: Expect the "this address isn't allowed"
      page (Host check), not the library.
- [ ] `BIND=0.0.0.0` + `SORTANDPOST_PASSWORD_FILE` (a Docker secret). Expect: login page; a
      wrong password 5 times → "Too many wrong passwords"; the right one works afterwards only
      after the wait.
- [ ] Over **Caddy HTTPS** (`docs/docker.md#https`): browse, search, rate on the phone. Expect: no
      reconnect loop; ratings show on the desktop after the next publish.
- [ ] Phone layout: the photo page's info goes under the image (< 640 px); the grid scrolls
      smoothly; the star widget is tappable.
- [ ] **Sign out** on the phone; **change the password** and restart. Expect: every device has to
      log in again.

## Everywhere

- [ ] Posting with `ROBOPOST_API_KEY` / `POSTIZ_API_KEY` in `.env` (no keychain in a container):
      Settings → Posting shows the key as "from the environment"; a draft sends.
- [ ] Settings → Overview → Updates shows the Docker hint (see DockerTestFindings.md F4 — the
      hint's command is wrong today).
- [ ] Achievements unlock and the profile picture upload works from the container.
