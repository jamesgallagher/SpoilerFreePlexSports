#!/bin/sh
# Drop root to PUID:PGID (unRAID convention: 99:100) before running sfps.
# gosu accepts numeric uid:gid, so no passwd entries are needed.
set -e

if [ "$(id -u)" = "0" ]; then
    PUID="${PUID:-1000}"
    PGID="${PGID:-1000}"
    chown "$PUID:$PGID" /config 2>/dev/null || true
    exec gosu "$PUID:$PGID" sfps "$@"
fi

exec sfps "$@"
