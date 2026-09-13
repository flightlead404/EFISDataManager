# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the eight-step percent_power pipeline (power.py).

Property-based tests (Hypothesis):
  - Property 2: sea-level percentage is affine through the 55%/75% anchors (3.2)
  - Property 4: OAT correction matches its Fahrenheit formula & is monotonic (3.3)
  - Property 9: Percent_Power is undefined on missing/invalid inputs (3.4)
Reference/example tests: worked table (RATED_HP = 190), incl. an extrapolation
  case and a degenerate m55 == m75 case (task 3.5).
"""

import math

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from efis_data_manager.power import (
    PowerMap,
    build_power_map,
    interp,
    isa_std_oat_f,
    percent_power,
)

from tests.test_power_map import (
    WORKED_RATED_HP,
    WORKED_RPM,
    WORKED_MAP55,
    WORKED_MAP75,
    WORKED_ALT,
    WORKED_DELTA,
    worked_sids,
)


_FINITE = dict(allow_nan=False, allow_infinity=False)


def _worked_map() -> PowerMap:
    pm = build_power_map(worked_sids())
    assert pm is not None
    return pm


# ---------------------------------------------------------------------------
# Property 2: Sea-level power percentage is affine through the 55%/75% anchors
# Feature: percent-power, Property 2: at zero altitude and standard OAT the
#   sea-level % is the affine line through (m55, 55) and (m75, 75); it equals 55
#   at MAP==m55, 75 at MAP==m75, and interpolates/extrapolates linearly. We
#   verify the sea-level anchor directly by isolating the altitude+OAT factors.
# Validates: Requirements 2.4
# ---------------------------------------------------------------------------
@settings(max_examples=200)
@given(
    rpm=st.floats(1800, 2900, **_FINITE),
    frac=st.floats(-0.5, 1.5, **_FINITE),
)
def test_property2_sea_level_affine_anchor(rpm, frac):
    pm = _worked_map()
    m55 = interp(pm.rpm, pm.map55, rpm)
    m75 = interp(pm.rpm, pm.map75, rpm)
    assume(m75 != m55)

    # A MAP a fraction of the way from the 55% anchor to the 75% anchor.
    map_inhg = m55 + frac * (m75 - m55)
    expected_sea_pct = 55.0 + (map_inhg - m55) * 20.0 / (m75 - m55)

    # Isolate the sea-level percentage: use altitude 0 (delta interp still adds
    # a value, so back it out) and OAT == ISA std so the correction is exactly 1.
    palt = 0.0
    oat_f = isa_std_oat_f(palt)  # correction == 1.0 exactly
    delta_hp = interp(pm.altitude, pm.delta_hp, palt)

    pp = percent_power(rpm, map_inhg, palt, oat_f, pm)
    assert pp is not None
    # pp = ((sea_pct/100*rated) + delta) / rated * 100 (correction == 1).
    # => sea_pct == pp - delta/rated*100.
    recovered_sea_pct = pp - delta_hp / pm.rated_hp * 100.0
    assert math.isclose(recovered_sea_pct, expected_sea_pct,
                        rel_tol=1e-9, abs_tol=1e-6)

    # Anchor endpoints: MAP==m55 -> 55%, MAP==m75 -> 75% (sea-level portion).
    pp55 = percent_power(rpm, m55, palt, oat_f, pm)
    pp75 = percent_power(rpm, m75, palt, oat_f, pm)
    assert math.isclose(pp55 - delta_hp / pm.rated_hp * 100.0, 55.0,
                        rel_tol=1e-9, abs_tol=1e-6)
    assert math.isclose(pp75 - delta_hp / pm.rated_hp * 100.0, 75.0,
                        rel_tol=1e-9, abs_tol=1e-6)


# ---------------------------------------------------------------------------
# Property 4: OAT correction matches its Fahrenheit formula and is monotonic
# Feature: percent-power, Property 4: correction == sqrt((460+s)/(460+o));
#   == 1 when o==s, > 1 when o < s, strictly decreasing in o.
# Validates: Requirements 2.8, 4.1
# ---------------------------------------------------------------------------
@settings(max_examples=200)
@given(
    rpm=st.floats(1800, 2900, **_FINITE),
    map_inhg=st.floats(18.0, 27.0, **_FINITE),
    palt=st.floats(0.0, 14000.0, **_FINITE),
    o1=st.floats(-40.0, 120.0, **_FINITE),
    delta=st.floats(1.0, 60.0, **_FINITE),
)
def test_property4_oat_correction(rpm, map_inhg, palt, o1, delta):
    pm = _worked_map()
    s = isa_std_oat_f(palt)

    # The correction the pipeline applies is recoverable by comparing two runs
    # that differ only in OAT: pp(o) / pp(std) == sqrt((460+s)/(460+o)), when
    # the (altitude-corrected HP) base is the same and nonzero.
    def base_and_pp(oat):
        m55 = interp(pm.rpm, pm.map55, rpm)
        m75 = interp(pm.rpm, pm.map75, rpm)
        if m75 == m55:
            return None, None
        sea_pct = 55.0 + (map_inhg - m55) * 20.0 / (m75 - m55)
        base = sea_pct / 100.0 * pm.rated_hp + interp(pm.altitude, pm.delta_hp, palt)
        return base, percent_power(rpm, map_inhg, palt, oat, pm)

    base, pp1 = base_and_pp(o1)
    assume(base is not None and abs(base) > 1e-6)

    # correction == 1 exactly when o == s.
    _, pp_std = base_and_pp(s)
    corr_std = pp_std / (base / pm.rated_hp * 100.0)
    assert math.isclose(corr_std, 1.0, rel_tol=1e-9, abs_tol=1e-9)

    # Explicit formula for o1.
    expected_corr = math.sqrt((460.0 + s) / (460.0 + o1))
    actual_corr = pp1 / (base / pm.rated_hp * 100.0)
    assert math.isclose(actual_corr, expected_corr, rel_tol=1e-9, abs_tol=1e-9)

    # Monotonic: colder OAT (o2 < o1) gives a strictly larger correction when
    # the base is positive.
    o2 = o1 - delta
    _, pp2 = base_and_pp(o2)
    if base > 0:
        assert pp2 > pp1
        # And o1 warmer-than-standard gives correction < 1.
        if o1 > s:
            assert actual_corr < 1.0
        if o1 < s:
            assert actual_corr > 1.0


# ---------------------------------------------------------------------------
# Property 9: Percent_Power is undefined on missing or invalid inputs
# Feature: percent-power, Property 9: if any of rpm/map/palt/oat is None or
#   non-finite (NaN/inf), percent_power returns None.
# Validates: Requirements 5.1
# ---------------------------------------------------------------------------
_BAD = st.sampled_from([None, float("nan"), float("inf"), float("-inf")])
_GOOD = st.floats(-1e4, 1e4, **_FINITE)


@settings(max_examples=200)
@given(
    which=st.integers(0, 3),
    bad=_BAD,
    rpm=_GOOD, map_inhg=_GOOD, palt=_GOOD, oat=_GOOD,
)
def test_property9_undefined_on_bad_input(which, bad, rpm, map_inhg, palt, oat):
    pm = _worked_map()
    args = [rpm, map_inhg, palt, oat]
    args[which] = bad
    assert percent_power(args[0], args[1], args[2], args[3], pm) is None


# ---------------------------------------------------------------------------
# Reference / example tests (Requirements 2, 7)
# ---------------------------------------------------------------------------
def test_reference_worked_point_2400_22_6000_40():
    """Hand-computed reference: ~68.3% (design Testing Strategy)."""
    pm = _worked_map()
    pp = percent_power(2400, 22.0, 6000, 40.0, pm)
    assert pp is not None
    assert abs(pp - 68.3) <= 0.5


def test_reference_manual_walkthrough_matches():
    """Recompute the 8 steps by hand from the imported table and match."""
    pm = _worked_map()
    rpm, map_inhg, palt, oat_f = 2400, 22.0, 6000, 40.0
    m55 = interp(pm.rpm, pm.map55, rpm)   # 19.7
    m75 = interp(pm.rpm, pm.map75, rpm)   # 24.4
    sea_pct = 55.0 + (map_inhg - m55) * 20.0 / (m75 - m55)
    sea_hp = sea_pct / 100.0 * pm.rated_hp
    delta = interp(pm.altitude, pm.delta_hp, palt)  # 6.9
    alt_hp = sea_hp + delta
    corr = math.sqrt((460 + isa_std_oat_f(palt)) / (460 + oat_f))
    expected = alt_hp * corr / pm.rated_hp * 100.0
    got = percent_power(rpm, map_inhg, palt, oat_f, pm)
    assert math.isclose(got, expected, rel_tol=1e-9, abs_tol=1e-9)


def test_reference_extrapolation_high_rpm_altitude():
    """An extrapolation case: RPM above 2700 and altitude above 14000 ft.

    interp extrapolates beyond the table ends; the result is still defined and
    NOT clamped.
    """
    pm = _worked_map()
    pp = percent_power(2800, 28.0, 16000, 20.0, pm)
    assert pp is not None
    assert math.isfinite(pp)
    # Extrapolated high MAP (well above the 75% anchor) should exceed 75%.
    m55 = interp(pm.rpm, pm.map55, 2800)
    m75 = interp(pm.rpm, pm.map75, 2800)
    assert 28.0 > m75  # confirms we are extrapolating above the 75% anchor
    assert pp > 75.0


def test_reference_low_extrapolation_below_2000_rpm():
    pm = _worked_map()
    pp = percent_power(1800, 18.0, 1000, 50.0, pm)
    assert pp is not None
    assert math.isfinite(pp)


def test_degenerate_anchor_returns_none():
    """A map where map55 == map75 at every RPM -> that sample returns None."""
    sids = worked_sids()
    # Force map75 == map55 across all rows.
    from efis_data_manager.power import SID_MAP75_COLUMN, SID_MAP55_COLUMN
    for i in range(len(WORKED_MAP55)):
        sids[SID_MAP75_COLUMN[i]] = sids[SID_MAP55_COLUMN[i]]
    pm = build_power_map(sids)
    assert pm is not None
    assert percent_power(2400, 22.0, 6000, 40.0, pm) is None


def test_oat_correction_domain_guard():
    """A corrupt OAT with 460 + oat <= 0 returns None rather than raising."""
    pm = _worked_map()
    assert percent_power(2400, 22.0, 6000, -500.0, pm) is None
