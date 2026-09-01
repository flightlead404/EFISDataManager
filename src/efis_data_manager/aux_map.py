# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""EIS auxiliary channel -> parameter mapping.

The GRT EIS has 6 auxiliary inputs (aux1-aux6). Which sensor is wired to each
channel varies per install, so the channel->meaning mapping is user
configurable (see config.aux_mapping). This module is the ONE place that
mapping is resolved into meaning, so display, extremes, and episode detection
never disagree.
"""

import logging

logger = logging.getLogger(__name__)

# The six configurable EIS auxiliary channels, in order.
AUX_CHANNELS = ("aux1", "aux2", "aux3", "aux4", "aux5", "aux6")

# Fixed catalog of known parameters: parameter_key -> (default_label, unit,
# precision). "custom" takes its label/unit from the per-channel config;
# "none" means the channel is unmapped and is not part of the catalog proper.
PARAM_CATALOG = {
    "amps": ("Amps", "A", 0),
    "manifold_pressure": ("MAP", '"', 1),
    "fuel_pressure": ("Fuel Press", "psi", 1),
    "fuel_level_left": ("Fuel L", "gal", 1),
    "fuel_level_right": ("Fuel R", "gal", 1),
    "vacuum": ("Vacuum", "inHg", 1),
    "coolant_pressure": ("Coolant Press", "psi", 1),
    "carb_temp": ("Carb Temp", "\u00b0F", 0),
    "custom": (None, None, 1),  # label/unit come from config
}

# Per-channel default mapping (mirrors config.DEFAULT_CONFIG["aux_mapping"]).
# Kept here so get_aux_mapping() can backfill defensively even if a saved
# config supplies a partial dict.
DEFAULT_AUX_MAPPING = {
    "aux1": {"parameter": "amps", "label": "Amps", "unit": "A"},
    "aux2": {"parameter": "manifold_pressure", "label": "MAP", "unit": "\""},
    "aux3": {"parameter": "fuel_pressure", "label": "Fuel Press", "unit": "psi"},
    "aux4": {"parameter": "none", "label": "", "unit": ""},
    "aux5": {"parameter": "none", "label": "", "unit": ""},
    "aux6": {"parameter": "none", "label": "", "unit": ""},
}


def _load_config():
    """Lazily load config, avoiding a circular import at module load time."""
    from .config import load_config

    return load_config()


def get_aux_mapping(config: dict = None) -> dict:
    """Return {channel: {parameter, label, unit}} with defaults backfilled.

    Any channel missing from the saved config (or missing keys within a
    channel) is filled from DEFAULT_AUX_MAPPING. The returned dict always
    contains all six channels with parameter/label/unit keys.
    """
    if config is None:
        config = _load_config()
    saved = config.get("aux_mapping", {}) or {}

    result = {}
    for channel in AUX_CHANNELS:
        default = DEFAULT_AUX_MAPPING[channel]
        entry = saved.get(channel, {}) or {}
        result[channel] = {
            "parameter": entry.get("parameter", default["parameter"]),
            "label": entry.get("label", default["label"]),
            "unit": entry.get("unit", default["unit"]),
        }
    return result


def _resolve_label_unit(parameter: str, entry: dict):
    """Resolve the effective (label, unit, precision) for a channel entry.

    For catalog parameters the canonical label/unit/precision come from
    PARAM_CATALOG (label may be overridden by a non-empty config label). For
    "custom", label/unit come entirely from the config entry.
    """
    if parameter == "custom":
        label = (entry.get("label") or "").strip()
        unit = entry.get("unit", "") or ""
        return label, unit, PARAM_CATALOG["custom"][2]

    default_label, default_unit, precision = PARAM_CATALOG[parameter]
    label = (entry.get("label") or "").strip() or (default_label or "")
    unit = entry.get("unit") or default_unit or ""
    return label, unit, precision


def resolve_aux(config: dict = None) -> dict:
    """Resolve the aux mapping into {parameter_key: {...}} for MAPPED channels.

    Returns a dict keyed by parameter_key. Each value is
    {"channel", "label", "unit", "precision"}.

    A channel is MAPPED if and only if its parameter is not None/"none" AND it
    resolves to a non-empty label (a Custom channel with an empty label is
    treated as unmapped). Mapping is the sole gate: whether the underlying
    column contains data is irrelevant here.

    Parameter->channel is unique. If two channels map to the same parameter
    (a misconfiguration), the first channel wins and a warning is logged.
    This is the ONE place aux meaning is derived.
    """
    mapping = get_aux_mapping(config)

    resolved = {}
    for channel in AUX_CHANNELS:
        entry = mapping[channel]
        parameter = entry.get("parameter")

        # Unmapped: explicit None/"none".
        if parameter is None or parameter == "none":
            continue
        # Unknown parameter (not in catalog) -> treat as unmapped, warn.
        if parameter not in PARAM_CATALOG:
            logger.warning(
                "Aux channel %s maps to unknown parameter %r; ignoring",
                channel,
                parameter,
            )
            continue

        label, unit, precision = _resolve_label_unit(parameter, entry)

        # Empty label (e.g. Custom without a label) -> treated as unmapped.
        if not label:
            continue

        # Parameter->channel must be unique: first wins, warn on duplicate.
        if parameter in resolved:
            logger.warning(
                "Parameter %r mapped to multiple channels (%s and %s); "
                "keeping %s",
                parameter,
                resolved[parameter]["channel"],
                channel,
                resolved[parameter]["channel"],
            )
            continue

        resolved[parameter] = {
            "channel": channel,
            "label": label,
            "unit": unit,
            "precision": precision,
        }

    return resolved
