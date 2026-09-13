# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the PowerMap model, SID layout, and build_power_map (power.py).

Property-based tests (Hypothesis):
  - Property 6: sea-level row validity iff rpm, map55, map75 all > 0 (task 2.2)
  - Property 7: altitude row validity iff altitude > 0 (task 2.3)
  - Property 8: map unavailability (task 2.4)
Example tests: SID layout + RATED_HP read from the worked table (task 2.5).
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from efis_data_manager.power import (
    SID_ALTITUDE_COLUMN,
    SID_DELTA_HP_COLUMN,
    SID_MAP55_COLUMN,
    SID_MAP75_COLUMN,
    SID_RATED_HP,
    SID_RPM_COLUMN,
    build_power_map,
)


# ---------------------------------------------------------------------------
# Worked reference table (this aircraft: 190 HP IO-360). Shared by example
# tests here and the pipeline reference tests.
# ---------------------------------------------------------------------------
WORKED_RATED_HP = 190
WORKED_RPM = [2000, 2100, 2200, 2300, 2400, 2500, 2600, 2700]
WORKED_MAP55 = [21.6, 21.1, 20.6, 20.1, 19.7, 19.2, 18.7, 18.2]
WORKED_MAP75 = [26.7, 26.1, 25.5, 24.9, 24.4, 23.8, 23.2, 22.7]
WORKED_ALT = [2000, 4000, 6000, 8000, 10000, 12000, 14000]
WORKED_DELTA = [2.3, 4.6, 6.9, 9.2, 11.5, 13.8, 16.0]


def worked_sids() -> dict:
    """Build the SID dict for the worked reference table (RATED_HP = 190)."""
    sids = {SID_RATED_HP: str(WORKED_RATED_HP)}
    for i, val in enumerate(WORKED_RPM):
        sids[SID_RPM_COLUMN[i]] = str(val)
    for i, val in enumerate(WORKED_MAP55):
        sids[SID_MAP55_COLUMN[i]] = str(val)
    for i, val in enumerate(WORKED_MAP75):
        sids[SID_MAP75_COLUMN[i]] = str(val)
    for i, val in enumerate(WORKED_ALT):
        sids[SID_ALTITUDE_COLUMN[i]] = str(val)
    for i, val in enumerate(WORKED_DELTA):
        sids[SID_DELTA_HP_COLUMN[i]] = str(val)
    return sids


# ---------------------------------------------------------------------------
# SID layout constants pin (Requirement 1.2)
# ---------------------------------------------------------------------------
def test_sid_layout_constants():
    assert SID_RATED_HP == "167"
    assert SID_RPM_COLUMN == [str(s) for s in range(168, 178)]
    assert SID_MAP55_COLUMN == [str(s) for s in range(178, 188)]
    assert SID_MAP75_COLUMN == [str(s) for s in range(188, 198)]
    assert SID_ALTITUDE_COLUMN == [str(s) for s in range(198, 208)]
    assert SID_DELTA_HP_COLUMN == [str(s) for s in range(208, 218)]
    # All string keys.
    for col in (SID_RPM_COLUMN, SID_MAP55_COLUMN, SID_MAP75_COLUMN,
                SID_ALTITUDE_COLUMN, SID_DELTA_HP_COLUMN):
        assert all(isinstance(k, str) for k in col)


# ---------------------------------------------------------------------------
# Worked-table build: columns and RATED_HP (Requirements 1.1, 1.2)
# ---------------------------------------------------------------------------
def test_worked_table_columns_and_rated_hp():
    pm = build_power_map(worked_sids())
    assert pm is not None
    assert pm.rated_hp == 190.0
    assert pm.rpm == [float(v) for v in WORKED_RPM]
    assert pm.map55 == [float(v) for v in WORKED_MAP55]
    assert pm.map75 == [float(v) for v in WORKED_MAP75]
    assert pm.altitude == [float(v) for v in WORKED_ALT]
    assert pm.delta_hp == [float(v) for v in WORKED_DELTA]


def test_rated_hp_absent_yields_none():
    sids = worked_sids()
    del sids[SID_RATED_HP]
    assert build_power_map(sids) is None


def test_rated_hp_present_sets_value():
    sids = worked_sids()
    sids[SID_RATED_HP] = "200"
    pm = build_power_map(sids)
    assert pm is not None
    assert pm.rated_hp == 200.0


# ---------------------------------------------------------------------------
# Property 6: Sea-level row validity
# Feature: percent-power, Property 6: a sea-level row survives iff rpm>0 AND
#   map55>0 AND map75>0; surviving rows preserve pairing.
# Validates: Requirements 1.3, 1.4
# ---------------------------------------------------------------------------
_cell = st.floats(min_value=-30.0, max_value=40.0,
                  allow_nan=False, allow_infinity=False)


