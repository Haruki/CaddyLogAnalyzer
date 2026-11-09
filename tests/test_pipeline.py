"""End-to-end pipeline test: fixture logs -> ingest -> alert run -> API.

Guarantees that every dashboard card has data (chart/totals/hosts, top-offenders,
rolling anomalies, persisted findings, drill-down) — the same invariant
dev_dashboard.sh provides manually in one command."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# The fixture scenario IPs (see dev_fixtures.py / fake_traffic.py).
SCENARIO_IPS = {
    "203.0.113.50",   # hot_ip (info)
    "198.51.100.77",  # first_sight_spike (warn)
    "192.0.2.88",     # auth_breach (critical)
    "198.18.0.99",    # exfiltration (critical)
    "203.0.113.9",    # server_errors (warn — host entity, not an IP)
    "100.64.20.30",   # cgnat + mobile UA share (no finding by design)
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A fully populated pipeline: fixture logs -> ingest -> alert run -> API client."""
    from dev_fixtures import generate

    summary = generate(tmp_path / "logs", days=2)
    db = tmp_path / "pipeline.duckdb"

    from init_duckdb import initialize_database
    initialize_database(db)

    from ingest import run as ingest_run
    ingest_run(tmp_path / "logs", db, Path(str(db) + ".state.json"))

    # Explicit window: the fixture's scenario hour — deterministic even if the test
    # happens to cross an hour boundary between generate() and this call.
    from alert import run as alert_run
    alert_run(
        db, Path(str(db) + ".alert_state.json"), Path(str(db) + ".blocks.json"),
        datetime.fromisoformat(summary["window_start"]), do_notify=False, do_ban=False,
    )

    import api
    monkeypatch.setattr(api, "DB_PATH", db)   # handlers read the module global per request
    return TestClient(api.app), summary


def test_stats_card(client):
    c, _ = client
    r = c.get("/stats?window=1d").json()
    assert r["total_requests"] > 0
    assert r["status_breakdown"], "no status breakdown"
    assert r["top_hosts"], "no top hosts"
    assert r["timeseries"], "empty timeseries"


def test_top_offenders_card(client):
    c, summary = client
    ws = datetime.fromisoformat(summary["window_start"])
    we = (ws + timedelta(hours=1)).isoformat(timespec="seconds")

    rows = {row["ip"]: row for row in c.get(f"/top-ips?from={ws.isoformat(timespec='seconds')}&to={we}").json()["ips"]}
    assert SCENARIO_IPS <= set(rows), f"missing scenario IPs: {SCENARIO_IPS - set(rows)}"

    # enrichment join present on every row; cgnat flag + mobile share for the CGNAT IP
    assert all("country_code" in r and "asn_org" in r for r in rows.values())
    assert rows["100.64.20.30"]["network_type"] == "cgnat"
    assert rows["100.64.20.30"]["mobile_ua_share"] > 0

    # rolling tab: the ongoing incident keeps the last-hour view populated at any minute
    assert c.get("/top-ips?window=1h").json()["count"] >= 1


def test_findings_card(client):
    c, summary = client
    findings = c.get("/findings?days=7").json()
    assert {f["kind"] for f in findings} == {"hot_ip", "first_sight_spike", "auth_breach", "exfiltration", "server_errors"}
    assert all(f["window_start"] == summary["window_start"] for f in findings)
    severities = sorted(f["severity"] for f in findings)
    assert severities == ["critical", "critical", "info", "warn", "warn"]


def test_rolling_anomalies_card(client):
    c, _ = client
    hints = c.get("/anomalies").json()
    # the ongoing incident (last ~50 minutes) guarantees at least one hint at any minute of the hour
    assert len(hints) >= 1
    assert all({"kind", "entity", "reason"} <= set(h) for h in hints)


def test_drilldown_events(client):
    c, summary = client
    ws = datetime.fromisoformat(summary["window_start"])
    we = (ws + timedelta(hours=1)).isoformat(timespec="seconds")
    r = c.get(f"/events?ip=192.0.2.88&from={ws.isoformat(timespec='seconds')}&to={we}").json()
    assert r["count"] == 9                                   # 4x 401 + 5x 200 (auth_breach pattern)
    assert {e["status"] for e in r["events"]} == {401, 200}


def test_ingest_skips_noise_without_errors(client):
    c, summary = client
    # every fixture file carries a malformed line + an ACME/TLS entry: skipped, not inserted
    total_lines = sum(summary["files"].values())
    inserted = c.get("/stats?window=30d").json()["total_requests"]
    assert inserted < total_lines
