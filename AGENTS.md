# AGENTS.md

Guidance for AI coding agents working in this repository.

## Project State (October 2026)

- Repository is a DuckDB-backed access-event pipeline; early prototype scripts were removed in October 2026.
- No package layout and no automated tests yet.

## Repository Map

- `duckdb_schema.sql`: Canonical DuckDB schema for raw core access events.
- `init_duckdb.py`: Minimal initializer that creates or updates the DuckDB schema in a database file.
- `ingest.py`: One-shot checkpointed importer of Caddy access logs into `access_events`; state kept in `<db>.state.json` next to the database.
- `systemd/`: Oneshot service + 5-minute timer running `ingest.py` on the Caddy host.
- `api.py`: Read-only FastAPI service over `access_events` (stats / anomalies / events drill-down / status); runs as a long-lived process alongside the ingest timer, DB path via env `CADDYLOG_DB`.
- `README.md`: Short project overview and DuckDB initialization note.

## Runtime Assumptions

- Future architecture assumes one Python writer process inserts events into DuckDB.
- Input format is JSON per line, with keys:
  - `request.remote_ip`
  - `request.uri`
  - `status`
- Raw event storage should not depend on source-file metadata.
- For the database-backed pipeline, prefer storing all valid events and applying filters in queries rather than dropping traffic at ingest time.

## Dependencies

- Third-party: `duckdb` (schema init + ingest), `fastapi[standard]` (read-only web API; bundles uvicorn).
- Toolchain: `uv` — dependencies declared in `pyproject.toml`, locked in the committed `uv.lock`. Set up with `uv sync`; run commands with `uv run <cmd>`.

## Known Issues To Consider Before Editing

1. No automated tests currently exist.

## Agent Working Agreement

1. Keep changes minimal and focused; avoid broad refactors unless requested.
2. Treat `duckdb_schema.sql` as the source of truth for the raw access-event table definition.
3. Preserve existing prototype behavior unless the user explicitly wants those scripts upgraded or retired.
4. Do not silently change hardcoded paths unless requested; prefer adding opt-in configurability.
5. If adding dependencies, document them clearly in README and usage notes.
6. Optimize future schema and ingest work for a single writer process and periodic consolidation jobs.

## Verification Checklist

Run these checks after edits:

```bash
uv run python -m py_compile init_duckdb.py ingest.py
```

If the DuckDB schema or database code changes, also verify:

- `uv run python init_duckdb.py access.duckdb` creates the database successfully
- `access_events` exists with the expected columns

If ingest behavior changes, also do a smoke run against a sample log directory and verify:

- malformed JSON lines are skipped without crashing
- non-access entries (ACME/TLS/admin) count as skipped, not errors
- a second identical run inserts 0 rows (checkpoint idempotency)

## Suggested Next Improvements (When Requested)

1. ~~Add an ingest module that maps Caddy access log JSON into `access_events` rows~~ — done (`ingest.py`, checkpointed, at-least-once).
2. Add incremental import from the current log file as a bridge before live event ingestion exists.
3. Add daily and monthly summary tables for consolidation.
4. Add tests for schema initialization and event-to-row mapping.
