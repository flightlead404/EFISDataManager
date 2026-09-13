# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for the cruise-average Percent_Power rule (analysis.py).

Property-based test (Hypothesis):
  - Property 10: cruise-average ignores undefined samples; equals the mean of
    the defined per-sample values, and None when none are defined (task 7.2).
"""

import math

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# monkeypatch sets the SAME module-level attributes on every generated input,
# so re-using the function-scoped fixture across examples is safe here (same
# pattern as tests/test_efis_settings_alerts.py).
_SUPPRESS = [HealthCheck.function_scoped_fixture]

import efis_data_manager.analysis as analysis
import efis_data_manager.power as power
import efis_data_manager.aux_map as aux_map

from tests.test_power_map import worked_sids


_FINITE = dict(allow_nan=False, allow_infinity=False)


def _patch_map(monkeypatch, celsius=False):
    """Patch the settings read so analysis builds the worked PowerMap, and make
    resolve_aux route MAP to internal_map (so cruise rows use r['internal_map']).
    """
    monkeypatch.setattr(power, "_read_settings",
                        lambda: (worked_sids(), celsius))
    # Empty resolved aux -> map_column_for falls back to 'internal_map'.
    monkeypatch.setattr(aux_map, "resolve_aux", lambda *a, **k: {})


# A cruise "row" is a plain dict supporting r[col] and r.keys() like sqlite3.Row.
def _row(rpm=None, mapv=None, palt=None, oat=None):
    return {"rpm1": rpm, "internal_map": mapv,
            "pressure_altitude": palt, "oat": oat}


_valid = st.tuples(
    st.floats(1900, 2800, **_FINITE),   # rpm
    st.floats(18.0, 27.0, **_FINITE),   # map
    st.floats(0.0, 14000.0, **_FINITE), # palt
    st.floats(-30.0, 110.0, **_FINITE), # oat (F)
)
# An "undefined" sample: at least one field is None (missing input).
_undefined = st.just((None, None, None, None))


# ---------------------------------------------------------------------------
# Property 10: Cruise-average percent power ignores undefined samples
# Feature: percent-power, Property 10: the cruise average equals the arithmetic
#   mean of the DEFINED per-sample Percent_Power values (undefined excluded),
#   and equals None when no cruise sample has a defined value.
# Validates: Requirements 6.2
# ---------------------------------------------------------------------------
@settings(max_examples=200, suppress_health_check=_SUPPRESS)
@given(
    samples=st.lists(st.one_of(_valid, _undefined), min_size=0, max_size=25),
)
def test_property10_cruise_average_ignores_undefined(samples, monkeypatch):
    _patch_map(monkeypatch)
    pm = power.build_power_map(worked_sids())

    rows = []
    expected_defined = []
    for rpm, mapv, palt, oat in samples:
        rows.append(_row(rpm, mapv, palt, oat))
        # Recompute the reference per-sample value (oat is already F here).
        pp = power.percent_power(rpm, mapv, palt, oat, pm)
        if pp is not None:
            expected_defined.append(pp)

    got = analysis._avg_percent_power_cruise(rows)

    if not expected_defined:
        assert got is None
    else:
        expected_mean = sum(expected_defined) / len(expected_defined)
        assert got is not None
        assert math.isclose(got, expected_mean, rel_tol=1e-9, abs_tol=1e-9)


def test_cruise_average_none_when_map_unavailable(monkeypatch):
    monkeypatch.setattr(power, "_read_settings", lambda: None)
    monkeypatch.setattr(aux_map, "resolve_aux", lambda *a, **k: {})
    rows = [_row(2400, 22.0, 6000, 40.0)]
    assert analysis._avg_percent_power_cruise(rows) is None


def test_cruise_average_empty_rows_is_none(monkeypatch):
    _patch_map(monkeypatch)
    assert analysis._avg_percent_power_cruise([]) is None
