# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests that extremes and episodes route aux channels through the resolver.

Covers Task 4 / Req 2.2, 2.3, 6.1:
- api_flight_extremes builds amps/MAP extreme specs only when those parameters
  are mapped (skips them when unmapped), reading the resolved channel column.
- detect_episodes runs fuel-pressure episodes only when fuel_pressure is mapped,
  reading the resolved channel column instead of a hardcoded aux3.
"""

from datetime import datetime, timedelta

import pytest

from efis_data_manager import database
from efis_data_manager import aux_map
from efis_data_manager import analysis
from efis_data_manager.dashboard import app as dashboard_app
from efis_data_manager.fdl_parser import FDLFile, FDLRecord


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point the database module at a throwaway SQLite file."""
    db_dir = tmp_path / "logs"
    db_dir.mkdir()
    db_path = db_dir / "efis_data.sqlite"
    monkeypatch.setattr(database, "DB_DIR", db_dir)
    monkeypatch.setattr(database, "DB_PATH", db_path)
    return db_path


@pytest.fixture
def client():
    dashboard_app.app.config["TESTING"] = True
    return dashboard_app.app.test_client()


def _set_config(monkeypatch, config):
    """Force resolve_aux() to see the given config (via aux_map._load_config)."""
    monkeypatch.setattr(aux_map, "_load_config", lambda: config)


def _import_flight(n=5, fuel_press_high=True):
    """Insert a flight with distinct aux1-aux6 values.

    aux1 (amps by default), aux2 (MAP by default), aux3 (fuel pressure by
    default). When fuel_press_high, aux3 is held above the high fuel-pressure
    threshold (35 psi) for the whole flight so a fuel-pressure "high" episode
    is guaranteed.
    """
    base = datetime(2026, 1, 1, 12, 0, 0)
    records = []
    for i in range(n):
        aux3 = 45.0 + i if fuel_press_high else 20.0 + i  # 20-24 is nominal
        records.append(
            FDLRecord(
                timestamp=base + timedelta(seconds=i),
                tick=i,
                rpm1=2400.0 + i,
                aux1=10.0 + i,   # amps by default
                aux2=25.0 + i,   # MAP by default
                aux3=aux3,       # fuel pressure by default
                aux4=40.0 + i,
                aux5=50.0 + i,
                aux6=60.0 + i,
            )
        )
    fdl = FDLFile(
        source_filename="TEST.FDL",
        source_path="/tmp/TEST.FDL",
        records=records,
    )
    return database.import_fdl_file(fdl)


# --------------------------------------------------------------------------
# Extremes
# --------------------------------------------------------------------------

def test_extremes_default_config_has_amps_and_map(temp_db, client, monkeypatch):
    """Req 6.1: default mapping yields amps (min+max) and MAP extremes."""
    _set_config(monkeypatch, {})
    op_id = _import_flight(n=5)

    out = client.get(f"/api/flight/{op_id}/extremes").get_json()
    by_key = {e["key"]: e for e in out}

    # MAP from aux2 (max = 25 + 4 = 29).
    assert "max_map" in by_key
    assert by_key["max_map"]["value"] == 29.0

    # Amps from aux1 (min = 10, max = 14).
    assert "min_amps" in by_key
    assert "max_amps" in by_key
    assert by_key["min_amps"]["value"] == 10.0
    assert by_key["max_amps"]["value"] == 14.0


def test_extremes_skip_unmapped_amps_and_map(temp_db, client, monkeypatch):
    """Req 2.3: when amps/MAP are unmapped, their extremes are omitted."""
    remap = {
        "aux_mapping": {
            "aux1": {"parameter": "none", "label": "", "unit": ""},
            "aux2": {"parameter": "none", "label": "", "unit": ""},
            "aux3": {"parameter": "fuel_pressure", "label": "Fuel Press", "unit": "psi"},
            "aux4": {"parameter": "none", "label": "", "unit": ""},
            "aux5": {"parameter": "none", "label": "", "unit": ""},
            "aux6": {"parameter": "none", "label": "", "unit": ""},
        }
    }
    _set_config(monkeypatch, remap)
    op_id = _import_flight(n=5)

    out = client.get(f"/api/flight/{op_id}/extremes").get_json()
    keys = {e["key"] for e in out}

    assert "max_map" not in keys
    assert "min_amps" not in keys
    assert "max_amps" not in keys
    # Non-aux extremes still present.
    assert "max_rpm" in keys


