#!/usr/bin/env bash
# Build dist/sortandpost-<version>-<arch>.AppImage (V3 P7).
#
#   packaging/linux/build.sh [--arch x86_64|aarch64] [--no-container]
#
# By default the build runs inside an Ubuntu 22.04 container
# (packaging/linux/Dockerfile): that sets the AppImage's floor -- glibc 2.35, GTK
# 3.24 -- so it runs on Ubuntu 22.04+, Debian 12+, Fedora 36+. Needs Docker; the
# other architecture also needs binfmt emulation (on an Apple Silicon Lima VM,
# Rosetta runs the x86_64 build). The venv, Python, downloads and PyInstaller's
# work folder live in a named volume (sortandpost-appimage-<arch>), so the repo
# only ever gets the finished AppImage in dist/.
#
# --no-container builds right here (CI on an ubuntu-22.04 runner, or inside the
# container -- how the default mode calls it). It needs the apt packages the
# Dockerfile installs, plus `uv`. The venv is synced to uv.lock (`--locked`: the
# build fails if the lock is stale rather than re-resolving).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HERE="$REPO/packaging/linux"

EXIFTOOL_VERSION="13.59"  # keep in step with packaging/macos/build.sh
EXIFTOOL_SHA256="668ea3acececb7235fbd0f4900e72d5f12c9b07e5c778fd36cb1e9b5828fd65a"
EXIFTOOL_URL="https://sourceforge.net/projects/exiftool/files/Image-ExifTool-${EXIFTOOL_VERSION}.tar.gz/download"

APPIMAGETOOL_VERSION="1.9.1"
RUNTIME_VERSION="20251108"  # AppImage/type2-runtime: static, needs only fusermount (fuse2 or 3)
declare -A APPIMAGETOOL_SHA256=(
  [x86_64]="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
  [aarch64]="f0837e7448a0c1e4e650a93bb3e85802546e60654ef287576f46c71c126a9158"
)
declare -A RUNTIME_SHA256=(
  [x86_64]="2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"
  [aarch64]="00cbdfcf917cc6c0ff6d3347d59e0ca1f7f45a6df1a428a0d6d8a78664d87444"
)

die() { echo "$*" >&2; exit 1; }
norm_arch() { case "$1" in arm64|aarch64) echo aarch64 ;; amd64|x86_64) echo x86_64 ;; *) echo "$1" ;; esac; }

ARCH="$(norm_arch "$(uname -m)")"
CONTAINER=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --arch) ARCH="$(norm_arch "${2:?--arch needs a value}")"; shift 2 ;;
    --no-container) CONTAINER=0; shift ;;
    *) die "usage: $0 [--arch x86_64|aarch64] [--no-container]" ;;
  esac
done
case "$ARCH" in
  x86_64) PLATFORM=linux/amd64 ;;
  aarch64) PLATFORM=linux/arm64 ;;
  *) die "unsupported architecture: $ARCH" ;;
esac
[[ "$(uname -s)" == "Linux" ]] || die "build on Linux (on a Mac: the Lima VM, see dev-notes/V3Plan.md)"

# -- container mode: build the builder image, re-run this script inside it ---- #
if [[ "$CONTAINER" == 1 ]]; then
  command -v docker >/dev/null 2>&1 || die "needs Docker (or pass --no-container)"
  IMAGE="sortandpost-appimage-builder:$ARCH"
  VOLUME="sortandpost-appimage-$ARCH"
  echo "==> builder image $IMAGE ($PLATFORM)"
  docker build --quiet --platform "$PLATFORM" -t "$IMAGE" -f "$HERE/Dockerfile" "$HERE" >/dev/null
  # The volume is created root-owned; hand it to the user the build runs as.
  docker run --rm --platform "$PLATFORM" -v "$VOLUME:/cache" "$IMAGE" \
    chown "$(id -u):$(id -g)" /cache
  exec docker run --rm --platform "$PLATFORM" \
    --user "$(id -u):$(id -g)" \
    -e HOME=/cache/home -e SP_WORK=/cache/work \
    -v "$VOLUME:/cache" -v "$REPO:$REPO" -w "$REPO" \
    "$IMAGE" "$HERE/build.sh" --no-container --arch "$ARCH"
fi

# -- the build ------------------------------------------------------------------ #
[[ "$(norm_arch "$(uname -m)")" == "$ARCH" ]] \
  || die "--no-container builds for this machine's architecture ($(uname -m)), not $ARCH"
command -v uv >/dev/null 2>&1 || die "needs uv (https://docs.astral.sh/uv/)"

WORK="${SP_WORK:-$REPO/build/linux-$ARCH}"
DIST="$REPO/dist"
mkdir -p "$WORK/downloads" "$DIST" "${HOME:?}"

export UV_PROJECT_ENVIRONMENT="$WORK/venv"
export UV_PYTHON_INSTALL_DIR="$WORK/python"
export UV_CACHE_DIR="$WORK/uv-cache"
export UV_LINK_MODE=copy
(cd "$REPO" && uv sync --locked --no-dev --extra desktop --quiet)
PY="$WORK/venv/bin/python"
"$PY" - <<'PYCHECK' || die "missing desktop build deps in $WORK/venv"
import importlib.util

import gi, open_clip, PyInstaller, torch  # noqa: F401

