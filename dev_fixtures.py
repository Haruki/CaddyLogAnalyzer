"""Deterministic Caddy-format fixture logs for local dashboard development.

Generates N days of realistic access-log lines (default 7) into a directory:

- normal external traffic from real IPs that resolve in GeoLite2 when geoip/*.mmdb
  files are present (89.233.90.76 DE/EWE-Tel — the project's actual top external IP,
  8.8.8.8 US/Google, 1.1.1.1 Cloudflare), plus LAN and CGNAT traffic with a
  mobile/desktop User-Agent mix (cgnat + mobile chips on the top-offenders card)
- in the last closed hour: the full alert scenario from fake_traffic.py — all five
  rules fire, so one alert run populates anomaly_findings and ip_enrichment
- an ongoing hot_ip pattern over the last ~50 minutes, keeping the rolling /anomalies
  card (and the top-offenders 5m/1h tabs) populated no matter when you look
- malformed lines and ACME/TLS noise sprinkled through every file, exercising the
  ingest skip path (skipped, not errors)

Timestamps are anchored to "now" at generation time with a fixed seed — identical
pattern on every run, but always fresh: the scenario lands in the last closed hour,
exactly the window alert.py evaluates by default. That is what lets dev_dashboard.sh
produce a fully populated dashboard (every card) in one command without server access.

    uv run python dev_fixtures.py --out /tmp/caddyloganalyzer/fixtures

copy_logs.sh remains available as a spot check against the real production format.
"""

import argparse
import json
import random
from datetime import datetime, timedelta
from pathlib import Path

from fake_traffic import DESKTOP_UA, ANDROID_UA, SCENARIO, build_scenario

SEED = 42
DEFAULT_DAYS = 7
EVENTS_PER_DAY = 300

# Real external IPs (weights) — all present in GeoLite2 data, so country/ASN chips
# appear on the top-offenders card as soon as geoip/*.mmdb files exist.
EXTERNAL_POOL = [("89.233.90.76", 5), ("8.8.8.8", 3), ("1.1.1.1", 2)]
LAN_POOL = ["192.168.0.40", "192.168.0.5"]
CGNAT_IP = "100.64.20.30"          # RFC 6598 shared carrier NAT -> cgnat chip + mobile UA share
HOSTS = ["navidrome.pimpelkram.com", "pimpelkram.com", "dav.pimpelkram.com"]
URIS = ["/app", "/browser", "/ping", "/api/status", "/remote.php"]


def _caddy_line(ts: float, ip: str, host: str, uri: str, status: int, size: int, ua: str) -> str:
    """One realistic Caddy access-log JSON line (shape matches ingest.to_row)."""
    return json.dumps({
        "level": "info", "ts": ts, "logger": "http.log.access", "msg": "handled request",
        "request": {
            "remote_ip": ip, "client_ip": ip, "proto": "HTTP/1.1",
            "method": "POST" if uri == "/remote.php" else "GET",
            "host": host, "uri": uri,
            "headers": {"User-Agent": [ua]},          # Caddy encodes headers as arrays
            "tls": {"resumed": False, "version": 772, "cipher_suite": 4865, "server_name": host},
        },
        "bytes_read": 0, "user_id": "", "duration": round(0.001 + (ts % 1), 6),
        "size": size, "status": status,
    })


def _tls_noise_line(ts: float) -> str:
    """A valid Caddy entry that is NOT an access event — ingest counts it as skipped."""
    return json.dumps({"level": "info", "ts": ts, "logger": "http.tls",
                       "msg": "obtained certificate", "identifier": "pimpelkram.com"})


