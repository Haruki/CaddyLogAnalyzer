# CaddyLogAnalyzer

Utilities and early prototypes for working with Caddy access logs.

## Setup

Create a virtual environment and install the required packages:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

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

This requires the dependencies in `requirements.txt` to be installed.
