# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Integration tests for the settings read + graceful degradation.

Covers:
  - efis_settings.load_current_settings_sids() over a temp archive dir:
    returns (sids, temp_units_celsius) for a valid Settings backup; None when
    no backup / no Settings dir.
  - power.load_power_map(): returns a PowerMap for a valid map and None when
    _read_settings yields None (patched) — no crash.

Validates Requirements 1.1, 1.2, 5.2.
"""

import efis_data_manager.efis_settings as es
import efis_data_manager.power as power
from efis_data_manager.power import PowerMap

from tests.test_power_map import worked_sids


def _write_backup(settings_dir, name: str, sids: dict):
    settings_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in sids.items()]
    (settings_dir / name).write_text("\n".join(lines) + "\n")


def _patch_archive(monkeypatch, tmp_path):
    """Point efis_settings' load_config at a temp archive_path."""
    def fake_load_config():
        return {"archive_path": str(tmp_path)}
    # load_current_settings_sids imports load_config lazily from the config
    # module, so patch it there.
    import efis_data_manager.config as config
    monkeypatch.setattr(config, "load_config", fake_load_config)
    return tmp_path / "Settings"


# ---------------------------------------------------------------------------
# load_current_settings_sids
# ---------------------------------------------------------------------------
def test_load_current_settings_sids_valid_backup(tmp_path, monkeypatch):
    settings_dir = _patch_archive(monkeypatch, tmp_path)
    sids_in = dict(worked_sids())
    sids_in["345"] = "0"  # Fahrenheit
    sids_in["UPDATE"] = "5"
    _write_backup(settings_dir, "Settings-2026-01-01.bak", sids_in)

    result = es.load_current_settings_sids()
    assert result is not None
    sids, temp_units_celsius = result
    assert sids.get("167") == "190"
    assert temp_units_celsius is False


def test_load_current_settings_sids_celsius_flag(tmp_path, monkeypatch):
    settings_dir = _patch_archive(monkeypatch, tmp_path)
    sids_in = dict(worked_sids())
    sids_in["345"] = "1"  # Celsius
    _write_backup(settings_dir, "Settings-2026-01-02.dat", sids_in)

    result = es.load_current_settings_sids()
    assert result is not None
    _sids, temp_units_celsius = result
    assert temp_units_celsius is True


def test_load_current_settings_sids_selects_higher_update(tmp_path, monkeypatch):
    settings_dir = _patch_archive(monkeypatch, tmp_path)
    old = dict(worked_sids()); old["UPDATE"] = "1"; old["167"] = "180"
    new = dict(worked_sids()); new["UPDATE"] = "9"; new["167"] = "190"
    _write_backup(settings_dir, "Settings-old.bak", old)
    _write_backup(settings_dir, "Settings-new.dat", new)

    result = es.load_current_settings_sids()
    assert result is not None
    sids, _ = result
    assert sids.get("167") == "190"  # the higher-UPDATE backup won


def test_load_current_settings_sids_none_when_no_settings_dir(tmp_path, monkeypatch):
    _patch_archive(monkeypatch, tmp_path)  # no Settings dir created
    assert es.load_current_settings_sids() is None


def test_load_current_settings_sids_none_when_dir_empty(tmp_path, monkeypatch):
    settings_dir = _patch_archive(monkeypatch, tmp_path)
    settings_dir.mkdir(parents=True, exist_ok=True)
    assert es.load_current_settings_sids() is None


# ---------------------------------------------------------------------------
# load_power_map graceful degradation
# ---------------------------------------------------------------------------
def test_load_power_map_valid(monkeypatch):
    monkeypatch.setattr(power, "_read_settings",
                        lambda: (worked_sids(), False))
    pm = power.load_power_map()
    assert isinstance(pm, PowerMap)
    assert pm.rated_hp == 190.0


def test_load_power_map_none_when_read_settings_none(monkeypatch):
    monkeypatch.setattr(power, "_read_settings", lambda: None)
    assert power.load_power_map() is None


def test_load_power_map_none_when_map_invalid(monkeypatch):
    # Valid settings tuple but a map with no rated_hp -> build returns None.
    monkeypatch.setattr(power, "_read_settings", lambda: ({}, False))
    assert power.load_power_map() is None


def test_load_power_map_end_to_end_from_archive(tmp_path, monkeypatch):
    settings_dir = _patch_archive(monkeypatch, tmp_path)
    _write_backup(settings_dir, "Settings-2026.bak", worked_sids())
    pm = power.load_power_map()
    assert isinstance(pm, PowerMap)
    assert pm.rated_hp == 190.0
