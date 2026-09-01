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
}


def ensure_dirs():
    """Create application support and default archive directories if they don't exist."""
    APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    """Load configuration from disk, creating defaults if not present."""
    ensure_dirs()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                saved = json.load(f)
            # Merge with defaults so new keys are picked up on upgrade
            config = {**DEFAULT_CONFIG, **saved}
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
