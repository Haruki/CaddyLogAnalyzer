import argparse
import time
from pathlib import Path

import duckdb


SCHEMA_PATH = Path(__file__).with_name("duckdb_schema.sql")

def connect_with_retry(db_path: Path, attempts: int = 5) -> duckdb.DuckDBPyConnection:
    """Connect, retrying while another writer (the ingest or alert job) holds the write lock."""
    for i in range(attempts):
        try:
            return duckdb.connect(str(db_path))
        except duckdb.IOException:
            if i == attempts - 1:
                raise
            time.sleep(2)


def initialize_database(database_path: Path) -> None:
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with connect_with_retry(database_path) as connection:
        connection.execute(schema_sql)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create or update the DuckDB schema for access event storage."
    )
    parser.add_argument(
        "database",
        nargs="?",
        default="access.duckdb",
        help="Path to the DuckDB database file to initialize.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    database_path = Path(args.database)
    initialize_database(database_path)
    print(f"Initialized DuckDB schema in {database_path}")


if __name__ == "__main__":
    main()