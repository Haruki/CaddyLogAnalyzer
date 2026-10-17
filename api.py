"""Read-only web API over the access_events DuckDB database.

Serves aggregate statistics for fixed time windows (5m / 1h / 1d / 30d / all),
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

import os
from contextlib import contextmanager
from pathlib import Path

import duckdb
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Database location: overridable via env so the same code runs in dev (point at
# a local sample DB) and under systemd on the Caddy host (default below).
DB_PATH = Path(os.environ.get("CADDYLOG_DB", "/var/lib/caddylog/access.duckdb"))

PORT = 8377

# Tunables for anomaly detection. Deliberately simple, explainable rules — each
# finding carries a human-readable reason string that the UI can show verbatim.
HOT_IP_THRESHOLD = 50      # external requests per hour that count as "hot"
ZSCORE_LIMIT = 3.0         # daily volume this many std-devs above the 30d mean ...
ZSCORE_MIN_DAYS = 7        # ... but only for IPs seen on at least this many days

# window name -> (lower-bound interval, or None for all history; SQL expression
# producing one timeseries bucket). Bucket sizes are chosen so every window
# yields roughly a dozen to ~40 points: enough shape for a graph, small enough
# for a widget.
WINDOWS: dict[str, tuple[str | None, str]] = {
    "5m":  ("INTERVAL '5 minutes'", "time_bucket(INTERVAL '1 minute', event_ts)"),
    "1h":  ("INTERVAL '1 hour'",    "time_bucket(INTERVAL '5 minutes', event_ts)"),
    "1d":  ("INTERVAL '1 day'",     "date_trunc('hour', event_ts)"),
    "30d": ("INTERVAL '30 days'",   "date_trunc('day', event_ts)"),
    "all": (None,                   "date_trunc('month', event_ts)"),
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
    """SQL predicate bounding a window to 'now minus interval' (or all history)."""
    interval = WINDOWS[window][0]
    return "TRUE" if interval is None else f"event_ts >= now() - {interval}"


# ---------------------------------------------------------------------------
# Response models — the JSON contract for dashboard, homepage widget and bar tool
# ---------------------------------------------------------------------------

class HostCount(BaseModel):
    host: str | None
    count: int


class StatsResponse(BaseModel):
    window: str
    total_requests: int
    status_breakdown: dict[int, int]   # HTTP status -> request count
    top_hosts: list[HostCount]
    timeseries: list[tuple[str, int]]  # (bucket start as ISO timestamp, count)


class Anomaly(BaseModel):
    entity: str | None                 # IP or host the finding is about
    kind: str                         # "hot_ip" | "volume_outlier" | "server_errors"
    value: float                      # observed metric (requests / z-score / errors)
    baseline: float | None            # what normal looks like, if a rule has one
    reason: str                       # human-readable explanation for the UI


class EventRow(BaseModel):
    event_ts: str                     # ISO timestamp
    remote_ip: str | None
    host: str | None
    method: str | None
    uri_path: str | None
    status: int | None
    response_size: int | None


class EventsResponse(BaseModel):
    window: str
    count: int                        # rows returned (capped by limit)
    events: list[EventRow]            # newest first


class StatusResponse(BaseModel):
    ok: bool                          # no anomaly findings right now
    requests_last_hour: int
    errors_last_hour: int             # status >= 400
    anomalies: int                    # number of open findings (see /anomalies)


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

@app.get("/")
def index() -> dict:
    """Landing info: what this service is and which endpoints exist."""
    return {
        "service": "caddyloganalyzer api",
        "endpoints": [
            "/stats?window=5m|1h|1d|30d|all",
            "/anomalies",
            "/events?window=...&ip=...&host=...&min_status=...",
            "/status",
        ],
    }


@app.get("/stats", response_model=StatsResponse)
def stats(window: str = Query("1h", description="5m | 1h | 1d | 30d | all")) -> StatsResponse:
    """Aggregate statistics for one time window, computed on the fly.

    One read-only connection, four small queries; each takes single-digit
    milliseconds even over a year of raw events (measured), so there is no
    caching or summary layer in between.
    """
    if window not in WINDOWS:
        raise HTTPException(400, f"unknown window {window!r}; expected one of {sorted(WINDOWS)}")
    since = _since(window)
    bucket = WINDOWS[window][1]

    with db() as con:
        total = con.execute(f"SELECT count(*) FROM access_events WHERE {since}").fetchone()[0]
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
                f"SELECT {bucket} t, count(*) FROM access_events WHERE {since} GROUP BY 1 ORDER BY 1"
            ).fetchall()
        ]

    return StatsResponse(
        window=window,
        total_requests=total,
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
        return _hot_ips(con) + _volume_outliers(con) + _server_errors(con)


@app.get("/events", response_model=EventsResponse)
def events(
    window: str = Query("1h"),
    ip: str | None = Query(None, description="filter on remote_ip"),
    host: str | None = Query(None, description="filter on host"),
    min_status: int | None = Query(None, ge=100, le=599, description="only status >= this"),
    limit: int = Query(200, ge=1, le=1000),
) -> EventsResponse:
    """Raw event rows for drill-down — 'look at the logs that raised suspicion'.

    Filters compose freely (window + ip + host + min_status); newest first.
    This is the endpoint an anomaly click in the dashboard will call with the
    flagged entity pre-filled.
    """
    if window not in WINDOWS:
        raise HTTPException(400, f"unknown window {window!r}; expected one of {sorted(WINDOWS)}")

    where = [_since(window)]
    params: list = []
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
        window=window,
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
    )


if __name__ == "__main__":
    # Dev convenience: `uv run python api.py`. Production runs uvicorn directly
    # (see README), which additionally offers --reload and worker tuning.
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT)