def generate(out_dir: Path, days: int = DEFAULT_DAYS, now: datetime | None = None) -> dict:
    """Write `days` of fixture logs into out_dir; returns a summary dict."""
    now = now or datetime.now()
    rng = random.Random(SEED)

    today = now.date()
    day_starts = [datetime.combine(today - timedelta(days=days - 1 - i), datetime.min.time())
                  for i in range(days)]
    by_file: dict[str, list[tuple[float, str]]] = {}

    def add(ts: float, line: str) -> None:
        name = "access.log" if datetime.fromtimestamp(ts).date() == today else \
            f"access-{datetime.fromtimestamp(ts):%Y-%m-%d}.log"
        by_file.setdefault(name, []).append((ts, line))

    # --- normal traffic -------------------------------------------------------
    for start in day_starts:
        end = now if start.date() == today else start + timedelta(days=1)
        n = EVENTS_PER_DAY if start.date() != today else max(60, int(EVENTS_PER_DAY * (end - start).total_seconds() / 86400))
        for _ in range(n):
            ts = start.timestamp() + rng.random() * (end - start).total_seconds()
            ip = rng.choices([ip for ip, _ in EXTERNAL_POOL] + LAN_POOL + [CGNAT_IP],
                             weights=[w for _, w in EXTERNAL_POOL] + [2, 1, 2])[0]
            ua = DESKTOP_UA
            r = rng.random()
            if ip == CGNAT_IP and r < 0.5:
                ua = ANDROID_UA
            elif r < 0.68:
                ua = ANDROID_UA
            elif r < 0.73:
                ua = "curl/8.5.0"
            status = rng.choices([200, 301, 404], weights=[96, 2, 2])[0]
            size = rng.randint(50, 8000) if status == 200 else rng.randint(4, 300)
            add(ts, _caddy_line(ts, ip, rng.choice(HOSTS), rng.choice(URIS), status, size, ua))

    # --- alert scenario in the last closed hour (reuses fake_traffic.py) ------
    ws = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)   # last closed hour
    SCENARIO.clear(); build_scenario()     # build_scenario() extends the module-level list; reset first
    for ip, host, uri, status, size, ua, off in SCENARIO:
        add(ws.timestamp() + off, _caddy_line(ws.timestamp() + off, ip, host, uri, status, size, ua))

    # --- ongoing incident in the current partial hour ---------------------------
    # The rolling /anomalies card only sees [now-1h, now]; a light hot_ip pattern spread
    # over the last ~50 minutes keeps it lit no matter when you look (and shows up on
    # the top-offenders 5m/1h tabs). It stays OUT of the closed scenario hour, so the
    # persisted findings are unaffected.
    ongoing_ip = "203.0.113.77"
    for i in range(55):
        ts = now.timestamp() - 50 * 60 + i * 54          # ends ~90 s ago; always inside [now-1h, now]
        add(ts, _caddy_line(ts, ongoing_ip, "pimpelkram.com", "/ping", 200, 4, DESKTOP_UA))

    # --- noise: malformed line + ACME/TLS entries per file ---------------------
    for name, lines in by_file.items():
        base_ts = min(ts for ts, _ in lines)
        lines.append((base_ts + 0.5, '{"level":"info","ts":1234'))              # truncated JSON -> skipped
        lines.append((base_ts + 0.6, _tls_noise_line(base_ts + 0.6)))          # non-access entry -> skipped

    out_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, lines in sorted(by_file.items()):
        lines.sort(key=lambda x: x[0])
        (out_dir / name).write_text("\n".join(line for _, line in lines) + "\n", encoding="utf-8")
        files[name] = len(lines)

    return {"files": files, "events": sum(files.values()),
            "window_start": ws.isoformat(timespec="seconds")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="/tmp/caddyloganalyzer/fixtures", help="Target log directory.")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help=f"Days of history to generate (default: %(default)s).")
    args = parser.parse_args()

    summary = generate(Path(args.out), days=args.days)
    print(f"fixtures: {summary['events']} lines in {len(summary['files'])} file(s) -> {args.out}")
    for name, n in sorted(summary["files"].items()):
        print(f"  {name}: {n} lines")
    print(f"scenario window (last closed hour): {summary['window_start']} — alert.py evaluates this by default.")


if __name__ == "__main__":
    main()
