"""Install a verified update once the app has stopped.

The order (docs/architecture.md → "Updates"):

1. the server verifies the download, writes ``pending.json``
   (:func:`~sortandpost.update.check.write_pending`), shuts down cleanly (indexer
   stop, final publish, lock release) and exits with ``UPDATE_EXIT_CODE``;
2. the tray re-checks the file (:func:`~sortandpost.update.check.take_pending`),
   starts the platform helper below **detached**, and ends;
3. the helper waits for the tray to exit, swaps the app in, writes
   ``result.txt`` and starts the app -- the new one, or the old one again if any
   step failed.

Every helper leaves the old app in place until the new one is fully copied,
and logs to ``updates/helper.log``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from sortandpost.update.check import RESULT_NAME, PendingUpdate, updates_dir

log = logging.getLogger(__name__)

# macOS: mount the DMG, copy the .app beside the old one, swap, relaunch.
_MACOS_SH = r"""#!/bin/sh
# sortandpost.photos updater: $1 tray pid, $2 dmg, $3 installed .app, $4 version, $5 folder
PID="$1"; DMG="$2"; TARGET="$3"; VERSION="$4"; DIR="$5"
RESULT="$DIR/result.txt"; MNT="$DIR/mnt"
OPEN="${SORTANDPOST_UPDATE_OPEN:-open}"  # tests swap in a stand-in
exec >>"$DIR/helper.log" 2>&1
echo "== $(date) updating $TARGET to $VERSION"
fail() {
  echo "failed: $1"
  echo "failed $1" > "$RESULT"
  [ -d "$MNT" ] && hdiutil detach "$MNT" -quiet -force
  rm -rf "$TARGET.new"
  [ -d "$TARGET" ] || { [ -d "$TARGET.old" ] && mv "$TARGET.old" "$TARGET"; }
  $OPEN "$TARGET"
  exit 1
}
n=0
while kill -0 "$PID" 2>/dev/null; do
  n=$((n + 1)); [ "$n" -gt 240 ] && fail "the app didn't quit"
  sleep 0.5
done
rm -rf "$MNT" "$TARGET.new" "$TARGET.old"
mkdir -p "$MNT"
hdiutil attach -nobrowse -readonly -noautoopen -mountpoint "$MNT" "$DMG" >/dev/null \
  || fail "couldn't open the update"
[ -d "$MNT/sortandpost.app" ] || fail "the update has no app in it"
ditto "$MNT/sortandpost.app" "$TARGET.new" || fail "couldn't copy the new version next to $TARGET"
hdiutil detach "$MNT" -quiet || hdiutil detach "$MNT" -quiet -force
rmdir "$MNT" 2>/dev/null
xattr -dr com.apple.quarantine "$TARGET.new" 2>/dev/null
mv "$TARGET" "$TARGET.old" || fail "couldn't move the old version aside"
mv "$TARGET.new" "$TARGET" || fail "couldn't put the new version in place"
rm -rf "$TARGET.old"
echo "ok $VERSION" > "$RESULT"
echo "ok"
$OPEN "$TARGET"
"""

# Linux: copy the AppImage beside the old one, swap (same folder = atomic), relaunch.
_LINUX_SH = r"""#!/bin/sh
# sortandpost.photos updater: $1 tray pid, $2 new AppImage, $3 installed AppImage,
#   $4 version, $5 folder
PID="$1"; NEW="$2"; TARGET="$3"; VERSION="$4"; DIR="$5"
RESULT="$DIR/result.txt"
exec >>"$DIR/helper.log" 2>&1
echo "== $(date) updating $TARGET to $VERSION"
start() { nohup "$TARGET" >/dev/null 2>&1 & }
fail() {
  echo "failed: $1"; echo "failed $1" > "$RESULT"; rm -f "$TARGET.new"; start; exit 1
}
n=0
while kill -0 "$PID" 2>/dev/null; do
  n=$((n + 1)); [ "$n" -gt 240 ] && fail "the app didn't quit"
  sleep 0.5
done
cp "$NEW" "$TARGET.new" || fail "couldn't copy the new version next to $TARGET"
chmod +x "$TARGET.new" || fail "couldn't make the new version runnable"
mv -f "$TARGET.new" "$TARGET" || fail "couldn't put the new version in place"
echo "ok $VERSION" > "$RESULT"
echo "ok"
start
"""

# Windows: run the installer silently (it waits for the app's mutex itself and
# relaunches the app with /RELAUNCH=1); if it fails, start the old app again.
_WINDOWS_CMD = r"""@echo off
rem sortandpost.photos updater: %1 setup.exe, %2 installed exe, %3 version, %4 folder
set "DIR=%~4"
echo == %DATE% %TIME% updating to %~3 >> "%DIR%\helper.log"
"%~1" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /RELAUNCH=1 /LOG="%DIR%\setup.log"
if errorlevel 1 (
  echo failed the installer stopped with code %ERRORLEVEL% (see setup.log) > "%DIR%\result.txt"
  echo failed %ERRORLEVEL% >> "%DIR%\helper.log"
  start "" "%~2"
) else (
  echo ok %~3 > "%DIR%\result.txt"
  echo ok >> "%DIR%\helper.log"
)
"""


def _write_script(folder: Path, name: str, text: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    newline = "\r\n" if name.endswith(".cmd") else "\n"
    path.write_text(text, encoding="utf-8", newline=newline)
    path.chmod(0o700)
    return path


def launch_installer(
    pending: PendingUpdate, target: Path, *, tray_pid: int, folder: Path | None = None
) -> None:
    """Start the platform helper detached; it outlives the tray. Raises
    ``OSError`` if it can't even be started (the caller then restarts the
    server on the old version)."""
    d = folder or updates_dir()
    (d / RESULT_NAME).unlink(missing_ok=True)
    if sys.platform == "darwin":
        script = _write_script(d, "update-macos.sh", _MACOS_SH)
        argv = ["/bin/sh", str(script), str(tray_pid), pending.path, str(target)]
        _spawn([*argv, pending.version, str(d)])
    elif sys.platform == "win32":  # pragma: no cover - Windows only
        script = _write_script(d, "update-windows.cmd", _WINDOWS_CMD)
        _spawn(["cmd.exe", "/c", str(script), pending.path, str(target), pending.version, str(d)])
    elif sys.platform.startswith("linux"):
        script = _write_script(d, "update-linux.sh", _LINUX_SH)
        argv = ["/bin/sh", str(script), str(tray_pid), pending.path, str(target)]
        _spawn([*argv, pending.version, str(d)])
    else:  # pragma: no cover
        raise OSError(f"no updater for {sys.platform}")
    log.info("update %s: installer started (%s)", pending.version, target)


def _spawn(argv: list[str]) -> None:
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":  # pragma: no cover - Windows only
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined,unused-ignore]
        kwargs["creationflags"] = flags | subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined,unused-ignore]
    else:
        kwargs["start_new_session"] = True  # survives the tray's exit
        from sortandpost.binaries import host_env

        kwargs["env"] = host_env(dict(os.environ))  # the relaunched AppImage gets a clean env
    subprocess.Popen(argv, **kwargs)  # type: ignore[call-overload]
