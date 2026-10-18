"""Hourly anomaly evaluation over closed 1-hour windows.

Intended to run periodically (see systemd/caddylog-alert.timer). Each run:
  1. evaluates the detection rules below over the last fully elapsed hour
     (the window [floor(now, hour) - 1h, floor(now, hour)) — never partial data)
  2. persists findings into anomaly_findings (history survives the rolling
     /anomalies endpoint, which only sees its own sliding window)
  3. pushes warn/critical findings to ntfy; the same kind+entity is not
     re-pushed within NOTIFY_DEDUP_HOURS so an ongoing incident doesn't page
     every hour
  4. on critical IP findings: blocks the IP at the Caddy edge via the admin API

Rules are deliberately simple and explainable — every finding carries a
human-readable reason string that the dashboard and push notifications show
verbatim. Thresholds live in the constants below; tune them there, not in SQL.

Auto-ban caveat: Caddy admin-API changes are runtime-only — they do NOT survive
a caddy restart/reload (config reverts to the Caddyfile). Blocks therefore last
until expiry or until Caddy restarts; a still-active attacker simply triggers a
fresh finding and gets blocked again on the next hourly run.
"""

import argparse
import ipaddress
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

from init_duckdb import connect_with_retry, initialize_database

# --- detection thresholds -----------------------------------------------------
HOT_IP_THRESHOLD = 50            # external requests in one closed hour -> info finding
FIRST_SIGHT_MIN_REQUESTS = 100   # first-ever event + this many in the first hour -> warn
AUTH_FAILURE_MIN = 3             # 401s from one IP on a host before success counts as breach
AUTH_SUCCESS_MIN = 3             # successful requests required after the failures (one stray 2xx is noise)
EXFIL_MIN_RESPONSES = 5          # successful responses on sensitive paths with varying sizes -> critical
SERVER_ERROR_WARN = 5            # 5xx per host-hour -> warn push (single blips stay dashboard-only)

# Sensitive-path patterns for the exfiltration rule. Deliberately narrow:
# generic /admin is excluded because homelab admins legitimately use admin UIs
# from mobile data IPs, and SPA fallbacks return fixed-size 200s anyway.
SENSITIVE_PATH_PATTERNS = ("%.env%", "%/wp-%", "%/.git/%", "%backup%", "%config%")

NOTIFY_DEDUP_HOURS = 6           # don't re-push the same kind+entity within this window
AUTO_BAN_HOURS = int(os.environ.get("AUTO_BAN_HOURS", "4"))

# --- endpoints (overridable via environment) -----------------------------------
NTFY_URL = os.environ.get("NTFY_URL", "http://127.0.0.1:88")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "caddylog-alerts")
CADDY_ADMIN = os.environ.get("CADDY_ADMIN", "http://127.0.0.1:2019")

SEV_INFO, SEV_WARN, SEV_CRITICAL = "info", "warn", "critical"
NTFY_PRIORITY = {SEV_WARN: 3, SEV_CRITICAL: 5}   # ntfy priorities; >=4 pages harder on phones

# External traffic only — LAN (192.168.*) is trusted by design of this setup.
EXT = "remote_ip IS NOT NULL AND remote_ip NOT LIKE '192.168%'"


def window_bounds(now: datetime) -> tuple[datetime, datetime]:
    """The last fully elapsed hour: [floor(now, hour) - 1h, floor(now, hour))."""
    we = now.replace(minute=0, second=0, microsecond=0)
    return we - timedelta(hours=1), we


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


# --- detection rules -----------------------------------------------------------
# Each rule takes (con, ws, we) and yields dicts with keys:
# kind, severity, entity, value, baseline, reason.

