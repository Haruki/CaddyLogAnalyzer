#!/usr/bin/env bash
# Copy the current Caddy log set from the homeserver into a local temp folder,
# ready for dev_dashboard.sh. One-time prerequisite on the server (read access):
#   sudo usermod -aG caddy walter     # then start a new SSH session
set -euo pipefail

DEST="${CADDYLOG_DEV_LOGS:-/tmp/caddyloganalyzer/logs}"
mkdir -p "$DEST"

# --delete keeps the local mirror exact (rotated files that fell off the server's
# roll_keep window disappear locally too) — dev_dashboard.sh builds a fresh DB from it.
rsync -az --delete 'homeserver:/var/log/caddy/access*' "$DEST"/

echo "logs in $DEST:"
du -sh "$DEST"
ls -lh "$DEST"
