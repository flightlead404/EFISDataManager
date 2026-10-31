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
from typing import Optional


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
    # Aircraft-keyed device configuration (multi-device-settings spec). Map of
    # Mode_S -> { device_count, devices{Link_ID -> Device_Config},
    # analysis_data_source }. An empty map means the managed aircraft has not
    # been learned yet (single-device defaults apply until then). This increment
    # manages at most ONE real aircraft entry (plus possibly the PENDING_
    # AIRCRAFT_KEY sentinel), but the map shape is forward-compatible with
    # multi-aircraft.
    "aircraft": {},
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


# Reserved sentinel key under ``config["aircraft"]`` that holds a pre-learn
# device-config record before the managed aircraft's Mode_S is known. It cannot
# collide with a real Mode_S: a real Mode_S is an ICAO 24-bit hex string (e.g.
# "A60670"), while ":pending" starts with a colon and is non-hex. This mirrors
# the existing ``LEGACY_SOURCE_KEY = ":legacy"`` precedent above.
PENDING_AIRCRAFT_KEY = ":pending"


def managed_mode_s(config: dict) -> Optional[str]:
    """Return the single managed aircraft's Mode_S, or None if not yet learned.

    The managed Mode_S is the single real (non-sentinel) key under
    ``config["aircraft"]``. ``PENDING_AIRCRAFT_KEY`` is ignored, so a config that
    only holds a pending pre-learn record reports no managed aircraft. Returns
    None when no real aircraft key exists.
    """
    aircraft = config.get("aircraft")
    if not isinstance(aircraft, dict):
        return None
    for key in aircraft:
        if key != PENDING_AIRCRAFT_KEY:
            return key
    return None


def managed_aircraft(config: dict) -> Optional[dict]:
    """Return the managed aircraft record, or None if not yet learned.

    The record is the value stored under the single real (non-sentinel) Mode_S
    key (see :func:`managed_mode_s`).
    """
    mode_s = managed_mode_s(config)
    if mode_s is None:
        return None
    return config["aircraft"].get(mode_s)


def configured_device(config: dict, link_id: str) -> Optional[dict]:
    """Return the Device_Config for a Link_ID under the managed aircraft.

    ``link_id`` is the string position key ("1" / "2" / "3"). Returns None when
    no aircraft is learned, the aircraft has no ``devices`` map, or no device is
    configured for that Link_ID.
    """
    aircraft = managed_aircraft(config)
    if not isinstance(aircraft, dict):
        return None
    devices = aircraft.get("devices")
    if not isinstance(devices, dict):
        return None
    return devices.get(link_id)


def device_count(config: dict) -> int:
    """Return the managed aircraft's device count, defaulting to 1.

    When no aircraft is learned (or the record lacks a ``device_count``), the
    install is treated as single-device and this returns 1.
    """
    aircraft = managed_aircraft(config)
    if not isinstance(aircraft, dict):
        return 1
    count = aircraft.get("device_count")
    if not isinstance(count, int):
        return 1
    return count


def device_label(device: dict) -> str:
    """Compose a device's display label.

    - ``Link N``                 when no friendly name is set (R1.6, R5.4)
    - ``{friendly name} (Link N)`` when a friendly name is set (R1.7, R5.3)

    N is the device's Link_ID. A friendly name that is empty or all-whitespace
    is treated as unset (falls back to ``Link N``).
    """
    link_id = device.get("link_id")
    name = (device.get("friendly_name") or "").strip()
    if name:
        return f"{name} (Link {link_id})"
    return f"Link {link_id}"


def migrate_aircraft_config(config: dict) -> dict:
    """Normalize a pre-aircraft-keyed config into the aircraft-keyed map shape.

    A config that predates this feature has NO top-level ``aircraft`` key (the
    defaults merge supplies ``{}``). This presents it as the aircraft-keyed map
    without raising (R6.3):

      - ``aircraft`` missing or not a dict -> set to ``{}`` (managed aircraft
        not yet learned); single-device behaviour applies until a Mode_S is
        learned.
      - ``aircraft`` already a dict (real Mode_S keys and/or the
        ``PENDING_AIRCRAFT_KEY`` sentinel, or empty) -> leave it untouched; an
        empty map is the valid "not yet learned" state.

    The Mode_S is bound later, on insert, per Requirement 3 — migration never
    invents a Mode_S. Mutates and returns ``config``. Safe to call repeatedly
    (idempotent) and on an already-migrated or missing ``aircraft`` key.
    """
    aircraft = config.get("aircraft")
    if not isinstance(aircraft, dict):
        # Missing or malformed -> normalize to an empty map (managed aircraft
        # not yet learned) so the load path never crashes on its absence.
        config["aircraft"] = {}
        return config
    # Already an aircraft-keyed map (real Mode_S keys and/or the pending
    # sentinel, or empty) — nothing to do.
    return config


