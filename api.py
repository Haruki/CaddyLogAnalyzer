"""Read-only web API over the access_events DuckDB database.

Serves aggregate statistics for fixed time windows (5m / 1h / 1d / 30d / year),
anomaly hints worth investigating, and raw event rows for drill-down. Runs as a
long-lived service next to the periodic ingest:

    uv run uvicorn api:app --host 127.0.0.1 --port 8377
    # or, for quick dev runs:
    uv run python api.py

Design notes
------------
- All queries are computed ON THE FLY from the raw access_events table — no
  caching, no materialized summary tables. Measured on real data: every window
  aggregate takes single-digit milliseconds even over a full year of events
  (DuckDB is a columnar OLAP engine and this is its home turf). Revisit only
  if queries start taking >~1 s at much larger scale.
- The service opens the database READ-ONLY with a fresh connection per request.
  Across processes, DuckDB allows one writer plus any number of readers on the
  same file (verified), so this coexists cleanly with ingest.py's periodic
  read-write runs: a reader never blocks on the writer and simply sees the last
  committed snapshot. Per-request connections also sidestep DuckDB's rule that
  all connections inside ONE process must share the first connection's mode —
  we therefore never hold a second, differently-configured connection here.
- Window bounds use DuckDB's now() in SQL so "last hour" always means relative
  to server time; events carry the same server clock Caddy wrote them with.
"""

import asyncio
import ipaddress
import json
import os
import socket
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Database location: overridable via env so the same code runs in dev (point at
# a local sample DB) and in the caddylog-api container on the Caddy host (default below).
DB_PATH = Path(os.environ.get("CADDYLOG_DB", "/var/lib/caddylog/access.duckdb"))

PORT = 8377
# Instance identity for multi-server deployments: which server this dashboard
# belongs to (shown in the page header and browser tab title) plus the image
# version it runs. HOSTNAME falls back to the container hostname; VERSION is
# baked into the image at build time by CI (Dockerfile ARG), "dev" locally.
HOSTNAME = os.environ.get("CADDYLOG_HOSTNAME") or socket.gethostname()
VERSION = os.environ.get("CADDYLOG_VERSION", "dev")

STREAM_TICK_SECONDS = 5   # /stream fingerprint check interval (see stream() below)

# Tunables for anomaly detection. Deliberately simple, explainable rules — each
# finding carries a human-readable reason string that the UI can show verbatim.
HOT_IP_THRESHOLD = 50      # external requests per hour that count as "hot"
ZSCORE_LIMIT = 3.0         # daily volume this many std-devs above the 30d mean ...
ZSCORE_MIN_DAYS = 7        # ... but only for IPs seen on at least this many days

# Rolling windows: name -> (lower-bound interval, SQL expression producing one
# timeseries bucket, series step, series start). Bucket sizes are chosen so every
# window yields roughly a dozen to ~40 points. The timeseries query zero-fills
# EVERY bucket between start and end (generate_series + LEFT JOIN), so hours with
# no traffic render as flat zeros instead of vanishing — otherwise the chart line
# would jump across gaps as if they were continuous. ("year" is not in this dict:
# it takes an optional ?year= parameter, resolved in stats().)
WINDOWS: dict[str, tuple[str, str, str, str]] = {
    "5m":  ("INTERVAL '5 minutes'", "time_bucket(INTERVAL '1 minute', event_ts)",   "INTERVAL '1 minute'",  "time_bucket(INTERVAL '1 minute', now() - INTERVAL '5 minutes')"),
    "1h":  ("INTERVAL '1 hour'",    "time_bucket(INTERVAL '5 minutes', event_ts)",  "INTERVAL '5 minutes'", "time_bucket(INTERVAL '5 minutes', now() - INTERVAL '1 hour')"),
    # date_trunc (not time_bucket) for day units: calendar-aligned to local
    # midnight, while time_bucket would align to UTC midnight.
    "1d":  ("INTERVAL '1 day'",     "date_trunc('hour', event_ts)",                 "INTERVAL '1 hour'",    "date_trunc('hour', now() - INTERVAL '1 day')"),
    "30d": ("INTERVAL '30 days'",   "date_trunc('day', event_ts)",                  "INTERVAL '1 day'",     "date_trunc('day', now() - INTERVAL '30 days')"),
}

# Bucket ladder for arbitrary ranges (click-to-zoom): the finest bucket that keeps
# the point count widget-sized (~10-45). Mirrors the preset windows exactly, so a
# drilled range and its preset twin render identically. The dashboard mirrors this
# in JS (static/app.js) to decide whether a clicked bucket can zoom further — keep in sync.
BUCKET_LADDER: tuple[tuple[float, str, str], ...] = (
    # max span seconds -> (bucket expression, step interval)
    (10 * 60,        "time_bucket(INTERVAL '1 minute', event_ts)",  "INTERVAL '1 minute'"),
    (2 * 3600,       "time_bucket(INTERVAL '5 minutes', event_ts)", "INTERVAL '5 minutes'"),
    (30 * 3600,      "date_trunc('hour', event_ts)",                "INTERVAL '1 hour'"),
    (45 * 86400,     "date_trunc('day', event_ts)",                 "INTERVAL '1 day'"),
)
_MONTH_BUCKET = ("date_trunc('month', event_ts)", "INTERVAL '1 month'")   # anything longer


def _bucket_for(span_seconds: float) -> tuple[str, str]:
    for max_span, bucket, step in BUCKET_LADDER:
        if span_seconds <= max_span:
            return bucket, step
    return _MONTH_BUCKET

# Fixed bucket sizes in ms (calendar months are None — their length varies).
STEP_MS = {
    "INTERVAL '1 minute'": 60_000, "INTERVAL '5 minutes'": 300_000,
    "INTERVAL '1 hour'": 3_600_000, "INTERVAL '1 day'": 86_400_000,
}

app = FastAPI(
    title="CaddyLog API",
    description="Statistics and anomaly hints over Caddy access events (read-only).",
)

# The homepage.dev widget and the dashboard page call this API cross-origin from
# the browser. On a LAN-only service bound to 127.0.0.1, allowing all origins is
# acceptable; tighten to explicit origins if it ever gets exposed wider.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"]
)


