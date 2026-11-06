# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Regression: archived-backup discovery reads the per-device Settings layout.

Validates Requirements 11.7.
"""

import efis_data_manager.efis_settings as es
from tests.test_power_map import worked_sids


def _write(path, sids):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"{k}={v}" for k, v in sids.items()) + "\n")


def test_backup_only_under_link1_is_found(tmp_path):
    settings_dir = tmp_path / "Settings"
    sids = dict(worked_sids())
    sids["UPDATE"] = "5"
    _write(settings_dir / "link1" / "Settings-2026-01-01.bak", sids)
    (settings_dir / "link2").mkdir()  # empty sibling is harmless

    result = es._select_current_archived_backup(settings_dir)
    assert result is not None and result.current is not None
    assert result.current.sids.get("167") == "190"


def test_deeper_nesting_is_not_searched(tmp_path):
    settings_dir = tmp_path / "Settings"
    _write(settings_dir / "link1" / "deep" / "Settings-2026-01-01.bak",
           dict(worked_sids()))
    assert es._select_current_archived_backup(settings_dir) is None


# ---------------------------------------------------------------------------
# Task 12.2: public entry points over the per-device layout (Req 11.7)
# ---------------------------------------------------------------------------
import pytest  # noqa: E402

import efis_data_manager.config as config_mod  # noqa: E402


def _primary(rated_hp="190", update="5", **extra):
    """Valid primary-display backup: SID 387 = 1, Mode_S 1063, mapped SIDs."""
    sids = dict(worked_sids())
    sids.update({"387": "1", "1063": "A60670", "167": str(rated_hp),
                 "UPDATE": str(update)})
    sids.update(extra)
    return sids


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """Point load_current_settings_sids at tmp_path; return the Settings dir."""
    monkeypatch.setattr(config_mod, "load_config",
                        lambda: {"archive_path": str(tmp_path)})
    return tmp_path / "Settings"


def _detect(tmp_path):
    return es.Import_Workflow.detect_new_backup({}, tmp_path)


def test_detect_new_backup_finds_link1_only(tmp_path):
    _write(tmp_path / "Settings" / "link1" / "Settings-2026-01-01.bak",
           _primary())
    info = _detect(tmp_path)
    assert info is not None
    assert info.available is True
    assert info.is_primary is True


def test_detect_new_backup_none_for_empty_or_missing_tree(tmp_path):
    assert _detect(tmp_path) is None  # no Settings dir at all
    (tmp_path / "Settings" / "link1").mkdir(parents=True)
    assert _detect(tmp_path) is None  # present but empty


def test_load_sids_finds_link1_backup_and_temp_flag(archive):
    _write(archive / "link1" / "Settings-2026-01-01.bak",
           _primary(**{"345": "1"}))
    result = es.load_current_settings_sids()
    assert result is not None
    sids, celsius = result
    assert sids.get("167") == "190"
    assert celsius is True


def test_load_sids_temp_flag_false_when_not_celsius(archive):
    _write(archive / "link1" / "Settings-2026-01-01.bak",
           _primary(**{"345": "0"}))
    result = es.load_current_settings_sids()
    assert result is not None and result[1] is False


def test_legacy_flat_files_still_found(archive, tmp_path):
    _write(archive / "Settings-2026-01-01.bak", _primary(rated_hp="180"))
    result = es.load_current_settings_sids()
    assert result is not None and result[0].get("167") == "180"
    assert _detect(tmp_path) is not None


def test_latest_archive_date_wins_across_devices(archive):
    _write(archive / "link1" / "Settings-2026-01-01.bak",
           _primary(rated_hp="180", update="99"))
    _write(archive / "link2" / "Settings-2026-02-01.bak",
           _primary(rated_hp="200", update="1"))
    result = es.load_current_settings_sids()
    assert result is not None and result[0].get("167") == "200"


def test_flat_and_subdir_compete_by_date(archive):
    _write(archive / "Settings-2026-03-01.bak", _primary(rated_hp="170"))
    _write(archive / "link1" / "Settings-2026-01-01.bak",
           _primary(rated_hp="180"))
    result = es.load_current_settings_sids()
    assert result is not None and result[0].get("167") == "170"


def test_same_date_pair_in_dir_resolved_by_update(archive):
    _write(archive / "link1" / "Settings-2026-01-01.bak",
           _primary(rated_hp="180", update="1"))
    _write(archive / "link1" / "Settings-2026-01-01.dat",
           _primary(rated_hp="190", update="9"))
    result = es.load_current_settings_sids()
    assert result is not None and result[0].get("167") == "190"


def test_non_settings_files_are_ignored(archive):
    _write(archive / "link1" / "Settings-2026-01-01.bak",
           _primary(rated_hp="180"))
    # Later-dated, otherwise valid files that must not be considered.
    for name in ("WP-2026-06-01.dat", "Plan-2026-06-01.dat",
                 "State-2026-06-01.dat", "WP-2026-06-01.bak"):
        _write(archive / "link1" / name, _primary(rated_hp="999"))
    result = es.load_current_settings_sids()
    assert result is not None and result[0].get("167") == "180"


def test_only_non_settings_files_yields_none(archive, tmp_path):
    _write(archive / "link1" / "WP-2026-06-01.dat", _primary())
    assert es.load_current_settings_sids() is None
    assert _detect(tmp_path) is None


def test_unresolved_snapshot_is_considered(archive, tmp_path):
    _write(archive / "unresolved" / "Settings-2026-01-01.bak", _primary())
    result = es.load_current_settings_sids()
    assert result is not None and result[0].get("167") == "190"
    assert _detect(tmp_path) is not None


def test_unparseable_file_is_skipped_without_hiding_valid_one(archive):
    _write(archive / "link1" / "Settings-2026-01-01.bak",
           _primary(rated_hp="180"))
    # Later-dated garbage in another dir and in the flat dir.
    for bad in (archive / "link2" / "Settings-2026-05-01.bak",
                archive / "Settings-2026-05-01.dat"):
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"\x00\xff\xfe not a settings file \x00")
    result = es.load_current_settings_sids()
    assert result is not None and result[0].get("167") == "180"


def test_empty_tree_with_sentinel_and_empty_subdirs_is_none(archive, tmp_path):
    archive.mkdir(parents=True)
    (archive / ".sentinel").write_text("x")
    for d in ("link1", "link2", "unresolved"):
        (archive / d).mkdir()
    assert es.load_current_settings_sids() is None
    assert es._select_current_archived_backup(archive) is None
    assert _detect(tmp_path) is None


# ---------------------------------------------------------------------------
# Task 14.1: import detection is restricted to the primary display (R11.8)
# ---------------------------------------------------------------------------
def _secondary(rated_hp="200", update="1", **extra):
    """Non-primary (Link 2) backup: SID 387 = 2."""
    return _primary(rated_hp=rated_hp, update=update, **{"387": "2", **extra})


def test_newer_link2_does_not_hide_older_link1(archive, tmp_path):
    _write(archive / "link1" / "Settings-2026-01-01.bak",
           _primary(rated_hp="180"))
    _write(archive / "link2" / "Settings-2026-02-01.bak", _secondary())
    info = _detect(tmp_path)
    assert info is not None
    assert info.is_primary is True
    assert info.available is True
    assert info.parsed.sids.get("167") == "180"


def test_primary_in_unresolved_is_found_despite_newer_link2(archive, tmp_path):
    _write(archive / "unresolved" / "Settings-2026-01-01.bak",
           _primary(rated_hp="180"))
    _write(archive / "link2" / "Settings-2026-02-01.bak", _secondary())
    info = _detect(tmp_path)
    assert info is not None and info.is_primary is True
    assert info.parsed.sids.get("167") == "180"


def test_primary_legacy_flat_file_is_found_despite_newer_link2(archive, tmp_path):
    _write(archive / "Settings-2026-01-01.bak", _primary(rated_hp="180"))
    _write(archive / "link2" / "Settings-2026-02-01.bak", _secondary())
    info = _detect(tmp_path)
    assert info is not None and info.is_primary is True
    assert info.parsed.sids.get("167") == "180"


def test_only_non_primary_backups_yields_none(archive, tmp_path):
    _write(archive / "link2" / "Settings-2026-02-01.bak", _secondary())
    _write(archive / "link3" / "Settings-2026-03-01.bak",
           _secondary(**{"387": "3"}))
    assert _detect(tmp_path) is None


def test_load_sids_still_selects_newer_link2(archive):
    _write(archive / "link1" / "Settings-2026-01-01.bak",
           _primary(rated_hp="180"))
    _write(archive / "link2" / "Settings-2026-02-01.bak",
           _secondary(rated_hp="200"))
    result = es.load_current_settings_sids()
    assert result is not None and result[0].get("167") == "200"


def test_select_link_id_none_is_unfiltered_and_filter_applies(archive):
    _write(archive / "link1" / "Settings-2026-01-01.bak",
           _primary(rated_hp="180"))
    _write(archive / "link2" / "Settings-2026-02-01.bak",
           _secondary(rated_hp="200"))
    unfiltered = es._select_current_archived_backup(archive)
    assert unfiltered.current.sids.get("167") == "200"
    filtered = es._select_current_archived_backup(archive, link_id="1")
    assert filtered.current.sids.get("167") == "180"
    assert es._select_current_archived_backup(archive, link_id="9") is None
