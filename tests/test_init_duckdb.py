"""Schema initialization: creates the canonical tables and is safe to re-run."""

import duckdb

from init_duckdb import initialize_database


def test_initialize_creates_schema_and_is_idempotent(tmp_path):
    path = tmp_path / "fresh.duckdb"
    initialize_database(path)
    initialize_database(path)   # re-run must be a no-op, not an error (IF NOT EXISTS everywhere)

    with duckdb.connect(str(path), read_only=True) as con:
        tables = {r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_type = 'BASE TABLE'"
        ).fetchall()}
        assert {"access_events", "anomaly_findings", "ip_enrichment"} <= tables

        cols = {r[0] for r in con.execute("DESCRIBE access_events").fetchall()}
        assert {"event_ts", "remote_ip", "client_ip", "host", "method", "uri_path",
                "uri_query", "status", "duration_ms", "bytes_read", "response_size",
                "user_id", "http_proto", "tls_server_name", "user_agent"} <= cols
