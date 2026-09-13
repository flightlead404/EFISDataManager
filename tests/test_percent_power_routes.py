# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Integration tests for the Percent_Power dashboard wiring (dashboard/app.py).

Exercises GET /api/flight/<id>/data and GET /api/flight/<id> through Flask's
test client against a temp DB. The EFIS settings read is patched at
efis_data_manager.power._read_settings (the single seam both the data route and
analysis._avg_percent_power_cruise consult), so a valid Power_Map exercises the
"available" branch and a None return exercises graceful degradation.

These are integration tests (not Hypothesis properties).

Requirements: 6.1, 6.2, 6.3, 5.1, 5.2
"""

from datetime import datetime, timedelta

import pytest

from efis_data_manager import database, power
from efis_data_manager.dashboard import app as dash_app


# ---------------------------------------------------------------------------
# Worked-table Power_Map (RATED_HP = 190, this aircraft's IO-360)
# ---------------------------------------------------------------------------

_RPM = [2000, 2100, 2200, 2300, 2400, 2500, 2600, 2700]
_MAP55 = [21.6, 21.1, 20.6, 20.1, 19.7, 19.2, 18.7, 18.2]
_MAP75 = [26.7, 26.1, 25.5, 24.9, 24.4, 23.8, 23.2, 22.7]
_ALT = [2000, 4000, 6000, 8000, 10000, 12000, 14000]
_DHP = [2.3, 4.6, 6.9, 9.2, 11.5, 13.8, 16.0]


def _worked_sids():
    """Build the string-keyed SID dict for the worked-reference Power_Map."""
    sids = {power.SID_RATED_HP: "190"}
    for i, col in enumerate(power.SID_RPM_COLUMN):
        sids[col] = str(_RPM[i]) if i < len(_RPM) else "0"
    for i, col in enumerate(power.SID_MAP55_COLUMN):
        sids[col] = str(_MAP55[i]) if i < len(_MAP55) else "0"
    for i, col in enumerate(power.SID_MAP75_COLUMN):
        sids[col] = str(_MAP75[i]) if i < len(_MAP75) else "0"
    for i, col in enumerate(power.SID_ALTITUDE_COLUMN):
        sids[col] = str(_ALT[i]) if i < len(_ALT) else "0"
    for i, col in enumerate(power.SID_DELTA_HP_COLUMN):
        sids[col] = str(_DHP[i]) if i < len(_DHP) else "0"
    return sids


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

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


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    """Patch config so aux resolution has no manifold_pressure mapping.

    The default aux mapping wires aux2 -> manifold_pressure, which would make
    map_column_for() pick aux2. The test rows carry MAP in internal_map, so we
    clear the aux mapping to force the internal_map fallback path.
    """
    cfg = {
        "aux_mapping": {ch: {"parameter": "none", "label": "", "unit": ""}
                        for ch in ("aux1", "aux2", "aux3", "aux4", "aux5", "aux6")},
        "num_cylinders": 4,
        "analysis_thresholds": {},
    }
    from efis_data_manager import config as config_mod
    monkeypatch.setattr(config_mod, "load_config", lambda: dict(cfg))


@pytest.fixture
def client():
    return dash_app.app.test_client()


def _insert_flight(op_id=1, rows=None):
    """Insert an operation plus fdl_data rows.

    rows: list of dicts with keys rpm1, internal_map, pressure_altitude, oat,
    indicated_airspeed, vertical_speed. Missing keys default to cruise-valid
    values so the sample participates in the cruise average.
    """
    conn = database.get_db_connection()
    n = len(rows)
    conn.execute(
        "INSERT INTO operations (id, source_filename, start_time, end_time, "
        "duration_seconds, record_count, has_flight, airborne_seconds, date, "
        "imported_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (op_id, "t.csv", "2026-01-01T00:00:00", "2026-01-01T00:02:00",
         n, n, 1, n, "2026-01-01", "2026-01-01T00:03:00"))
    t0 = datetime(2026, 1, 1, 0, 0, 0)
    for i, r in enumerate(rows):
        ts = (t0 + timedelta(seconds=i)).isoformat()
        conn.execute(
            "INSERT INTO fdl_data (operation_id, timestamp, tick, rpm1, "
            "internal_map, pressure_altitude, oat, indicated_airspeed, "
            "vertical_speed) VALUES (?,?,?,?,?,?,?,?,?)",
            (op_id, ts, i,
             r.get("rpm1", 2400),
             r.get("internal_map", 22.0),
             r.get("pressure_altitude", 6000),
             r.get("oat", 40.0),
             r.get("indicated_airspeed", 120),
             r.get("vertical_speed", 0)))
    conn.commit()
    conn.close()


def _cruise_rows(n=5):
    return [{} for _ in range(n)]


# ---------------------------------------------------------------------------
# Available branch: valid Power_Map
# ---------------------------------------------------------------------------

def test_data_route_emits_series_and_descriptor(temp_db, client, monkeypatch):
    """With a valid Power_Map, the data route returns engine.percent_power
    aligned to timestamps and a percent_power descriptor in aux_params."""
    monkeypatch.setattr(power, "_read_settings", lambda: (_worked_sids(), False))
    _insert_flight(rows=_cruise_rows(5))

    resp = client.get("/api/flight/1/data")
    assert resp.status_code == 200
    data = resp.get_json()

    assert "percent_power" in data["engine"]
    series = data["engine"]["percent_power"]
    assert len(series) == len(data["timestamps"])
    # All these rows have valid inputs -> all defined, ~68% at the worked point.
    assert all(v is not None for v in series)
    assert 60 < series[0] < 75

    descs = [d for d in data["aux_params"] if d["key"] == "percent_power"]
    assert len(descs) == 1
    assert descs[0]["label"] == "% Power"
    assert descs[0]["group"] == "engine"
    assert descs[0]["precision"] == 0


def test_detail_route_reports_numeric_avg(temp_db, client, monkeypatch):
    """With a valid Power_Map, the detail route reports a numeric
    avg_percent_power_cruise."""
    monkeypatch.setattr(power, "_read_settings", lambda: (_worked_sids(), False))
    _insert_flight(rows=_cruise_rows(5))

    resp = client.get("/api/flight/1")
    assert resp.status_code == 200
    detail = resp.get_json()
    assert detail["avg_percent_power_cruise"] is not None
    assert 60 < detail["avg_percent_power_cruise"] < 75


def test_data_route_null_at_missing_input(temp_db, client, monkeypatch):
    """A sample missing OAT (or RPM) yields null at that index (Req 5.1)."""
    monkeypatch.setattr(power, "_read_settings", lambda: (_worked_sids(), False))
    rows = [
        {},                       # index 0: fully valid -> defined
        {"oat": None},            # index 1: missing OAT -> null
        {"rpm1": None},           # index 2: missing RPM -> null
    ]
    _insert_flight(rows=rows)

    resp = client.get("/api/flight/1/data")
    series = resp.get_json()["engine"]["percent_power"]
    assert series[0] is not None
    assert series[1] is None
    assert series[2] is None


# ---------------------------------------------------------------------------
# Unavailable branch: no Power_Map
# ---------------------------------------------------------------------------

def test_data_route_omits_series_and_descriptor_when_unavailable(
        temp_db, client, monkeypatch):
    """With no settings backup, the data route omits BOTH the series key and
    the descriptor (Req 5.2, 6.3)."""
    monkeypatch.setattr(power, "_read_settings", lambda: None)
    _insert_flight(rows=_cruise_rows(5))

    resp = client.get("/api/flight/1/data")
    data = resp.get_json()
    assert "percent_power" not in data["engine"]
    assert not any(d["key"] == "percent_power" for d in data["aux_params"])


def test_detail_route_avg_is_none_when_unavailable(temp_db, client, monkeypatch):
    """avg_percent_power_cruise is None when the Power_Map is unavailable."""
    monkeypatch.setattr(power, "_read_settings", lambda: None)
    _insert_flight(rows=_cruise_rows(5))

    resp = client.get("/api/flight/1")
    assert resp.get_json()["avg_percent_power_cruise"] is None
