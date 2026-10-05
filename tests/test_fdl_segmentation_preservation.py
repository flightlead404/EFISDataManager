# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Preservation tests for FDL operation segmentation.

Spec: .kiro/specs/fdl-operation-segmentation/
Task 2 - Property 2: Preservation - Single Contiguous Run Unchanged.

Observation-first methodology: these tests OBSERVE and lock in the CURRENT
behavior for inputs where the bug condition does NOT hold (a single contiguous
1 Hz run with no gap > 180 s and nothing to merge across a file boundary). After
the fix, a single contiguous run must still produce exactly ONE operation with
the same start, end, duration, record_count, and summary.

**EXPECTED OUTCOME on unfixed code: these tests PASS** - they are the baseline
to preserve. The forward-looking assertion against the not-yet-existing
`segment_records` is marked xfail (deferred until Task 4 adds the function); the
observable current behavior (via FDLFile + import_fdl_file) is asserted now and
must pass today.

Validates: Requirements 3.1, 3.2, 3.4, 3.5
"""

from datetime import datetime, timedelta

import pytest
from hypothesis import given, settings, HealthCheck, strategies as st

from efis_data_manager import database
from efis_data_manager.fdl_parser import FDLRecord, FDLFile


# --------------------------------------------------------------------------
# Fixtures / helpers (match the project's temp_db convention).
# --------------------------------------------------------------------------

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    dbp = tmp_path / "efis_data.sqlite"
    monkeypatch.setattr(database, "DB_PATH", dbp)
    monkeypatch.setattr(database, "DB_DIR", tmp_path)
    conn = database.get_db_connection()
    conn.executescript(database.SCHEMA_SQL)
    conn.commit()
    conn.close()
    return dbp


def _contiguous(start: datetime, n: int, **field_overrides) -> list[FDLRecord]:
    """A contiguous 1 Hz run of n records. field_overrides -> callables(i)->val
    or constants applied to every record (e.g. rpm1=1000)."""
    recs = []
    for i in range(n):
        kwargs = {"timestamp": start + timedelta(seconds=i)}
        for k, v in field_overrides.items():
            kwargs[k] = v(i) if callable(v) else v
        recs.append(FDLRecord(**kwargs))
    return recs


def _one_op_row(dbp):
    conn = database.get_db_connection()
    rows = conn.execute("SELECT * FROM operations").fetchall()
    conn.close()
    return rows


# --------------------------------------------------------------------------
# Observed CURRENT behavior for a contiguous single-session file.
# --------------------------------------------------------------------------

def test_contiguous_stream_is_one_operation_current(temp_db):
    """A contiguous 1 Hz stream of N records imports as exactly one operation
    with start=records[0], end=records[-1], duration=N-1 s, record_count=N."""
    n = 600
    start = datetime(2026, 9, 28, 20, 12, 17)
    recs = _contiguous(start, n)
    fdl = FDLFile("GRT FDL 0001.CSV", "x", recs)

    op_id = database.import_fdl_file(fdl)
    assert op_id is not None

    rows = _one_op_row(temp_db)
    assert len(rows) == 1
    op = rows[0]
    assert op["start_time"] == start.isoformat()
    assert op["end_time"] == (start + timedelta(seconds=n - 1)).isoformat()
    assert op["duration_seconds"] == n - 1
    assert op["record_count"] == n


def test_op14_short_ground_op_not_split_current(temp_db):
    """op-14 shape: 180 contiguous records / 179 s stays ONE operation."""
    start = datetime(2026, 9, 28, 17, 10, 33)
    recs = _contiguous(start, 180, rpm1=1000.0)  # engine running, not airborne
    fdl = FDLFile("GRT FDL 0001.CSV", "x", recs)

    op_id = database.import_fdl_file(fdl)
    assert op_id is not None

    rows = _one_op_row(temp_db)
    assert len(rows) == 1
    op = rows[0]
    assert op["record_count"] == 180
    assert op["duration_seconds"] == 179
    assert op["has_flight"] == 0  # ground op


def test_op17_short_ground_op_not_split_current(temp_db):
    """op-17 shape: 120 contiguous records / 119 s stays ONE operation."""
    start = datetime(2026, 9, 28, 17, 10, 33)
    recs = _contiguous(start, 120, rpm1=1000.0)
    fdl = FDLFile("GRT FDL 0002.CSV", "x", recs)

    op_id = database.import_fdl_file(fdl)
    assert op_id is not None

    rows = _one_op_row(temp_db)
    assert len(rows) == 1
    op = rows[0]
    assert op["record_count"] == 120
    assert op["duration_seconds"] == 119
    assert op["has_flight"] == 0


def test_summary_computed_for_contiguous_stream_current(temp_db):
    """_compute_operation_summary runs over a contiguous run and populates the
    expected fields (has_flight, airborne_seconds, max_rpm, hourmeter span)."""
    start = datetime(2026, 9, 28, 20, 12, 17)
    # 300 records: airborne (IAS > 40) with rising hourmeter and steady RPM.
    recs = _contiguous(
        start, 300,
        indicated_airspeed=120.0,
        true_airspeed=130.0,
        vertical_speed=0.0,
        rpm1=2300.0,
        hourmeter=lambda i: 1000.0 + i / 3600.0,
    )
    fdl = FDLFile("GRT FDL 0001.CSV", "x", recs)

    op_id = database.import_fdl_file(fdl)
    assert op_id is not None

    rows = _one_op_row(temp_db)
    op = rows[0]
    assert op["has_flight"] == 1
    assert op["airborne_seconds"] == 300
    assert op["max_rpm"] == 2300.0
    assert op["hourmeter_start"] == 1000.0
    # end hourmeter is the last record's value
    assert op["hourmeter_end"] == pytest.approx(1000.0 + 299 / 3600.0)


# --------------------------------------------------------------------------
# Preservation property (Hypothesis): any contiguous stream -> one FDLFile op
# whose span/count match the records exactly. This is the current unit of
# import and must be preserved.
# --------------------------------------------------------------------------

@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    n=st.integers(min_value=1, max_value=500),
    step=st.integers(min_value=1, max_value=180),  # <= threshold => contiguous
)
def test_contiguous_stream_preserved_property(temp_db, n, step):
    """For any contiguous stream (every consecutive gap <= 180 s), the current
    importer produces exactly one operation with span = first..last and
    record_count = N. This is the preservation baseline."""
    # Clear any rows from a previous example (function-scoped fixture reused).
    conn = database.get_db_connection()
    conn.execute("DELETE FROM fdl_data")
    conn.execute("DELETE FROM operations")
    conn.commit()
    conn.close()

    start = datetime(2026, 9, 28, 0, 0, 0)
    recs = [FDLRecord(timestamp=start + timedelta(seconds=i * step)) for i in range(n)]
    # Unique filename per example so dedup never suppresses the insert.
    fdl = FDLFile(f"GRT FDL {n:04d}_{step}.CSV", "x", recs)

    op_id = database.import_fdl_file(fdl)
    assert op_id is not None

    rows = _one_op_row(temp_db)
    assert len(rows) == 1
    op = rows[0]
    assert op["record_count"] == n
    assert op["start_time"] == start.isoformat()
    assert op["end_time"] == (start + timedelta(seconds=(n - 1) * step)).isoformat()
    assert op["duration_seconds"] == (n - 1) * step


# --------------------------------------------------------------------------
# Post-fix preservation assertions against segment_records (Task 4 added it).
# A single contiguous run must segment to exactly one operation preserving span
# and count; short ground ops must never be split.
# --------------------------------------------------------------------------

def test_contiguous_stream_segments_to_one_run():
    """A contiguous run segments to exactly one operation preserving span and
    count (Property 2)."""
    from efis_data_manager import fdl_parser

    start = datetime(2026, 9, 28, 20, 12, 17)
    recs = [FDLRecord(timestamp=start + timedelta(seconds=i)) for i in range(600)]
    runs = fdl_parser.segment_records(recs)
    assert len(runs) == 1
    assert runs[0][0].timestamp == start
    assert runs[0][-1].timestamp == start + timedelta(seconds=599)
    assert len(runs[0]) == 600


def test_short_ground_ops_not_split():
    """op-14 (180/179 s) and op-17 (120/119 s) each stay one run."""
    from efis_data_manager import fdl_parser

    start = datetime(2026, 9, 28, 17, 10, 33)
    for n in (180, 120):
        recs = [FDLRecord(timestamp=start + timedelta(seconds=i)) for i in range(n)]
        runs = fdl_parser.segment_records(recs)
        assert len(runs) == 1
        assert len(runs[0]) == n