def rule_volume(con, ws, we):
    """hot_ip / sustained_attack / first_sight_spike from per-IP hourly counts."""
    prev_ws = ws - timedelta(hours=1)
    rows = con.execute(
        f"""
        SELECT ip.remote_ip, ip.c, COALESCE(prev.c, 0),
               (SELECT min(event_ts) FROM access_events WHERE remote_ip = ip.remote_ip) AS first_seen
        FROM (SELECT remote_ip, count(*) c
              FROM access_events
              WHERE event_ts >= ? AND event_ts < ? AND {EXT}
              GROUP BY 1 HAVING count(*) > ?) ip
        LEFT JOIN (SELECT remote_ip, count(*) c
                   FROM access_events
                   WHERE event_ts >= ? AND event_ts < ? AND {EXT}
                   GROUP BY 1) prev USING (remote_ip)
        """,
        [ws, we, HOT_IP_THRESHOLD, prev_ws, ws],
    ).fetchall()

    findings = []
    for ip, c, prev_c, first_seen in rows:
        if prev_c > HOT_IP_THRESHOLD:
            findings.append({
                "kind": "sustained_attack", "severity": SEV_WARN, "entity": ip,
                "value": float(c), "baseline": float(HOT_IP_THRESHOLD),
                "reason": f"{ip}: {c} external requests this hour and {prev_c} last hour — sustained attack",
            })
        elif first_seen is not None and first_seen >= ws and c >= FIRST_SIGHT_MIN_REQUESTS:
            findings.append({
                "kind": "first_sight_spike", "severity": SEV_WARN, "entity": ip,
                "value": float(c), "baseline": float(FIRST_SIGHT_MIN_REQUESTS),
                "reason": f"{ip}: first-ever request, already {c} in its first hour — automated client",
            })
        else:
            findings.append({
                "kind": "hot_ip", "severity": SEV_INFO, "entity": ip,
                "value": float(c), "baseline": float(HOT_IP_THRESHOLD),
                "reason": f"{ip}: {c} external requests in the hour (threshold {HOT_IP_THRESHOLD})",
            })
    return findings


def rule_auth_breach(con, ws, we):
    """Auth failures followed by repeated success from the same IP on the same host.

    401 only: Caddy's 'Access denied' fallback returns 403 to ALL external
    traffic on LAN-only services by design, so 403 is noise here — a 401 is a
    real auth challenge issued by the application itself.

    Repeated success (>= AUTH_SUCCESS_MIN) is required: CalDAV-style services
    answer every unauthenticated request with 401, and one stray 2xx after a
    burst of 401s (an unauthenticated admin page like radicale's /.web/, an
    SPA fallback) is scanner noise. A real compromise shows the attacker
    actually using the account — many successful requests.
    """
    rows = con.execute(
        f"""
        WITH fails AS (
            SELECT remote_ip, host, count(*) f
            FROM access_events
            WHERE event_ts >= ? AND event_ts < ? AND status = 401 AND {EXT}
            GROUP BY 1, 2 HAVING count(*) >= ?
        ), succ AS (
            SELECT remote_ip, host, count(*) s
            FROM access_events
            WHERE event_ts >= ? AND event_ts < ? AND status BETWEEN 200 AND 299 AND {EXT}
            GROUP BY 1, 2 HAVING count(*) >= ?
        )
        SELECT f.remote_ip, f.host, f.f, s.s
        FROM fails f JOIN succ s USING (remote_ip, host)
        """,
        [ws, we, AUTH_FAILURE_MIN, ws, we, AUTH_SUCCESS_MIN],
    ).fetchall()

    return [{
        "kind": "auth_breach", "severity": SEV_CRITICAL, "entity": ip,
        "value": float(f), "baseline": float(AUTH_FAILURE_MIN),
        "reason": f"{ip}: {f} auth failures (401) then {s} successful requests on {host} — possible credential compromise",
    } for ip, host, f, s in rows]


def rule_exfiltration(con, ws, we):
    """Brand-new IP pulling successful responses of varying sizes off sensitive paths.

    Varying response size is the discriminator: SPA fallbacks and static-file
    scanners return identical byte counts (e.g. one 857-byte index.html for
    every path), while real data exfiltration does not. Grouped per host —
    mixing two different SPA-fallback constants from one IP must not look like
    varying data.
    """
    patterns = " OR ".join(f"uri_path LIKE '{p}'" for p in SENSITIVE_PATH_PATTERNS)
    rows = con.execute(
        f"""
        WITH new_ips AS (   -- IPs whose first-ever event falls inside this window
            SELECT remote_ip FROM access_events WHERE {EXT}
            GROUP BY 1 HAVING min(event_ts) >= ?
        ), resp AS (
            SELECT e.remote_ip, e.host, count(*) n, list(DISTINCT e.uri_path) paths
            FROM access_events e JOIN new_ips USING (remote_ip)
            WHERE e.event_ts >= ? AND event_ts < ? AND e.status BETWEEN 200 AND 299
              AND ({patterns})
            GROUP BY 1, 2 HAVING count(*) >= ? AND min(e.response_size) != max(e.response_size)
        )
        SELECT remote_ip, host, n, paths FROM resp
        """,
        [ws, ws, we, EXFIL_MIN_RESPONSES],
    ).fetchall()

    # one finding per IP: merge hits on multiple hosts into a single reason
    merged = {}
    for ip, host, n, paths in rows:
        m = merged.setdefault(ip, {"n": 0, "hosts": []})
        m["n"] += n
        m["hosts"].append(f"{host} ({', '.join(paths[:2])})")
    return [{
        "kind": "exfiltration", "severity": SEV_CRITICAL, "entity": ip,
        "value": float(m["n"]), "baseline": float(EXFIL_MIN_RESPONSES),
        "reason": f"{ip}: {m['n']} successful responses with varying sizes on sensitive paths ({'; '.join(m['hosts'])}) from a brand-new IP",
    } for ip, m in merged.items()]


