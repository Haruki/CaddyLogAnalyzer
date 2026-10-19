CREATE TABLE IF NOT EXISTS access_events (
    event_ts TIMESTAMP NOT NULL,
    ingested_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    remote_ip VARCHAR,
    client_ip VARCHAR,
    host VARCHAR,
    method VARCHAR,
    uri_path VARCHAR,
    uri_query VARCHAR,
    status SMALLINT,
    duration_ms DOUBLE,
    bytes_read BIGINT,
    response_size BIGINT,
    user_id VARCHAR,
    http_proto VARCHAR,
    tls_server_name VARCHAR,
    user_agent VARCHAR              -- raw User-Agent header; NULL for entries without one
);
-- Migration for databases created before the user_agent column existed.
ALTER TABLE access_events ADD COLUMN IF NOT EXISTS user_agent VARCHAR;

-- Persisted anomaly findings, written by alert.py over closed 1-hour windows.
-- Unlike the rolling /anomalies endpoint (which only sees the last hour), rows
-- here survive indefinitely so past events stay visible on the dashboard and in
-- push-notification history. One row per rule hit per window; ongoing incidents
-- dedupe for notification purposes via notified_at, not by collapsing rows.
CREATE SEQUENCE IF NOT EXISTS finding_id_seq;

CREATE TABLE IF NOT EXISTS anomaly_findings (
 finding_id BIGINT PRIMARY KEY DEFAULT nextval('finding_id_seq'),
 window_start TIMESTAMP NOT NULL,   -- closed hour evaluated: [window_start, +1 h)
 kind VARCHAR NOT NULL,             -- hot_ip | first_sight_spike | sustained_attack | auth_breach | exfiltration | server_errors
 severity VARCHAR NOT NULL,         -- info (dashboard only) | warn (push) | critical (push + auto-ban)
 entity VARCHAR NOT NULL,           -- the IP or host the finding is about
 value DOUBLE,                      -- metric that fired (request count, error count, ...)
 baseline DOUBLE,                   -- threshold/baseline for context
 reason VARCHAR NOT NULL,           -- human-readable; shown verbatim in UI and ntfy push
 action_taken VARCHAR,              -- automated containment performed, e.g. 'blocked_ip_4h'
 notified_at TIMESTAMP,             -- when the push was sent (NULL = not pushed)
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE (window_start, kind, entity)   -- re-running a window must not duplicate findings
);
-- Lazy IP enrichment, written by alert.py only for IPs that were loud enough to matter
-- (top external requesters per closed hour + finding entities). LAN IPs never enter here.
-- country_code/asn stay NULL until GeoLite2 .mmdb files are present in geoip/.
CREATE TABLE IF NOT EXISTS ip_enrichment (
  ip VARCHAR PRIMARY KEY,
  network_type VARCHAR NOT NULL,     -- 'cgnat' (100.64.0.0/10, RFC 6598 shared carrier NAT) | 'public'
  country_code VARCHAR,              -- ISO-3166 alpha-2 ('DE'), from GeoLite2-Country
  country_name VARCHAR,
  asn INTEGER,                       -- AS number, from GeoLite2-ASN
  asn_org VARCHAR,                   -- e.g. 'Telekom Deutschland GmbH' - for DE residential IPs usually the ISP itself
  enriched_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);