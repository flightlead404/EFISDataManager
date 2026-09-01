# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for aux4-aux6 persistence and migration in database.py (Req 5)."""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from efis_data_manager import database
from efis_data_manager.fdl_parser import FDLFile, FDLRecord


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point the database module at a throwaway SQLite file."""
    db_dir = tmp_path / "logs"
    db_dir.mkdir()
    db_path = db_dir / "efis_data.sqlite"
    monkeypatch.setattr(database, "DB_DIR", db_dir)
    monkeypatch.setattr(database, "DB_PATH", db_path)
    return db_path


def _make_record(ts, **kwargs):
    return FDLRecord(timestamp=ts, **kwargs)


def _make_fdl(n=3, aux_values=None):
    """Build a minimal FDLFile with n records. aux_values overrides aux4-6."""
    base = datetime(2026, 1, 1, 12, 0, 0)
    records = []
    for i in range(n):
        kw = dict(
            tick=i,
            aux1=1.0 + i, aux2=2.0 + i, aux3=3.0 + i,
        )
        if aux_values:
            kw.update(aux_values)
        records.append(_make_record(base + timedelta(seconds=i), **kw))
    return FDLFile(
        source_filename="TEST.FDL",
        source_path="/tmp/TEST.FDL",
        records=records,
    )


def test_fresh_schema_has_aux4_6(temp_db):
    """A freshly created DB includes aux4/aux5/aux6 columns."""
    conn = database.get_db_connection()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(fdl_data)")}
    finally:
        conn.close()
    assert {"aux4", "aux5", "aux6"}.issubset(cols)


def test_insert_persists_aux4_6(temp_db):
    """Importing a flight stores aux4/aux5/aux6 values (not just aux1-3)."""
    fdl = _make_fdl(n=2, aux_values={"aux4": 44.0, "aux5": 55.0, "aux6": 66.0})
    op_id = database.import_fdl_file(fdl)
    assert op_id is not None

    conn = database.get_db_connection()
    try:
        rows = conn.execute(
            "SELECT aux1, aux2, aux3, aux4, aux5, aux6 FROM fdl_data "
            "WHERE operation_id = ? ORDER BY id",
            (op_id,),
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 2
    for row in rows:
        assert row["aux4"] == 44.0
        assert row["aux5"] == 55.0
        assert row["aux6"] == 66.0


def test_migration_adds_columns_without_data_loss(temp_db):
    """An older DB lacking aux4-6 gets them added, keeping all rows."""
    # Build a legacy fdl_data table with only aux1-aux3 and two rows.
    conn = sqlite3.connect(str(temp_db))
    conn.execute(
        """CREATE TABLE fdl_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            aux1 REAL, aux2 REAL, aux3 REAL
        )"""
    )
    conn.executemany(
        "INSERT INTO fdl_data (operation_id, timestamp, aux1, aux2, aux3) "
        "VALUES (?,?,?,?,?)",
        [
            (1, "2026-01-01T12:00:00", 1.0, 2.0, 3.0),
            (1, "2026-01-01T12:00:01", 1.1, 2.1, 3.1),
        ],
    )
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM fdl_data").fetchone()[0]
    legacy_cols = {r[1] for r in conn.execute("PRAGMA table_info(fdl_data)")}
    conn.close()

    assert "aux4" not in legacy_cols  # sanity: truly legacy

    # Opening through the app runs _ensure_schema migrations.
    conn = database.get_db_connection()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(fdl_data)")}
        after = conn.execute("SELECT COUNT(*) FROM fdl_data").fetchone()[0]
        # Existing data preserved, aux4-6 present and NULL for old rows.
        old_rows = conn.execute(
            "SELECT aux1, aux4, aux5, aux6 FROM fdl_data ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    assert {"aux4", "aux5", "aux6"}.issubset(cols)
    assert after == before == 2  # no row loss
    for row in old_rows:
        assert row["aux1"] is not None
        assert row["aux4"] is None
        assert row["aux5"] is None
        assert row["aux6"] is None
