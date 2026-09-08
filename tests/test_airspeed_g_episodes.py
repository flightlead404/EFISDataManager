# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for Vne (TAS) and G-load exceedance episode detection.

Per design:
  - Vne is checked against TRUE airspeed (flutter/TAS limit), warning only,
    disabled by default (9999 placeholder).
  - Positive and negative G each have a caution and a limit (warning at/beyond
    the limit).
"""

from datetime import datetime, timedelta

import pytest

from efis_data_manager import analysis, database


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    dbp = tmp_path / "efis_data.sqlite"
    monkeypatch.setattr(database, "DB_PATH", dbp)
    monkeypatch.setattr(database, "DB_DIR", tmp_path)
    conn = database.get_db_connection()
    conn.executescript(database.SCHEMA_SQL)
    conn.commit(); conn.close()
    return dbp


def _insert(op_id=1, tas=None, g=None, n=120):
    conn = database.get_db_connection()
    conn.execute(
        "INSERT INTO operations (id, source_filename, start_time, end_time, "
        "duration_seconds, record_count, date, imported_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (op_id, "t.csv", "2026-01-01T00:00:00", "2026-01-01T00:02:00",
         n, n, "2026-01-01", "2026-01-01T00:03:00"))
    t0 = datetime(2026, 1, 1, 0, 0, 0)
    for i in range(n):
        ts = (t0 + timedelta(seconds=i)).isoformat()
        conn.execute(
            "INSERT INTO fdl_data (operation_id, timestamp, tick, true_airspeed, g_load) "
            "VALUES (?,?,?,?,?)",
            (op_id, ts, i, tas[i] if tas else None, g[i] if g else None))
    conn.commit(); conn.close()


def _set(monkeypatch, **over):
    base = dict(analysis.DEFAULT_THRESHOLDS); base.update(over)
    monkeypatch.setattr(analysis, "get_thresholds", lambda: base)


def test_vne_default_is_active(temp_db, monkeypatch):
    # Default Vne is now 200 KTAS (RV-8). A sustained 250 KTAS run flags a warning.
    _insert(tas=[250] * 120)
    _set(monkeypatch)
    eps = [e for e in analysis.detect_episodes(1) if e.parameter.startswith("Vne")]
    assert eps and all(e.severity == "warning" for e in eps)


def test_vne_can_be_disabled(temp_db, monkeypatch):
    # Setting the placeholder 9999 disables the check.
    _insert(tas=[250] * 120)
    _set(monkeypatch, vne_tas_redline=9999)
    assert not any(e.parameter.startswith("Vne") for e in analysis.detect_episodes(1))


def test_vne_tas_warning(temp_db, monkeypatch):
    _insert(tas=[178] * 120)
    _set(monkeypatch, vne_tas_redline=174)
    eps = [e for e in analysis.detect_episodes(1) if e.parameter.startswith("Vne")]
    assert eps and all(e.severity == "warning" for e in eps)


def test_vne_no_caution_tier(temp_db, monkeypatch):
    # Just below Vne -> no episode at all (Vne is warning-only, no caution band).
    _insert(tas=[170] * 120)
    _set(monkeypatch, vne_tas_redline=174)
    assert not any(e.parameter.startswith("Vne") for e in analysis.detect_episodes(1))


def test_pos_g_caution_then_limit(temp_db, monkeypatch):
    # caution-only excursion
    _insert(op_id=1, g=[1.0] * 40 + [3.4] * 40 + [1.0] * 40)
    _set(monkeypatch)  # +caution 3.0, +limit 3.8
    eps = [e for e in analysis.detect_episodes(1) if e.parameter == "G-load (+)"]
    assert eps and all(e.severity == "caution" for e in eps)


def test_pos_g_limit_warning(temp_db, monkeypatch):
    _insert(op_id=1, g=[1.0] * 40 + [4.2] * 40 + [1.0] * 40)
    _set(monkeypatch)
    eps = [e for e in analysis.detect_episodes(1) if e.parameter == "G-load (+)"]
    assert eps and any(e.severity == "warning" for e in eps)


def test_neg_g_caution(temp_db, monkeypatch):
    _insert(op_id=1, g=[1.0] * 50 + [-2.0] * 30 + [1.0] * 40)
    _set(monkeypatch)  # -caution -1.5, -limit -3.0
    eps = [e for e in analysis.detect_episodes(1) if e.parameter == "G-load (-)"]
    assert eps and all(e.severity == "caution" for e in eps)


def test_neg_g_limit_warning(temp_db, monkeypatch):
    _insert(op_id=1, g=[1.0] * 50 + [-3.5] * 30 + [1.0] * 40)
    _set(monkeypatch)
    eps = [e for e in analysis.detect_episodes(1) if e.parameter == "G-load (-)"]
    assert eps and any(e.severity == "warning" for e in eps)
