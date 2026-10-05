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

The existing parser scripts are still present as first prototypes, but they are not the intended foundation for the next development phase.

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
