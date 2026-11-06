"""API contract tests: the click-to-zoom bucket ladder (mirrored in static/app.js),
explicit-range validation, and bucket-boundary flooring."""

from datetime import datetime

import pytest
from fastapi import HTTPException

from api import _bucket_for, _floor_to_bucket, _parse_range


def test_bucket_for_ladder_boundaries():
    # span <= max_span is inclusive at each rung; anything past the last rung -> monthly
    assert _bucket_for(599)[1] == "INTERVAL '1 minute'"
    assert _bucket_for(600)[1] == "INTERVAL '1 minute'"
    assert _bucket_for(601)[1] == "INTERVAL '5 minutes'"
    assert _bucket_for(2 * 3600)[1] == "INTERVAL '5 minutes'"
    assert _bucket_for(2 * 3600 + 1)[1] == "INTERVAL '1 hour'"
    assert _bucket_for(45 * 86400)[1] == "INTERVAL '1 day'"
    assert _bucket_for(45 * 86400 + 1)[1] == "INTERVAL '1 month'"


def test_parse_range_validation():
    f, t = _parse_range("2026-01-01T00:00:00", "2026-01-02T00:00:00")
    assert (f, t) == (datetime(2026, 1, 1), datetime(2026, 1, 2))

    with pytest.raises(HTTPException):
        _parse_range("2026-01-01T00:00:00", None)          # one-sided pair
    with pytest.raises(HTTPException):
        _parse_range(None, "2026-01-01T00:00:00")
    with pytest.raises(HTTPException):
        _parse_range("2026-01-01T00:00:00+01:00", "2026-01-02T00:00:00")   # offset-bearing
    with pytest.raises(HTTPException):
        _parse_range("not-a-timestamp", "2026-01-02T00:00:00")              # malformed
    with pytest.raises(HTTPException):
        _parse_range("2026-01-02T00:00:00", "2026-01-01T00:00:00")         # to <= from
    with pytest.raises(HTTPException):
        _parse_range("2024-01-01T00:00:00", "2026-07-01T00:00:00")         # > 2 years


def test_floor_to_bucket_alignment():
    t = datetime(2026, 11, 6, 14, 37, 22)
    assert _floor_to_bucket(t, "INTERVAL '5 minutes'") == datetime(2026, 11, 6, 14, 35)
    assert _floor_to_bucket(t, "INTERVAL '1 hour'") == datetime(2026, 11, 6, 14, 0)
    assert _floor_to_bucket(t, "INTERVAL '1 day'") == datetime(2026, 11, 6, 0, 0)
    assert _floor_to_bucket(t, "INTERVAL '1 month'") == datetime(2026, 11, 1, 0, 0)
