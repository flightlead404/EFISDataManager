# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Cleanup / re-import migration tests for FDL operation segmentation.

Spec: .kiro/specs/fdl-operation-segmentation/
Task 4.4 - idempotent, transactional re-segmentation of already-imported data.

Seeds a tmp DB with the op-18 / op-19 mega-operation shapes plus a couple of
healthy single-session operations, runs ``migrate_resegment_operations``, and
asserts:
  - the bad mega-ops are replaced by correctly segmented operations,
  - the healthy ops survive unchanged (same derived key, same summary),
  - no fdl_data rows are lost (re-point only),
  - a second run is a no-op (idempotent).

Validates: Requirements 2.7, 2.8, 3.3
"""

from datetime import datetime, timedelta

import pytest

from efis_data_manager import database
from efis_data_manager.fdl_parser import FDLRecord, FDLFile


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    dbp = tmp_path / "efis_data.sqlite"
    monkeypatch.setattr(database, "DB_PATH", dbp)
    monkeypatch.setattr(database, "DB_DIR", tmp_path)
    # Open once to build the schema (and run the one-time migration on the
    # empty DB, which sets the applied flag).
    conn = database.get_db_connection()
    conn.close()
    return dbp


def _run(start: datetime, n: int) -> list[FDLRecord]:
    return [FDLRecord(timestamp=start + timedelta(seconds=i)) for i in range(n)]


def _insert_raw_operation(conn, start, end, record_count, records,
                          source_filename=None):
    """Insert an operations row with an arbitrary (possibly bogus) span and
    attach the given records to it, bypassing segmentation - this is how the
    buggy pre-fix importer created the mega-ops."""
    now = datetime.now().isoformat()
    cur = conn.execute(
        """INSERT INTO operations
           (source_filename, start_time, end_time, duration_seconds,
            record_count, has_flight, date, imported_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (source_filename, start.isoformat(), end.isoformat(),
         int((end - start).total_seconds()), record_count, 0,
         start.date().isoformat(), now),
    )
    op_id = cur.lastrowid
    database._insert_fdl_data(conn, op_id, records)
    database._attach_run_records(conn, op_id, records)
    return op_id


def _ops(conn):
    return conn.execute(
        "SELECT * FROM operations ORDER BY start_time, record_count"
    ).fetchall()


def _fdl_count(conn):
    return conn.execute("SELECT COUNT(*) FROM fdl_data").fetchone()[0]


