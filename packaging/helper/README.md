# File-manager helper (Docker Desktop only)

When sortandpost runs in Docker, the container can't open Finder / File Explorer on your
computer or browse its folders. `tools/sortandpost_helper.py` is a small agent that runs on
the **same computer as Docker Desktop**, outside Docker, and does that for the browser. It
needs only Python 3 (standard library).

It doesn't help on a NAS: there's no screen there to open. Use the **Copy path** buttons.

**Before you start:** set `LOCAL_DIR` in `.env` to a folder on this computer (never inside a
synced folder) and `docker compose up -d`. The helper writes `helper.json` there, which is how
the container finds it. **Settings → File-manager helper** shows the commands below with your
folders filled in, and whether the helper is running.

## Run it once

```sh
python3 tools/sortandpost_helper.py --local-dir "<LOCAL_DIR>" --sync-dir "<SYNC_DIR>"
```

## Start it at login

**macOS** (a launchd agent, `~/Library/LaunchAgents/com.sortandpost.helper.plist`):

```sh
packaging/helper/install-macos.sh --local-dir "<LOCAL_DIR>" --sync-dir "<SYNC_DIR>"
packaging/helper/install-macos.sh --uninstall
```

If `SYNC_DIR` is in `~/Library/CloudStorage` (OneDrive, Google Drive), macOS may ask once
whether Python may access it; allow it. Log: `~/Library/Logs/sortandpost-helper.log`.

**Windows**: edit the two folder lines at the top of `packaging\helper\sortandpost-helper.bat`,
then either

- press Win+R, type `shell:startup`, and put a **shortcut** to the `.bat` in that folder; or
- create a logon task (PowerShell):

  ```powershell
  schtasks /Create /SC ONLOGON /TN "sortandpost helper" /TR "\"C:\path\to\sortandpost\packaging\helper\sortandpost-helper.bat\""
  ```

## What it can do (Security.md M2)

- Listens on `127.0.0.1` only. Every request but `/health` needs the random token from
  `helper.json`, which only the sortandpost pages get (behind the login, if one is set).
- **Reveal / Open** only act on files inside a library folder registered in Settings.
- **Open** runs the default app, or the app saved in **Settings → Open with**, which it reads
  from `<LOCAL_DIR>/ui_prefs.json` itself. A request can't name a program.
- **Browse** lists folder names only under your library folders, your home folder, and the
  usual places for drives and apps (`/Volumes` and `/Applications` on macOS; drive letters and
  Program Files on Windows; `/media` and `/mnt` on Linux).
