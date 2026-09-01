# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the aux channel mapping resolver (aux_map.py)."""

from efis_data_manager.aux_map import (
    PARAM_CATALOG,
    get_aux_mapping,
    resolve_aux,
)
from efis_data_manager.config import DEFAULT_CONFIG


def test_default_resolution_preserves_hardcoded_convention():
    """No config -> falls back to DEFAULT_CONFIG (amps/MAP/fuel-pressure)."""
    resolved = resolve_aux(DEFAULT_CONFIG)

    # Exactly the three legacy parameters are mapped.
    assert set(resolved) == {"amps", "manifold_pressure", "fuel_pressure"}

    assert resolved["amps"]["channel"] == "aux1"
    assert resolved["amps"]["label"] == "Amps"
    assert resolved["amps"]["unit"] == "A"

    assert resolved["manifold_pressure"]["channel"] == "aux2"
    assert resolved["manifold_pressure"]["label"] == "MAP"

    assert resolved["fuel_pressure"]["channel"] == "aux3"
    assert resolved["fuel_pressure"]["label"] == "Fuel Press"
    assert resolved["fuel_pressure"]["unit"] == "psi"


def test_default_config_key_present():
    """DEFAULT_CONFIG carries the aux_mapping and engine_category keys."""
    assert DEFAULT_CONFIG["engine_category"] == "traditional"
    assert set(DEFAULT_CONFIG["aux_mapping"]) == {
        "aux1", "aux2", "aux3", "aux4", "aux5", "aux6"
    }


def test_precision_comes_from_catalog():
    """Resolved precision matches PARAM_CATALOG."""
    resolved = resolve_aux(DEFAULT_CONFIG)
    assert resolved["amps"]["precision"] == PARAM_CATALOG["amps"][2]
    assert resolved["manifold_pressure"]["precision"] == 1


def test_custom_mapping():
    """A custom mapping is resolved against its channels/labels/units."""
    config = {
        "aux_mapping": {
            "aux1": {"parameter": "vacuum", "label": "Vac", "unit": "inHg"},
            "aux2": {"parameter": "fuel_level_left", "label": "L Tank", "unit": "gal"},
            "aux3": {"parameter": "none", "label": "", "unit": ""},
            "aux4": {"parameter": "coolant_pressure", "label": "Cool P", "unit": "psi"},
            "aux5": {"parameter": "none", "label": "", "unit": ""},
            "aux6": {"parameter": "none", "label": "", "unit": ""},
        }
    }
    resolved = resolve_aux(config)

    assert set(resolved) == {"vacuum", "fuel_level_left", "coolant_pressure"}
    assert resolved["vacuum"]["channel"] == "aux1"
    assert resolved["vacuum"]["label"] == "Vac"
    assert resolved["fuel_level_left"]["channel"] == "aux2"
    assert resolved["fuel_level_left"]["label"] == "L Tank"
    assert resolved["coolant_pressure"]["channel"] == "aux4"


def test_custom_parameter_uses_config_label_and_unit():
    """The 'custom' parameter takes label/unit from the config entry."""
    config = {
        "aux_mapping": {
            "aux1": {"parameter": "custom", "label": "Boost", "unit": "kPa"},
            "aux2": {"parameter": "none", "label": "", "unit": ""},
            "aux3": {"parameter": "none", "label": "", "unit": ""},
            "aux4": {"parameter": "none", "label": "", "unit": ""},
            "aux5": {"parameter": "none", "label": "", "unit": ""},
            "aux6": {"parameter": "none", "label": "", "unit": ""},
        }
    }
    resolved = resolve_aux(config)
    assert set(resolved) == {"custom"}
    assert resolved["custom"]["channel"] == "aux1"
    assert resolved["custom"]["label"] == "Boost"
    assert resolved["custom"]["unit"] == "kPa"


