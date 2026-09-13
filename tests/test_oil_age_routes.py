# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Integration tests for the oil-age warning dashboard wiring.

Exercises GET /api/oil (the new `oil_age` object) and the /api/config
round-trip for `oil_age_warning_hours` through Flask's test client against a
temp DB. These are integration tests (not Hypothesis properties): they exercise
SQLite/Flask behavior that does not vary meaningfully with generated input.

Requirements: 1.3, 1.5, 1.6, 2.1, 4.1
"""

import copy

import pytest

from efis_data_manager import database
from efis_data_manager import config as config_mod
from efis_data_manager.dashboard import app as dash_app


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


@pytest.fixture
def state_config(monkeypatch):
    """Patch load_config/save_config in the dashboard + config namespaces.

    Starts from DEFAULT_CONFIG so unrelated keys behave normally; tests mutate
    state["config"] via the /api/config POST path or directly.
    """
    state = {"config": dict(config_mod.DEFAULT_CONFIG)}

    def fake_load_config():
        return copy.deepcopy(state["config"])

    def fake_save_config(cfg):
        state["config"] = copy.deepcopy(cfg)

    monkeypatch.setattr(dash_app, "load_config", fake_load_config)
    monkeypatch.setattr(dash_app, "save_config", fake_save_config)
    monkeypatch.setattr(config_mod, "load_config", fake_load_config)
    monkeypatch.setattr(config_mod, "save_config", fake_save_config)
    return state


@pytest.fixture
def client():
    return dash_app.app.test_client()


def _insert_operation(op_id, hourmeter_end):
    conn = database.get_db_connection()
    conn.execute(
        "INSERT INTO operations (id, source_filename, start_time, end_time, "
        "duration_seconds, record_count, date, imported_at, hourmeter_end) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (op_id, "t.csv", "2026-01-01T00:00:00", "2026-01-01T00:02:00",
         120, 120, "2026-01-01", "2026-01-01T00:03:00", hourmeter_end))
    conn.commit()
    conn.close()


def _seed_change(hourmeter, date="2026-01-01"):
    conn = database.get_db_connection()
    conn.execute(
        "INSERT INTO oil_events (date, hourmeter, event_type, quarts_added, "
        "quarts_low, note, source, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (date, hourmeter, "change", 6, 0, "", "manual",
         "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 6.2 — /api/oil oil_age field
# ---------------------------------------------------------------------------

def test_api_oil_includes_oil_age_exceeded(temp_db, client, state_config):
    state_config["config"]["oil_age_warning_hours"] = 45
    _insert_operation(1, 100.0)
    _seed_change(50.0)

    resp = client.get("/api/oil")
    assert resp.status_code == 200
    data = resp.get_json()

    # The other four keys are still present / unchanged in shape.
    for key in ("consumption", "changes", "events", "cutoff_date"):
        assert key in data

    age = data["oil_age"]
    assert set(age.keys()) == {
        "status", "oil_hours_since_change", "threshold", "limit_exceeded"}
    assert age["status"] == "exceeded"
    assert age["oil_hours_since_change"] == pytest.approx(50.0)
    assert age["threshold"] == 45
    assert age["limit_exceeded"] is True


def test_api_oil_default_config_is_ok(temp_db, client, state_config):
    # Default config has oil_age_warning_hours == 0 (disabled).
    _insert_operation(1, 100.0)
    _seed_change(50.0)

    resp = client.get("/api/oil")
    data = resp.get_json()
    age = data["oil_age"]
    assert age["status"] == "ok"
    assert age["limit_exceeded"] is False
    assert age["oil_hours_since_change"] is None


# ---------------------------------------------------------------------------
# 8.2 — /api/config round-trip for oil_age_warning_hours
# ---------------------------------------------------------------------------

def test_config_roundtrip_positive(temp_db, client, state_config):
    resp = client.post("/api/config", json={"oil_age_warning_hours": 45})
    assert resp.status_code == 200

    got = client.get("/api/config").get_json()
    assert got["oil_age_warning_hours"] == 45


def test_config_negative_stored_but_disabled(temp_db, client, state_config):
    _insert_operation(1, 100.0)
    _seed_change(50.0)

    client.post("/api/config", json={"oil_age_warning_hours": -5})
    got = client.get("/api/config").get_json()
    # Stored verbatim …
    assert got["oil_age_warning_hours"] == -5

    # … but the computed status treats it as disabled (sanitized at read time).
    resp = client.get("/api/oil")
    age = resp.get_json()["oil_age"]
    assert age["status"] == "ok"
    assert age["limit_exceeded"] is False
    assert age["threshold"] == 0
