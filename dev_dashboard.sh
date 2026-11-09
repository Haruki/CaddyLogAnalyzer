#!/usr/bin/env bash
# One-command dev dashboard — every card populated, no server access needed:
# fresh temp DB + generated fixture logs (dev_fixtures.py) + ingest + alert run
# (--no-notify --no-ban) + API on http://127.0.0.1:8390 (Ctrl-C stops it).
#
# Fixtures are deterministic (fixed seed, anchored to now): the alert scenario always
# lands in the last closed hour, so one alert run populates anomaly_findings and
# ip_enrichment — chart/totals/hosts/top-offenders/anomalies/findings all have data.
# Country/ASN chips appear when geoip/*.mmdb files are present (see geoip/README.md).
#
# Fresh database per run, deliberately NOT a persistent checkpointed one: re-running
# regenerates the fixtures, so a fresh DB is predictable and duplicate-free at the
# cost of one full ingest per session.
#
# Spot check against real production format instead? Copy logs first (./copy_logs.sh)
# and point CADDYLOG_DEV_LOGS at them:  CADDYLOG_DEV_LOGS=/tmp/caddyloganalyzer/logs ./dev_dashboard.sh
set -euo pipefail
cd "$(dirname "$0")"

DB="/tmp/caddyloganalyzer/access.duckdb"
PORT="${PORT:-8390}"

mkdir -p "$(dirname "$DB")"
rm -f "$DB" "$DB".state.json "$DB".alert_state.json "$DB".blocks.json   # fresh per run: no stale checkpoints/state

if [ -n "${CADDYLOG_DEV_LOGS:-}" ] && [ -d "${CADDYLOG_DEV_LOGS}" ]; then
  LOGS="$CADDYLOG_DEV_LOGS"
  echo "ingesting real logs from $LOGS (spot-check mode)"
else
  LOGS="/tmp/caddyloganalyzer/fixtures"
  rm -rf "$LOGS"; mkdir -p "$LOGS"
  uv run python dev_fixtures.py --out "$LOGS"
fi

uv run python ingest.py --log-dir "$LOGS" --db "$DB"
# Populate anomaly_findings + ip_enrichment for the fixture scenario hour — no pushes, no bans.
uv run python alert.py --db "$DB" --no-notify --no-ban
echo "dashboard: http://127.0.0.1:$PORT/  (Ctrl-C stops the API)"
CADDYLOG_DB="$DB" exec .venv/bin/python -m uvicorn api:app --host 127.0.0.1 --port "$PORT"
