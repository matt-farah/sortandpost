#!/usr/bin/env bash
# Build sortandpost.app + sortandpost-<version>-arm64.dmg (V2 P5).
#
#   packaging/macos/build.sh
#
# Needs: macOS on Apple Silicon, `uv` (`brew install uv`), network access for
# the one-time exiftool download. The repo venv is synced to `uv.lock` first
# (`--locked`: the build fails if the lock is stale rather than re-resolving),
# so the app bundles exactly the locked versions. PYTHON=<python> builds from
# that interpreter's env instead and skips the sync.
#
# Signing: ad-hoc by default. On macOS 15+ a downloaded ad-hoc app can't be
# opened with right-click -> Open any more: open it once, then System Settings ->
# Privacy & Security -> Open Anyway. Each ad-hoc build also re-asks for Keychain
# access (new signature). Set
# SORTANDPOST_SIGN_ID="Developer ID Application: …" to sign with the hardened
# runtime, and additionally NOTARY_PROFILE=<notarytool keychain profile> to
# notarize + staple the DMG.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PY="${PYTHON:-$REPO/.venv/bin/python}"
WORK="$REPO/build/macos"
DIST="$REPO/dist"

EXIFTOOL_VERSION="13.59"
EXIFTOOL_SHA256="668ea3acececb7235fbd0f4900e72d5f12c9b07e5c778fd36cb1e9b5828fd65a"
EXIFTOOL_URL="https://sourceforge.net/projects/exiftool/files/Image-ExifTool-${EXIFTOOL_VERSION}.tar.gz/download"

[[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]] || { echo "build on an Apple Silicon Mac" >&2; exit 1; }
if [[ -z "${PYTHON:-}" ]]; then
  command -v uv >/dev/null 2>&1 || { echo "needs uv (brew install uv)" >&2; exit 1; }
  (cd "$REPO" && uv sync --locked --extra dev --extra desktop)
fi
"$PY" -c "import PyInstaller, pystray, AppKit, torch, open_clip" \
  || { echo "missing desktop build deps: install the 'desktop' extra into $PY's env" >&2; exit 1; }
VERSION="$("$PY" -c 'import sortandpost; print(sortandpost.__version__)')"
echo "==> sortandpost $VERSION"

mkdir -p "$WORK/downloads" "$DIST"

# -- exiftool (Perl distribution; runs on macOS's /usr/bin/perl) ------------- #
TARBALL="$WORK/downloads/Image-ExifTool-${EXIFTOOL_VERSION}.tar.gz"
if [[ ! -f "$TARBALL" ]]; then
  echo "==> downloading exiftool $EXIFTOOL_VERSION"
  curl -sSfL -o "$TARBALL.part" "$EXIFTOOL_URL"
  mv "$TARBALL.part" "$TARBALL"
fi
echo "$EXIFTOOL_SHA256  $TARBALL" | shasum -a 256 -c - >/dev/null \
  || { echo "exiftool checksum mismatch -- delete $TARBALL and retry" >&2; exit 1; }
rm -rf "$WORK/exiftool-src" "$WORK/exiftool"
mkdir -p "$WORK/exiftool-src" "$WORK/exiftool"
tar -xzf "$TARBALL" -C "$WORK/exiftool-src"
SRC="$WORK/exiftool-src/Image-ExifTool-${EXIFTOOL_VERSION}"
cp "$SRC/exiftool" "$WORK/exiftool/exiftool"
cp -R "$SRC/lib" "$WORK/exiftool/lib"
chmod 755 "$WORK/exiftool/exiftool"
"$WORK/exiftool/exiftool" -ver >/dev/null  # sanity: runs with the system perl

# -- icon ------------------------------------------------------------------ #
"$PY" "$REPO/packaging/macos/make_icon.py" "$WORK" >/dev/null

# -- bundle ---------------------------------------------------------------- #
echo "==> PyInstaller (a few minutes: torch is big)"
rm -rf "$DIST/sortandpost.app" "$DIST/sortandpost"
SP_EXIFTOOL_DIR="$WORK/exiftool" SP_ICNS="$WORK/sortandpost.icns" \
  "$PY" -m PyInstaller --noconfirm --log-level WARN \
  --distpath "$DIST" --workpath "$WORK/pyinstaller" \
  "$REPO/packaging/macos/sortandpost.spec"
APP="$DIST/sortandpost.app"
rm -rf "$DIST/sortandpost"  # the intermediate onedir; the .app holds the same files

BUNDLED_ET="$(find "$APP/Contents" -path '*/exiftool/exiftool' -type f | head -1)"
[[ -n "$BUNDLED_ET" ]] || { echo "bundled exiftool missing" >&2; exit 1; }
echo "==> bundled exiftool $("$BUNDLED_ET" -ver)"

# -- sign ------------------------------------------------------------------ #
if [[ -n "${SORTANDPOST_SIGN_ID:-}" ]]; then
  echo "==> signing with $SORTANDPOST_SIGN_ID (hardened runtime)"
  codesign --force --deep --timestamp --options runtime \
    --entitlements "$REPO/packaging/macos/entitlements.plist" \
    -s "$SORTANDPOST_SIGN_ID" "$APP"
else
  echo "==> ad-hoc signing (no Developer ID)"
  codesign --force --deep -s - "$APP"
fi
codesign --verify --deep --strict "$APP"

# -- dmg ------------------------------------------------------------------- #
DMG="$DIST/sortandpost-${VERSION}-arm64.dmg"
STAGE="$WORK/dmg"
rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -quiet -volname "sortandpost $VERSION" -srcfolder "$STAGE" -ov -format UDZO "$DMG"

if [[ -n "${SORTANDPOST_SIGN_ID:-}" && -n "${NOTARY_PROFILE:-}" ]]; then
  echo "==> notarizing"
  codesign --timestamp -s "$SORTANDPOST_SIGN_ID" "$DMG"
  xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$DMG"
fi

echo "==> $(du -sh "$APP" | cut -f1)  $APP"
echo "==> $(du -sh "$DMG" | cut -f1)  $DMG"
