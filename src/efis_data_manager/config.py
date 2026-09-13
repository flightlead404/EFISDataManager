# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Configuration management for EFIS Data Manager.

Settings are persisted as JSON in ~/Library/Application Support/EFISDataManager/config.json.
"""

import json
import os
from pathlib import Path


APP_NAME = "EFISDataManager"
APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
CONFIG_FILE = APP_SUPPORT_DIR / "config.json"

DEFAULT_CONFIG = {
    "archive_path": str(Path.home() / "Documents" / "EFIS_Archive"),
    "usb_image_path": str(Path.home() / "Documents" / "EFIS_USBImage"),
    "tail_number": "",
    # Which Seattle Avionics chart products to download. Users select based on
    # their subscription and needs; e.g. VFR-only pilots turn off IFR low/high
    # and approach plates. Keys map to SA download-table description substrings
    # in currency.CHART_TYPE_MATCHERS.
    "chart_types": {
        "sectional": True,
        "ifr_low": True,
        "ifr_high": False,
        "approach_plates": True,
    },
    "check_charts_interval_hours": 12,
    "check_nav_interval_hours": 24,
    "check_software_interval_hours": 24,
    # Engine/aircraft configuration
    "engine_category": "traditional",  # "traditional" | "water_cooled"
    "num_cylinders": 4,
    "engine_type": "IO-360",
    # EIS auxiliary channel mapping. Each aux channel maps to a parameter with a
    # display label and unit. Defaults preserve the original hardcoded install
    # convention (aux1=amps, aux2=MAP, aux3=fuel pressure) so existing installs
    # are unaffected. aux4-6 are unmapped ("none") by default.
    "aux_mapping": {
        "aux1": {"parameter": "amps", "label": "Amps", "unit": "A"},
        "aux2": {"parameter": "manifold_pressure", "label": "MAP", "unit": "\""},
        "aux3": {"parameter": "fuel_pressure", "label": "Fuel Press", "unit": "psi"},
        "aux4": {"parameter": "none", "label": "", "unit": ""},
        "aux5": {"parameter": "none", "label": "", "unit": ""},
        "aux6": {"parameter": "none", "label": "", "unit": ""},
    },
    # Analysis thresholds (overridable)
    "analysis_thresholds": {},
    # Per-Source_Key EFIS settings-import markers (map of Source_Key ->
    # Import_Marker, plus a reserved "bound_import_source" string once bound).
    # Absent/empty means no import has been performed yet.
    "settings_import": {},
    # Flight detection
    "airborne_ias_threshold": 40,
    "cruise_vs_threshold": 300,
    "cruise_rpm_min": 1800,
    # Dashboard
    "dashboard_port": 5050,
    "trend_window_hours": 25,
    # Oil tracking: ignore oil events before this date (YYYY-MM-DD), for
    # discarding unreliable historical data. Empty = use all.
    "oil_cutoff_date": "",
    # Oil-age warning: engine hours since the most recent oil change above which
    # the Oil page shows a banner. 0 disables it (the default, so existing
    # installs are unaffected until the user opts in). Recommended ~40-50h. A
    # negative value is treated as 0 (disabled).
    "oil_age_warning_hours": 0,
}


def ensure_dirs():
    """Create application support and default archive directories if they don't exist."""
    APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)


# Synthetic Source_Key used to carry a legacy single-object settings_import
# marker so source-keyed detection can compare against it via the
# undetermined-source path without crashing or regressing (see
# migrate_settings_import). It cannot collide with a real Source_Key, which is
# always "<Mode_S>:<Link_ID>", a bare Mode_S, or ":<Link_ID>".
LEGACY_SOURCE_KEY = ":legacy"

# Field names that identify a LEGACY single-object settings_import marker (the
# pre-Req-14 shape stored scalars directly under settings_import).
_LEGACY_MARKER_FIELDS = {
    "last_update_value",
    "last_backup_sha256",
    "content_hash",
    "backup_name",
    "imported_at",
}


def migrate_settings_import(config: dict) -> dict:
    """Normalize a legacy single-object ``settings_import`` into the map shape.

    The pre-Requirement-14 config stored a single Import_Marker directly under
    ``settings_import`` (scalar fields like ``last_update_value`` /
    ``last_backup_sha256``). The current shape is a MAP of per-Source_Key
    Import_Markers plus a reserved ``bound_import_source`` string.

    This converts a legacy single-object marker into an entry under the
    synthetic ``LEGACY_SOURCE_KEY`` so source-keyed detection routes it through
    the undetermined-source Content_Hash + UPDATE fallback (never regressing).
    An absent ``bound_import_source`` is left absent — it means "not yet bound",
    and the first eligible Primary import binds it; no explicit step is needed.

    Mutates and returns ``config``. Safe to call repeatedly (idempotent) and on
    an already-migrated or missing ``settings_import`` (leaves it untouched).
    ``num_cylinders`` remains a top-level key and is never moved here.
    """
    si = config.get("settings_import")
    if not isinstance(si, dict):
        # Missing or malformed -> normalize to an empty map so the load path
        # (and detection) never crashes on its absence.
        config["settings_import"] = {}
        return config

    # A legacy single-object marker has scalar marker fields at the top level.
    looks_legacy = any(field in si for field in _LEGACY_MARKER_FIELDS)
    if not looks_legacy:
        return config  # already the map shape (or empty) — nothing to do

    # Extract the reserved bound key (if somehow present) and the legacy scalars.
    from efis_data_manager.efis_settings import BOUND_IMPORT_SOURCE_KEY

    bound = si.get(BOUND_IMPORT_SOURCE_KEY)
    legacy_marker = {
        k: v for k, v in si.items()
        if k != BOUND_IMPORT_SOURCE_KEY and not isinstance(v, dict)
    }
    # Normalize the legacy hash field name to content_hash for the fallback.
    if "content_hash" not in legacy_marker and "last_backup_sha256" in legacy_marker:
        legacy_marker["content_hash"] = legacy_marker.get("last_backup_sha256")

    new_si: dict = {}
    # Preserve any already-keyed map entries alongside the legacy object.
    for k, v in si.items():
        if isinstance(v, dict):
            new_si[k] = v
    new_si[LEGACY_SOURCE_KEY] = legacy_marker
    if bound is not None:
        new_si[BOUND_IMPORT_SOURCE_KEY] = bound

    config["settings_import"] = new_si
    return config


def load_config() -> dict:
    """Load configuration from disk, creating defaults if not present."""
    ensure_dirs()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                saved = json.load(f)
            # Merge with defaults so new keys are picked up on upgrade
            config = {**DEFAULT_CONFIG, **saved}
            # Tolerate a legacy single-object settings_import; ensure the load
            # path never crashes on an absent/legacy shape (Req 14.2, 14.8, 15.3).
            migrate_settings_import(config)
            return config
        except (json.JSONDecodeError, OSError):
            # Corrupt config — reset to defaults
            pass
    return dict(DEFAULT_CONFIG)


def save_config(config: dict):
    """Persist configuration to disk."""
    ensure_dirs()
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_archive_path(config: dict = None) -> Path:
    """Return the archive path, creating it if necessary."""
    if config is None:
        config = load_config()
    path = Path(config["archive_path"])
    path.mkdir(parents=True, exist_ok=True)
    return path
