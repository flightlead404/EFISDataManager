# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for Settings_Mapper and the declarative SID table (efis_settings.py).

Example tests for the SID->threshold table plus property-based tests
(Hypothesis, Properties 8, 9, 10, 11, 12, 13, 15).

Requirements: 4.1-4.9, 5.1, 5.3, 5.4, 5.7, 6.1, 6.2, 6.3, 6.4, 7.1-7.5,
              12.1, 13.1, 13.2, 13.4, 1.3, 4.10
"""

from efis_data_manager.efis_settings import (
    ParsedBackup,
    Settings_Mapper,
    VNE_DISABLED_SENTINEL,
)
from hypothesis import given, settings
from hypothesis import strategies as st


def _parsed(sids: dict) -> ParsedBackup:
    return ParsedBackup(
        path="Settings.bak",
        sids={k: str(v) for k, v in sids.items()},
        update_value=1,
        checksize=None,
        checksum=None,
        line_count=len(sids),
        valid_pairs=len(sids),
    )


def _by_key(mapped) -> dict:
    """Merge values + tier_choices into {threshold_key: MappedValue}."""
    out = {}
    for mv in list(mapped.values) + list(mapped.tier_choices):
        out[mv.threshold_key] = mv
    return out


# ---------------------------------------------------------------------------
# Example tests: one assertion per SID->threshold-key table entry (task 3.2)
# ---------------------------------------------------------------------------
def test_sid_table_direct_entries():
    mapped = Settings_Mapper.map(_parsed({
        "120": 55, "118": 90,          # oil pressure (Req 4.1)
        "147": 120,                    # egt spread (Req 4.5)
        "1463": 15, "1464": 35,        # fuel pressure (Req 4.6)
        "353": 13, "354": 15,          # voltage (Req 4.7)
        "123": 2700,                   # rpm redline (Req 4.8)
        "150": 60,                     # cht cooling rate (Req 4.9)
        "331": 3, "329": 4,            # g positive (Req 6.1)
        "332": 2, "330": 3,            # g negative (Req 6.2/6.3 -> negated)
    }))
    m = _by_key(mapped)
    assert m["oil_pressure_low_cruise"].value == 55
    assert m["oil_pressure_high"].value == 90
    assert m["egt_spread_caution"].value == 120
    assert m["fuel_pressure_low"].value == 15
    assert m["fuel_pressure_high"].value == 35
    assert m["voltage_low"].value == 13.0
    assert m["voltage_high"].value == 15.0
    assert m["rpm_redline"].value == 2700
    assert m["cht_cooling_rate_max"].value == 60
    assert m["g_pos_caution"].value == 3.0
    assert m["g_pos_limit"].value == 4.0
    assert m["g_neg_caution"].value == -2.0
    assert m["g_neg_limit"].value == -3.0


def test_sid_table_tier_choice_entries():
    mapped = Settings_Mapper.map(_parsed({"121": 240, "151": 430, "144": 1500}))
    keys = {mv.threshold_key for mv in mapped.tier_choices}
    assert keys == {"oil_temp", "cht", "egt"}
    for mv in mapped.tier_choices:
        assert mv.needs_tier_choice is True


def test_disabled_value_boundaries():
    # Exactly at the disabled sentinel (0) -> skipped.
    mapped = Settings_Mapper.map(_parsed({"147": 0, "123": 0, "150": 0}))
    m = _by_key(mapped)
    assert "egt_spread_caution" not in m
    assert "rpm_redline" not in m
    assert "cht_cooling_rate_max" not in m
    assert set(mapped.skipped_disabled) >= {"147", "123", "150"}


def test_unknown_sids_never_map():
    mapped = Settings_Mapper.map(_parsed({"999999": 42, "abc": 7}))
    assert mapped.values == []
    assert mapped.tier_choices == []
    assert mapped.num_cylinders is None


# ---------------------------------------------------------------------------
# Property 8: Mapper ignores unrecognized SIDs
# Feature: efis-settings-import, Property 8: Mapper ignores unrecognized SIDs
# Validates: Requirements 1.3, 4.10
# ---------------------------------------------------------------------------
KNOWN_KEYS = {
    "oil_pressure_low_cruise", "oil_pressure_high", "oil_temp", "cht", "egt",
    "egt_spread_caution", "fuel_pressure_low", "fuel_pressure_high",
    "voltage_low", "voltage_high", "rpm_redline", "cht_cooling_rate_max",
    "g_pos_caution", "g_pos_limit", "g_neg_caution", "g_neg_limit",
    "vne_tas_redline", "vne_ias_redline",
}
# SIDs not in the mapping table.
unknown_sids = st.integers(min_value=2000, max_value=9000).map(str).filter(
    lambda s: s not in {"1463", "1464"}
)


@settings(max_examples=200)
@given(
    unknown=st.dictionaries(unknown_sids, st.integers(0, 500).map(str), max_size=10),
)
def test_property8_unrecognized_sids_ignored(unknown):
    mapped = Settings_Mapper.map(_parsed(unknown))
    for mv in list(mapped.values) + list(mapped.tier_choices):
        assert mv.threshold_key in KNOWN_KEYS


# ---------------------------------------------------------------------------
# Property 9: Disabled values are skipped
# Feature: efis-settings-import, Property 9: Disabled values are skipped
# Validates: Requirements 4.10, 5.7, 6.4
# ---------------------------------------------------------------------------
@settings(max_examples=200)
@given(st.just(0))
def test_property9_disabled_values_skipped(_zero):
    # SIDs with disabled=0 in the table: 147, 123, 150; plus Vne(22)==0.
    mapped = Settings_Mapper.map(_parsed({
        "147": 0, "123": 0, "150": 0, "22": 0,
    }))
    m = _by_key(mapped)
    assert "egt_spread_caution" not in m
    assert "rpm_redline" not in m
    assert "cht_cooling_rate_max" not in m
    assert "vne_tas_redline" not in m
    assert "vne_ias_redline" not in m


# ---------------------------------------------------------------------------
# Property 10: Temperature unit conversion
# Feature: efis-settings-import, Property 10: Temperature unit conversion
# Validates: Requirements 7.3, 7.4, 7.5, 12.1
# ---------------------------------------------------------------------------
@settings(max_examples=200)
@given(v=st.integers(min_value=-50, max_value=2000), celsius=st.booleans())
def test_property10_temperature_conversion(v, celsius):
    sids = {"151": v}  # Max CHT (temp kind, tier_choice)
    if celsius:
        sids["345"] = 1
    mapped = Settings_Mapper.map(_parsed(sids))
    m = _by_key(mapped)
    expected = round(v * 9 / 5 + 32) if celsius else round(v)
    assert m["cht"].value == expected


@settings(max_examples=200)
@given(v=st.integers(min_value=1, max_value=500), celsius=st.booleans())
def test_property10_cooling_rate_delta_form(v, celsius):
    sids = {"150": v}  # Max CHT cooling rate (rate kind, °/min)
    if celsius:
        sids["345"] = 1
    mapped = Settings_Mapper.map(_parsed(sids))
    m = _by_key(mapped)
    expected = round(v * 9 / 5) if celsius else round(v)  # delta: no +32
    assert m["cht_cooling_rate_max"].value == expected


# ---------------------------------------------------------------------------
# Property 11: One-decimal rounding for G and voltage
# Feature: efis-settings-import, Property 11: One-decimal rounding for G and voltage
# Validates: Requirements 7.1, 7.2
# ---------------------------------------------------------------------------
@settings(max_examples=200)
@given(
    g=st.floats(min_value=0.05, max_value=9.0, allow_nan=False, allow_infinity=False),
    volt=st.floats(min_value=8.0, max_value=32.0, allow_nan=False, allow_infinity=False),
)
def test_property11_one_decimal_rounding(g, volt):
    mapped = Settings_Mapper.map(_parsed({"329": g, "354": volt}))
    m = _by_key(mapped)
    assert m["g_pos_limit"].value == round(g, 1)
    assert m["voltage_high"].value == round(volt, 1)


# ---------------------------------------------------------------------------
# Property 12: Negative-G sign preserved
# Feature: efis-settings-import, Property 12: Negative-G sign preserved
# Validates: Requirements 6.3
# ---------------------------------------------------------------------------
@settings(max_examples=200)
@given(
    a=st.floats(min_value=-9.0, max_value=9.0, allow_nan=False, allow_infinity=False),
    b=st.floats(min_value=-9.0, max_value=9.0, allow_nan=False, allow_infinity=False),
)
def test_property12_negative_g_sign(a, b):
    # SIDs 332 (g_neg_caution) and 330 (g_neg_limit): stored value must be <= 0.
    if a == 0:
        a = -1.0
    if b == 0:
        b = -1.0
    mapped = Settings_Mapper.map(_parsed({"332": a, "330": b}))
    m = _by_key(mapped)
    assert m["g_neg_caution"].value <= 0
    assert m["g_neg_limit"].value <= 0


# ---------------------------------------------------------------------------
# Property 13: Vne TAS/IAS mutual exclusivity
# Feature: efis-settings-import, Property 13: Vne TAS/IAS mutual exclusivity
# Validates: Requirements 5.1, 5.3, 5.4
# ---------------------------------------------------------------------------
@settings(max_examples=200)
@given(vne=st.integers(min_value=1, max_value=500), convert=st.integers(0, 1))
def test_property13_vne_mutual_exclusivity(vne, convert):
    mapped = Settings_Mapper.map(_parsed({"22": vne, "889": convert}))
    m = _by_key(mapped)
    tas = m["vne_tas_redline"].value
    ias = m["vne_ias_redline"].value
    if convert == 1:
        assert tas == round(vne)
        assert ias == VNE_DISABLED_SENTINEL
    else:
        assert ias == round(vne)
        assert tas == VNE_DISABLED_SENTINEL
    # Exactly one active (below sentinel).
    active = [x for x in (tas, ias) if x < VNE_DISABLED_SENTINEL]
    assert len(active) == 1


# ---------------------------------------------------------------------------
# Property 15: Cylinder-count precedence
# Feature: efis-settings-import, Property 15: Cylinder-count precedence
# Validates: Requirements 13.1, 13.2, 13.4
# ---------------------------------------------------------------------------
@settings(max_examples=200)
@given(
    has_1055=st.booleans(), has_1054=st.booleans(), has_58=st.booleans(),
    v1055=st.integers(1, 12), v1054=st.integers(1, 12), v58=st.integers(1, 12),
)
def test_property15_cylinder_precedence(has_1055, has_1054, has_58, v1055, v1054, v58):
    sids = {}
    if has_1055:
        sids["1055"] = v1055
    if has_1054:
        sids["1054"] = v1054
    if has_58:
        sids["58"] = v58
    mapped = Settings_Mapper.map(_parsed(sids))
    if has_1055:
        assert mapped.num_cylinders == v1055
    elif has_1054:
        assert mapped.num_cylinders == v1054
    elif has_58:
        assert mapped.num_cylinders == v58
    else:
        assert mapped.num_cylinders is None
