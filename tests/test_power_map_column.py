# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Example tests for map_column_for (power.py).

Validates Requirement 2.1: percent power sources MAP from the same column the
dashboard plots — the aux channel mapped to manifold_pressure, else internal_map.
"""

from efis_data_manager.power import map_column_for


def test_maps_to_resolved_channel():
    resolved = {
        "manifold_pressure": {"channel": "aux2", "label": "MAP",
                              "unit": '"', "precision": 1},
    }
    assert map_column_for(resolved) == "aux2"


def test_maps_to_arbitrary_channel():
    resolved = {"manifold_pressure": {"channel": "aux5"}}
    assert map_column_for(resolved) == "aux5"


def test_falls_back_to_internal_map_when_unmapped():
    # No manifold_pressure entry (e.g. amps + fuel pressure mapped only).
    resolved = {"amps": {"channel": "aux1"}}
    assert map_column_for(resolved) == "internal_map"


def test_falls_back_on_empty_resolved():
    assert map_column_for({}) == "internal_map"
