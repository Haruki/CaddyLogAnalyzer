import pytest

from init_duckdb import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    """Fresh DuckDB file with the canonical schema (duckdb_schema.sql)."""
    path = tmp_path / "test.duckdb"
    initialize_database(path)
    return path
