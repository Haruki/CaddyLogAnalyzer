"""Alert contract tests: closed-window math, detection-rule semantics against
crafted traffic (the subtle parts), and auto-ban state handling."""

from datetime import datetime, timedelta

import duckdb

from alert import (AUTH_FAILURE_MIN, EXFIL_MIN_RESPONSES, load_blocks, rule_auth_breach,
                   rule_exfiltration, save_blocks, window_bounds)

WS = datetime(2026, 11, 6, 13, 0)     # a closed hour: [13:00, 14:00)
WE = WS + timedelta(hours=1)


def _insert(con, ts_epoch: float, ip: str, host: str, status: int, path="/x", response_size=None):
    if response_size is None:
        con.execute(
            "INSERT INTO access_events (event_ts, remote_ip, host, method, uri_path, status)"
            " VALUES (to_timestamp(?), ?, ?, 'GET', ?, ?)", [ts_epoch, ip, host, path, status])
    else:
        con.execute(
            "INSERT INTO access_events (event_ts, remote_ip, host, method, uri_path, status, response_size)"
            " VALUES (to_timestamp(?), ?, ?, 'GET', ?, ?, ?)", [ts_epoch, ip, host, path, status, response_size])


def test_window_bounds():
    assert window_bounds(datetime(2026, 11, 6, 14, 37, 22)) == (WS, WE)
    # exactly on the hour: the just-finished hour is still the last fully elapsed one
    assert window_bounds(datetime(2026, 11, 6, 14, 0, 0)) == (WS, WE)


def test_rule_auth_breach_semantics(db_path):
    """Fires only on >= AUTH_FAILURE_MIN 401s AND repeated success from the same IP+host."""
    t = WS.timestamp()
    with duckdb.connect(str(db_path)) as con:
        for i in range(AUTH_FAILURE_MIN):            # breach pattern: failures then real usage
            _insert(con, t + i, "203.0.113.10", "svc.example", 401)
        for i in range(3):
            _insert(con, t + 100 + i, "203.0.113.10", "svc.example", 200)

        for i in range(AUTH_FAILURE_MIN + 2):       # failures only: no success -> quiet
            _insert(con, t + i, "203.0.113.11", "svc.example", 401)

        for i in range(AUTH_FAILURE_MIN - 1):       # below failure threshold: quiet even with success
            _insert(con, t + i, "203.0.113.12", "svc.example", 401)
        for i in range(5):
            _insert(con, t + 50 + i, "203.0.113.12", "svc.example", 200)

        for i in range(AUTH_FAILURE_MIN):           # LAN traffic: excluded entirely (EXT predicate)
            _insert(con, t + i, "192.168.0.50", "svc.example", 401)
        for i in range(3):
            _insert(con, t + 100 + i, "192.168.0.50", "svc.example", 200)

        findings = rule_auth_breach(con, WS, WE)

    assert [f["entity"] for f in findings] == ["203.0.113.10"]
    assert findings[0]["severity"] == "critical"


def test_rule_exfiltration_requires_varying_sizes_and_new_ip(db_path):
    """Fires only for brand-new IPs pulling >= EXFIL_MIN_RESPONSES 2xx of VARYING sizes."""
    t = WS.timestamp()
    with duckdb.connect(str(db_path)) as con:
        # uniform response size on a sensitive path (SPA-fallback signature) -> quiet
        for i in range(EXFIL_MIN_RESPONSES):
            _insert(con, t + i, "203.0.113.20", "spa.example", 200, path="/.env", response_size=857)

        # varying sizes from a brand-new IP (first-ever event inside the window) -> fires
        for i, size in enumerate([100, 999, 42, 777, 31]):
            _insert(con, t + i, "203.0.113.21", "spa.example", 200, path="/.env", response_size=size)

        # varying sizes but the IP has history before the window -> not brand-new: quiet
        _insert(con, t - 86400, "203.0.113.22", "old.example", 200)
        for i in range(EXFIL_MIN_RESPONSES):
            _insert(con, t + i, "203.0.113.22", "spa.example", 200, path="/backup.sql", response_size=10 * (i + 1))

        findings = rule_exfiltration(con, WS, WE)

    assert [f["entity"] for f in findings] == ["203.0.113.21"]
    assert findings[0]["severity"] == "critical"


def test_blocks_state_roundtrip_and_corruption_failsafe(tmp_path):
    p = tmp_path / "access.duckdb.blocks.json"
    blocks = {"203.0.113.99": {"blocked_at": "2026-11-06T10:00:00",
                                "expires": "2026-11-06T14:00:00", "finding_id": 7}}
    save_blocks(p, blocks)
    assert load_blocks(p) == blocks

    # a corrupted state file must fail safe (forget the blocks), never crash the alert run
    p.write_text("{corrupted json", encoding="utf-8")
    assert load_blocks(p) == {}
