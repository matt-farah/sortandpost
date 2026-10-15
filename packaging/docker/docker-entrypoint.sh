#!/bin/sh
# Drop root before running the app. PUID / PGID (default 1000) should own the host
# folders mounted at /library and /data/sync -- on a Synology, the user that owns the
# shared folders (`id <user>` over SSH). Docker Desktop on a Mac / PC maps ownership
# itself, so the default works there.
set -e
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" = "0" ]; then
    # Only the container's own data is re-owned -- never the photo library or the
    # shared folder (hard constraint #1: nothing under a library root changes).
    chown -R "$PUID:$PGID" /data/local
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- "$@"
fi
exec "$@"
