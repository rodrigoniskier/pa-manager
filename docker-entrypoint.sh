#!/bin/sh
set -eu

DB_PATH="${PAM_DB_PATH:-/data/pa_manager.db}"
DATA_DIR="$(dirname "$DB_PATH")"

# Railway mounts persistent volumes as root. Prepare only the persistent
# data directory as root, then immediately drop privileges for the app.
mkdir -p "$DATA_DIR"
chown -R pam:pam "$DATA_DIR"

exec gosu pam "$@"