def rule_server_errors(con, ws, we):
    """5xx bursts per host — a service struggling or crashing under load."""
    rows = con.execute(
        f"""
        SELECT COALESCE(host, '(no host)'), count(*) c
        FROM access_events
        WHERE event_ts >= ? AND event_ts < ? AND status >= 500
        GROUP BY 1 HAVING count(*) >= ?
        """,
        [ws, we, SERVER_ERROR_WARN],
    ).fetchall()

    return [{
        "kind": "server_errors", "severity": SEV_WARN, "entity": host,
        "value": float(c), "baseline": float(SERVER_ERROR_WARN),
        "reason": f"{host}: {c} server errors (5xx) in the hour — service struggling or under attack",
    } for host, c in rows]


RULES = (rule_volume, rule_auth_breach, rule_exfiltration, rule_server_errors)


def evaluate_window(con, ws: datetime, we: datetime) -> list[dict]:
    findings = []
    for rule in RULES:
        try:
            findings.extend(rule(con, ws, we))
        except duckdb.Error as e:
            print(f"error: rule {rule.__name__} failed: {e}", file=sys.stderr)
    return findings


# --- persistence ----------------------------------------------------------------

def persist_findings(con, ws: datetime, findings: list[dict]) -> None:
    """INSERT OR IGNORE on the (window_start, kind, entity) unique key —
    re-running a window never duplicates rows."""
    for f in findings:
        con.execute(
            "INSERT OR IGNORE INTO anomaly_findings"
            " (window_start, kind, severity, entity, value, baseline, reason)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [ws, f["kind"], f["severity"], f["entity"], f["value"], f["baseline"], f["reason"]],
        )


def finding_id(con, ws: datetime, kind: str, entity: str) -> int | None:
    row = con.execute(
        "SELECT finding_id FROM anomaly_findings WHERE window_start = ? AND kind = ? AND entity = ?",
        [ws, kind, entity],
    ).fetchone()
    return row[0] if row else None


# --- notification ---------------------------------------------------------------

def already_notified(con, kind: str, entity: str) -> bool:
    """Dedupe ongoing incidents: same kind+entity pushed recently?"""
    row = con.execute(
        "SELECT max(notified_at) FROM anomaly_findings"
        " WHERE kind = ? AND entity = ? AND notified_at IS NOT NULL",
        [kind, entity],
    ).fetchone()
    return bool(row and row[0] is not None
                and datetime.now() - row[0] < timedelta(hours=NOTIFY_DEDUP_HOURS))


