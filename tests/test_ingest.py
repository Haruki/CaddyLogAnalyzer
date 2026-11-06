"""Ingest contract tests: event-to-row mapping, checkpoint idempotency,
rotation handling (no duplicates / no loss), and bulk-load type fidelity."""

import json
from pathlib import Path

import duckdb

from ingest import parse_line, run, to_row


def _line(ip: str, ts) -> str:
    return json.dumps({
        "ts": ts,
        "request": {"remote_ip": ip, "host": "h.example", "method": "GET", "uri": "/x"},
        "status": 200,
    })


def _count(db_path: Path) -> int:
    with duckdb.connect(str(db_path), read_only=True) as con:
        return con.execute("SELECT count(*) FROM access_events").fetchone()[0]


# --- event-to-row mapping ------------------------------------------------------

def test_to_row_full_mapping():
    raw = json.dumps({
        "level": "info", "ts": 1793094337.5825188, "logger": "http.log.access",
        "msg": "handled request",
        "request": {
            "remote_ip": "203.0.113.7", "remote_port": "59892", "client_ip": "203.0.113.7",
            "proto": "HTTP/2.0", "method": "GET", "host": "navidrome.pimpelkram.com",
            "uri": "/music?x=1&y=2",
            "headers": {"User-Agent": ["Mozilla/5.0 (Windows NT 10.0; Win64; x64)"]},
            "tls": {"server_name": "navidrome.pimpelkram.com"},
        },
        "status": 200, "duration": 0.5, "bytes_read": 100, "size": 512,
    })
    assert to_row(json.loads(raw)) == (
        1793094337.5825188,          # epoch seconds; converted in SQL at insert time
        "203.0.113.7", "203.0.113.7", "navidrome.pimpelkram.com", "GET",
        "/music", "x=1&y=2",         # uri split on the first '?'
        200, 500.0,                  # duration seconds -> ms
        100, 512, None,              # user_id absent -> NULL
        "HTTP/2.0", "navidrome.pimpelkram.com",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",   # multi-value header list unwrapped
    )


def test_to_row_minimal_fields():
    raw = json.dumps({"ts": 1700000000, "request": {"uri": "/"}, "status": 503})
    assert to_row(json.loads(raw)) == (
        1700000000, None, None, None, None, "/", None,
        503, 0.0,                    # missing duration -> 0 ms, not NULL
        None, None, None, None, None, None,
    )


def test_parse_line_skips_malformed_and_non_access():
    assert parse_line(b'{"broken') is None                       # malformed JSON
    assert parse_line(b"") is None                              # empty line
    # valid Caddy entry that is not an access event (ACME/TLS/admin logs): skipped, not an error
    assert parse_line(b'{"level":"info","ts":1,"msg":"ACME renewal"}') is None


# --- checkpointed runs ---------------------------------------------------------

def test_run_checkpoint_idempotency(db_path, tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    live = log_dir / "access.log"
    state = Path(str(db_path) + ".state.json")

    lines = [_line(f"203.0.113.{i}", 1700000000 + i) for i in range(5)] + ["not json at all"]
    live.write_text("\n".join(lines) + "\n", encoding="utf-8")

    run(log_dir, db_path, state)
    assert _count(db_path) == 5          # the malformed line is skipped, not an error

    run(log_dir, db_path, state)         # nothing new: checkpoint holds, zero re-inserts
    assert _count(db_path) == 5

    with live.open("a", encoding="utf-8") as f:   # append one more complete line
        f.write(_line("203.0.113.99", 1700000100) + "\n")
    run(log_dir, db_path, state)         # exactly the appended line lands
    assert _count(db_path) == 6


def test_run_rotation_no_duplicates_no_loss(db_path, tmp_path):
    """Caddy-style rotation: live file gzipped under an access-* name, fresh live log.

    The already-ingested prefix of the rotated archive must not be re-read
    (no duplicates), and the new live lines must all land (no loss)."""
    import gzip

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    live = log_dir / "access.log"
    state = Path(str(db_path) + ".state.json")

    old_lines = [_line(f"203.0.113.{i}", 1700000000 + i) for i in range(5)]
    live.write_text("\n".join(old_lines) + "\n", encoding="utf-8")
    run(log_dir, db_path, state)

    rotated = log_dir / "access-2026-11-06T10-00-00.000.log.gz"
    with open(live, "rb") as src, gzip.open(rotated, "wb") as dst:
        dst.writelines(src)             # byte-exact gzip of the rotated file
    live.unlink()
    new_lines = [_line(f"203.0.114.{i}", 1700000500 + i) for i in range(3)]
    live.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    run(log_dir, db_path, state)
    assert _count(db_path) == 8         # 5 rotated (once) + 3 new


# --- bulk-load fidelity ---------------------------------------------------------

def test_bulk_load_roundtrip_fidelity(db_path, tmp_path):
    """Every column type round-trips through the Arrow relation exactly.

    Regression guard for the SQL-side insert path: a schema/type mismatch in
    ROW_SCHEMA or INSERT_SQL would corrupt or drop values here."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    live = log_dir / "access.log"
    state = Path(str(db_path) + ".state.json")

    ua = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36, "quoted", comma'
    rows = [
        {"ts": 1700000000.25,
         "request": {"remote_ip": "203.0.113.1", "client_ip": "203.0.113.1",
                     "host": "h.example", "method": "POST", "uri": "/a?b=c",
                     "proto": "HTTP/3.0", "headers": {"User-Agent": [ua]},
                     "tls": {"server_name": "h.example"}},
         "status": 201, "duration": 0.5, "bytes_read": 77, "size": 888, "user_id": "walter"},
        {"ts": 1700000001, "request": {"remote_ip": "198.51.100.2", "uri": "/b"}, "status": 404},
    ]
    live.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    run(log_dir, db_path, state)

    # Compare against to_timestamp() in the same session: a perfect round-trip that is
    # immune to DuckDB's epoch()/TIMESTAMP timezone conventions (which are not inverses
    # of each other on stored columns under non-UTC session timezones).
    with duckdb.connect(str(db_path), read_only=True) as con:
        got = con.execute(
            "SELECT event_ts = to_timestamp(1700000000.25),"   # row 1's instant
            " event_ts = to_timestamp(1700000001),"            # row 2's instant
            " remote_ip, client_ip, host, method, uri_path,"
            " uri_query, status, duration_ms, bytes_read, response_size,"
            " user_id, http_proto, tls_server_name, user_agent FROM access_events ORDER BY event_ts"
        ).fetchall()

    assert got[0][0] is True and got[0][1] is False
    assert got[0][2:] == ("203.0.113.1", "203.0.113.1", "h.example", "POST", "/a", "b=c",
                          201, 500.0, 77, 888, "walter", "HTTP/3.0", "h.example", ua)
    assert got[1][0] is False and got[1][1] is True
    assert got[1][2:] == ("198.51.100.2", None, None, None, "/b", None,
                          404, 0.0, None, None, None, None, None, None)
