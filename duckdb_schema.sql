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
    tls_server_name VARCHAR
);