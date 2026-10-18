# CaddyLogAnalyzer

Utilities and early prototypes for working with Caddy access logs.

## Setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) once per machine, then in the project directory run:

```bash
uv sync          # creates .venv with the exact versions from uv.lock
uv run python init_duckdb.py access.duckdb   # any command runs inside the env
```

Dependencies are declared in `pyproject.toml` and pinned by the committed `uv.lock`; `.python-version` pins the interpreter. Add a dependency with `uv add <package>`.

## Current Direction

The long-term direction of this repository is a DuckDB-backed pipeline:

- a single Python ingest service writes raw access events into DuckDB
- analytical queries run against the raw access event table
- periodic consolidation jobs roll recent raw events into smaller statistical summaries


## DuckDB Schema

The core access event schema lives in [duckdb_schema.sql](duckdb_schema.sql).

It defines an append-only `access_events` table with columns for:

- event timestamp and ingest timestamp
- remote and client IPs
- host, method, and split URI fields (`uri_path`, `uri_query`)
- status, duration, and byte counts
- user and protocol metadata

plus an `anomaly_findings` table written by the alerting job (see [Alerting](#alerting)).

Initialize a database with:

```bash
python3 init_duckdb.py access.duckdb
```

This requires the project environment to be set up (`uv sync`).

## Ingestion

`ingest.py` is a one-shot, checkpointed importer for Caddy access logs: each run reads new complete lines since the last checkpoint (live `access.log` plus rotated archives), inserts them into `access_events` in a single transaction, then advances the checkpoint stored next to the database (`<db>.state.json`). Re-runs are safe; at-least-once semantics mean rare duplicates are possible after crashes or rotations, never lost rows.

Run manually:

```bash
uv run python ingest.py --log-dir /var/log/caddy --db access.duckdb
```

Runs as a container on the Caddy host — see [Deployment](#deployment-on-the-caddy-host-docker-compose) below.

## Alerting

`alert.py` is a one-shot anomaly evaluator intended to run hourly (it loops in the deployment, see [Deployment](#deployment-on-the-caddy-host-docker-compose)). Each run evaluates the rules below over the **last fully elapsed hour** — never partial data — persists findings into `anomaly_findings`, pushes warn/critical findings to ntfy, and on critical IP findings blocks the IP at the Caddy edge via the admin API.

| Rule | Severity | Fires when |
|---|---|---|
| `hot_ip` | info (dashboard only) | an external IP makes >50 requests in one hour |
| `first_sight_spike` | warn (push) | an IP's first-ever request is already ≥100 in its first hour — automated client |
| `sustained_attack` | warn (push) | an IP exceeds 50 requests/hour in two consecutive hours |
| `auth_breach` | critical (push + auto-ban) | ≥3 auth failures (401) from one IP on a host, then ≥3 successful requests from the same IP on the same host — possible credential compromise |
| `exfiltration` | critical (push + auto-ban) | a brand-new IP pulls ≥5 successful responses of **varying** sizes off sensitive paths (`/.env`, `/wp-*`, `/.git/`, backups, configs) in one hour |
| `server_errors` | warn (push) | ≥5 server errors (5xx) on one host within an hour |

Design notes:

- **Explainable by construction** — every finding carries a human-readable reason string that the dashboard and push notifications show verbatim; thresholds are constants at the top of `alert.py`, not hidden in SQL. LAN traffic (`192.168.*`) is excluded from all rules.
- **Persistence is the point.** The rolling `/anomalies` endpoint only sees its own sliding window — last night's scan vanishes an hour later. Findings persist indefinitely, so past events stay visible on the dashboard ("recent anomalies" card) and in push history. Re-running a window never duplicates rows (unique key per window/kind/entity).
- **Push dedupe** — the same rule+entity is not re-pushed within 6 h, so an ongoing incident doesn't page every hour; it still accumulates one persisted row per hour.
- **Auto-ban caveat:** Caddy admin-API changes are runtime-only and do **not** survive a caddy restart (config reverts to the Caddyfile). Blocks last until their expiry (`AUTO_BAN_HOURS`, default 4) or a restart; a still-active attacker simply triggers a fresh finding and gets blocked again on the next hourly run. Blocked IPs get a silent `444` at the edge, before any site routing.

Run manually (defaults target the Caddy host layout):

```bash
uv run python alert.py --db access.duckdb                 # evaluate last closed hour
uv run python alert.py --window-start 2026-10-18T03:00   # backfill one specific hour (no checkpoint change)
uv run python alert.py --no-notify --no-ban               # persist only, no side effects
```

Environment overrides: `NTFY_URL` (default `http://127.0.0.1:88` — your own ntfy instance), `NTFY_TOPIC` (default `caddylog-alerts`), `CADDY_ADMIN` (default `http://127.0.0.1:2019`), `AUTO_BAN_HOURS` (default 4).

## API & Dashboard

`api.py` is a read-only FastAPI service over the database: aggregate statistics for fixed windows (`5m | 1h | 1d | 30d | year`, where `year` is a calendar year with monthly buckets and accepts `?year=YYYY` for past years) or an explicit range (`from`/`to`, naive server-local ISO timestamps; the bucket size is derived from the span — per-minute up to monthly — and the series is zero-filled across exactly `[from, to)`), anomaly hints with human-readable reasons, and raw event rows for drill-down. All queries run on the fly against `access_events` (single-digit milliseconds even over a year of events); the service coexists with the ingest job — DuckDB allows one writer plus any number of readers across processes.

Run locally (point at any database file via env):

```bash
CADDYLOG_DB=access.duckdb uv run python api.py          # dev, binds 127.0.0.1:8377
# or explicitly: CADDYLOG_DB=/var/lib/caddylog/access.duckdb uv run uvicorn api:app --port 8377
```

The service also serves a small self-contained dashboard at `/` (static files from `static/`, no build step): window tabs, request chart (vendored uPlot), status breakdown, top hosts and anomaly findings with click-through drill-down into raw rows. The chart is click-to-zoom: clicking a bucket drills one level deeper (hour → 5-minute → per-minute → raw events) and breadcrumb chips track the path back; presets always reset to now-anchored views. A "recent anomalies" card lists persisted alert findings from the last 7 days (`GET /findings`); clicking one opens the drill-down scoped to that finding's own hour. Live updates arrive via the SSE endpoint `GET /stream` — the server pushes an event whenever new rows land in the database (fingerprint check every 5 s); if a proxy buffers SSE, the dashboard falls back to plain polling automatically.

Endpoints: `GET /stats?window=…` or `GET /stats?from=…&to=…` · `GET /anomalies` · `GET /findings?days=7` · `GET /events?(window=…|from=…&to=…)&ip=…&host=…&min_status=…` · `GET /status` · `GET /stream`. Interactive OpenAPI docs at `/docs`. Binds 127.0.0.1 only; expose via a Caddy reverse proxy if needed (SSE needs `proxy_buffering off`).

## Deployment on the Caddy host (Docker Compose)

The whole pipeline runs as a Docker Compose project — no host systemd units, no cron. The stack lives in `/data/pool/docker_stacks/caddyloganalyzer/` alongside your other services; this repository is checked out there and `compose.yaml` + `Dockerfile` are committed to it.

Three containers share one image (`caddyloganalyzer`, built from the repo):

| Service | What it does |
|---|---|
| `ingest` | runs `ingest.py` every 5 min in a loop (replaces the old timer) |
| `alert` | runs `alert.py` hourly in a loop; reaches Caddy's admin API and ntfy on the host via `host.docker.internal` |
| `api` | long-lived uvicorn serving the dashboard + JSON endpoints, bound to `127.0.0.1:8377` only |

All containers run as uid/gid `966:966` — the host's `caddy` user — so they read `/var/log/caddy` (mode 750) without any permission changes, and own the database directory.

All services set `TZ=Europe/Berlin` to match the host clock. That is not cosmetic: DuckDB stores `event_ts` from epoch seconds using the process's local timezone, so ingest and alert must agree on it — otherwise the closed-hour windows silently evaluate the wrong traffic (and dashboard times would be off by the UTC offset).

One-time setup on the host (the data dir must exist before compose starts; Docker would otherwise create a missing bind-mount source as root):

```bash
git clone <repo> /data/pool/docker_stacks/caddyloganalyzer && cd $_
sudo mkdir -p data && sudo chown 966:966 data
docker compose up -d --build
```

The first `ingest` run imports the full log history (live file plus rotated archives) into a fresh database. The dashboard is fronted by Caddy like every other service — add this site block to `/etc/caddy/Caddyfile`:

```
caddylog.pimpelkram.com {
	import dns01_inwx
	reverse_proxy 127.0.0.1:8377
}
```

Caddy's `reverse_proxy` streams responses by default, so the SSE live-update channel works through it without extra config (unlike nginx-style proxies that need buffering disabled).
