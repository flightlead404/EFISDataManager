# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property tests for power.py pure helpers: interp, isa_std_oat_f,
oat_to_fahrenheit.

Property-based tests (Hypothesis):
  - Property 1: interpolation/extrapolation are piecewise-linear (task 1.2)
  - Property 3: ISA standard OAT is linear and 59 F at sea level (task 1.3)
  - Property 5: OAT unit conversion (task 1.4)
"""

import math

from hypothesis import given, settings
from hypothesis import strategies as st

from efis_data_manager.power import interp, isa_std_oat_f, oat_to_fahrenheit


_FINITE = dict(allow_nan=False, allow_infinity=False, width=64)


@st.composite
def _ascending_xs_ys(draw):
    """Distinct ascending xs (len >= 2) paired with arbitrary finite ys."""
    n = draw(st.integers(min_value=2, max_value=8))
    xs_set = draw(
        st.lists(
            st.floats(min_value=-1e4, max_value=1e4, **_FINITE),
            min_size=n, max_size=n, unique=True,
        )
    )
    xs = sorted(xs_set)
    # Guarantee strictly ascending with a comfortable gap (avoid float ties).
    for i in range(1, len(xs)):
        if xs[i] - xs[i - 1] < 1e-3:
            xs[i] = xs[i - 1] + 1.0
    ys = draw(
        st.lists(
            st.floats(min_value=-1e4, max_value=1e4, **_FINITE),
            min_size=n, max_size=n,
        )
    )
    return xs, ys


# ---------------------------------------------------------------------------
# Property 1: Interpolation and extrapolation are piecewise-linear
# Feature: percent-power, Property 1: interp(xs, ys, x) equals the linear
#   segment value; extrapolation is collinear with the first/last two points;
#   interp(xs, ys, xs[k]) == ys[k].
# Validates: Requirements 3.1, 3.2, 2.2, 2.3, 2.6
# ---------------------------------------------------------------------------
@settings(max_examples=200)
@given(data=_ascending_xs_ys(), t=st.floats(min_value=-3.0, max_value=3.0, **_FINITE))
def test_property1_interp_piecewise_linear(data, t):
    xs, ys = data
    n = len(xs)

    # Breakpoint identity holds exactly at every knot.
    for k in range(n):
        assert interp(xs, ys, xs[k]) == ys[k]

    # Pick a segment index and evaluate at t (t<0 or t>1 -> extrapolation).
    # Interior segments use their own bounds; below xs[0] and above xs[-1] use
    # the first/last segment respectively, matching interp's contract.
    for i in range(n - 1):
        x0, x1 = xs[i], xs[i + 1]
        y0, y1 = ys[i], ys[i + 1]
        x = x0 + t * (x1 - x0)
        # Only assert when x lands in the region this segment governs, so the
        # expected linear value matches interp's chosen segment.
        if t < 0 and i != 0:
            continue  # below-range extrapolation only uses the first segment
        if t > 1 and i != n - 2:
            continue  # above-range extrapolation only uses the last segment
        expected = y0 + t * (y1 - y0)
        got = interp(xs, ys, x)
        assert math.isclose(got, expected, rel_tol=1e-9, abs_tol=1e-6)


# ---------------------------------------------------------------------------
# Property 3: ISA standard OAT is linear in altitude and 59 F at sea level
# Feature: percent-power, Property 3: isa_std_oat_f(h) == 59.0 - 3.564*(h/1000)
# Validates: Requirements 2.8, 4.2
# ---------------------------------------------------------------------------
@settings(max_examples=200)
@given(h=st.floats(min_value=-2000.0, max_value=60000.0, **_FINITE))
def test_property3_isa_std_oat_linear(h):
    got = isa_std_oat_f(h)
    expected = 59.0 - 3.564 * (h / 1000.0)
    assert math.isclose(got, expected, rel_tol=1e-12, abs_tol=1e-9)
    # Equivalent Celsius-lapse form.
    expected_c_form = (15.0 - 1.98 * h / 1000.0) * 9 / 5 + 32
    assert math.isclose(got, expected_c_form, rel_tol=1e-9, abs_tol=1e-6)


def test_property3_isa_sea_level_is_59():
    assert isa_std_oat_f(0.0) == 59.0
    # Decreases by 3.564 F per 1000 ft.
    assert math.isclose(isa_std_oat_f(0.0) - isa_std_oat_f(1000.0), 3.564,
                        rel_tol=1e-12, abs_tol=1e-9)


# ---------------------------------------------------------------------------
# Property 5: OAT unit conversion
# Feature: percent-power, Property 5: oat_to_fahrenheit(v, True) == v*9/5+32;
#   oat_to_fahrenheit(v, False) == v.
# Validates: Requirements 4.3
# ---------------------------------------------------------------------------
@settings(max_examples=200)
@given(v=st.floats(min_value=-100.0, max_value=200.0, **_FINITE))
def test_property5_oat_unit_conversion(v):
    assert math.isclose(oat_to_fahrenheit(v, True), v * 9 / 5 + 32,
                        rel_tol=1e-12, abs_tol=1e-9)
    assert oat_to_fahrenheit(v, False) == v
