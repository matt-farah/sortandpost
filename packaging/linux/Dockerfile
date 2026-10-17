# The build environment for the Linux AppImage (V3 P7); build.sh builds and runs
# it. Ubuntu 22.04 sets the AppImage's floor: glibc 2.35 and GTK 3.24 from here
# run on Ubuntu 22.04+, Debian 12+, Fedora 36+ and friends. Both images are pinned
# by digest (multi-arch indexes: build.sh picks linux/arm64 or linux/amd64).
FROM ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 AS uv
FROM ubuntu:22.04@sha256:5ec03bb3441e8b0bf3b4f9cd4629a1ae763010dc3035bb8da3ae6cf026486401

ENV DEBIAN_FRONTEND=noninteractive
# build-essential + the -dev packages: PyGObject / pycairo build from source.
# gir1.2-* + libayatana-appindicator3-1: the tray's GTK / AppIndicator typelibs
# and libraries, which PyInstaller's gi hooks copy into the bundle.
# binutils + patchelf + file: PyInstaller. perl: runs the bundled exiftool for the
# sanity check. desktop-file-utils: appimagetool validates the .desktop file.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential pkg-config ca-certificates curl file binutils patchelf perl \
      libgirepository1.0-dev libcairo2-dev gobject-introspection \
      gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 libayatana-appindicator3-1 \
      desktop-file-utils \
 && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /uvx /usr/local/bin/
