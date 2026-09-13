# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alerting-side property tests for the EFIS Settings Import feature.

These run the REAL analysis engine (detect_episodes / get_flight_stats) against
synthetic fdl_data rows in a temp SQLite database — no mocking of the engine.

Covers:
  - Property 21: RPM redline alert          (tasks.md 8.4, Req 11.1-11.3)
  - Property 22: CHT shock-cooling rate alert (tasks.md 8.5, Req 12.2-12.4)
  - Property 14: Active airspeed limit alert (tasks.md 8.6, Req 5.5, 5.6)

Uses the same temp_db / _insert / _set pattern as
tests/test_airspeed_g_episodes.py.
"""

from datetime import datetime, timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# The temp_db fixture is a fresh empty SQLite DB; each generated example calls
# _insert() which DELETEs and re-inserts its own rows, so re-using the
# function-scoped fixture across Hypothesis examples is safe here.
_SUPPRESS = [HealthCheck.function_scoped_fixture]

from efis_data_manager import analysis, database


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


def _insert(op_id=1, tas=None, ias=None, g=None, rpm=None, cht=None, n=120):
    """Insert one operation with n 1-Hz fdl_data rows.

    Any of tas/ias/g/rpm may be a per-sample list; cht may be a per-sample list
    written to cht1..cht4 (same value across cylinders — the engine tracks the
    hottest cylinder, so identical values give a deterministic series).
    """
    # Reset any prior rows for this op so property re-runs are independent.
    conn = database.get_db_connection()
    conn.execute("DELETE FROM fdl_data WHERE operation_id = ?", (op_id,))
    conn.execute("DELETE FROM operations WHERE id = ?", (op_id,))
    conn.execute(
        "INSERT INTO operations (id, source_filename, start_time, end_time, "
        "duration_seconds, record_count, date, imported_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (op_id, "t.csv", "2026-01-01T00:00:00", "2026-01-01T00:02:00",
         n, n, "2026-01-01", "2026-01-01T00:03:00"))
    t0 = datetime(2026, 1, 1, 0, 0, 0)
    for i in range(n):
        ts = (t0 + timedelta(seconds=i)).isoformat()
        cht_v = cht[i] if cht else None
        conn.execute(
            "INSERT INTO fdl_data (operation_id, timestamp, tick, "
            "indicated_airspeed, true_airspeed, g_load, rpm1, "
            "cht1, cht2, cht3, cht4) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (op_id, ts, i,
             ias[i] if ias else None,
             tas[i] if tas else None,
             g[i] if g else None,
             rpm[i] if rpm else None,
             cht_v, cht_v, cht_v, cht_v))
    conn.commit()
    conn.close()


def _set(monkeypatch, **over):
    base = dict(analysis.DEFAULT_THRESHOLDS)
    base.update(over)
    monkeypatch.setattr(analysis, "get_thresholds", lambda: base)


# ---------------------------------------------------------------------------
# Property 21: RPM redline alert
# Feature: efis-settings-import, Property 21
# Validates: Requirements 11.1, 11.2, 11.3
# ---------------------------------------------------------------------------

@given(
    peak=st.integers(min_value=2000, max_value=3200),
    redline=st.integers(min_value=2200, max_value=2900),
)
@settings(max_examples=150, deadline=None, suppress_health_check=_SUPPRESS)
def test_property21_rpm_redline_alert(temp_db, monkeypatch, peak, redline):
    """Episode AND summary alert appear iff max(rpm1) >= rpm_redline; when
    rpm_redline is unset, no RPM alert for any series."""
    # A steady run at `peak` RPM for the whole flight (sustained so an episode
    # survives the min-gap hysteresis).
    _insert(rpm=[peak] * 120)

    # --- rpm_redline SET ---
    _set(monkeypatch, rpm_redline=redline)
    eps = [e for e in analysis.detect_episodes(1) if e.parameter == "RPM"]
    stats = analysis.get_flight_stats(1)
    summary = [a for a in stats.alerts if "RPM" in a]

    if peak >= redline:
        assert eps and all(e.severity == "warning" for e in eps)
        assert summary
    else:
        assert not eps
        assert not summary

    # --- rpm_redline UNSET (None) => never any RPM alert ---
    _set(monkeypatch, rpm_redline=None)
    assert not any(e.parameter == "RPM" for e in analysis.detect_episodes(1))
    stats = analysis.get_flight_stats(1)
    assert not any("RPM" in a for a in stats.alerts)


# ---------------------------------------------------------------------------
# Property 22: CHT shock-cooling rate alert
# Feature: efis-settings-import, Property 22
# Validates: Requirements 12.2, 12.3, 12.4
# ---------------------------------------------------------------------------

# The engine measures cooling over a W=10 s window at 1 Hz:
#   windowed_rate(t) = (cht[t-W] - cht[t]) / (W/60)  [°/min, + = cooling]
# For a constant linear slope of `s` °/sec, cht[t-W]-cht[t] = s*W, so the
# windowed rate is exactly s*60 °/min, independent of W. We exploit that clean
# relationship to make the pass/fail boundary deterministic.

def _linear_cool(start, slope_per_sec, n=120):
    """CHT series cooling linearly at slope_per_sec °/sec (positive = cooling)."""
    return [start - slope_per_sec * i for i in range(n)]


@given(
    slope=st.floats(min_value=0.1, max_value=3.0),
    threshold=st.integers(min_value=30, max_value=150),
)
@settings(max_examples=150, deadline=None, suppress_health_check=_SUPPRESS)
def test_property22_cht_cooling_alert(temp_db, monkeypatch, slope, threshold):
    """Episode AND summary alert fire iff the max windowed cooling rate exceeds
    cht_cooling_rate_max."""
    # Start hot enough that 120 s of cooling stays above the 100°F valid floor.
    start = 400.0 + slope * 120
    _insert(cht=_linear_cool(start, slope))
    expected_rate = slope * 60.0  # °/min

    _set(monkeypatch, cht_cooling_rate_max=threshold)
    eps = [e for e in analysis.detect_episodes(1) if e.parameter == "CHT cooling"]
    stats = analysis.get_flight_stats(1)
    summary = [a for a in stats.alerts if "cooling" in a.lower()]

    # Guard against floating-point boundary flakiness: only assert away from the
    # exact equality edge (the code uses strict > for the rate episode/summary).
    if abs(expected_rate - threshold) > 1.0:
        if expected_rate > threshold:
            assert eps and all(e.severity == "caution" for e in eps)
            assert summary
        else:
            assert not eps
            assert not summary


def test_property22_noisy_flat_no_false_alert(temp_db, monkeypatch):
    """A noisy-but-flat CHT series (1 Hz sensor jitter, no real trend) must NOT
    trip the cooling alert — the window rejects single-sample noise."""
    # Alternating +/-8°F jitter around 380°F: big sample-to-sample deltas but a
    # net-zero trend over the 10 s window.
    cht = [380 + (8 if i % 2 == 0 else -8) for i in range(120)]
    _insert(cht=cht)
    _set(monkeypatch, cht_cooling_rate_max=30)
    eps = [e for e in analysis.detect_episodes(1) if e.parameter == "CHT cooling"]
    stats = analysis.get_flight_stats(1)
    assert not eps
    assert not any("cooling" in a.lower() for a in stats.alerts)


def test_property22_unset_no_alert_even_steep(temp_db, monkeypatch):
    """When cht_cooling_rate_max is unset, no cooling alert even for a steep
    descent (Req 12.4)."""
    _insert(cht=_linear_cool(700.0, 3.0))   # 180°/min cooling
    _set(monkeypatch, cht_cooling_rate_max=None)
    eps = [e for e in analysis.detect_episodes(1) if e.parameter == "CHT cooling"]
    stats = analysis.get_flight_stats(1)
    assert not eps
    assert not any("cooling" in a.lower() for a in stats.alerts)


# ---------------------------------------------------------------------------
# Property 14 (analysis side): Active airspeed limit alerts iff reached
# Feature: efis-settings-import, Property 14
# Validates: Requirements 5.5, 5.6
# ---------------------------------------------------------------------------

@given(
    peak=st.integers(min_value=120, max_value=320),
    vne=st.integers(min_value=150, max_value=260),
    tas_active=st.booleans(),
)
@settings(max_examples=150, deadline=None, suppress_health_check=_SUPPRESS)
def test_property14_active_airspeed_vne(temp_db, monkeypatch, peak, vne, tas_active):
    """For both the TAS-active and IAS-active configurations, the Vne alert
    fires iff max(active airspeed) >= active Vne. The mapper keeps the two keys
    mutually exclusive (inactive one holds the 9999 sentinel)."""
    if tas_active:
        # TAS active, IAS disabled. Put the airspeed only on the active channel.
        _insert(tas=[peak] * 120)
        _set(monkeypatch, vne_tas_redline=vne, vne_ias_redline=9999)
        label = "Vne (TAS)"
    else:
        _insert(ias=[peak] * 120)
        _set(monkeypatch, vne_ias_redline=vne, vne_tas_redline=9999)
        label = "Vne (IAS)"

    eps = [e for e in analysis.detect_episodes(1) if e.parameter == label]
    stats = analysis.get_flight_stats(1)
    summary = [a for a in stats.alerts if "Vne" in a]

    if peak >= vne:
        assert eps and all(e.severity == "warning" for e in eps)
        assert summary
    else:
        assert not eps
        assert not summary

    # Exactly one Vne key is active — the inactive channel never alerts even if
    # it held the same airspeed value.
    other = "Vne (IAS)" if tas_active else "Vne (TAS)"
    assert not any(e.parameter == other for e in analysis.detect_episodes(1))
