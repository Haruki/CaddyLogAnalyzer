# AGENTS.md

Guidance for AI coding agents working in this repository.

## Project State (October 2026)

- Repository is a DuckDB-backed access-event pipeline; early prototype scripts were removed in October 2026.
- No package layout; core pipeline logic is covered by the pytest suite in `tests/`.

## Repository Map

- `duckdb_schema.sql`: Canonical DuckDB schema for raw core access events.
- `init_duckdb.py`: Minimal initializer that creates or updates the DuckDB schema in a database file.
- `ingest.py`: One-shot checkpointed importer of Caddy access logs into `access_events`; state kept in `<db>.state.json` next to the database.
- `alert.py`: One-shot hourly anomaly evaluator over closed 1-hour windows; persists findings into `anomaly_findings`, pushes warn/critical to ntfy, auto-bans critical IPs via the Caddy admin API. Checkpoint in `<db>.alert_state.json`, block state in `<db>.blocks.json`.
- `compose.yaml` / `Dockerfile`: Docker Compose deployment on the Caddy host — three containers (`ingest` 5-min loop, `alert` hourly loop, `api` long-lived), all running as uid/gid 966 (host caddy user); DB + state in `./data/`, log dir bind-mounted read-only.
- `api.py`: Read-only FastAPI service over `access_events` (stats / anomalies / findings history / events drill-down / status); runs as a long-lived process alongside the ingest job, DB path via env `CADDYLOG_DB`.
- `copy_logs.sh`: rsyncs the current Caddy log set from the homeserver into `/tmp/caddyloganalyzer/logs` (local dev; needs one-time ACL read access on the server).
- `dev_dashboard.sh`: throwaway local dashboard over copied logs — fresh temp DB + ingest run + API at 127.0.0.1:8390.
- `tests/`: pytest suite covering ingest mapping/checkpoint/rotation, alert rule semantics + window math + block state, API bucket ladder/range validation, and schema initialization (dev-only dependency, excluded from the image).
- `README.md`: Short project overview and DuckDB initialization note.

## Runtime Assumptions

- Future architecture assumes one Python writer process inserts events into DuckDB.
- **Timezone discipline**: all services run with `TZ=Europe/Berlin`; `event_ts` is stored as naive local wall clock, and every query must use that same convention. DuckDB gotcha (verified 2026-11): `to_timestamp(double)` returns TIMESTAMPTZ, so its INSERT cast into the TIMESTAMP column uses session TZ — but `epoch()` applied to a *stored* TIMESTAMP treats the wall clock as UTC and silently shifts by exactly one hour (the UTC offset). Never call `epoch(event_ts)` directly; use `epoch(event_ts::TIMESTAMPTZ)`, or compare instants via in-session `to_timestamp(...)` equality (see `tests/test_ingest.py`).
- Input format is JSON per line, with keys:
  - `request.remote_ip`
  - `request.uri`
  - `status`
- Raw event storage should not depend on source-file metadata.
- For the database-backed pipeline, prefer storing all valid events and applying filters in queries rather than dropping traffic at ingest time.

## Dependencies

- Third-party: `duckdb` (schema init + ingest), `pyarrow` (bulk row load in ingest), `fastapi[standard]` (read-only web API; bundles uvicorn).
- Dev-only: `pytest` (`[dependency-groups] dev`) — the test suite in `tests/`; excluded from the Docker image by `uv sync --no-dev`.
- Toolchain: `uv` — dependencies declared in `pyproject.toml`, locked in the committed `uv.lock`. Set up with `uv sync`; run commands with `uv run <cmd>`.

## Agent Working Agreement

0. Do not run `git commit` or `git push`; leave all changes uncommitted in the working tree for the user to review and ship (explicit per-task instruction overrides this).
1. Keep changes minimal and focused; avoid broad refactors unless requested.
2. Treat `duckdb_schema.sql` as the source of truth for the raw access-event table definition.
3. Preserve existing prototype behavior unless the user explicitly wants those scripts upgraded or retired.
4. Do not silently change hardcoded paths unless requested; prefer adding opt-in configurability.
5. If adding dependencies, document them clearly in README and usage notes.
6. Optimize future schema and ingest work for a single writer process and periodic consolidation jobs.

## Verification Checklist

Run these checks after edits:

```bash
uv run python -m py_compile common.py init_duckdb.py ingest.py alert.py api.py
uv run pytest
```
If `compose.yaml` or `Dockerfile` changes, also verify the image still builds: `docker build -t caddyloganalyzer .`.

If the DuckDB schema or database code changes, also verify:

- `uv run python init_duckdb.py access.duckdb` creates the database successfully
- `access_events` and `anomaly_findings` exist with the expected columns

If ingest behavior changes, also do a smoke run against a sample log directory and verify:

- malformed JSON lines are skipped without crashing
- non-access entries (ACME/TLS/admin) count as skipped, not errors
- a second identical run inserts 0 rows (checkpoint idempotency)

## Suggested Next Improvements (When Requested)

1. ~~Add an ingest module that maps Caddy access log JSON into `access_events` rows~~ — done (`ingest.py`, checkpointed, at-least-once).
2. Add incremental import from the current log file as a bridge before live event ingestion exists.
3. Add daily and monthly summary tables for consolidation.
4. ~~Add tests for schema initialization and event-to-row mapping~~ — done (`tests/`, pytest; see Verification Checklist).
