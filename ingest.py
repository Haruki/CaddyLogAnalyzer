"""One-shot, checkpointed ingestion of Caddy access logs into DuckDB.

Intended to run periodically (see systemd/caddylog-ingest.timer). Each run:
  1. reads new complete lines since the last checkpoint (live file + rotated archives)
  2. inserts them in a single transaction
  3. advances the checkpoint only after the commit succeeds

At-least-once semantics: a crash or an ambiguous rotation may re-insert some
rows; rows are never lost. Duplicates are noise for analytics, not corruption.
"""

import argparse
import gzip
import json
import os
import sys
from pathlib import Path

import duckdb

from init_duckdb import initialize_database

LIVE_NAME = "access.log"
FILE_PREFIX = "access"
BATCH_SIZE = 5000

INSERT_SQL = (
    "INSERT INTO access_events"
    " (event_ts, remote_ip, client_ip, host, method, uri_path, uri_query,"
    " status, duration_ms, bytes_read, response_size, user_id, http_proto, tls_server_name)"
    " VALUES (to_timestamp(?), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def to_row(obj: dict):
    """Map one Caddy access-log JSON object to an access_events row tuple."""
    req = obj["request"]
    uri_path, _, uri_query = req.get("uri", "").partition("?")
    return (
        obj["ts"],                                  # epoch seconds; converted in SQL
        req.get("remote_ip"),
        req.get("client_ip"),
        req.get("host"),
        req.get("method"),
        uri_path or None,
        uri_query or None,
        obj.get("status"),
        (obj.get("duration") or 0) * 1000.0,       # Caddy logs seconds -> ms
        obj.get("bytes_read"),
        obj.get("size"),                           # response size in bytes
        obj.get("user_id") or None,
        req.get("proto"),
        (req.get("tls") or {}).get("server_name"),  # absent for plain-HTTP entries
    )


# Returns None for unparseable lines AND for valid Caddy entries that are not
# access events (ACME renewal, TLS maintenance, admin API logs) - both count as "skipped".
def parse_line(raw: bytes):
    try:
        return to_row(json.loads(raw.decode("utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError, UnicodeDecodeError):
        return None


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("state root must be an object")
        return state
    except (json.JSONDecodeError, ValueError):
        print(f"WARNING: state file {path} unreadable; starting fresh "
              f"(re-ingestion may duplicate rows)", file=sys.stderr)
        return {}


def save_state(path: Path, state: dict) -> None:
    """Atomic write: temp file + rename, so a crash never corrupts the checkpoint."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_live_lines(path: Path, offset: int):
    """Complete lines from `offset`; returns (lines, new_offset).

    Never advances past a partial trailing line - it is re-read next run.
    """
    size = path.stat().st_size
    if size < offset:            # truncated in place; start over
        offset = 0
    with open(path, "rb") as f:
        f.seek(offset)
        chunk = f.read()
    parts = chunk.rsplit(b"\n", 1)
    if len(parts) == 1:         # no newline yet -> nothing complete
        return [], offset
    complete, _tail = parts    # `complete` excludes the final newline itself
    new_offset = offset + len(complete) + 1   # include that trailing newline in "consumed"
    return [l for l in complete.splitlines()], new_offset


def iter_archive_lines(path: Path):
    """Lazily yield decompressed lines from a rotated archive (.gz or plain)."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as f:
        for line in f:
            yield line.rstrip(b"\n")


def archive_size(path: Path) -> int:
    """Total decompressed size of an archive (streaming pass)."""
    return sum(len(line) + 1 for line in iter_archive_lines(path))


def read_archive_lines(path: Path, skip: int):
    """Lines from an archive, dropping the first `skip` decompressed bytes.

    `skip` always lands on a line boundary (our offset invariant), so no line
    is ever split.
    """
    count = 0
    for line in iter_archive_lines(path):
        count += len(line) + 1
        if count <= skip:
            continue
        yield line


def run(log_dir: Path, db_path: Path, state_path: Path) -> None:
    state = load_state(state_path)
    initialize_database(db_path)

    live_path = log_dir / LIVE_NAME
    candidates = sorted(
        p for p in log_dir.iterdir() if p.is_file() and p.name.startswith(FILE_PREFIX))

    # Rotation detection: the live file's inode changed since our last checkpoint.
    entry = state.get(str(live_path), {})
    rotated_size = rotated_offset = None
    rotated_match = None
    if live_path.exists() and entry and live_path.stat().st_ino != entry.get("inode"):
        rotated_size, rotated_offset = entry.get("size"), entry.get("offset", 0)
        matches = [p for p in candidates
                   if p != live_path and not state.get(str(p), {}).get("done")
                   and archive_size(p) == rotated_size]
        # Exactly one size match: that archive is the just-rotated file, skip its
        # already-consumed prefix. Ambiguous (0 or >1 matches): re-read from 0 -
        # duplicates, never loss.
        if len(matches) == 1:
            rotated_match = matches[0]

    rows = []
    per_file = []
    for path in candidates:
        key = str(path)
        st = path.stat()
        if path == live_path:
            same_file = bool(entry) and st.st_ino == entry.get("inode")
            offset = entry.get("offset", 0) if same_file else 0
            lines, new_offset = read_live_lines(path, offset)
            state[key] = {"inode": st.st_ino, "offset": new_offset, "size": st.st_size}
        else:
            if state.get(key, {}).get("done"):
                continue
            skip = rotated_offset if path == rotated_match else 0
            lines = list(read_archive_lines(path, skip))
            state[key] = {"done": True}

        inserted_here = skipped_here = 0
        for raw in lines:
            row = parse_line(raw)
            if row is None:
                skipped_here += 1
            else:
                rows.append(row)
                inserted_here += 1
        per_file.append((path.name, len(lines), inserted_here, skipped_here))

    con = duckdb.connect(str(db_path))
    try:
        if rows:
            con.begin()
            for i in range(0, len(rows), BATCH_SIZE):
                con.executemany(INSERT_SQL, rows[i:i + BATCH_SIZE])
            con.commit()
    finally:
        con.close()

    save_state(state_path, state)      # only reached after a successful commit

    print(f"ingest: {len(candidates)} file(s) scanned, "
          f"{sum(r[1] for r in per_file)} lines read, "
          f"{sum(r[2] for r in per_file)} inserted, "
          f"{sum(r[3] for r in per_file)} skipped")
    for name, read_n, ins, skip in per_file:
        if read_n or skip:
            print(f"  {name}: read {read_n}, inserted {ins}, skipped {skip}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest Caddy access logs into the DuckDB access_events table "
                    "(one-shot, checkpointed).")
    parser.add_argument("--log-dir", default="/var/log/caddy",
                        help=f"Directory containing {LIVE_NAME} and rotated archives "
                             f"(default: %(default)s)")
    parser.add_argument("--db", default="access.duckdb",
                        help="Path to the DuckDB database file (default: %(default)s)")
    parser.add_argument("--state", default=None,
                        help="Checkpoint file path (default: <db>.state.json)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    log_dir = Path(args.log_dir)
    if not log_dir.is_dir():
        print(f"error: log directory {log_dir} does not exist", file=sys.stderr)
        sys.exit(1)
    db_path = Path(args.db)
    state_path = Path(args.state) if args.state else Path(str(db_path) + ".state.json")
    run(log_dir, db_path, state_path)


if __name__ == "__main__":
    main()