assert importlib.util.find_spec("pystray"), "pystray"  # importing it needs a display
gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
PYCHECK
VERSION="$("$PY" -c 'import sortandpost; print(sortandpost.__version__)')"
echo "==> sortandpost $VERSION ($ARCH, glibc $(ldd --version | head -1 | awk '{print $NF}'))"

fetch() {  # fetch <url> <dest> <sha256>
  if [[ ! -f "$2" ]]; then
    curl -sSfL -o "$2.part" "$1"
    mv "$2.part" "$2"
  fi
  echo "$3  $2" | sha256sum -c - >/dev/null || die "checksum mismatch: delete $2 and retry"
}

# -- exiftool (Perl distribution; runs on the system perl) --------------------- #
TARBALL="$WORK/downloads/Image-ExifTool-${EXIFTOOL_VERSION}.tar.gz"
fetch "$EXIFTOOL_URL" "$TARBALL" "$EXIFTOOL_SHA256"
rm -rf "$WORK/exiftool-src" "$WORK/exiftool"
mkdir -p "$WORK/exiftool-src" "$WORK/exiftool"
tar -xzf "$TARBALL" -C "$WORK/exiftool-src"
SRC="$WORK/exiftool-src/Image-ExifTool-${EXIFTOOL_VERSION}"
cp "$SRC/exiftool" "$WORK/exiftool/exiftool"
cp -R "$SRC/lib" "$WORK/exiftool/lib"
chmod 755 "$WORK/exiftool/exiftool"
"$WORK/exiftool/exiftool" -ver >/dev/null  # sanity: runs with the system perl

# -- PyInstaller onedir ------------------------------------------------------------ #
echo "==> PyInstaller (a few minutes: torch is big)"
rm -rf "$WORK/bundle"
# PYSTRAY_BACKEND=dummy: collect_all("pystray") imports the package, which
# otherwise looks for a display while choosing its backend. Build time only.
SP_EXIFTOOL_DIR="$WORK/exiftool" PYSTRAY_BACKEND=dummy \
  "$PY" -m PyInstaller --noconfirm --log-level WARN \
  --distpath "$WORK/bundle" --workpath "$WORK/pyinstaller" \
  "$HERE/sortandpost.spec"

# -- AppDir ---------------------------------------------------------------------- #
APPDIR="$WORK/AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/applications" "$APPDIR/usr/share/metainfo" \
  "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp -a "$WORK/bundle/sortandpost" "$APPDIR/usr/bin/sortandpost"
install -m 755 "$HERE/AppRun" "$APPDIR/AppRun"
install -m 644 "$HERE/app.sortandpost.desktop" "$APPDIR/app.sortandpost.desktop"
install -m 644 "$HERE/app.sortandpost.desktop" "$APPDIR/usr/share/applications/"
install -m 644 "$HERE/app.sortandpost.appdata.xml" "$APPDIR/usr/share/metainfo/"
"$PY" "$HERE/make_icon.py" "$APPDIR" >/dev/null
cp "$APPDIR/sortandpost.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/sortandpost.png"
ln -s sortandpost.png "$APPDIR/.DirIcon"

BUNDLED_ET="$(find "$APPDIR/usr/bin/sortandpost" -path '*/exiftool/exiftool' -type f | head -1)"
[[ -n "$BUNDLED_ET" ]] || die "bundled exiftool missing"
echo "==> bundled exiftool $("$BUNDLED_ET" -ver)"
echo "==> app reports $("$APPDIR/AppRun" --version)"

# -- AppImage ----------------------------------------------------------------------- #
TOOL="$WORK/downloads/appimagetool-${APPIMAGETOOL_VERSION}-$ARCH.AppImage"
RUNTIME="$WORK/downloads/runtime-${RUNTIME_VERSION}-$ARCH"
fetch "https://github.com/AppImage/appimagetool/releases/download/${APPIMAGETOOL_VERSION}/appimagetool-$ARCH.AppImage" \
  "$TOOL" "${APPIMAGETOOL_SHA256[$ARCH]}"
fetch "https://github.com/AppImage/type2-runtime/releases/download/${RUNTIME_VERSION}/runtime-$ARCH" \
  "$RUNTIME" "${RUNTIME_SHA256[$ARCH]}"
# Run a copy with the AppImage magic (bytes 8-10, "AI\x02" in the ELF header's
# padding) zeroed: binfmt handlers (Rosetta, qemu-user) match the plain ELF
# header and refuse it otherwise ("Exec format error"). Harmless natively.
RUN_TOOL="$WORK/appimagetool-run"
cp "$TOOL" "$RUN_TOOL"
printf '\0\0\0' | dd of="$RUN_TOOL" bs=1 seek=8 count=3 conv=notrunc status=none
chmod 755 "$RUN_TOOL"

OUT="$DIST/sortandpost-${VERSION}-${ARCH}.AppImage"
rm -f "$OUT"
echo "==> appimagetool"
# No FUSE in a container: the tool runs from an extracted copy of itself.
APPIMAGE_EXTRACT_AND_RUN=1 ARCH="$ARCH" "$RUN_TOOL" --no-appstream \
  --runtime-file "$RUNTIME" --comp zstd "$APPDIR" "$OUT" >"$WORK/appimagetool.log" 2>&1 \
  || { tail -30 "$WORK/appimagetool.log" >&2; die "appimagetool failed"; }

echo "==> $(du -sh "$APPDIR/usr/bin/sortandpost" | cut -f1)  bundle"
echo "==> $(du -sh "$OUT" | cut -f1)  $OUT"
