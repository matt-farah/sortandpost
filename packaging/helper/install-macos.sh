#!/bin/sh
# Start the sortandpost file-manager helper at login (macOS launchd agent).
#
#   packaging/helper/install-macos.sh --local-dir <LOCAL_DIR> --sync-dir <SYNC_DIR>
#   packaging/helper/install-macos.sh --uninstall
#
# Use the same host folders as LOCAL_DIR / SYNC_DIR in your .env (Settings -> File-manager
# helper shows the command with them filled in). The agent runs this checkout's
# tools/sortandpost_helper.py with the python3 found on PATH; re-run this after moving
# the checkout. Log: ~/Library/Logs/sortandpost-helper.log
set -eu
here=$(cd "$(dirname "$0")" && pwd)
helper="$(cd "$here/../.." && pwd)/tools/sortandpost_helper.py"
exec python3 - "$helper" "$@" <<'PY'
import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.sortandpost.helper"
helper, argv = sys.argv[1], sys.argv[2:]
p = argparse.ArgumentParser(prog="install-macos.sh")
p.add_argument("--local-dir", type=Path)
p.add_argument("--sync-dir", type=Path)
p.add_argument("--uninstall", action="store_true")
args = p.parse_args(argv)

plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
domain = f"gui/{os.getuid()}"
subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
if args.uninstall:
    plist.unlink(missing_ok=True)
    print("sortandpost helper removed from login items")
    raise SystemExit(0)
if args.local_dir is None or args.sync_dir is None:
    p.error("give --local-dir and --sync-dir (or --uninstall)")
local_dir, sync_dir = args.local_dir.expanduser().resolve(), args.sync_dir.expanduser().resolve()
for d in (local_dir, sync_dir):
    if not d.is_dir():
        p.error(f"{d} is not a folder")

log = Path.home() / "Library" / "Logs" / "sortandpost-helper.log"
plist.parent.mkdir(parents=True, exist_ok=True)
with plist.open("wb") as fh:
    plistlib.dump(
        {
            "Label": LABEL,
            "ProgramArguments": [
                sys.executable,
                helper,
                "--local-dir",
                str(local_dir),
                "--sync-dir",
                str(sync_dir),
            ],
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(log),
            "StandardErrorPath": str(log),
        },
        fh,
    )
subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)
print(f"sortandpost helper started and will start at login ({plist})")
print(f"log: {log}")
PY