def ntfy_push(title: str, body: str, priority: int) -> bool:
    req = urllib.request.Request(
        f"{NTFY_URL}/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        method="POST",
        headers={"X-Title": title, "Priority": str(priority)},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as e:   # a push hiccup must not kill the job
        print(f"error: ntfy push failed ({NTFY_URL}/{NTFY_TOPIC}): {e}", file=sys.stderr)
        return False


def notify(con, ws: datetime, we: datetime, f: dict, action_taken: str | None) -> bool:
    """Push warn/critical findings; returns True if a push was sent."""
    if f["severity"] == SEV_INFO or already_notified(con, f["kind"], f["entity"]):
        return False
    body = f["reason"] + f"\nwindow: {ws:%Y-%m-%d %H:%M}–{we:%H:%M}"
    if action_taken:
        body += f"\nauto-ban: {action_taken}"
    ok = ntfy_push(f"[{f['severity'].upper()}] {f['kind']}: {f['entity']}", body, NTFY_PRIORITY[f["severity"]])
    if ok:
        fid = finding_id(con, ws, f["kind"], f["entity"])
        if fid is not None:
            con.execute("UPDATE anomaly_findings SET notified_at = now() WHERE finding_id = ?", [fid])
    return ok


# --- auto-ban via Caddy admin API -------------------------------------------------

def caddy_request(method: str, path: str, body=None):
    req = urllib.request.Request(
        f"{CADDY_ADMIN}{path}",
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:   # surface Caddy's validation message for debugging
        raise RuntimeError(
            f"Caddy admin API {method} {path} -> HTTP {e.code}: "
            f"{e.read().decode('utf-8', 'replace')}"
        ) from None


def caddy_main_server() -> str | None:
    """Name of the HTTP server listening on :443 (the public reverse proxy)."""
    servers = caddy_request("GET", "/config/apps/http/servers") or {}
    for name, srv in servers.items():
        if any(":443" in str(l) for l in (srv.get("listen") or [])):
            return name
    return next(iter(servers), None)


def _is_block_route(route: dict) -> bool:
    """Our auto-ban route: a single remote_ip range matcher + silent 444 static_response.
    Deliberately strict so it can never be confused with site routes that use IP
    matchers for other purposes (e.g. the @local LAN-only rules)."""
    if (route.get("handle") or []) != [{"handler": "static_response", "status_code": 444}]:
        return False
    match = route.get("match") or []
    return len(match) == 1 and isinstance(
        (match[0].get("remote_ip") or {}).get("ranges"), list)


def caddy_blocked_ips() -> set[str]:
    """IPs currently blocked by the auto-ban route on the public server."""
    try:
        routes = caddy_request(
            "GET", f"/config/apps/http/servers/{caddy_main_server()}/routes") or []
    except Exception:
        return set()
    ips = set()
    for route in routes:
        if not _is_block_route(route):
            continue
        for rng in route["match"][0]["remote_ip"]["ranges"]:
            ip, _, cidr = rng.partition("/")
            if not cidr or cidr == "32":   # only exact IPs we wrote ourselves
                ips.add(ip)
    return ips


def caddy_set_blocked(ips: set[str]) -> None:
    """Replace the auto-ban route's IP list; an empty set removes the route.

    The route is prepended to the server's routes so it evaluates before any site
    routing (edge block). Applied as an atomic full-config load (POST /config/):
    Caddy validates first and keeps running the old config if ours is malformed,
    so a bad payload can never take the proxy down. Admin-API changes are
    runtime-only — they do not survive a caddy restart."""
    server = caddy_main_server()
    if not server:
        raise RuntimeError("no Caddy HTTP server found")

    cfg = caddy_request("GET", "/config/") or {}
    routes = (((cfg.get("apps") or {}).get("http") or {}).get("servers") or {}) \
             .get(server, {}).get("routes") or []

    others = [r for r in routes if not _is_block_route(r)]
    new_routes = (
        [{"match": [{"remote_ip": {"ranges": [f"{ip}/32" for ip in sorted(ips)]}}],
          # Caddy >=2.11 renamed the old 'respond' handler to static_response (status_code field)
          "handle": [{"handler": "static_response", "status_code": 444}]}] + others
        if ips else others
    )
    # sanity: we must never drop or duplicate a site route
    assert len(new_routes) == len(others) + (1 if ips else 0) \
        and all(r in new_routes for r in others), "route list changed unexpectedly"

    cfg["apps"]["http"]["servers"][server]["routes"] = new_routes
    caddy_request("POST", "/config/", cfg)   # atomic load; old config kept on validation failure


def load_blocks(blocks_path: Path) -> dict:
    try:
        return json.loads(blocks_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_blocks(blocks_path: Path, blocks: dict) -> None:
    blocks_path.write_text(json.dumps(blocks, indent=2), encoding="utf-8")


def reconcile_expired(blocks_path: Path) -> None:
    """Drop expired blocks from Caddy and the state file (self-healing)."""
    blocks = load_blocks(blocks_path)
    now = datetime.now()
    expired = [ip for ip, meta in blocks.items() if datetime.fromisoformat(meta["expires"]) <= now]
    if not expired:
        return
    current = caddy_blocked_ips()
    live = current - set(expired)
    if live != current:
        caddy_set_blocked(live)
    for ip in expired:
        del blocks[ip]
    save_blocks(blocks_path, blocks)
    print(f"  auto-ban: expired {', '.join(sorted(expired))}")


def ensure_block(con, ws: datetime, f: dict, blocks_path: Path) -> str | None:
    """Block a critical finding's IP for AUTO_BAN_HOURS; returns action label."""
    try:
        ipaddress.ip_address(f["entity"])
    except ValueError:
        return None   # not an IP (e.g. host-based finding) — nothing to block at the edge

    blocks = load_blocks(blocks_path)
    now = datetime.now()
    meta = blocks.get(f["entity"])
    if meta and datetime.fromisoformat(meta["expires"]) > now:
        return None   # already blocked, still within its window

    expires = now + timedelta(hours=AUTO_BAN_HOURS)
    caddy_set_blocked(caddy_blocked_ips() | {f["entity"]})
    blocks[f["entity"]] = {
        "blocked_at": iso(now),
        "expires": iso(expires),
        "finding_id": finding_id(con, ws, f["kind"], f["entity"]),
    }
    save_blocks(blocks_path, blocks)
    return f"blocked_ip_{AUTO_BAN_HOURS}h (until {expires:%Y-%m-%d %H:%M})"


# --- main ------------------------------------------------------------------------

def run(db_path: Path, state_path: Path, blocks_path: Path,
        window_start: datetime | None, do_notify: bool, do_ban: bool) -> None:
    initialize_database(db_path)
    now = datetime.now()
    if window_start is not None:
        ws, we = window_start, window_start + timedelta(hours=1)
        mode = f"backfill {iso(ws)}–{iso(we)}"
    else:
        ws, we = window_bounds(now)
        state = {}
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        if state.get("last_window_end") == iso(we):
            print(f"alert: window {ws:%Y-%m-%d %H:%M}–{we:%H:%M} already evaluated — nothing to do")
            return
        mode = "hourly"

    con = connect_with_retry(db_path)
    try:
        findings = evaluate_window(con, ws, we)
        persist_findings(con, ws, findings)

        pushed = blocked = 0
        for f in sorted(findings, key=lambda x: (x["severity"] != SEV_CRITICAL, x["kind"])):
            action = ensure_block(con, ws, f, blocks_path) \
                if f["severity"] == SEV_CRITICAL and do_ban else None
            if action:
                blocked += 1
                fid = finding_id(con, ws, f["kind"], f["entity"])
                if fid is not None:
                    con.execute("UPDATE anomaly_findings SET action_taken = ? WHERE finding_id = ?", [action, fid])
            if do_notify and notify(con, ws, we, f, action):
                pushed += 1

        reconcile_expired(blocks_path)
        con.execute("CHECKPOINT")   # force WAL flush before the checkpoint file advances

        if window_start is None:
            state_path.write_text(json.dumps({"last_window_end": iso(we)}), encoding="utf-8")

        print(f"alert: {mode}, {len(findings)} finding(s) persisted, "
              f"{pushed} pushed, {blocked} blocked")
        for f in findings:
            mark = {"info": "  ", "warn": "! ", "critical": "**"}[f["severity"]]
            print(f"  {mark}[{f['severity']}] {f['kind']} {f['entity']} — {f['reason']}")
    finally:
        con.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate anomaly rules over the last closed hour, persist findings,"
                    " push warn/critical to ntfy, auto-ban critical IPs at the Caddy edge.")
    parser.add_argument("--db", default="/var/lib/caddylog/access.duckdb",
                        help="Path to the DuckDB database file (default: %(default)s)")
    parser.add_argument("--state", default=None,
                        help="Checkpoint file path (default: <db>.alert_state.json)")
    parser.add_argument("--blocks", default=None,
                        help="Auto-ban state file path (default: <db>.blocks.json)")
    parser.add_argument("--window-start", default=None, metavar="ISO",
                        help="Evaluate this specific hour instead of the last closed one"
                             " (e.g. 2026-10-18T03:00); does not touch the checkpoint")
    parser.add_argument("--no-notify", action="store_true",
                        help="Skip ntfy pushes (findings are still persisted)")
    parser.add_argument("--no-ban", action="store_true",
                        help="Skip Caddy admin-API changes (blocks file is not written either)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    db_path = Path(args.db)
    state_path = Path(args.state) if args.state else Path(str(db_path) + ".alert_state.json")
    blocks_path = Path(args.blocks) if args.blocks else Path(str(db_path) + ".blocks.json")
    window_start = datetime.fromisoformat(args.window_start) if args.window_start else None

    try:
        run(db_path, state_path, blocks_path, window_start,
            do_notify=not args.no_notify, do_ban=not args.no_ban)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
