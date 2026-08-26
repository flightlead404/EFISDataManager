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
    "tail_number": "N488BF",
    "check_charts_interval_hours": 12,
    "check_nav_interval_hours": 24,
    "check_software_interval_hours": 24,
    # Engine/aircraft configuration
    "num_cylinders": 4,
    "engine_type": "IO-360",
    # Analysis thresholds (overridable)
    "analysis_thresholds": {},
    # Flight detection
    "airborne_ias_threshold": 40,
    "cruise_vs_threshold": 300,
    "cruise_rpm_min": 1800,
    # Dashboard
    "dashboard_port": 5050,
    "trend_window_hours": 25,
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
