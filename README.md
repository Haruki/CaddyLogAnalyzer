# CaddyLogAnalyzer

Utilities and early prototypes for working with Caddy access logs.

## Setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) once per machine, then in the project directory run:

```bash
uv sync          # creates .venv with the exact versions from uv.lock
uv run python init_duckdb.py access.duckdb   # any command runs inside the env
```

Dependencies are declared in `pyproject.toml` and pinned by the committed `uv.lock`; `.python-version` pins the interpreter. Add a dependency with `uv add <package>`.

## Current Direction

The long-term direction of this repository is a DuckDB-backed pipeline:

- a single Python ingest service writes raw access events into DuckDB
- analytical queries run against the raw access event table
- periodic consolidation jobs roll recent raw events into smaller statistical summaries


## DuckDB Schema

The core access event schema lives in [duckdb_schema.sql](duckdb_schema.sql).

It defines a single append-only `access_events` table with columns for:

- event timestamp and ingest timestamp
- remote and client IPs
- host, method, and split URI fields (`uri_path`, `uri_query`)
- status, duration, and byte counts
- user and protocol metadata

Initialize a database with:

```bash
python3 init_duckdb.py access.duckdb
```

This requires the project environment to be set up (`uv sync`).

## Ingestion

`ingest.py` is a one-shot, checkpointed importer for Caddy access logs: each run reads new complete lines since the last checkpoint (live `access.log` plus rotated archives), inserts them into `access_events` in a single transaction, then advances the checkpoint stored next to the database (`<db>.state.json`). Re-runs are safe; at-least-once semantics mean rare duplicates are possible after crashes or rotations, never lost rows.

Run manually:

```bash
uv run python ingest.py --log-dir /var/log/caddy --db access.duckdb
```

On the Caddy host, install the units from `systemd/`:

```bash
sudo cp systemd/caddylog-ingest.service systemd/caddylog-ingest.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now caddylog-ingest.timer
```

Host prerequisites: this repository checked out at `/opt/caddyloganalyzer` with `uv sync` run once (the service calls `.venv/bin/python` directly, so uv is not needed at runtime), and Caddy's log directory readable by the `caddy` user (true by default). The database and checkpoint live in `/var/lib/caddylog/`, created by systemd via `StateDirectory=`.

## API & Dashboard

`api.py` is a read-only FastAPI service over the database: aggregate statistics for fixed windows (`5m | 1h | 1d | 30d | year`, where `year` is a calendar year with monthly buckets and accepts `?year=YYYY` for past years) or an explicit range (`from`/`to`, naive server-local ISO timestamps; the bucket size is derived from the span — per-minute up to monthly — and the series is zero-filled across exactly `[from, to)`), anomaly hints with human-readable reasons, and raw event rows for drill-down. All queries run on the fly against `access_events` (single-digit milliseconds even over a year of events); the service coexists with the ingest timer — DuckDB allows one writer plus any number of readers across processes.

Run locally (point at any database file via env):

```bash
CADDYLOG_DB=access.duckdb uv run python api.py          # dev, binds 127.0.0.1:8377
# or explicitly: CADDYLOG_DB=/var/lib/caddylog/access.duckdb uv run uvicorn api:app --port 8377
```

The service also serves a small self-contained dashboard at `/` (static files from `static/`, no build step): window tabs, request chart (vendored uPlot), status breakdown, top hosts and anomaly findings with click-through drill-down into raw rows. The chart is click-to-zoom: clicking a bucket drills one level deeper (hour → 5-minute → per-minute → raw events) and breadcrumb chips track the path back; presets always reset to now-anchored views. Live updates arrive via the SSE endpoint `GET /stream` — the server pushes an event whenever new rows land in the database (fingerprint check every 5 s); if a proxy buffers SSE, the dashboard falls back to plain polling automatically.

Endpoints: `GET /stats?window=…` or `GET /stats?from=…&to=…` · `GET /anomalies` · `GET /events?(window=…|from=…&to=…)&ip=…&host=…&min_status=…` · `GET /status` · `GET /stream`. Interactive OpenAPI docs at `/docs`. Binds 127.0.0.1 only; expose via a Caddy reverse proxy if needed (SSE needs `proxy_buffering off`).
