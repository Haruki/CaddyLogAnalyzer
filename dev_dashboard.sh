#!/usr/bin/env bash
# Point a throwaway dev dashboard at the logs copied by copy_logs.sh:
# fresh temp DB + one ingest run + API on http://127.0.0.1:8390 (Ctrl-C stops it).
#
# Fresh database per run, deliberately NOT a persistent checkpointed one: re-copying
# replaces the log files, so inode-based checkpoints can't be trusted across copies —
# a fresh DB is predictable and duplicate-free at the cost of one full ingest per session.
set -euo pipefail
cd "$(dirname "$0")"

LOGS="${CADDYLOG_DEV_LOGS:-/tmp/caddyloganalyzer/logs}"
# DB deliberately OUTSIDE $LOGS: ingest scans every "access*" file in the log dir,
# so a database inside it would be ingested as (garbage) log lines itself.
DB="/tmp/caddyloganalyzer/access.duckdb"
PORT="${PORT:-8390}"

[ -d "$LOGS" ] || { echo "no logs in $LOGS — run ./copy_logs.sh first"; exit 1; }
mkdir -p "$(dirname "$DB")"
rm -f "$DB" "$DB".state.json   # fresh evaluation DB per run: no stale checkpoints, no duplicates

uv run python ingest.py --log-dir "$LOGS" --db "$DB"
echo "dashboard: http://127.0.0.1:$PORT/  (Ctrl-C stops the API)"
CADDYLOG_DB="$DB" exec .venv/bin/python -m uvicorn api:app --host 127.0.0.1 --port "$PORT"