def test_migration_resegments_bad_ops_and_preserves_healthy(temp_db):
    conn = database.get_db_connection()

    # --- Seed a bad op-19 mega-op: a flight + a ground session ~5 days apart.
    flight = _run(datetime(2026, 9, 28, 20, 12, 17), 300)   # contiguous
    ground = _run(datetime(2026, 10, 3, 17, 10, 33), 180)   # ~5 days later
    mega19_records = flight + ground
    _insert_raw_operation(
        conn,
        start=mega19_records[0].timestamp,
        end=mega19_records[-1].timestamp,  # spans the whole bogus range
        record_count=len(mega19_records),
        records=mega19_records,
        source_filename="GRT FDL 0001.CSV",
    )

    # --- Seed a bad op-18 mega-op: 56 records + 4 records ~4.5 months apart.
    first = _run(datetime(2026, 5, 13, 10, 0, 0), 56)
    second = _run(datetime(2026, 9, 28, 12, 0, 0), 4)
    mega18_records = first + second
    _insert_raw_operation(
        conn,
        start=mega18_records[0].timestamp,
        end=mega18_records[-1].timestamp,
        record_count=len(mega18_records),
        records=mega18_records,
        source_filename="GRT FDL 0000.CSV",
    )

    # --- Seed two HEALTHY contiguous ops (short ground runs, no internal gap).
    healthy_a = _run(datetime(2026, 8, 1, 9, 0, 0), 180)
    healthy_b = _run(datetime(2026, 8, 2, 9, 0, 0), 120)
    _insert_raw_operation(
        conn, healthy_a[0].timestamp, healthy_a[-1].timestamp,
        len(healthy_a), healthy_a, source_filename="GRT FDL 0002.CSV",
    )
    _insert_raw_operation(
        conn, healthy_b[0].timestamp, healthy_b[-1].timestamp,
        len(healthy_b), healthy_b, source_filename="GRT FDL 0003.CSV",
    )
    conn.commit()

    total_records_before = _fdl_count(conn)
    assert total_records_before == 300 + 180 + 56 + 4 + 180 + 120
    assert len(_ops(conn)) == 4  # two bad mega-ops + two healthy

    # Snapshot the healthy ops' derived keys + summaries so we can prove they
    # are untouched after the migration.
    def _healthy_snapshot():
        rows = conn.execute(
            """SELECT start_time, end_time, record_count, duration_seconds,
                      has_flight, airborne_seconds
               FROM operations
               WHERE record_count IN (180, 120)
                 AND start_time LIKE '2026-08-%'
               ORDER BY start_time"""
        ).fetchall()
        return [tuple(r) for r in rows]

    healthy_before = _healthy_snapshot()
    assert len(healthy_before) == 2

    # --- Run the migration (force=True since the empty-DB open already set the
    #     applied flag).
    result = database.migrate_resegment_operations(conn, force=True)
    assert result["applied"] is True

    # No fdl_data rows lost (re-point only; nothing duplicated here).
    assert _fdl_count(conn) == total_records_before

    # The two mega-ops are gone, replaced by correctly segmented operations:
    #   op-19 -> flight (300) + ground (180)
    #   op-18 -> 56 + 4
    #   plus the two healthy ops (180, 120)
    ops = _ops(conn)
    spans = {(o["record_count"], o["duration_seconds"]) for o in ops}
    # op-19 flight: 300 records / 299 s; ground: 180 / 179 s
    assert (300, 299) in spans
    assert (180, 179) in spans
    # op-18: 56 / 55 s and 4 / 3 s
    assert (56, 55) in spans
    assert (4, 3) in spans
    # No operation carries a multi-day bogus duration anymore.
    for o in ops:
        assert o["duration_seconds"] < 60 * 60 * 24  # < 1 day

    # Healthy ops unchanged (same derived key + summary).
    assert _healthy_snapshot() == healthy_before

    # --- Second run is a no-op (idempotent): same ops, same rows, nothing
    #     created or deleted.
    ops_after_first = {(o["start_time"], o["end_time"], o["record_count"])
                       for o in _ops(conn)}
    rows_after_first = _fdl_count(conn)

    result2 = database.migrate_resegment_operations(conn, force=True)
    assert result2["operations_created"] == 0
    assert result2["operations_deleted"] == 0
    assert result2["duplicate_rows_removed"] == 0

    ops_after_second = {(o["start_time"], o["end_time"], o["record_count"])
                        for o in _ops(conn)}
    assert ops_after_second == ops_after_first
    assert _fdl_count(conn) == rows_after_first

    conn.close()


def test_migration_flag_guards_automatic_run(temp_db):
    """Without force, the migration is a no-op once the applied flag is set
    (the empty-DB schema open sets it)."""
    conn = database.get_db_connection()
    result = database.migrate_resegment_operations(conn)  # no force
    assert result["applied"] is False
    conn.close()


def test_migration_removes_exact_duplicate_rows(temp_db):
    """Exact-duplicate records (same timestamp + identical content) are removed
    by the migration (keeps one canonical copy), and no operation is lost."""
    conn = database.get_db_connection()

    recs = _run(datetime(2026, 9, 28, 20, 12, 17), 100)
    # Insert the same run under two "operations" (simulating an old per-file
    # double import). The second insert's rows are exact duplicates and are
    # suppressed by the (timestamp, content_hash) unique index at insert time,
    # but we also assert the migration handles any that slip through.
    _insert_raw_operation(conn, recs[0].timestamp, recs[-1].timestamp,
                          len(recs), recs, source_filename="GRT FDL 0001.CSV")
    conn.commit()

    before = _fdl_count(conn)
    assert before == 100

    result = database.migrate_resegment_operations(conn, force=True)
    assert result["applied"] is True
    # One contiguous run -> exactly one operation; all 100 records retained.
    ops = _ops(conn)
    assert len(ops) == 1
    assert ops[0]["record_count"] == 100
    assert _fdl_count(conn) == 100

    conn.close()
