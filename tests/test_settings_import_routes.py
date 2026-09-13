# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Integration tests for the Flask settings-import routes (dashboard/app.py).

Exercises GET /api/settings-import/status, GET /api/settings-import/preview,
and POST /api/settings-import/apply through Flask's test client against a temp
archive directory and an in-memory config. Detection is source-keyed and
primary-gated, so the archived Settings backups are written with identity SIDs
(1063 Mode_S, 387 Link_ID) plus a couple of mapped SIDs and an UPDATE= line.

These are integration tests (not Hypothesis properties).

Requirements: 9.1, 9.2, 9.4, 9.5, 9.6, 14.4, 14.5, 15.2
"""

import copy

import pytest

from efis_data_manager.dashboard import app as dash_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_backup(archive_root, name, sids, update_value):
    """Write a KEY=VALUE Settings backup under <archive_root>/Settings/."""
    settings_dir = archive_root / "Settings"
    settings_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"UPDATE={update_value}"]
    lines += [f"{sid}={value}" for sid, value in sids.items()]
    (settings_dir / name).write_text("\n".join(lines) + "\n", encoding="ascii")


def _primary_sids(mode_s="A60670", link_id="1", extra=None):
    """Identity SIDs for a Primary_Display plus optional mapped SIDs."""
    sids = {"1063": mode_s, "387": link_id}
    if extra:
        sids.update(extra)
    return sids


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A Flask test client wired to a temp archive dir + mutable in-memory config.

    load_config / save_config are patched in the dashboard.app namespace (and
    config.save_config, which Import_Workflow.apply imports for the default
    persist path) so no real user config file is touched.
    """
    archive_root = tmp_path / "archive"
    archive_root.mkdir()

    state = {
        "config": {
            "archive_path": str(archive_root),
            "analysis_thresholds": {},
            "num_cylinders": 4,
            "settings_import": {},
        }
    }

    def fake_load_config():
        return copy.deepcopy(state["config"])

    def fake_save_config(cfg):
        state["config"] = copy.deepcopy(cfg)

    monkeypatch.setattr(dash_app, "load_config", fake_load_config)
    monkeypatch.setattr(dash_app, "save_config", fake_save_config)
    # Import_Workflow.apply's default save path imports config.save_config.
    from efis_data_manager import config as config_mod
    monkeypatch.setattr(config_mod, "save_config", fake_save_config)

    client = dash_app.app.test_client()
    return client, archive_root, state


# ---------------------------------------------------------------------------
# status — first-for-source, non-primary suppression, stale n-1
# ---------------------------------------------------------------------------

