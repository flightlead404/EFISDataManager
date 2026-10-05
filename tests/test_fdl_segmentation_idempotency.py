# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Idempotency / identity-key tests for FDL operation segmentation.

Spec: .kiro/specs/fdl-operation-segmentation/
Task 3 - Property 3: Idempotent Record-Level Dedup (timestamp + content,
filename-agnostic).

These tests originally documented (on UNFIXED code) the two weaknesses of the
old `(source_filename, start_time)` dedup, which is UNSOUND because GRT FDL
filenames cycle (start at 0000 on every fresh USB and are reused for different
data):
  (a) FALSE DUPLICATE: a cycled filename (same name + same start_time) carrying
      DIFFERENT data was wrongly SUPPRESSED - real data dropped.
  (b) FALSE DISTINCT: the SAME records under a DIFFERENT filename were imported
      AGAIN - duplicate records / operations.

Per the bugfix methodology, those "current behavior" tests captured the BUG, not
the desired behavior. After Task 4 they are flipped to assert the CORRECTED
behavior: dedup is RECORD-LEVEL by (timestamp, content_hash) and operation
identity is derived from the deduped records, so re-import (incl. the same
records under a different filename) is idempotent and distinct content under a
reused name is NOT dropped. The exact re-import no-op is preserved.

Validates: Requirements 2.6, 2.7, 3.3
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
    conn = database.get_db_connection()
    conn.executescript(database.SCHEMA_SQL)
    conn.commit()
    conn.close()
    return dbp


def _records(start: datetime, n: int, rpm=None):
    recs = []
    for i in range(n):
        recs.append(FDLRecord(timestamp=start + timedelta(seconds=i), rpm1=rpm))
    return recs


def _counts():
    conn = database.get_db_connection()
    ops = conn.execute("SELECT COUNT(*) AS c FROM operations").fetchone()["c"]
    rows = conn.execute("SELECT COUNT(*) AS c FROM fdl_data").fetchone()["c"]
    conn.close()
    return ops, rows


# --------------------------------------------------------------------------
# Corrected (post-fix) behavior: record-level dedup by (timestamp,
# content_hash), filename-agnostic. These tests were originally written to lock
# in the UNSOUND (source_filename, start_time) status quo (the documented bug);
# per the bugfix methodology they are now flipped to assert the CORRECTED
# behavior the fix delivers. There is exactly one assertion of each corrected
# behavior (the earlier deferred xfail duplicates were folded in here).
# --------------------------------------------------------------------------

def test_cycled_filename_keeps_distinct_content(temp_db):
    """FIX for weakness (a): the same filename carrying DIFFERENT content must
    NOT be dropped. GRT FDL filenames cycle (start at 0000 on every fresh USB),
    so filename is not an identity key - distinct records persist, keyed by
    (timestamp, content_hash).

    'GRT FDL 0000.CSV' starting 2026-05-13 10:00:00 is imported twice with
    different record content (different count + rpm). The genuinely different
    second run must make it into the database."""
    start = datetime(2026, 5, 13, 10, 0, 0)
    first = FDLFile("GRT FDL 0000.CSV", "usb1", _records(start, 56, rpm=800.0))
    # Fresh USB months later reuses 0000.CSV; coincidentally same start_time but
    # different data (e.g. a different ground run that happened at 10:00:00).
    second = FDLFile("GRT FDL 0000.CSV", "usb2", _records(start, 300, rpm=2200.0))

    op1 = database.import_fdl_file(first)
    op2 = database.import_fdl_file(second)

    assert op1 is not None
    # Fixed behavior: the second (distinct) run is NOT suppressed by filename.
    assert op2 is not None
    _ops, rows = _counts()
    # Both runs' records persist. They share the first 56 timestamps but differ
    # in content (rpm 800 vs 2200), so those collide only if content matches -
    # here content differs, so all 56 + 300 land (filename-agnostic dedup).
    assert rows == 56 + 300


def test_same_data_under_different_filename_is_idempotent(temp_db):
    """FIX for weakness (b): identical records under a DIFFERENT filename are a
    record-level no-op (idempotent). One real operation stays one operation,
    because identity is (timestamp, content_hash), not filename. This is exactly
    the rotated-file / re-run hazard the record-level dedup neutralizes."""
    start = datetime(2026, 9, 28, 20, 12, 17)
    recs = _records(start, 100, rpm=2300.0)
    a = FDLFile("GRT FDL 0002.CSV", "x", list(recs))
    b = FDLFile("GRT FDL 0003.CSV", "x", list(recs))  # same samples, diff name

    database.import_fdl_file(a)
    database.import_fdl_file(b)

    ops, rows = _counts()
    # Fixed behavior: identical samples collapse - one operation, 100 rows.
    assert ops == 1
    assert rows == 100  # not 200: identical samples are deduped


def test_exact_reimport_is_noop(temp_db):
    """Baseline preserved: re-importing the EXACT same file is a no-op. The
    second import adds no records and reports nothing new (returns None)."""
    start = datetime(2026, 9, 28, 20, 12, 17)
    recs = _records(start, 100, rpm=2300.0)
    fdl = FDLFile("GRT FDL 0001.CSV", "x", recs)

    assert database.import_fdl_file(fdl) is not None
    assert database.import_fdl_file(FDLFile("GRT FDL 0001.CSV", "x", list(recs))) is None

    ops, rows = _counts()
    assert ops == 1
    assert rows == 100