# ---------------------------------------------------------------------------
# Settings-save validation (reject-and-retain)
# ---------------------------------------------------------------------------

# Allowed device-count / Link_ID range for the managed aircraft (R1.3, R1.9).
_MIN_DEVICE_COUNT = 1
_MAX_DEVICE_COUNT = 3

# The sensible default managed-aircraft record used when there is no prior valid
# record to fall back to and a proposed field is rejected (R1.3/R1.9: a single
# chart-capable device at Link_ID "1"). This mirrors the single-device default
# that ``device_count``/``configured_device`` imply for an unlearned aircraft.
_DEFAULT_DEVICE_COUNT = 1
_DEFAULT_ANALYSIS_SOURCE = "1"


def _default_device(link_id: str) -> dict:
    """Return a default Device_Config for a Link_ID (empty name, chart-capable)."""
    return {"link_id": link_id, "friendly_name": "", "chart_capable": True}


def _default_record() -> dict:
    """Return the sensible fallback managed-aircraft record (single device)."""
    return {
        "device_count": _DEFAULT_DEVICE_COUNT,
        "devices": {"1": _default_device("1")},
        "analysis_data_source": _DEFAULT_ANALYSIS_SOURCE,
    }


def validate_device_config(
    prior: Optional[dict], proposed: dict
) -> "tuple[dict, list[str]]":
    """Validate proposed device-config edits against the prior valid record.

    This is the pure, deterministic, side-effect-free validation extracted from
    the Settings-save handler so both ``settings_window.py`` and tests can use
    it without a UI dependency (no I/O, no ``save_config``, no rumps).

    ``prior`` is the last-known VALID managed-aircraft record (shape:
    ``{device_count, devices{link_id -> Device_Config}, analysis_data_source}``),
    or ``None`` when no prior record exists. ``proposed`` carries the user's
    attempted edits in the same shape.

    Returns ``(record_to_persist, rejections)``. The "reject-and-retain"
    invariant (Property 10) governs every field: when a proposed field is
    rejected, the PRIOR valid value for THAT field is retained (never a
    partial/invalid state); other valid edits still apply. When ``prior`` is
    ``None`` and a rejected field has nothing to fall back to, a sensible default
    is used (``device_count`` 1, a single chart-capable device at Link_ID "1").
    ``rejections`` is a list of human-readable reasons the UI can surface; an
    empty list means every edit was accepted.

    Validation rules (Requirement 1):

    - ``device_count`` must be an int in 1..3 inclusive; otherwise rejected and
      the prior valid count retained (R1.3, R1.4).
    - Each device carries ``friendly_name`` (free string, may be empty),
      ``chart_capable`` (a user-declared boolean, stored as given with NO catalog
      lookup; R1.5, R1.8), and ``link_id`` ("1"/"2"/"3") in range 1..3 (R1.9). A
      ``link_id`` out of range, or one that duplicates another configured
      device's Link_ID, is rejected and the prior Link_ID assignment for that
      device slot retained (R1.9, R1.10).
    - ``analysis_data_source`` is a single Link_ID naming exactly ONE configured
      device; any configured device is selectable regardless of its
      ``chart_capable`` value (NO capability gating; R1.11, R1.12). A selection
      naming a non-configured device is rejected and the prior selection retained.
    """
    rejections: list[str] = []
    prior = prior if isinstance(prior, dict) else None
    proposed = proposed if isinstance(proposed, dict) else {}

    prior_devices = prior.get("devices") if prior else None
    prior_devices = prior_devices if isinstance(prior_devices, dict) else {}
    proposed_devices = proposed.get("devices")
    proposed_devices = proposed_devices if isinstance(proposed_devices, dict) else {}

    # --- device_count (R1.3, R1.4) ----------------------------------------
    prior_count = prior.get("device_count") if prior else None
    if not isinstance(prior_count, int) or not (
        _MIN_DEVICE_COUNT <= prior_count <= _MAX_DEVICE_COUNT
    ):
        prior_count = _DEFAULT_DEVICE_COUNT

    proposed_count = proposed.get("device_count", prior_count)
    # A bool is an int subclass; reject it explicitly so True/False can't pass
    # as a device count.
    if (
        isinstance(proposed_count, bool)
        or not isinstance(proposed_count, int)
        or not (_MIN_DEVICE_COUNT <= proposed_count <= _MAX_DEVICE_COUNT)
    ):
        rejections.append(
            f"Device count {proposed_count!r} is out of range "
            f"({_MIN_DEVICE_COUNT}-{_MAX_DEVICE_COUNT}); keeping {prior_count}."
        )
        device_count = prior_count
    else:
        device_count = proposed_count

    # --- per-device Link_ID (R1.5, R1.8, R1.9, R1.10) ---------------------
    # Resolve each device slot in a deterministic order. Slots are keyed by the
    # STORED key of the proposed devices map (falling back to prior keys). For
    # each slot, validate the proposed link_id against the 1..3 range and against
    # Link_IDs already assigned to EARLIER-resolved slots; on rejection, retain
    # the prior Link_ID for that slot (or the slot key when there is no prior).
    valid_links = {str(n) for n in range(_MIN_DEVICE_COUNT, _MAX_DEVICE_COUNT + 1)}

    # Deterministic slot ordering: proposed device keys first (sorted), then any
    # prior-only keys (sorted), so the result does not depend on dict insertion
    # order.
    slot_keys: list[str] = sorted(proposed_devices.keys())
    for key in sorted(prior_devices.keys()):
        if key not in proposed_devices:
            slot_keys.append(key)

    devices: dict = {}
    assigned: set[str] = set()
    for slot in slot_keys:
        prop_dev = proposed_devices.get(slot)
        prop_dev = prop_dev if isinstance(prop_dev, dict) else {}
        prior_dev = prior_devices.get(slot)
        prior_dev = prior_dev if isinstance(prior_dev, dict) else None

        # friendly_name and chart_capable are stored AS GIVEN (no validation;
        # R1.5, R1.8). Fall back to the prior value, then to a sensible default.
        if "friendly_name" in prop_dev:
            friendly_name = prop_dev.get("friendly_name") or ""
        elif prior_dev is not None:
            friendly_name = prior_dev.get("friendly_name") or ""
        else:
            friendly_name = ""

        if "chart_capable" in prop_dev:
            chart_capable = bool(prop_dev.get("chart_capable"))
        elif prior_dev is not None:
            chart_capable = bool(prior_dev.get("chart_capable"))
        else:
            chart_capable = True

        # Determine the prior valid Link_ID for this slot to fall back to.
        prior_link = prior_dev.get("link_id") if prior_dev else None
        if prior_link not in valid_links or prior_link in assigned:
            # No usable prior Link_ID: fall back to the slot key if it is itself
            # a valid, unused Link_ID, else the first free Link_ID.
            if slot in valid_links and slot not in assigned:
                prior_link = slot
            else:
                prior_link = next(
                    (lid for lid in sorted(valid_links) if lid not in assigned),
                    slot,
                )

        proposed_link = prop_dev.get("link_id", prior_link)
        if proposed_link not in valid_links:
            rejections.append(
                f"Link_ID {proposed_link!r} for device {slot!r} is out of range "
                f"(1-3); keeping {prior_link!r}."
            )
            link_id = prior_link
        elif proposed_link in assigned:
            rejections.append(
                f"Link_ID {proposed_link!r} for device {slot!r} duplicates another "
                f"device; keeping {prior_link!r}."
            )
            link_id = prior_link
        else:
            link_id = proposed_link

        assigned.add(link_id)
        devices[link_id] = {
            "link_id": link_id,
            "friendly_name": friendly_name,
            "chart_capable": chart_capable,
        }

    if not devices:
        # Nothing configured on either side: fall back to a single default
        # device so the persisted record is always resolvable (R1.3/R6.1).
        devices = {"1": _default_device("1")}

    # --- analysis_data_source (R1.11, R1.12) ------------------------------
    # Exactly one Link_ID naming one configured device; NO capability gating.
    prior_source = prior.get("analysis_data_source") if prior else None
    if prior_source not in devices:
        # Prior selection is no longer configured (or absent): fall back to the
        # lowest configured Link_ID deterministically.
        prior_source = sorted(devices.keys())[0]

    proposed_source = proposed.get("analysis_data_source", prior_source)
    if proposed_source in devices:
        analysis_data_source = proposed_source
    else:
        rejections.append(
            f"Analysis-data-source {proposed_source!r} names no configured "
            f"device; keeping {prior_source!r}."
        )
        analysis_data_source = prior_source

    record = {
        "device_count": device_count,
        "devices": devices,
        "analysis_data_source": analysis_data_source,
    }
    return record, rejections


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
            # Normalize the aircraft-keyed device-config map; same precedent and
            # crash-safe guarantees as migrate_settings_import (Req 6.1-6.3).
            migrate_aircraft_config(config)
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
