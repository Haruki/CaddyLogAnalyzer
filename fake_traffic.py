"""Synthetic external traffic for local testing of alert rules and dashboard paths.

Use when the database has no recent *external* traffic (e.g. only LAN IPs in the
last hour) so code paths that need it can be exercised locally: every alert rule,
the ip_enrichment step, and the top-offenders card with CGNAT/mobile chips.

Inserts one closed hour of scenario data using documentation-range IPs (RFC 5737
TEST-NET-1/2/3, APNIC doctest 198.18.x) that never appear in real traffic:

    IP               pattern                                              fires
    ---------------  ---------------------------------------------------  ------------------
    203.0.113.50     60 reqs this hour, 1 prior event last hour           hot_ip (info)
    198.51.100.77    120 reqs, no events before the window               first_sight_spike (warn)
    192.0.2.88       4x 401 then 5x 2xx on dav.pimpelkram.com           auth_breach (critical)
    198.18.0.99      brand-new IP, 6x 2xx varying sizes, sensitive paths exfiltration (critical)
    203.0.113.9      7x 5xx on broken.pimpelkram.com                    server_errors (warn)
    100.64.20.30     CGNAT, mostly Android User-Agent (no finding)       cgnat + mobile chips

Then run the alert job against the same database to persist findings and populate
ip_enrichment (CGNAT flag always; country/ASN when geoip/*.mmdb files exist):

    uv run python fake_traffic.py --db access.duckdb
    uv run python alert.py --db access.duckdb --no-notify --no-ban

The scenario lands in the last closed hour — the same window alert.py evaluates.
On the dashboard it is visible via the 1d/30d tabs or an explicit from/to range
covering that hour (rolling 5m/1h only if you look within ~45 min of its start).

Cleanup when done: DELETE FROM access_events WHERE remote_ip IN (...); — the exact
statement is printed at the end. Findings/enrichment rows clean up after themselves
on the next hourly run once the fake IPs stop being loud (or delete by window_start).
"""

import argparse
from datetime import datetime, timedelta
from pathlib import Path

from init_duckdb import connect_with_retry, initialize_database

DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"
ANDROID_UA = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Mobile Chrome/126"

# (remote_ip, host, uri_path, status, response_size, user_agent, offset_seconds into the hour)
SCENARIO: list[tuple[str, str, str, int, int, str, float]] = []


def _add(ip: str, host: str, uri: str, status: int, size: int, ua: str, offsets: list[float]) -> None:
    SCENARIO.extend((ip, host, uri, status, size, ua, off) for off in offsets)


def build_scenario() -> None:
    """One event per offset; all offsets stay inside [30 s, 3570 s] of the hour."""
    # hot_ip: 60 this hour + exactly 1 prior event (offset -1800 = last hour)
    _add("203.0.113.50", "pimpelkram.com", "/ping", 200, 4, DESKTOP_UA, [-1800] + [60 * i for i in range(60)])
    # first_sight_spike: 120 requests, never seen before the window
    _add("198.51.100.77", "pimpelkram.com", "/ping", 200, 4, "python-requests/2.31.0", [30 * i for i in range(120)])
    # auth_breach: 4 failures then 5 successes on the same host (9 total < hot threshold)
    _add("192.0.2.88", "dav.pimpelkram.com", "/remote.php", 401, 312, DESKTOP_UA, [600 + 30 * i for i in range(4)])
    _add("192.0.2.88", "dav.pimpelkram.com", "/remote.php", 200, 512, DESKTOP_UA, [780 + 30 * i for i in range(5)])
    # exfiltration: brand-new IP, sensitive paths with VARYING response sizes (the discriminator)
    _add("198.18.0.99", "pimpelkram.com", "/.env", 200, 1234, "curl/8.5.0", [900])
    _add("198.18.0.99", "pimpelkram.com", "/wp-config.php", 200, 5678, "curl/8.5.0", [930])
    _add("198.18.0.99", "pimpelkram.com", "/.git/config", 200, 890, "curl/8.5.0", [960])
    _add("198.18.0.99", "pimpelkram.com", "/backup.sql", 200, 45678, "curl/8.5.0", [990])
    _add("198.18.0.99", "pimpelkram.com", "/config.yaml", 200, 2345, "curl/8.5.0", [1020])
    _add("198.18.0.99", "pimpelkram.com", "/app", 200, 857, "curl/8.5.0", [1050 + 30 * i for i in range(3)])
    # server_errors: 7x 5xx on one host (plus a few healthy responses)
    _add("203.0.113.9", "broken.pimpelkram.com", "/api/status", 502, 163, DESKTOP_UA, [1200 + 60 * i for i in range(7)])
    _add("203.0.113.9", "broken.pimpelkram.com", "/api/status", 200, 42, DESKTOP_UA, [1560 + 60 * i for i in range(3)])
    # CGNAT mobile: no finding (below threshold) — exercises cgnat flag + mobile UA share only
    _add("100.64.20.30", "pimpelkram.com", "/app", 200, 857, ANDROID_UA, [1800 + 90 * i for i in range(8)])
    _add("100.64.20.30", "pimpelkram.com", "/app", 200, 857, DESKTOP_UA, [2520, 2610])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Insert a synthetic external-traffic scenario (one closed hour) for local testing.")
    parser.add_argument("--db", default="access.duckdb", help="DuckDB database file to insert into.")
    parser.add_argument("--window-start", default=None, metavar="ISO",
                        help="Scenario hour start (default: the last fully elapsed hour — "
                             "the same window alert.py evaluates).")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    db_path = Path(args.db)
    initialize_database(db_path)

    now = datetime.now()
    if args.window_start is not None:
        ws = datetime.fromisoformat(args.window_start).replace(minute=0, second=0, microsecond=0)
    else:
        ws = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)   # last closed hour
    we = ws + timedelta(hours=1)

    build_scenario()
    rows = [(ws.timestamp() + off, ip, host, uri, status, size, ua)
            for ip, host, uri, status, size, ua, off in SCENARIO]

    con = connect_with_retry(db_path)
    try:
        con.executemany(
            "INSERT INTO access_events (event_ts, remote_ip, host, method, uri_path,"
            " status, response_size, user_agent)"
            " VALUES (to_timestamp(?), ?, ?, 'GET', ?, ?, ?, ?)",
            rows,
        )
        con.commit()
    finally:
        con.close()

    ips = sorted({ip for ip, *_ in SCENARIO})
    print(f"fake_traffic: inserted {len(rows)} synthetic events "
          f"for {len(ips)} IPs into {ws:%Y-%m-%d %H:%M}–{we:%H:%M} ({db_path})")
    print("next: uv run python alert.py --db " + str(db_path) + " --no-notify --no-ban")
    print(f"cleanup: DELETE FROM access_events WHERE remote_ip IN ({', '.join(repr(ip) for ip in ips)});")


if __name__ == "__main__":
    main()
