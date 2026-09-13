# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for legacy settings_import config migration (config.py).

Pre-Requirement-14 configs stored a single Import_Marker directly under
``settings_import`` (scalar fields like ``last_update_value`` /
``last_backup_sha256``). ``migrate_settings_import`` (wired into
``load_config``) normalizes that into the per-Source_Key MAP shape by parking
the legacy scalars under the synthetic ``LEGACY_SOURCE_KEY`` and renaming
``last_backup_sha256`` -> ``content_hash`` so source-keyed detection routes it
through the undetermined-source fallback without crashing or regressing.

We also confirm ``Import_Workflow._decide`` tolerates a migrated legacy marker:
a genuinely undetermined candidate reaches the R14.8 branch (via a patched
``extract_source_key`` that keeps the candidate primary but source-key-less)
and is decided against the legacy marker with no crash and no regression.

Requirements: 14.2, 14.8
"""

import copy

from efis_data_manager import config as config_mod
from efis_data_manager import efis_settings as es
from efis_data_manager.config import (
    LEGACY_SOURCE_KEY,
    migrate_settings_import,
)
from efis_data_manager.efis_settings import (
    BOUND_IMPORT_SOURCE_KEY,
    Import_Workflow,
    ParsedBackup,
    content_hash,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parsed(sids: dict, update_value=1, path="Settings.dat") -> ParsedBackup:
    str_sids = {str(k): str(v) for k, v in sids.items()}
    return ParsedBackup(
        path=path,
        sids=str_sids,
        update_value=update_value,
        checksize="10",
        checksum="ABCD",
        line_count=len(str_sids),
        valid_pairs=len(str_sids),
    )


# ---------------------------------------------------------------------------
# migrate_settings_import — legacy single-object marker
# ---------------------------------------------------------------------------

def test_legacy_single_object_migrates_into_map_shape():
    """A legacy scalar marker moves under LEGACY_SOURCE_KEY without crashing."""
    config = {
        "settings_import": {
            "last_update_value": 5,
            "last_backup_sha256": "abc",
            "imported_at": "2025-01-01T00:00:00",
        }
    }
    migrate_settings_import(config)

    si = config["settings_import"]
    # The top-level scalar fields are gone; a per-source map entry exists.
    assert "last_update_value" not in si
    assert LEGACY_SOURCE_KEY in si
    legacy = si[LEGACY_SOURCE_KEY]
    assert legacy["last_update_value"] == 5
    assert legacy["imported_at"] == "2025-01-01T00:00:00"
    # last_backup_sha256 normalized to content_hash for the fallback path.
    assert legacy["content_hash"] == "abc"


def test_absent_settings_import_yields_empty_map():
    """An absent settings_import normalizes to an empty map (absent-tolerant)."""
    config = {}
    migrate_settings_import(config)
    assert config["settings_import"] == {}
    # bound_import_source stays absent (means "not yet bound").
    assert BOUND_IMPORT_SOURCE_KEY not in config["settings_import"]


def test_malformed_settings_import_does_not_raise():
    """A malformed (non-dict) settings_import is tolerated, normalized to {}."""
    for bad in ("a string", 42, ["list"], None):
        config = {"settings_import": bad}
        # Must not raise.
        migrate_settings_import(config)
        assert config["settings_import"] == {}


def test_already_map_shape_is_left_untouched():
    """An already source-keyed map (with bound key) is idempotent under migrate."""
    original = {
        "A60670:1": {
            "last_update_value": 12,
            "content_hash": "hh",
            "backup_name": "Settings-2026-01-01.dat",
            "imported_at": "2026-01-01T00:00:00",
        },
        BOUND_IMPORT_SOURCE_KEY: "A60670:1",
    }
    config = {"settings_import": copy.deepcopy(original)}
    migrate_settings_import(config)
    assert config["settings_import"] == original
    # Idempotent: a second pass changes nothing.
    migrate_settings_import(config)
    assert config["settings_import"] == original


def test_load_config_tolerates_legacy_shape(monkeypatch, tmp_path):
    """load_config runs migrate_settings_import so a legacy config loads clean."""
    import json

    cfg_file = tmp_path / "config.json"
    legacy = {
        "archive_path": str(tmp_path / "archive"),
        "settings_import": {
            "last_update_value": 7,
            "last_backup_sha256": "deadbeef",
            "imported_at": "2024-12-12T00:00:00",
        },
    }
    cfg_file.write_text(json.dumps(legacy), encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(config_mod, "ensure_dirs", lambda: None)

    loaded = config_mod.load_config()
    si = loaded["settings_import"]
    assert isinstance(si, dict)
    assert LEGACY_SOURCE_KEY in si
    assert si[LEGACY_SOURCE_KEY]["content_hash"] == "deadbeef"
    # num_cylinders stays a top-level key (merged from defaults).
    assert "num_cylinders" in loaded


# ---------------------------------------------------------------------------
# detect tolerates the legacy marker via the undetermined path (no regression)
# ---------------------------------------------------------------------------

def test_decide_routes_legacy_marker_through_undetermined_path():
    """A migrated legacy marker is compared via Content_Hash + UPDATE (R14.8).

    A genuinely undetermined candidate (no readable Source_Key) that is still
    primary reaches the R14.8 fallback and is decided against the legacy marker
    parked under LEGACY_SOURCE_KEY — no crash, and it never regresses.
    """
    # Legacy config after migration: scalars under LEGACY_SOURCE_KEY.
    config = {"settings_import": {
        "last_update_value": 10,
        "last_backup_sha256": "prior-hash",
        "imported_at": "2024-01-01T00:00:00",
    }}
    migrate_settings_import(config)
    assert LEGACY_SOURCE_KEY in config["settings_import"]

    # Candidate is primary (link_id treated as "1") but Source_Key undetermined.
    parsed = _parsed({"387": "1", "151": 400}, update_value=20)

    real_extract = es.extract_source_key

    def fake_extract(p):
        ident = real_extract(p)
        return es.SourceIdentity(source_key=None, mode_s=None,
                                 link_id=ident.link_id, flight_id=None)

    es.extract_source_key = fake_extract
    try:
        info = es._decide(parsed, config)
    finally:
        es.extract_source_key = real_extract

    # No crash. Candidate UPDATE (20) > legacy (10) and content differs from the
    # legacy "prior-hash" => it is offered via the undetermined-source fallback.
    assert info.available is True
    assert info.source_key is None

    # Regression guard: a not-newer candidate against the same legacy marker is
    # suppressed (never regresses through the undetermined path).
    parsed_old = _parsed({"387": "1", "151": 400}, update_value=5)
    es.extract_source_key = fake_extract
    try:
        info_old = es._decide(parsed_old, config)
    finally:
        es.extract_source_key = real_extract
    assert info_old.available is False


def test_decide_no_crash_on_migrated_absent_marker():
    """detect logic tolerates an absent/empty settings_import after migration."""
    config = {}
    migrate_settings_import(config)  # -> {"settings_import": {}}
    parsed = _parsed({"1063": "A60670", "387": "1", "123": 2700}, update_value=100)
    info = es._decide(parsed, config)
    # First-for-source primary is offered; no crash on empty migrated map.
    assert info.available is True
    assert info.is_first_for_source is True