def test_custom_with_empty_label_treated_as_unmapped():
    """Custom with an empty/whitespace label is NOT surfaced."""
    config = {
        "aux_mapping": {
            "aux1": {"parameter": "custom", "label": "   ", "unit": "x"},
            "aux2": {"parameter": "custom", "label": "", "unit": "y"},
            "aux3": {"parameter": "none", "label": "", "unit": ""},
            "aux4": {"parameter": "none", "label": "", "unit": ""},
            "aux5": {"parameter": "none", "label": "", "unit": ""},
            "aux6": {"parameter": "none", "label": "", "unit": ""},
        }
    }
    resolved = resolve_aux(config)
    assert resolved == {}


def test_none_channels_excluded():
    """Channels with parameter 'none' are absent from the resolved mapping."""
    resolved = resolve_aux(DEFAULT_CONFIG)
    # aux4-6 default to 'none' and must not appear.
    channels = {v["channel"] for v in resolved.values()}
    assert "aux4" not in channels
    assert "aux5" not in channels
    assert "aux6" not in channels


def test_duplicate_parameter_first_wins(caplog):
    """Two channels mapping to the same parameter: first channel wins, warn."""
    config = {
        "aux_mapping": {
            "aux1": {"parameter": "amps", "label": "Amps1", "unit": "A"},
            "aux2": {"parameter": "amps", "label": "Amps2", "unit": "A"},
            "aux3": {"parameter": "none", "label": "", "unit": ""},
            "aux4": {"parameter": "none", "label": "", "unit": ""},
            "aux5": {"parameter": "none", "label": "", "unit": ""},
            "aux6": {"parameter": "none", "label": "", "unit": ""},
        }
    }
    with caplog.at_level("WARNING"):
        resolved = resolve_aux(config)

    assert set(resolved) == {"amps"}
    assert resolved["amps"]["channel"] == "aux1"
    assert resolved["amps"]["label"] == "Amps1"
    assert any("multiple channels" in rec.message for rec in caplog.records)


def test_unknown_parameter_ignored(caplog):
    """A parameter not in the catalog is skipped with a warning."""
    config = {
        "aux_mapping": {
            "aux1": {"parameter": "does_not_exist", "label": "X", "unit": "?"},
            "aux2": {"parameter": "none", "label": "", "unit": ""},
            "aux3": {"parameter": "none", "label": "", "unit": ""},
            "aux4": {"parameter": "none", "label": "", "unit": ""},
            "aux5": {"parameter": "none", "label": "", "unit": ""},
            "aux6": {"parameter": "none", "label": "", "unit": ""},
        }
    }
    with caplog.at_level("WARNING"):
        resolved = resolve_aux(config)
    assert resolved == {}
    assert any("unknown parameter" in rec.message for rec in caplog.records)


def test_get_aux_mapping_backfills_partial_config():
    """A partial saved config is backfilled from per-channel defaults."""
    config = {
        "aux_mapping": {
            # Only aux1 supplied, and it's partial (missing unit).
            "aux1": {"parameter": "amps", "label": "Amps"},
        }
    }
    mapping = get_aux_mapping(config)
    # All six channels present.
    assert set(mapping) == {"aux1", "aux2", "aux3", "aux4", "aux5", "aux6"}
    # Missing unit backfilled from default.
    assert mapping["aux1"]["unit"] == "A"
    # Untouched channels get their defaults.
    assert mapping["aux2"]["parameter"] == "manifold_pressure"
    assert mapping["aux4"]["parameter"] == "none"


def test_get_aux_mapping_empty_config_returns_defaults():
    """An empty config yields the full default mapping."""
    mapping = get_aux_mapping({})
    assert mapping == {
        "aux1": {"parameter": "amps", "label": "Amps", "unit": "A"},
        "aux2": {"parameter": "manifold_pressure", "label": "MAP", "unit": "\""},
        "aux3": {"parameter": "fuel_pressure", "label": "Fuel Press", "unit": "psi"},
        "aux4": {"parameter": "none", "label": "", "unit": ""},
        "aux5": {"parameter": "none", "label": "", "unit": ""},
        "aux6": {"parameter": "none", "label": "", "unit": ""},
    }