def test_extremes_read_resolved_channel(temp_db, client, monkeypatch):
    """Req 2.2: extremes read the resolved channel, not a hardcoded column."""
    # amps remapped onto aux4 (values 40..44); MAP remapped onto aux5 (50..54).
    remap = {
        "aux_mapping": {
            "aux1": {"parameter": "none", "label": "", "unit": ""},
            "aux2": {"parameter": "none", "label": "", "unit": ""},
            "aux3": {"parameter": "none", "label": "", "unit": ""},
            "aux4": {"parameter": "amps", "label": "Amps", "unit": "A"},
            "aux5": {"parameter": "manifold_pressure", "label": "MAP", "unit": '"'},
            "aux6": {"parameter": "none", "label": "", "unit": ""},
        }
    }
    _set_config(monkeypatch, remap)
    op_id = _import_flight(n=5)

    out = client.get(f"/api/flight/{op_id}/extremes").get_json()
    by_key = {e["key"]: e for e in out}

    # amps now from aux4 (min 40, max 44); MAP from aux5 (max 54).
    assert by_key["min_amps"]["value"] == 40.0
    assert by_key["max_amps"]["value"] == 44.0
    assert by_key["max_map"]["value"] == 54.0


# --------------------------------------------------------------------------
# Episodes
# --------------------------------------------------------------------------

def test_fuel_pressure_episode_present_default_config(temp_db, monkeypatch):
    """Req 6.1: with default config, a fuel-pressure episode is detected."""
    _set_config(monkeypatch, {})
    op_id = _import_flight(n=5, fuel_press_high=True)

    episodes = analysis.detect_episodes(op_id)
    fuel_eps = [e for e in episodes if e.parameter == "Fuel Pressure"]
    assert fuel_eps, "expected a fuel-pressure episode with default mapping"
    assert any(e.direction == "high" for e in fuel_eps)


def test_fuel_pressure_episode_absent_when_unmapped(temp_db, monkeypatch):
    """Req 2.3: when fuel_pressure is unmapped, no fuel-pressure episode runs."""
    remap = {
        "aux_mapping": {
            "aux1": {"parameter": "amps", "label": "Amps", "unit": "A"},
            "aux2": {"parameter": "manifold_pressure", "label": "MAP", "unit": '"'},
            "aux3": {"parameter": "none", "label": "", "unit": ""},
            "aux4": {"parameter": "none", "label": "", "unit": ""},
            "aux5": {"parameter": "none", "label": "", "unit": ""},
            "aux6": {"parameter": "none", "label": "", "unit": ""},
        }
    }
    _set_config(monkeypatch, remap)
    # aux3 still holds high fuel-pressure data, but it's unmapped now.
    op_id = _import_flight(n=5, fuel_press_high=True)

    episodes = analysis.detect_episodes(op_id)
    fuel_eps = [e for e in episodes if e.parameter == "Fuel Pressure"]
    assert not fuel_eps, "fuel-pressure episode must be skipped when unmapped"


def test_fuel_pressure_episode_reads_resolved_channel(temp_db, monkeypatch):
    """Req 2.2: fuel_pressure episodes read the resolved channel, not aux3.

    Map fuel_pressure onto aux4 (which holds nominal 40..44 values, above the
    high threshold of 35) while aux3 holds unrelated data. An episode should be
    detected from aux4, proving the resolver drives the channel selection.
    """
    remap = {
        "aux_mapping": {
            "aux1": {"parameter": "none", "label": "", "unit": ""},
            "aux2": {"parameter": "none", "label": "", "unit": ""},
            "aux3": {"parameter": "none", "label": "", "unit": ""},
            "aux4": {"parameter": "fuel_pressure", "label": "Fuel Press", "unit": "psi"},
            "aux5": {"parameter": "none", "label": "", "unit": ""},
            "aux6": {"parameter": "none", "label": "", "unit": ""},
        }
    }
    _set_config(monkeypatch, remap)
    # aux4 = 40..44 (> 35 high threshold); aux3 = 20..24 (nominal, unmapped).
    op_id = _import_flight(n=5, fuel_press_high=False)

    episodes = analysis.detect_episodes(op_id)
    fuel_eps = [e for e in episodes if e.parameter == "Fuel Pressure"]
    assert fuel_eps, "expected fuel-pressure episode read from resolved aux4"
    assert any(e.direction == "high" for e in fuel_eps)