def test_status_offers_first_for_source_primary(env):
    """A primary backup with no prior marker is offered (first-for-source)."""
    client, archive_root, _ = env
    _write_backup(archive_root, "Settings-2026-01-01.dat",
                  _primary_sids(extra={"123": "2700"}), update_value=100)

    resp = client.get("/api/settings-import/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["available"] is True
    assert data["is_primary"] is True
    assert data["is_first_for_source"] is True
    assert data["source_key"] == "A60670:1"


def test_status_suppresses_non_primary(env):
    """A non-primary display (Link_ID != 1) is not offered (Req 15.2)."""
    client, archive_root, _ = env
    _write_backup(archive_root, "Settings-2026-01-01.dat",
                  _primary_sids(link_id="2", extra={"123": "2700"}), update_value=100)

    resp = client.get("/api/settings-import/status")
    data = resp.get_json()
    assert data["available"] is False
    assert data["reason"] == "not the primary display"


def test_status_suppresses_unchanged_content(env):
    """A candidate whose Content_Hash matches its source's marker is suppressed
    regardless of Archive_Date / UPDATE= (Req 14.5 as refined by Req 17.5)."""
    client, archive_root, state = env
    source_key = "A60670:1"
    from efis_data_manager.efis_settings import Settings_Parser, content_hash
    # Write the candidate first, then set the marker's content_hash to match it
    # exactly so detection sees "nothing new to import".
    _write_backup(archive_root, "Settings-2026-01-02.dat",
                  _primary_sids(extra={"123": "2700"}), update_value=100)
    parsed = Settings_Parser.parse(str(archive_root / "Settings" / "Settings-2026-01-02.dat"))
    state["config"]["settings_import"] = {
        source_key: {
            "last_update_value": 100,
            "content_hash": content_hash(parsed),
            "backup_name": "old.dat",
            "imported_at": "2026-01-01T00:00:00",
        },
        "bound_import_source": source_key,
    }

    resp = client.get("/api/settings-import/status")
    data = resp.get_json()
    assert data["available"] is False
    assert "suppressed" in data["reason"].lower()


def test_status_no_backup(env):
    """No archived Settings backup -> not available with a clear reason."""
    client, _, _ = env
    resp = client.get("/api/settings-import/status")
    data = resp.get_json()
    assert data["available"] is False
    assert data["reason"] == "no settings backup found"


# ---------------------------------------------------------------------------
# preview — diff table and caution<redline guard warning
# ---------------------------------------------------------------------------

def test_preview_returns_diff_table(env):
    """Preview returns a per-key diff table with current + proposed values."""
    client, archive_root, _ = env
    _write_backup(archive_root, "Settings-2026-01-01.dat",
                  _primary_sids(extra={"123": "2700", "118": "95", "1055": "6"}),
                  update_value=100)

    resp = client.get("/api/settings-import/preview")
    assert resp.status_code == 200
    data = resp.get_json()
    keys = {c["key"] for c in data["changes"]}
    assert "rpm_redline" in keys
    assert "oil_pressure_high" in keys
    assert "num_cylinders" in keys
    for c in data["changes"]:
        assert c["proposed"] is not None
    assert data["can_apply"] is True


def test_preview_guard_warns_on_caution_above_redline(env):
    """Choosing 'caution' tier for a CHT limit above the current redline trips
    the caution<redline guard: can_apply false with a warning (Req 8.2)."""
    client, archive_root, state = env
    state["config"]["analysis_thresholds"] = {"cht_caution": 380, "cht_redline": 400}
    # SID 151 (CHT single limit) = 450; as caution that exceeds redline 400.
    _write_backup(archive_root, "Settings-2026-01-01.dat",
                  _primary_sids(extra={"151": "450"}), update_value=100)

    resp = client.get("/api/settings-import/preview?tier_cht=caution")
    data = resp.get_json()
    assert data["can_apply"] is False
    assert any("caution" in w.lower() for w in data["warnings"])


# ---------------------------------------------------------------------------
# apply — no-op without both confirms, writes on double confirm
# ---------------------------------------------------------------------------

def test_apply_noop_without_both_confirms(env):
    """Apply is a no-op (declined) unless both confirmations are set (Req 9.4)."""
    client, archive_root, state = env
    _write_backup(archive_root, "Settings-2026-01-01.dat",
                  _primary_sids(extra={"123": "2700"}), update_value=100)
    before = copy.deepcopy(state["config"])

    resp = client.post("/api/settings-import/apply",
                       json={"confirm1": True, "confirm2": False})
    data = resp.get_json()
    assert data["status"] == "declined"
    # Config unchanged (nothing persisted).
    assert state["config"] == before


def test_apply_writes_thresholds_on_double_confirm(env):
    """Double-confirm applies: thresholds persisted and returned (Req 9.5, 9.6)."""
    client, archive_root, state = env
    _write_backup(archive_root, "Settings-2026-01-01.dat",
                  _primary_sids(extra={"123": "2700", "118": "95"}), update_value=100)

    resp = client.post("/api/settings-import/apply",
                       json={"confirm1": True, "confirm2": True})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    # rpm_redline (SID 123) mapped and persisted into analysis_thresholds.
    assert state["config"]["analysis_thresholds"]["rpm_redline"] == 2700
    assert state["config"]["analysis_thresholds"]["oil_pressure_high"] == 95
    assert data["updated_thresholds"]["rpm_redline"] == 2700
    # A marker was written for the candidate source and it became bound.
    si = state["config"]["settings_import"]
    assert si["A60670:1"]["last_update_value"] == 100
    assert si["bound_import_source"] == "A60670:1"


# ---------------------------------------------------------------------------
# Per-threshold selection (Requirement 16) through the routes
# ---------------------------------------------------------------------------

def test_preview_echoes_selected_and_selection_key(env):
    """Preview serializes each change's selected + selection_key; absent
    selected_keys => every row selected (fresh all-checked, Req 16.1/16.8)."""
    client, archive_root, _ = env
    _write_backup(archive_root, "Settings-2026-01-01.dat",
                  _primary_sids(extra={"123": "2700", "22": "180", "889": "1"}),
                  update_value=100)

    resp = client.get("/api/settings-import/preview")
    data = resp.get_json()
    for c in data["changes"]:
        assert "selected" in c and "selection_key" in c
        assert c["selected"] is True
    # Both vne_* rows share the "vne" selection key (one atomic unit).
    vne_rows = [c for c in data["changes"] if c["selection_key"] == "vne"]
    assert len(vne_rows) == 2


def test_preview_reruns_guard_with_selected_keys(env):
    """selected_keys reruns the guard server-side: selecting only the CHT
    caution row above the current redline blocks; deselecting it clears the
    block (Req 16.6)."""
    client, archive_root, state = env
    state["config"]["analysis_thresholds"] = {"cht_caution": 380, "cht_redline": 400}
    _write_backup(archive_root, "Settings-2026-01-01.dat",
                  _primary_sids(extra={"151": "450", "123": "2700"}), update_value=100)

    # Selecting cht_caution (450 as caution >= redline 400) blocks apply.
    resp = client.get(
        "/api/settings-import/preview?tier_cht=caution&selected_keys=cht_caution")
    data = resp.get_json()
    assert data["can_apply"] is False

    # Selecting only rpm_redline (no CHT) clears the guard.
    resp = client.get(
        "/api/settings-import/preview?tier_cht=caution&selected_keys=rpm_redline")
    data = resp.get_json()
    assert data["can_apply"] is True


def test_apply_subset_writes_only_selected(env):
    """apply with a subset writes only that subset; unselected stays current."""
    client, archive_root, state = env
    _write_backup(archive_root, "Settings-2026-01-01.dat",
                  _primary_sids(extra={"123": "2700", "118": "95"}), update_value=100)

    resp = client.post("/api/settings-import/apply", json={
        "confirm1": True, "confirm2": True,
        "selected_keys": ["rpm_redline"],
    })
    data = resp.get_json()
    assert data["status"] == "ok"
    thr = state["config"]["analysis_thresholds"]
    assert thr["rpm_redline"] == 2700
    # oil_pressure_high was NOT selected -> not written.
    assert "oil_pressure_high" not in thr


def test_apply_empty_selected_keys_is_noop(env):
    """apply with an empty selected_keys list is a no-op ('nothing selected')."""
    client, archive_root, state = env
    _write_backup(archive_root, "Settings-2026-01-01.dat",
                  _primary_sids(extra={"123": "2700"}), update_value=100)
    before = copy.deepcopy(state["config"])

    resp = client.post("/api/settings-import/apply", json={
        "confirm1": True, "confirm2": True,
        "selected_keys": [],
    })
    data = resp.get_json()
    assert data["status"] == "declined"
    assert "nothing selected" in data["reason"].lower()
    assert state["config"] == before


def test_apply_omitted_selected_keys_applies_all(env):
    """Backward compat: apply with selected_keys OMITTED writes all changes."""
    client, archive_root, state = env
    _write_backup(archive_root, "Settings-2026-01-01.dat",
                  _primary_sids(extra={"123": "2700", "118": "95"}), update_value=100)

    resp = client.post("/api/settings-import/apply",
                       json={"confirm1": True, "confirm2": True})
    data = resp.get_json()
    assert data["status"] == "ok"
    thr = state["config"]["analysis_thresholds"]
    assert thr["rpm_redline"] == 2700
    assert thr["oil_pressure_high"] == 95
