# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests that /api/flight/<id>/data routes aux channels through the resolver.

These are integration-style tests exercising the response-building logic of
api_flight_data with a real (throwaway) SQLite DB and the Flask test client.
They stand in for the "verify against a running dashboard" step in the task:
the same code path builds the JSON, just without a live HTTP server.

Covers Req 2.1 (single resolver drives the response), 2.2 (mapping changes
reflected), 3.1/3.2 (aux_params lists only mapped channels, keyed by
parameter), and 6.1 (default config preserves amps/MAP/fuel-pressure).
"""

from datetime import datetime, timedelta

import pytest

from efis_data_manager import database
from efis_data_manager import aux_map
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


def _import_flight(n=3):
    """Insert a small flight with distinct aux1-aux6 values, return op_id."""
    base = datetime(2026, 1, 1, 12, 0, 0)
    records = []
    for i in range(n):
        records.append(
            FDLRecord(
                timestamp=base + timedelta(seconds=i),
                tick=i,
                rpm1=2400.0 + i,
                aux1=10.0 + i,   # amps by default
                aux2=25.0 + i,   # MAP by default
                aux3=30.0 + i,   # fuel pressure by default
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


def _set_config(monkeypatch, config):
    """Force resolve_aux() to see the given config."""
    monkeypatch.setattr(aux_map, "_load_config", lambda: config)


def test_default_config_emits_amps_map_fuel_pressure(temp_db, client, monkeypatch):
    """Req 6.1: default mapping keeps amps/MAP/fuel-pressure from aux1/2/3."""
    # Default config (empty aux_mapping -> backfilled defaults).
    _set_config(monkeypatch, {})
    op_id = _import_flight(n=3)

    resp = client.get(f"/api/flight/{op_id}/data")
    assert resp.status_code == 200
    data = resp.get_json()

    engine = data["engine"]
    # Parameter keys come straight from the resolver.
    assert "amps" in engine
    assert "manifold_pressure" in engine
    assert "fuel_pressure" in engine

    # Series pull from the mapped columns (aux1/aux2/aux3).
    assert engine["amps"] == [10.0, 11.0, 12.0]
    assert engine["manifold_pressure"] == [25.0, 26.0, 27.0]
    assert engine["fuel_pressure"] == [30.0, 31.0, 32.0]

    # Fixed engine params are still present.
    assert engine["rpm"] == [2400.0, 2401.0, 2402.0]

    # aux_params lists only the three mapped channels, keyed by parameter.
    keys = {p["key"] for p in data["aux_params"]}
    assert keys == {"amps", "manifold_pressure", "fuel_pressure"}
    for p in data["aux_params"]:
        assert p["group"] == "engine"
        assert isinstance(p["label"], str) and p["label"]
        assert isinstance(p["precision"], int)


def test_unmapped_channels_absent(temp_db, client, monkeypatch):
    """Req 3.2: unmapped channels (aux4-6 = none) never appear in the response."""
    _set_config(monkeypatch, {})
    op_id = _import_flight(n=2)

    data = client.get(f"/api/flight/{op_id}/data").get_json()
    engine = data["engine"]
    # No raw aux column keys, and nothing for the unmapped channels.
    for absent in ("aux1", "aux2", "aux3", "aux4", "aux5", "aux6", "map"):
        assert absent not in engine


def test_remapped_config_reflected(temp_db, client, monkeypatch):
    """Req 2.2/3.1: a remapped config changes which series/keys are emitted."""
    remap = {
        "aux_mapping": {
            "aux1": {"parameter": "manifold_pressure", "label": "MAP", "unit": '"'},
            "aux2": {"parameter": "none", "label": "", "unit": ""},
            "aux3": {"parameter": "none", "label": "", "unit": ""},
            "aux4": {"parameter": "amps", "label": "Amps", "unit": "A"},
            "aux5": {"parameter": "custom", "label": "Boost", "unit": "psi"},
            "aux6": {"parameter": "none", "label": "", "unit": ""},
        }
    }
    _set_config(monkeypatch, remap)
    op_id = _import_flight(n=3)

    data = client.get(f"/api/flight/{op_id}/data").get_json()
    engine = data["engine"]

    # MAP now comes from aux1, amps from aux4, custom "Boost" from aux5.
    assert engine["manifold_pressure"] == [10.0, 11.0, 12.0]
    assert engine["amps"] == [40.0, 41.0, 42.0]
    assert engine["custom"] == [50.0, 51.0, 52.0]

    # Unmapped ones (fuel_pressure was on aux3->none) are gone.
    assert "fuel_pressure" not in engine

    keys = {p["key"] for p in data["aux_params"]}
    assert keys == {"manifold_pressure", "amps", "custom"}
    labels = {p["key"]: p["label"] for p in data["aux_params"]}
    assert labels["custom"] == "Boost"


def test_custom_empty_label_treated_unmapped(temp_db, client, monkeypatch):
    """Req 3.4: Custom with an empty label is unmapped (no series, no entry)."""
    remap = {
        "aux_mapping": {
            "aux1": {"parameter": "amps", "label": "Amps", "unit": "A"},
            "aux2": {"parameter": "custom", "label": "", "unit": ""},
            "aux3": {"parameter": "none", "label": "", "unit": ""},
            "aux4": {"parameter": "none", "label": "", "unit": ""},
            "aux5": {"parameter": "none", "label": "", "unit": ""},
            "aux6": {"parameter": "none", "label": "", "unit": ""},
        }
    }
    _set_config(monkeypatch, remap)
    op_id = _import_flight(n=2)

    data = client.get(f"/api/flight/{op_id}/data").get_json()
    engine = data["engine"]
    assert "custom" not in engine
    keys = {p["key"] for p in data["aux_params"]}
    assert keys == {"amps"}
