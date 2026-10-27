# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Example tests for aircraft-keyed config accessors and the migration chain.

Covers the pure accessors added in config.py for the multi-device-settings
foundation — ``managed_mode_s``, ``managed_aircraft``, ``configured_device``,
``device_count`` — over an empty aircraft map, a pending-sentinel-only config,
a single real Mode_S record, a mixed real+pending config, and absent/non-dict
``aircraft`` values. Also exercises the ``load_config`` migration chain
(defaults merge -> migrate_settings_import -> migrate_aircraft_config) on a
pre-feature config lacking ``aircraft``, asserting it never raises and that
single-device defaults hold.

Requirements: 1.1, 1.2, 6.1, 6.2, 6.3
"""

import json

from efis_data_manager import config as config_mod
from efis_data_manager.config import (
    DEFAULT_CONFIG,
    PENDING_AIRCRAFT_KEY,
    configured_device,
    device_count,
    load_config,
    managed_aircraft,
    managed_mode_s,
    migrate_aircraft_config,
    migrate_settings_import,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _real_record() -> dict:
    """A single real two-device managed-aircraft record (Mode_S A60670)."""
    return {
        "device_count": 2,
        "devices": {
            "1": {"link_id": "1", "friendly_name": "", "chart_capable": True},
            "2": {
                "link_id": "2",
                "friendly_name": "Backup Mini",
                "chart_capable": False,
            },
        },
        "analysis_data_source": "1",
    }


def _pending_record() -> dict:
    """A pre-learn pending record (shape identical to a real record)."""
    return {
        "device_count": 1,
        "devices": {
            "1": {"link_id": "1", "friendly_name": "", "chart_capable": True},
        },
        "analysis_data_source": "1",
    }


# ---------------------------------------------------------------------------
# Accessors — empty aircraft map
# ---------------------------------------------------------------------------

def test_empty_aircraft_map_accessors_default():
    """An empty aircraft map reports no managed aircraft and single-device."""
    cfg = {"aircraft": {}}
    assert managed_mode_s(cfg) is None
    assert managed_aircraft(cfg) is None
    assert device_count(cfg) == 1
    assert configured_device(cfg, "1") is None


# ---------------------------------------------------------------------------
# Accessors — pending-sentinel-only
# ---------------------------------------------------------------------------

def test_pending_sentinel_only_is_not_managed():
    """A pending-sentinel-only config reports no managed aircraft (sentinel skipped)."""
    cfg = {"aircraft": {PENDING_AIRCRAFT_KEY: _pending_record()}}
    # The sentinel is NOT returned as the managed Mode_S.
    assert managed_mode_s(cfg) is None
    assert managed_aircraft(cfg) is None
    # Not learned yet -> single-device default.
    assert device_count(cfg) == 1
    assert configured_device(cfg, "1") is None


# ---------------------------------------------------------------------------
# Accessors — single real Mode_S record
# ---------------------------------------------------------------------------

def test_single_real_record_accessors():
    """A single real record resolves Mode_S, record, count, and devices."""
    record = _real_record()
    cfg = {"aircraft": {"A60670": record}}

    assert managed_mode_s(cfg) == "A60670"
    assert managed_aircraft(cfg) is record
    assert device_count(cfg) == 2

    # configured_device returns the Link 2 device.
    dev2 = configured_device(cfg, "2")
    assert dev2 is record["devices"]["2"]
    assert dev2["friendly_name"] == "Backup Mini"
    assert dev2["chart_capable"] is False

    # An unconfigured Link_ID returns None.
    assert configured_device(cfg, "3") is None


# ---------------------------------------------------------------------------
# Accessors — mixed real + pending
# ---------------------------------------------------------------------------

def test_mixed_real_and_pending_returns_real_key():
    """With both a real key and the pending sentinel, the real key is managed."""
    record = _real_record()
    cfg = {
        "aircraft": {
            PENDING_AIRCRAFT_KEY: _pending_record(),
            "A60670": record,
        }
    }
    mode_s = managed_mode_s(cfg)
    assert mode_s == "A60670"
    assert mode_s != PENDING_AIRCRAFT_KEY
    assert managed_aircraft(cfg) is record
    assert device_count(cfg) == 2
    assert configured_device(cfg, "2") is record["devices"]["2"]


# ---------------------------------------------------------------------------
# Accessors — aircraft key absent or non-dict (tolerance)
# ---------------------------------------------------------------------------

def test_accessors_tolerate_missing_aircraft_key():
    """Accessors tolerate an entirely missing aircraft key (None / default 1)."""
    cfg = {}
    assert managed_mode_s(cfg) is None
    assert managed_aircraft(cfg) is None
    assert device_count(cfg) == 1
    assert configured_device(cfg, "1") is None


def test_accessors_tolerate_non_dict_aircraft():
    """Accessors tolerate a non-dict aircraft value without raising."""
    for bad in ("a string", 42, ["list"], None):
        cfg = {"aircraft": bad}
        assert managed_mode_s(cfg) is None
        assert managed_aircraft(cfg) is None
        assert device_count(cfg) == 1
        assert configured_device(cfg, "1") is None


# ---------------------------------------------------------------------------
# Migration chain — in-memory (no aircraft key, legacy settings_import)
# ---------------------------------------------------------------------------

def test_migration_chain_on_prefeature_config_no_aircraft():
    """The defaults-merge + both migrations tolerate a pre-feature config."""
    # A pre-feature saved config lacks an `aircraft` key entirely and carries a
    # legacy single-object settings_import (pre-Req-14 scalar marker) so both
    # migrations are exercised.
    saved = {
        "archive_path": "/tmp/EFIS_Archive",
        "tail_number": "N12345",
        "settings_import": {
            "last_update_value": 7,
            "last_backup_sha256": "deadbeef",
            "imported_at": "2024-12-12T00:00:00",
        },
    }
    assert "aircraft" not in saved

    config = {**DEFAULT_CONFIG, **saved}
    # Neither migration raises.
    migrate_settings_import(config)
    migrate_aircraft_config(config)

    # aircraft normalized to the empty "not yet learned" map.
    assert config["aircraft"] == {}
    # Single-device defaults hold.
    assert device_count(config) == 1
    assert managed_mode_s(config) is None
    assert managed_aircraft(config) is None
    # settings_import migrated into the map shape without crashing.
    assert isinstance(config["settings_import"], dict)


# ---------------------------------------------------------------------------
# Migration chain — real load_config via monkeypatched CONFIG_FILE
# ---------------------------------------------------------------------------

def test_load_config_migrates_prefeature_file(monkeypatch, tmp_path):
    """load_config on a pre-feature JSON file supplies the aircraft key cleanly."""
    cfg_file = tmp_path / "config.json"
    prefeature = {
        "archive_path": str(tmp_path / "archive"),
        "tail_number": "N99999",
        # No `aircraft` key; also no settings_import -> defaults/empty map apply.
    }
    cfg_file.write_text(json.dumps(prefeature), encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(config_mod, "ensure_dirs", lambda: None)

    loaded = load_config()

    # The aircraft key is present after the migration chain and single-device
    # defaults hold.
    assert "aircraft" in loaded
    assert loaded["aircraft"] == {}
    assert device_count(loaded) == 1
    assert managed_mode_s(loaded) is None
    # The saved value survived the defaults merge.
    assert loaded["tail_number"] == "N99999"