@contextmanager
def db():
    """Yield a read-only DuckDB connection, opened per request and always closed.

    Per-request (instead of one app-lifetime connection) keeps the service
    stateless — any uvicorn worker thread can serve any request — and avoids
    holding a connection across ingest runs. Opening a read-only connection
    costs milliseconds; at dashboard poll rates that is noise next to the
    query itself.
    """
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        yield con
    finally:
        con.close()


def _since(window: str) -> str:
    """SQL predicate bounding a rolling window to 'now minus its interval'."""
    return f"event_ts >= now() - {WINDOWS[window][0]}"


# Floor a datetime onto its bucket boundary. Whole-hour UTC offsets (every EU
# timezone) keep epoch- and wall-clock alignment identical, so truncating the
# local fields matches time_bucket()/date_trunc() exactly.
def _floor_to_bucket(dt: datetime, step: str) -> datetime:
    if "month" in step:
        return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if "day" in step:
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if "hour" in step:
        return dt.replace(minute=0, second=0, microsecond=0)
    minutes = 5 if "5 minutes" in step else 1
    return dt.replace(second=0, microsecond=0, minute=(dt.minute // minutes) * minutes)


def _parse_range(from_: str | None, to: str | None) -> tuple[datetime, datetime] | None:
    """Validate the optional from/to pair (naive server-local ISO timestamps)."""
    if (from_ is None) != (to is None):
        raise HTTPException(400, "'from' and 'to' must be given together")
    if from_ is None:
        return None
    try:
        f = datetime.fromisoformat(from_)
        t = datetime.fromisoformat(to)
    except ValueError:
        raise HTTPException(400, "from/to must be ISO timestamps (e.g. 2026-08-01T00:00:00)")
    if f.tzinfo is not None or t.tzinfo is not None:
        raise HTTPException(400, "offset-bearing timestamps are not supported; send server-local naive ISO")
    if t <= f:
        raise HTTPException(400, "'to' must be after 'from'")
    if (t - f).total_seconds() > 730 * 86400:   # keep the chart at ~24 points or fewer
        raise HTTPException(400, "range too large; maximum span is 2 years")
    return f, t


# ---------------------------------------------------------------------------
# Response models — the JSON contract for dashboard, homepage widget and bar tool
# ---------------------------------------------------------------------------

class HostCount(BaseModel):
    host: str | None
    count: int


class StatsResponse(BaseModel):
    window: str | None                 # preset name, null in from/to range mode
    bucket_ms: int | None              # timeseries bucket size; null for calendar months
    total_requests: int
    latest_event: str | None          # newest event in the window (ISO), null if empty
    status_breakdown: dict[int, int]   # HTTP status -> request count
    top_hosts: list[HostCount]
    timeseries: list[tuple[str, int]]  # (bucket start as ISO timestamp, count)


class Anomaly(BaseModel):
    entity: str | None                 # IP or host the finding is about
    kind: str                         # "hot_ip" | "volume_outlier" | "server_errors"
    value: float                      # observed metric (requests / z-score / errors)
    baseline: float | None            # what normal looks like, if a rule has one
    reason: str                       # human-readable explanation for the UI
    country_code: str | None = None   # from ip_enrichment when entity is an IP
    asn_org: str | None = None        # e.g. 'Telekom Deutschland GmbH' - DE residential IPs usually resolve to the ISP itself


class FindingRow(BaseModel):
    window_start: str                 # ISO timestamp of the closed hour evaluated
    kind: str                         # "hot_ip" | "first_sight_spike" | ... (see alert.py)
    severity: str                     # "info" | "warn" | "critical"
    entity: str                       # IP or host the finding is about
    value: float | None               # metric that fired, if the rule has one
    reason: str                       # human-readable; shown verbatim in UI and push notifications
    action_taken: str | None          # automated containment performed (e.g. "blocked_ip_4h")
    notified_at: str | None           # when the ntfy push was sent, if any
    country_code: str | None = None   # from ip_enrichment when entity is an IP
    asn_org: str | None = None        # e.g. 'Telekom Deutschland GmbH' - DE residential IPs usually resolve to the ISP itself

class EventRow(BaseModel):
    event_ts: str                     # ISO timestamp
    remote_ip: str | None
    host: str | None
    method: str | None
    uri_path: str | None
    status: int | None
    response_size: int | None


class EventsResponse(BaseModel):
    window: str | None                 # preset name, null in from/to range mode
    count: int                        # rows returned (capped by limit)
    events: list[EventRow]            # newest first


class TopIpRow(BaseModel):
    ip: str
    requests: int                     # total in the window
    first_seen: str | None            # ISO; when this IP first appears within the window
    last_seen: str | None             # ISO
    country_code: str | None          # from ip_enrichment (NULL until GeoLite2 files exist)
    country_name: str | None
    asn: int | None
    asn_org: str | None               # for DE residential IPs usually the ISP itself
    network_type: str                 # 'cgnat' (shared carrier NAT, RFC 6598) | 'public'
    mobile_ua_share: float            # 0.0-1.0 share of this IP's requests with a phone User-Agent


class TopIpsResponse(BaseModel):
    window: str | None                 # preset name, null in from/to range mode
    count: int                        # rows returned (capped by limit)
    ips: list[TopIpRow]               # most requests first


class StatusResponse(BaseModel):
    ok: bool                          # no anomaly findings right now
    requests_last_hour: int
    errors_last_hour: int             # status >= 400
    anomalies: int                    # number of open findings (see /anomalies)
    hostname: str                     # which server this instance runs on (CADDYLOG_HOSTNAME, else container hostname)
    version: str                      # image version baked in at build time (git tag/SHA via CI, "dev" locally)


# ---------------------------------------------------------------------------
# Anomaly rules — each is one plain SQL query over the raw events
# ---------------------------------------------------------------------------

def _hot_ips(con) -> list[Anomaly]:
    """External IPs whose last-hour volume exceeds HOT_IP_THRESHOLD.

    LAN addresses are excluded because they are the homelab itself. A fixed
    threshold is a v1 rule: if it produces too many (or too few) findings in
    practice, replace with per-IP baselines as _volume_outliers does.
    """
    rows = con.execute(
        "SELECT remote_ip, count(*) c FROM access_events"
        " WHERE event_ts >= now() - INTERVAL '1 hour' AND remote_ip NOT LIKE '192.168%'"
        " GROUP BY 1 HAVING c > ? ORDER BY c DESC",
        [HOT_IP_THRESHOLD],
    ).fetchall()
    return [
        Anomaly(
            entity=ip, kind="hot_ip", value=float(c), baseline=float(HOT_IP_THRESHOLD),
            reason=f"{c} requests in the last hour (threshold: {HOT_IP_THRESHOLD})",
        )
        for ip, c in rows
    ]


def _volume_outliers(con) -> list[Anomaly]:
    """IPs whose daily volume today is an outlier vs their own 30-day history.

    Z-score per IP against its trailing per-day counts. The minimum-history
    guard (ZSCORE_MIN_DAYS) keeps one-off visitors from triggering it — a new
    IP with 40 requests in a day has no baseline yet, so it is not "unusual".
    """
    rows = con.execute(
        """WITH base AS (
               SELECT remote_ip, date_trunc('day', event_ts) d, count(*) c
               FROM access_events
               WHERE event_ts >= now() - INTERVAL '30 days' AND remote_ip NOT LIKE '192.168%'
               GROUP BY 1, 2
           ), stats AS (
               SELECT remote_ip, avg(c) mu, stddev(c) sd
               FROM base GROUP BY 1 HAVING count(*) >= ?
           )
           SELECT b.remote_ip, b.c, s.mu, (b.c - s.mu) / NULLIF(s.sd, 0) z
           FROM base b JOIN stats s USING (remote_ip)
           WHERE b.d = date_trunc('day', now()) AND (b.c - s.mu) / NULLIF(s.sd, 0) > ?
           ORDER BY z DESC LIMIT 10""",
        [ZSCORE_MIN_DAYS, ZSCORE_LIMIT],
    ).fetchall()
    return [
        Anomaly(
            entity=ip, kind="volume_outlier", value=float(z), baseline=float(mu),
            reason=f"{c} requests today vs ~{mu:.0f}/day average over the last 30 days (z={z:.1f})",
        )
        for ip, c, mu, z in rows
    ]


def _server_errors(con) -> list[Anomaly]:
    """Any 5xx in the last hour, grouped by host.

    On a homelab any server error is worth a look — no threshold needed; this
    rule exists so a broken backend surfaces on the dashboard immediately.
    """
    rows = con.execute(
        "SELECT host, count(*) FROM access_events"
        " WHERE event_ts >= now() - INTERVAL '1 hour' AND status >= 500"
        " GROUP BY 1 ORDER BY 2 DESC",
    ).fetchall()
    return [
        Anomaly(
            entity=host, kind="server_errors", value=float(c), baseline=None,
            reason=f"{c} server errors (status >= 500) in the last hour",
        )
        for host, c in rows
    ]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/stats", response_model=StatsResponse)
def stats(
    window: str = Query("1h", description="5m | 1h | 1d | 30d | year"),
    year: int | None = Query(None, ge=2000, le=2100,
                            description="calendar year for the 'year' window (default: current)"),
    from_: str | None = Query(None, alias="from",
                             description="range start (naive ISO); with 'to', overrides window — used by click-to-zoom"),
    to: str | None = Query(None, description="range end, exclusive (naive ISO)"),
) -> StatsResponse:
    """Aggregate statistics for one time window or explicit range, computed on the fly.

    One read-only connection, four small queries; each takes single-digit
    milliseconds even over a year of raw events (measured), so there is no
    caching or summary layer in between.
    """
    series_filter = ""      # extra predicate for generate_series output (range mode only)
    rng = _parse_range(from_, to)
    if rng is not None:
        # Explicit range mode (click-to-zoom): bucket size derived from the span,
        # zero-filled across exactly [from, to).
        f, t = rng
        since = f"event_ts >= '{f.isoformat()}' AND event_ts < '{t.isoformat()}'"
        bucket, step = _bucket_for((t - f).total_seconds())
        # generate_series is INCLUSIVE of its end value: when 'to' lands exactly on a
        # bucket boundary that would add one stray empty bucket past the range.
        series_start = f"TIMESTAMP '{_floor_to_bucket(f, step).isoformat()}'"
        series_end = f"TIMESTAMP '{t.isoformat()}'"
        series_filter = f" WHERE g.t < '{t.isoformat()}'"
    elif window == "year":
        # Calendar-year window with monthly buckets: always at most 12 points
        # (fewer for the current, still-in-progress year) no matter how much
        # history exists — which is why this replaced the old unbounded
        # 'all time' window. ?year= selects a past year once more than one
        # year of data has been ingested; the UI picker comes later.
        current = datetime.now().year
        y = year or current
        since = f"event_ts >= make_date({y}, 1, 1) AND event_ts < make_date({y + 1}, 1, 1)"
        bucket = "date_trunc('month', event_ts)"
        step = "INTERVAL '1 month'"
        series_start = f"make_date({y}, 1, 1)"
        # generate_series is INCLUSIVE of its end value: for past years that
        # would add a stray January of the next year, so stop at December.
        series_end = "now()" if y == current else f"make_date({y + 1}, 1, 1) - INTERVAL '1 month'"
    elif window in WINDOWS:
        since = _since(window)
        bucket, step, series_start = WINDOWS[window][1], WINDOWS[window][2], WINDOWS[window][3]
        series_end = "now()"
    else:
        raise HTTPException(400, f"unknown window {window!r}; expected one of {sorted(WINDOWS) + ['year']}")

    with db() as con:
        total, latest = con.execute(
            f"SELECT count(*), max(event_ts) FROM access_events WHERE {since}"
        ).fetchone()
        breakdown = con.execute(
            f"SELECT status, count(*) FROM access_events WHERE {since} GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
        top_hosts = [
            HostCount(host=host or None, count=count)
            for host, count in con.execute(
                f"SELECT host, count(*) c FROM access_events WHERE {since}"
                " GROUP BY 1 ORDER BY c DESC LIMIT 10"
            ).fetchall()
        ]
        timeseries = [
            (bucket_start.isoformat(), count)
            for bucket_start, count in con.execute(
                "SELECT g.t::TIMESTAMP, count(e.t)"
                f" FROM generate_series({series_start}, {series_end}, {step}) AS g(t)"
                f" LEFT JOIN (SELECT {bucket} t FROM access_events WHERE {since}) e ON e.t = g.t"
                f"{series_filter}"
                " GROUP BY 1 ORDER BY 1"
            ).fetchall()
        ]

    return StatsResponse(
        window=None if rng is not None else window,
        bucket_ms=STEP_MS.get(step),
        total_requests=total,
        latest_event=latest.isoformat() if latest is not None else None,
        status_breakdown={status: count for status, count in breakdown if status is not None},
        top_hosts=top_hosts,
        timeseries=timeseries,
    )


@app.get("/anomalies", response_model=list[Anomaly])
def anomalies() -> list[Anomaly]:
    """All anomaly findings.

    The rules are deliberately dumb and explainable (threshold + z-score) so
    the reason string can be shown verbatim in the UI — no black-box scoring.
    Clicking a finding in the dashboard drills into /events with the entity
    pre-filled.
    """
    with db() as con:
        out = _hot_ips(con) + _volume_outliers(con) + _server_errors(con)
    info = _enrich_entities([a.entity for a in out if a.entity])
    for a in out:                     # IP entities get country/ASN context; hosts stay plain
        if a.entity in info:
            a.country_code, a.asn_org = info[a.entity]
    return out


@app.get("/findings", response_model=list[FindingRow])
def findings(days: int = Query(7, ge=1, le=90)) -> list[FindingRow]:
    """Persisted anomaly history written by alert.py over closed 1-hour windows.

    Unlike /anomalies (rolling window — a finding vanishes once its hour has
    passed), rows here survive indefinitely: last night's scan is still visible
    weeks later. The dashboard renders this as the "recent anomalies" card.
    """
    with db() as con:
        try:
            rows = con.execute(
                "SELECT window_start, kind, severity, entity, value, reason,"
                " action_taken, notified_at FROM anomaly_findings"
                " WHERE window_start >= CAST(? AS TIMESTAMP)"
                " ORDER BY window_start DESC LIMIT 200",
                [datetime.now() - timedelta(days=days)],
            ).fetchall()
        except duckdb.CatalogException:   # table absent — alert.py has never run here
            return []

    rows_out = [
        FindingRow(
            window_start=window_start.isoformat(), kind=kind, severity=severity,
            entity=entity, value=value, reason=reason, action_taken=action_taken,
            notified_at=notified_at.isoformat() if notified_at is not None else None,
        )
        for window_start, kind, severity, entity, value, reason, action_taken, notified_at in rows
    ]
    info = _enrich_entities([r.entity for r in rows_out])
    for r in rows_out:                # IP entities get country/ASN context; hosts stay plain
        if r.entity in info:
            r.country_code, r.asn_org = info[r.entity]
    return rows_out

def _enrich_entities(entities: list[str]) -> dict[str, tuple]:
    """ip_enrichment rows for the IP-typed entities (host names are skipped)."""
    ips = []
    for e in entities:
        try:
            ipaddress.ip_address(e)
            ips.append(e)
        except ValueError:
            pass
    if not ips:
        return {}
    with db() as con:
        rows = con.execute(
            "SELECT ip, country_code, asn_org FROM ip_enrichment WHERE ip IN ("
            + ", ".join("?" * len(ips)) + ")", ips).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def _network_type(ip: str, stored: str | None) -> str:
    """CGNAT is decidable from the IP alone (RFC 6598), so an IP not enriched yet still gets it right."""
    if stored:
        return stored
    return "cgnat" if ipaddress.ip_address(ip) in ipaddress.ip_network("100.64.0.0/10") else "public"


@app.get("/top-ips", response_model=TopIpsResponse)
def top_ips(
    window: str | None = Query(None, description="preset window; or use from/to"),
    limit: int = Query(20, ge=1, le=100),
    from_: str | None = Query(None, alias="from",
                             description="range start (naive ISO); with 'to', overrides window"),
    to: str | None = Query(None, description="range end, exclusive (naive ISO)"),
) -> TopIpsResponse:
    """Top external requesters of a window - the dashboard's "top offenders" card.

    Each row carries ip_enrichment context when available (country, ASN org,
    CGNAT flag; written by alert.py for loud IPs only) plus the share of this
    IP's requests that carried a phone User-Agent - the practical answer to
    "is this someone's mobile or a box". LAN traffic is excluded.
    """
    rng = _parse_range(from_, to)
    where: list[str] = ["remote_ip IS NOT NULL", "remote_ip NOT LIKE '192.168%'"]
    params: list = []
    if rng is not None:
        f, t = rng
        where += ["event_ts >= ?", "event_ts < ?"]
        params += [f, t]
    elif window in WINDOWS:
        where.append(_since(window))
    else:
        raise HTTPException(400, "either a valid 'window' or both 'from' and 'to' are required")

    with db() as con:
        try:
            rows = con.execute(
                "SELECT e.remote_ip, count(*), min(e.event_ts), max(e.event_ts),"
                " count(*) FILTER (e.user_agent ILIKE '%Android%' OR e.user_agent ILIKE '%iPhone%'),"
                " i.network_type, i.country_code, i.country_name, i.asn, i.asn_org"
                " FROM access_events e LEFT JOIN ip_enrichment i ON i.ip = e.remote_ip"
                f" WHERE {' AND '.join(where)}"
                " GROUP BY 1, 6, 7, 8, 9, 10 ORDER BY 2 DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        except duckdb.CatalogException:   # pre-enrichment DB: no ip_enrichment (and maybe no user_agent) either
            rows = con.execute(
                "SELECT e.remote_ip, count(*), min(e.event_ts), max(e.event_ts),"
                " 0, NULL, NULL, NULL, NULL, NULL"
                f" FROM access_events e WHERE {' AND '.join(where)}"
                " GROUP BY 1 ORDER BY 2 DESC LIMIT ?",
                [*params, limit],
            ).fetchall()

    return TopIpsResponse(
        window=None if rng is not None else window,
        count=len(rows),
        ips=[
            TopIpRow(
                ip=ip, requests=reqs,
                first_seen=first.isoformat() if first is not None else None,
                last_seen=last.isoformat() if last is not None else None,
                country_code=cc, country_name=cn, asn=asn, asn_org=ao,
                network_type=_network_type(ip, nt),
                mobile_ua_share=round(mobile / reqs, 3) if reqs else 0.0,
            )
            for ip, reqs, first, last, mobile, nt, cc, cn, asn, ao in rows
        ],
    )


@app.get("/events", response_model=EventsResponse)
def events(
    window: str | None = Query(None, description="preset window; or use from/to"),
    ip: str | None = Query(None, description="filter on remote_ip"),
    host: str | None = Query(None, description="filter on host"),
    min_status: int | None = Query(None, ge=100, le=599, description="only status >= this"),
    limit: int = Query(200, ge=1, le=1000),
    from_: str | None = Query(None, alias="from",
                             description="range start (naive ISO); with 'to', overrides window"),
    to: str | None = Query(None, description="range end, exclusive (naive ISO)"),
) -> EventsResponse:
    """Raw event rows for drill-down — 'look at the logs that raised suspicion'.

    Filters compose freely (window or from/to + ip + host + min_status); newest first.
    This is the endpoint an anomaly click in the dashboard will call with the
    flagged entity pre-filled, and where a clicked finest-grain bucket lands.
    """
    rng = _parse_range(from_, to)
    where: list[str] = []
    params: list = []
    if rng is not None:
        f, t = rng
        where += ["event_ts >= ?", "event_ts < ?"]
        params += [f, t]
    elif window in WINDOWS:
        where.append(_since(window))
    else:
        raise HTTPException(400, "either a valid 'window' or both 'from' and 'to' are required")
    if ip is not None:
        where.append("remote_ip = ?")
        params.append(ip)
    if host is not None:
        where.append("host = ?")
        params.append(host)
    if min_status is not None:
        where.append("status >= ?")
        params.append(min_status)

    with db() as con:
        rows = con.execute(
            "SELECT event_ts, remote_ip, host, method, uri_path, status, response_size"
            f" FROM access_events WHERE {' AND '.join(where)} ORDER BY event_ts DESC LIMIT ?",
            [*params, limit],
        ).fetchall()

    return EventsResponse(
        window=None if rng is not None else window,
        count=len(rows),
        events=[
            EventRow(
                event_ts=event_ts.isoformat(), remote_ip=remote_ip, host=host_,
                method=method, uri_path=uri_path, status=status, response_size=response_size,
            )
            for event_ts, remote_ip, host_, method, uri_path, status, response_size in rows
        ],
    )


@app.get("/status", response_model=StatusResponse)
def status() -> StatusResponse:
    """Compact one-line state for shell-bar tools (waybar): poll this endpoint
    and render an icon plus a couple of numbers. ok == no anomaly findings."""
    with db() as con:
        requests = con.execute(
            "SELECT count(*) FROM access_events WHERE event_ts >= now() - INTERVAL '1 hour'"
        ).fetchone()[0]
        errors = con.execute(
            "SELECT count(*) FROM access_events"
            " WHERE event_ts >= now() - INTERVAL '1 hour' AND status >= 400"
        ).fetchone()[0]
        findings = _hot_ips(con) + _volume_outliers(con) + _server_errors(con)

    return StatusResponse(
        ok=not findings,
        requests_last_hour=requests,
        errors_last_hour=errors,
        anomalies=len(findings),
        hostname=HOSTNAME,
        version=VERSION,
    )


@app.get("/stream")
async def stream() -> StreamingResponse:
    """Server-Sent Events push channel for the dashboard's auto-refresh.

    Emits an "update" event whenever new data has landed in the database —
    detected by fingerprinting (max(event_ts), count(*)) on a fresh read-only
    connection every STREAM_TICK_SECONDS — otherwise a heartbeat comment line
    that keeps the connection alive through proxies. No IPC with ingest is
    needed: with the single-writer design, the database itself is the signal.

    The dashboard's EventSource auto-reconnects if this stream drops; clients
    behind a proxy that buffers SSE fall back to plain polling (see app.js).
    """
    async def event_gen():
        last = None
        while True:
            with db() as con:   # sync query (~ms); fine inside the loop at this tick rate
                fingerprint = con.execute(
                    "SELECT max(event_ts), count(*) FROM access_events"
                ).fetchone()
            if fingerprint != last:
                payload = json.dumps({
                    "latest_event": fingerprint[0].isoformat() if fingerprint[0] is not None else None,
                    "total_rows": fingerprint[1],
                })
                yield f"event: update\ndata: {payload}\n\n"
                last = fingerprint
            else:
                yield ": heartbeat\n\n"
            await asyncio.sleep(STREAM_TICK_SECONDS)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Serve the dashboard as static files. Mounted LAST so every API route above
# keeps priority; with html=True, "/" resolves to static/index.html.
app.mount(
    "/",
    StaticFiles(directory=Path(__file__).with_name("static"), html=True),
    name="static",
)


if __name__ == "__main__":
    # Dev convenience: `uv run python api.py`. Production runs uvicorn directly
    # (see README), which additionally offers --reload and worker tuning.
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT)
