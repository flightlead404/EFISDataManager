# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Regression: fuel_used ignores post-refuel gauge resets (sum of decreases).

Bug: fuel_used was abs(first - last) of the fuel_total series, which counted a
post-start refuel reset (an upward step) as burned fuel — a ~15 min flight
reported 20+ gal. Fix: sum only the per-sample decreases, so upward resets
contribute nothing while a no-reset series still telescopes to first - last.

Spec: fuel-used-ignores-refuel-reset.
"""

import pytest

from efis_data_manager.database import _fuel_used_from_totals


def test_refuel_reset_is_not_counted_as_burn():
    """The observed KPIM->KCCO shape: 18.8 (idle) -> 42.0 (reset) -> 39.0 end.
    Old abs(first-last) = 20.2; correct burn = ~3.0."""
    series = [18.8] * 12 + [42.0, 41.0, 40.0, 39.0]
    assert _fuel_used_from_totals(series) == pytest.approx(3.0, abs=1e-6)


def test_monotonic_decreasing_equals_first_minus_last():
    """No reset: sum of decreases telescopes to first - last (preservation)."""
    series = [40.0, 38.5, 37.0, 35.2, 33.0]
    assert _fuel_used_from_totals(series) == pytest.approx(40.0 - 33.0)


def test_multiple_resets_accumulate_only_drains():
    """Two refuels mid-log: burn is the sum of the drains between resets."""
    # drain 2, reset up, drain 3, reset up, drain 1  => 6 total
    series = [42.0, 40.0, 50.0, 47.0, 48.0, 47.0]
    # decreases: 2 (40<-42), 3 (47<-50), 1 (47<-48) = 6
    assert _fuel_used_from_totals(series) == pytest.approx(6.0)


def test_fewer_than_two_samples_is_none():
    assert _fuel_used_from_totals([]) is None
    assert _fuel_used_from_totals([25.0]) is None
    assert _fuel_used_from_totals([None, None]) is None


def test_none_samples_are_ignored():
    series = [None, 40.0, None, 38.0, None]
    assert _fuel_used_from_totals(series) == pytest.approx(2.0)


def test_migration_recomputes_inflated_value(tmp_path, monkeypatch):
    """The recompute migration fixes an already-stored inflated fuel_used."""
    import sqlite3
    from efis_data_manager import database as db

    dbfile = tmp_path / "t.sqlite"
    conn = sqlite3.connect(str(dbfile))
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA_SQL)
    # One operation with the bad stored value.
    conn.execute(
        "INSERT INTO operations "
        "(id, source_filename, start_time, end_time, duration_seconds, "
        " record_count, has_flight, airborne_seconds, date, imported_at, fuel_used) "
        "VALUES (1, 'x.csv', '2026-10-24T16:00:00', '2026-10-24T16:15:00', 900, "
        " 6, 1, 875, '2026-10-24', '2026-10-24T18:00:00', 20.2)"
    )
    # fuel_total samples reproducing the reset shape, in timestamp order.
    series = [18.8, 18.8, 42.0, 41.0, 40.0, 39.0]
    for i, v in enumerate(series):
        conn.execute(
            "INSERT INTO fdl_data (operation_id, timestamp, fuel_total) VALUES (?, ?, ?)",
            (1, f"2026-10-24T16:0{i}:00", v),
        )
    conn.commit()

    summary = db.migrate_recompute_fuel_used(conn, force=True)
    assert summary["applied"] is True
    assert summary["operations_updated"] == 1

    new_val = conn.execute("SELECT fuel_used FROM operations WHERE id = 1").fetchone()[0]
    assert new_val == pytest.approx(3.0, abs=1e-6)
    conn.close()