@settings(max_examples=200)
@given(
    rpms=st.lists(st.floats(-100, 3000, allow_nan=False, allow_infinity=False),
                  min_size=10, max_size=10),
    m55s=st.lists(_cell, min_size=10, max_size=10),
    m75s=st.lists(_cell, min_size=10, max_size=10),
)
def test_property6_sea_level_row_validity(rpms, m55s, m75s):
    sids = {SID_RATED_HP: "190"}
    for i in range(10):
        sids[SID_RPM_COLUMN[i]] = str(rpms[i])
        sids[SID_MAP55_COLUMN[i]] = str(m55s[i])
        sids[SID_MAP75_COLUMN[i]] = str(m75s[i])
    # Guarantee at least one valid altitude row so the sea-level check is what
    # gates None (isolate Property 6 from Property 7/8).
    sids[SID_ALTITUDE_COLUMN[0]] = "5000"
    sids[SID_DELTA_HP_COLUMN[0]] = "6.0"

    expected = [
        (rpms[i], m55s[i], m75s[i])
        for i in range(10)
        if rpms[i] > 0 and m55s[i] > 0 and m75s[i] > 0
    ]
    pm = build_power_map(sids)

    if not expected:
        assert pm is None
        return

    assert pm is not None
    # Same set of surviving rows (order is sorted by rpm), pairing preserved.
    built = list(zip(pm.rpm, pm.map55, pm.map75))
    assert sorted(built) == sorted(expected)
    # And built is ascending by rpm.
    assert pm.rpm == sorted(pm.rpm)


# ---------------------------------------------------------------------------
# Property 7: Altitude row validity
# Feature: percent-power, Property 7: an altitude row survives iff altitude>0
#   (delta_hp may be anything); surviving rows preserve pairing.
# Validates: Requirements 1.5
# ---------------------------------------------------------------------------
@settings(max_examples=200)
@given(
    alts=st.lists(st.floats(-5000, 20000, allow_nan=False, allow_infinity=False),
                  min_size=10, max_size=10),
    deltas=st.lists(st.floats(-50, 50, allow_nan=False, allow_infinity=False),
                    min_size=10, max_size=10),
)
def test_property7_altitude_row_validity(alts, deltas):
    sids = {SID_RATED_HP: "190"}
    # Guarantee one valid sea-level row so altitude validity gates None.
    sids[SID_RPM_COLUMN[0]] = "2400"
    sids[SID_MAP55_COLUMN[0]] = "19.7"
    sids[SID_MAP75_COLUMN[0]] = "24.4"
    for i in range(10):
        sids[SID_ALTITUDE_COLUMN[i]] = str(alts[i])
        sids[SID_DELTA_HP_COLUMN[i]] = str(deltas[i])

    expected = [(alts[i], deltas[i]) for i in range(10) if alts[i] > 0]
    pm = build_power_map(sids)

    if not expected:
        assert pm is None
        return

    assert pm is not None
    built = list(zip(pm.altitude, pm.delta_hp))
    assert sorted(built) == sorted(expected)
    assert pm.altitude == sorted(pm.altitude)


# ---------------------------------------------------------------------------
# Property 8: Map unavailability
# Feature: percent-power, Property 8: build_power_map returns None iff RATED_HP
#   missing/<=0 OR no valid sea-level row OR no valid altitude row; else non-None.
# Validates: Requirements 1.6, 1.7
# ---------------------------------------------------------------------------
@settings(max_examples=200)
@given(
    rated=st.one_of(st.none(), st.floats(-50, 300, allow_nan=False,
                                         allow_infinity=False)),
    has_sea=st.booleans(),
    has_alt=st.booleans(),
)
def test_property8_map_unavailability(rated, has_sea, has_alt):
    sids = {}
    if rated is not None:
        sids[SID_RATED_HP] = str(rated)
    if has_sea:
        sids[SID_RPM_COLUMN[0]] = "2400"
        sids[SID_MAP55_COLUMN[0]] = "19.7"
        sids[SID_MAP75_COLUMN[0]] = "24.4"
    if has_alt:
        sids[SID_ALTITUDE_COLUMN[0]] = "6000"
        sids[SID_DELTA_HP_COLUMN[0]] = "6.9"

    rated_ok = rated is not None and rated > 0
    should_be_available = rated_ok and has_sea and has_alt

    pm = build_power_map(sids)
    if should_be_available:
        assert pm is not None
    else:
        assert pm is None


def test_property8_non_numeric_rated_is_unavailable():
    sids = worked_sids()
    sids[SID_RATED_HP] = "not-a-number"
    assert build_power_map(sids) is None
