# AGENTS.md

Guidance for AI coding agents working in this repository.

## Project State (April 2026)

- Repository is transitioning from one-off log parsing prototypes to a DuckDB-backed access-event pipeline.
- No package layout and no automated tests yet.
- Existing scripts are early experiments, not the long-term architecture.

## Repository Map

- `caddylog.py`: Batch parser for JSON-lines access log. Builds in-memory map of `remote_ip -> [(uri, status), ...]` and prints records.
- `caddylog_seek.py`: Intended tail/follow mode parser (`tail -f` style) for the same log format.
- `watchdog.py`: File change watcher prototype using the `watchdog` library.
- `duckdb_schema.sql`: Canonical DuckDB schema for raw core access events.
- `init_duckdb.py`: Minimal initializer that creates or updates the DuckDB schema in a database file.
- `README.md`: Short project overview and DuckDB initialization note.

## Runtime Assumptions

- Future architecture assumes one Python writer process inserts events into DuckDB.
- Current prototype scripts still hardcode log path `/mnt/d/access.log`.
- Input format is JSON per line, with keys:
  - `request.remote_ip`
  - `request.uri`
  - `status`
- Raw event storage should not depend on source-file metadata.
- For the database-backed pipeline, prefer storing all valid events and applying filters in queries rather than dropping traffic at ingest time.

## Dependencies

- Standard library: `json`, `time`.
- Third-party: `duckdb` (database initialization and future ingest path), `watchdog` (needed by `watchdog.py`).

## Known Issues To Consider Before Editing

1. `caddylog_seek.py` calls `add_data(...)` but does not define or import it.
2. `caddylog_seek.py` has duplicate `import json`.
3. `watchdog.py` uses `time.sleep(...)` but does not import `time`.
4. `watchdog.py` schedules observer with `path="/mnt/d/access.log"`; watchdog scheduling is typically directory-based, so behavior may vary by platform.
5. No automated tests currently exist.

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
python3 -m py_compile caddylog.py caddylog_seek.py watchdog.py init_duckdb.py
```

If the DuckDB schema or database code changes, also verify:

- `python3 init_duckdb.py access.duckdb` creates the database successfully
- `access_events` exists with the expected columns

If behavior changes in prototype parsers, also do a smoke run against a sample log file and verify:

- malformed JSON lines are skipped without crashing
- missing keys are skipped without crashing
- LAN IP filtering still works
- output includes expected `remote_ip`, `uri`, and `status`

## Suggested Next Improvements (When Requested)

1. Add an ingest module that maps Caddy access log JSON into `access_events` rows.
2. Add incremental import from the current log file as a bridge before live event ingestion exists.
3. Add daily and monthly summary tables for consolidation.
4. Add tests for schema initialization and event-to-row mapping.
