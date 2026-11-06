# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Flask web application for EFIS flight data dashboard."""

import json
import logging
import os
from datetime import datetime

from flask import Flask, render_template, jsonify, request, send_file

from efis_data_manager.config import load_config, save_config
from efis_data_manager.aux_map import resolve_aux, PARAM_CATALOG, AUX_CHANNELS
from efis_data_manager.database import get_db_connection
from efis_data_manager.analysis import (
    get_flight_stats, get_all_flight_stats, compute_trends,
    detect_anomalies, compute_oil_consumption_rolling, get_thresholds,
    DEFAULT_THRESHOLDS,
)
from efis_data_manager.export import (
    export_flight_summaries, export_trends, export_oil_consumption,
    export_flight_raw,
)

from efis_data_manager import DASHBOARD_VERSION

logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder="templates")


@app.context_processor
def inject_version():
    """Make the dashboard version available in all templates."""
    return {"dashboard_version": DASHBOARD_VERSION}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Main dashboard — flight list with summary stats."""
    return render_template("index.html")


@app.route("/flight/<int:flight_id>")
def flight_detail(flight_id):
    """Single flight view with dual time-series charts."""
    return render_template("flight.html", flight_id=flight_id)


@app.route("/flight/<int:flight_id>/gami")
def gami_page(flight_id):
    """GAMI lean test analysis view for an operation."""
    return render_template("gami.html", flight_id=flight_id)


@app.route("/api/flight/<int:flight_id>/gami")
def api_gami(flight_id):
    """Detected GAMI lean strokes + the EGT-vs-fuel-flow curve for each."""
    from efis_data_manager.gami import detect_gami_strokes, stroke_to_dict, get_stroke_curves
    strokes = detect_gami_strokes(flight_id)
    out = []
    for s in strokes:
        d = stroke_to_dict(s)
        d["curves"] = get_stroke_curves(flight_id, s.start_time, s.end_time)
        out.append(d)
    return jsonify(out)


@app.route("/trends")
def trends_page():
    """Trend analysis view."""
    return render_template("trends.html")


@app.route("/alerts")
def alerts_page():
    """Alerts and anomalies view."""
    return render_template("alerts.html")


@app.route("/oil")
def oil_page():
    """Oil consumption tracking view."""
    return render_template("oil.html")


@app.route("/settings")
def settings_page():
    """Settings management."""
    return render_template("settings.html")


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.route("/api/operations/delete", methods=["POST"])
def api_delete_operations():
    """Delete one or more operations (and their time-series data)."""
    from efis_data_manager.database import delete_operation
    ids = (request.get_json() or {}).get("ids", [])
    deleted = 0
    for op_id in ids:
        try:
            if delete_operation(int(op_id)):
                deleted += 1
        except (ValueError, TypeError):
            pass
    return jsonify({"status": "ok", "deleted": deleted})


@app.route("/api/flights")
def api_flights():
    """Get all operations with summary data.

    Query param ?flights_only=1 excludes ground operations.
    """
    flights_only = request.args.get("flights_only") == "1"
    conn = get_db_connection()
    try:
        query = "SELECT * FROM operations"
        if flights_only:
            query += " WHERE has_flight = 1"
        query += " ORDER BY date DESC, start_time DESC"
        ops = conn.execute(query).fetchall()
        return jsonify([dict(o) for o in ops])
    finally:
        conn.close()


@app.route("/api/flight/<int:flight_id>")
def api_flight_detail(flight_id):
    """Get detailed stats for a specific flight."""
    stats = get_flight_stats(flight_id)
    if not stats:
        return jsonify({"error": "Flight not found"}), 404

    return jsonify({
        "flight_id": stats.flight_id,
        "operation_id": stats.operation_id,
        "has_flight": stats.has_flight,
        "date": stats.date,
        "duration_seconds": stats.duration_seconds,
        "airborne_seconds": stats.airborne_seconds,
        "max_ground_speed": stats.max_ground_speed,
        "max_indicated_airspeed": stats.max_indicated_airspeed,
        "avg_ias_cruise": stats.avg_ias_cruise,
        "max_altitude": stats.max_altitude,
        "max_g_load": stats.max_g_load,
        "min_g_load": stats.min_g_load,
        "max_rpm": stats.max_rpm,
        "avg_rpm_cruise": stats.avg_rpm_cruise,
        "avg_percent_power_cruise": stats.avg_percent_power_cruise,
        "max_cht": stats.max_cht,
        "max_egt": stats.max_egt,
        "cht_spread_max": stats.cht_spread_max,
        "egt_spread_max": stats.egt_spread_max,
        "max_oil_temp": stats.max_oil_temp,
        "min_oil_pressure_cruise": stats.min_oil_pressure_cruise,
        "fuel_used": stats.fuel_used,
        "avg_fuel_flow_cruise": stats.avg_fuel_flow_cruise,
        "min_voltage": stats.min_voltage,
        "hourmeter_start": stats.hourmeter_start,
        "hourmeter_end": stats.hourmeter_end,
        "cylinders": [
            {"number": c.number, "max_cht": c.max_cht, "avg_cht_cruise": c.avg_cht_cruise,
             "max_egt": c.max_egt, "avg_egt_cruise": c.avg_egt_cruise}
            for c in stats.cylinders
        ],
        "alerts": stats.alerts,
    })


@app.route("/api/flight/<int:flight_id>/data")
def api_flight_data(flight_id):
    """Get full-resolution time-series data for a flight's panels.

    Returns every 1-second sample for the operation. Flight sizes (up to ~5hr
    = ~18k points) render fine in WebGL, so all zoom/pan is pure client-side
    with no downsampling or re-fetch.
    """
    conn = get_db_connection()
    try:
        # flight_id is the operation id
        flight = conn.execute(
            "SELECT id as operation_id FROM operations WHERE id = ?", (flight_id,)
        ).fetchone()
        if not flight:
            return jsonify({"error": "Operation not found"}), 404

        rows = conn.execute(
            """SELECT timestamp, indicated_airspeed, true_airspeed, ground_speed,
                      pressure_altitude, gps_altitude, density_altitude,
                      vertical_speed, oat, g_load,
                      roll, pitch, mag_heading, track,
                      rpm1, cht1, cht2, cht3, cht4,
                      egt1, egt2, egt3, egt4,
                      fuel_flow, oil_temp, oil_pressure, eis_volts,
                      internal_map,
                      aux1, aux2, aux3, aux4, aux5, aux6
               FROM fdl_data
               WHERE operation_id = ? ORDER BY timestamp""",
            (flight["operation_id"],)
        ).fetchall()

        # Resolve the aux channel -> parameter mapping. This is the ONE place
        # aux meaning is derived; only MAPPED channels are returned, keyed by
        # parameter_key, so unmapped channels never surface (Req 2.1, 3.2).
        resolved_aux = resolve_aux()

        # Percent_Power is computed on the fly per row from the EFIS Power_Map
        # (Req 6.1, 5.1, 5.2). Obtain the map once; when unavailable the whole
        # flight shows no Percent_Power — both the series and its descriptor are
        # omitted (Req 5.2, 6.3). Imported lazily to avoid an import cycle.
        from efis_data_manager import power
        power_map = power.load_power_map()
        if power_map is not None:
            map_col = power.map_column_for(resolved_aux)
            settings = power._read_settings()
            temp_units_celsius = settings[1] if settings else False
        else:
            map_col = None
            temp_units_celsius = False

        # Build response as column arrays. Fixed engine params first, then one
        # empty series per mapped aux parameter (keyed by parameter_key).
        engine = {
            "rpm": [],
            "cht1": [], "cht2": [], "cht3": [], "cht4": [],
            "egt1": [], "egt2": [], "egt3": [], "egt4": [],
            "fuel_flow": [], "oil_temp": [], "oil_pressure": [],
            "eis_volts": [],
        }
        for param_key in resolved_aux:
            engine[param_key] = []
        if power_map is not None:
            engine["percent_power"] = []

        data = {
            "timestamps": [],
            "point_count": len(rows),
            "engine": engine,
            "flight": {
                "ias": [], "tas": [], "ground_speed": [],
                "pressure_alt": [], "gps_alt": [], "density_alt": [],
                "vertical_speed": [], "oat": [], "g_load": [],
                "roll": [], "pitch": [], "mag_heading": [], "track": [],
            },
            # Mapped aux parameters for the client to merge into its PARAMS
            # table (label + precision). Only mapped channels appear here.
            "aux_params": [
                {
                    "key": param_key,
                    "label": info["label"],
                    "group": "engine",
                    "precision": info["precision"],
                }
                for param_key, info in resolved_aux.items()
            ],
        }

        # Register Percent_Power as a plottable parameter via the same
        # aux_params -> mergeAuxParams() path mapped aux parameters use, so it
        # appears in the picker with graceful omission when unavailable
        # (Req 6.1, 6.3). Only appended when the Power_Map is available.
        if power_map is not None:
            data["aux_params"].append({
                "key": "percent_power",
                "label": "% Power",
                "group": "engine",
                "precision": 0,
            })

        # (parameter_key, source column) pairs for the mapped aux series.
        aux_series = [
            (param_key, info["channel"])
            for param_key, info in resolved_aux.items()
        ]

        for row in rows:
            data["timestamps"].append(row["timestamp"])
            data["engine"]["rpm"].append(row["rpm1"])
            data["engine"]["cht1"].append(row["cht1"])
            data["engine"]["cht2"].append(row["cht2"])
            data["engine"]["cht3"].append(row["cht3"])
            data["engine"]["cht4"].append(row["cht4"])
            data["engine"]["egt1"].append(row["egt1"])
            data["engine"]["egt2"].append(row["egt2"])
            data["engine"]["egt3"].append(row["egt3"])
            data["engine"]["egt4"].append(row["egt4"])
            data["engine"]["fuel_flow"].append(row["fuel_flow"])
            data["engine"]["oil_temp"].append(row["oil_temp"])
            data["engine"]["oil_pressure"].append(row["oil_pressure"])
            data["engine"]["eis_volts"].append(row["eis_volts"])
            for param_key, col in aux_series:
                data["engine"][param_key].append(row[col])
            if power_map is not None:
                oat_raw = row["oat"]
                oat_f = (
                    power.oat_to_fahrenheit(oat_raw, temp_units_celsius)
                    if oat_raw is not None else None
                )
                data["engine"]["percent_power"].append(
                    power.percent_power(
                        row["rpm1"], row[map_col],
                        row["pressure_altitude"], oat_f, power_map,
                    )
                )
            data["flight"]["ias"].append(row["indicated_airspeed"])
            data["flight"]["tas"].append(row["true_airspeed"])
            data["flight"]["ground_speed"].append(row["ground_speed"])
            data["flight"]["pressure_alt"].append(row["pressure_altitude"])
            data["flight"]["gps_alt"].append(row["gps_altitude"])
            data["flight"]["density_alt"].append(row["density_altitude"])
            data["flight"]["vertical_speed"].append(row["vertical_speed"])
            data["flight"]["oat"].append(row["oat"])
            data["flight"]["g_load"].append(row["g_load"])
            data["flight"]["roll"].append(row["roll"])
            data["flight"]["pitch"].append(row["pitch"])
            data["flight"]["mag_heading"].append(row["mag_heading"])
            data["flight"]["track"].append(row["track"])

        return jsonify(data)

    finally:
        conn.close()


@app.route("/api/flight/<int:flight_id>/extremes")
def api_flight_extremes(flight_id):
    """Compute the standard 'jump to' extremes (value + timestamp) for a flight.

    Returns a list of {key, label, value, unit, timestamp, mode} in display order.
    mode is 'max' or 'min' (affects nothing on the client; informational).
    Aux-derived extremes (amps, MAP) are built only when those parameters are
    mapped, resolved through the single resolver (resolve_aux) so display,
    extremes, and episodes never disagree (Req 2.1, 2.2).
    """
    from efis_data_manager.database import get_db_connection
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """SELECT timestamp, rpm1, eis_volts,
                      fuel_flow, oil_temp, oil_pressure, indicated_airspeed,
                      aux1, aux2, aux3, aux4, aux5, aux6,
                      cht1, cht2, cht3, cht4, cht5, cht6,
                      egt1, egt2, egt3, egt4, egt5, egt6
               FROM fdl_data WHERE operation_id = ? ORDER BY timestamp""",
            (flight_id,),
        ).fetchall()
        if not rows:
            return jsonify([])

        # Resolve the aux mapping once; only mapped parameters get extremes.
        resolved_aux = resolve_aux()

        cht_cols = ["cht1", "cht2", "cht3", "cht4", "cht5", "cht6"]
        egt_cols = ["egt1", "egt2", "egt3", "egt4", "egt5", "egt6"]

        def extreme_single(col, mode, min_valid=None):
            """Return (value, timestamp) of the max/min of a single column."""
            best_v, best_t = None, None
            for r in rows:
                v = r[col]
                if v is None:
                    continue
                if min_valid is not None and v < min_valid:
                    continue
                if best_v is None or (mode == "max" and v > best_v) or (mode == "min" and v < best_v):
                    best_v, best_t = v, r["timestamp"]
            return best_v, best_t

        def extreme_cyl(cols, min_valid):
            """Max across a set of per-cylinder columns (hottest instant)."""
            best_v, best_t = None, None
            for r in rows:
                for c in cols:
                    v = r[c]
                    if v is None or v < min_valid:
                        continue
                    if best_v is None or v > best_v:
                        best_v, best_t = v, r["timestamp"]
            return best_v, best_t

        def extreme_spread(cols, min_valid):
            """Max cylinder spread and the instant it occurred."""
            best_v, best_t = None, None
            for r in rows:
                vals = [r[c] for c in cols if r[c] is not None and r[c] >= min_valid]
                if len(vals) >= 2:
                    s = max(vals) - min(vals)
                    if best_v is None or s > best_v:
                        best_v, best_t = s, r["timestamp"]
            return best_v, best_t

        # Fixed (non-aux) extremes, in display order. MAP and amps are inserted
        # only when mapped (below), preserving today's display order.
        specs = [
            ("max_cht",   "Max CHT",        "°F",  lambda: extreme_cyl(cht_cols, 100)),
            ("max_egt",   "Max EGT",        "°F",  lambda: extreme_cyl(egt_cols, 100)),
            ("max_cht_spread", "Max CHT Spread", "°F", lambda: extreme_spread(cht_cols, 200)),
            ("max_oil_temp", "Max Oil Temp", "°F", lambda: extreme_single("oil_temp", "max")),
            ("min_oil_press", "Min Oil Press", "psi", lambda: extreme_single("oil_pressure", "min", min_valid=1)),
            ("max_oil_press", "Max Oil Press", "psi", lambda: extreme_single("oil_pressure", "max")),
            ("max_rpm",   "Max RPM",        "",    lambda: extreme_single("rpm1", "max")),
        ]

        # MAP extreme: only when manifold_pressure is mapped. Read the resolved
        # channel column, not a hardcoded aux column (Req 2.2, 2.3).
        if "manifold_pressure" in resolved_aux:
            map_info = resolved_aux["manifold_pressure"]
            map_col = map_info["channel"]
            specs.append(
                ("max_map", "Max MAP", map_info["unit"] or '"',
                 lambda c=map_col: extreme_single(c, "max"))
            )

        specs += [
            ("max_ff",    "Max Fuel Flow",  "gph", lambda: extreme_single("fuel_flow", "max")),
            ("max_ias",   "Max IAS",        "kt",  lambda: extreme_single("indicated_airspeed", "max")),
            ("min_volts", "Min Volts",      "V",   lambda: extreme_single("eis_volts", "min", min_valid=1)),
            ("max_volts", "Max Volts",      "V",   lambda: extreme_single("eis_volts", "max")),
        ]

        # Amps extremes (min + max): only when amps is mapped.
        if "amps" in resolved_aux:
            amps_info = resolved_aux["amps"]
            amps_col = amps_info["channel"]
            amps_unit = amps_info["unit"] or "A"
            specs += [
                ("min_amps", "Min Amps", amps_unit,
                 lambda c=amps_col: extreme_single(c, "min")),
                ("max_amps", "Max Amps", amps_unit,
                 lambda c=amps_col: extreme_single(c, "max")),
            ]

        out = []
        for key, label, unit, fn in specs:
            v, t = fn()
            if v is None or t is None:
                continue  # skip extremes with no usable data
            out.append({"key": key, "label": label, "value": v,
                        "unit": unit, "timestamp": t})
        return jsonify(out)
    finally:
        conn.close()


@app.route("/api/flight/<int:flight_id>/episodes")
def api_flight_episodes(flight_id):
    """Per-episode exceedances for one flight (hysteresis, on the fly)."""
    from efis_data_manager.analysis import detect_episodes
    episodes = detect_episodes(flight_id)
    return jsonify([
        {
            "parameter": e.parameter,
            "direction": e.direction,
            "severity": e.severity,
            "start_timestamp": e.start_timestamp,
            "peak_timestamp": e.peak_timestamp,
            "peak_value": e.peak_value,
            "threshold": e.threshold,
            "duration_s": e.duration_s,
            "message": e.message,
        }
        for e in episodes
    ])


@app.route("/api/trends")
def api_trends():
    """Get trend data for all tracked parameters."""
    config = load_config()
    window = config.get("trend_window_hours", 25)
    trends = compute_trends(window_hours=window)

    result = {}
    for key, series in trends.items():
        result[key] = {
            "name": series.name,
            "unit": series.unit,
            "points": [
                {"date": p.date, "hourmeter": p.hourmeter, "value": p.value}
                for p in series.points
            ],
            "rolling_avg": series.rolling_avg,
        }
    return jsonify(result)


@app.route("/api/alerts")
def api_alerts():
    """Get all detected anomalies."""
    anomalies = detect_anomalies()
    return jsonify([
        {"flight_id": a.flight_id, "date": a.date, "parameter": a.parameter,
         "severity": a.severity, "message": a.message,
         "value": a.value, "threshold": a.threshold,
         "timestamp": a.timestamp}
        for a in anomalies
    ])


@app.route("/api/oil")
def api_oil():
    """Get oil consumption data + change markers."""
    config = load_config()
    window = config.get("trend_window_hours", 25)
    from efis_data_manager.analysis import get_oil_changes, get_oil_age_status
    from efis_data_manager.database import get_oil_events
    cutoff = config.get("oil_cutoff_date", "")
    return jsonify({
        "consumption": compute_oil_consumption_rolling(window),
        "changes": get_oil_changes(),
        "events": get_oil_events(cutoff_date=cutoff),
        "cutoff_date": cutoff,
        "oil_age": get_oil_age_status(),
    })


@app.route("/api/oil/event", methods=["POST"])
def api_add_oil_event():
    """Add an oil change or addition event."""
    from efis_data_manager.database import add_oil_event
    data = request.get_json()
    try:
        event_id = add_oil_event(
            date=data["date"],
            hourmeter=float(data["hourmeter"]),
            event_type=data["event_type"],
            quarts_added=float(data.get("quarts_added", 0) or 0),
            quarts_low=float(data.get("quarts_low", 0) or 0),
            note=data.get("note", ""),
        )
        return jsonify({"status": "ok", "id": event_id})
    except (KeyError, ValueError) as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/api/oil/event/<int:event_id>", methods=["DELETE"])
def api_delete_oil_event(event_id):
    """Delete an oil event."""
    from efis_data_manager.database import delete_oil_event
    delete_oil_event(event_id)
    return jsonify({"status": "ok"})


@app.route("/api/config", methods=["GET"])
def api_get_config():
    """Get current configuration."""
    config = load_config()
    return jsonify(config)


@app.route("/api/config", methods=["POST"])
def api_save_config():
    """Save configuration changes."""
    updates = request.get_json()
    config = load_config()
    config.update(updates)
    save_config(config)
    return jsonify({"status": "ok"})


@app.route("/api/thresholds")
def api_thresholds():
    """Get current thresholds (defaults + overrides)."""
    return jsonify(get_thresholds())


# ---------------------------------------------------------------------------
# EFIS settings-import endpoints (thin wrappers over Import_Workflow)
# ---------------------------------------------------------------------------

def _settings_archive_root():
    """Path(load_config()["archive_path"]) — the settings-import archive root."""
    from pathlib import Path
    return Path(load_config()["archive_path"])


def _import_info_to_dict(info):
    """Serialize a NewBackupInfo for the status route (omit the parsed object)."""
    return {
        "available": info.available,
        "source_key": info.source_key,
        "is_primary": info.is_primary,
        "bound_import_source": info.bound_import_source,
        "is_first_for_source": info.is_first_for_source,
        "is_different_source": info.is_different_source,
        "is_bound_source_mismatch": info.is_bound_source_mismatch,
        "candidate_update_value": info.candidate_update_value,
        "source_last_imported_update": info.source_last_imported_update,
        "reason": info.reason,
    }


def _preview_to_dict(preview):
    """Serialize an ImportPreview for the preview/apply routes (omit parsed)."""
    return {
        "changes": [
            {
                "key": c.key,
                "label": c.label,
                "current": c.current,
                "proposed": c.proposed,
                "source_sid": c.source_sid,
                "tier": c.tier,
                "selected": c.selected,
                "selection_key": c.selection_key,
            }
            for c in preview.changes
        ],
        "tier_prompts": preview.tier_prompts,
        "warnings": preview.warnings,
        "backup_update_value": preview.backup_update_value,
        "source_key": preview.source_key,
        "is_different_source": preview.is_different_source,
        "is_bound_source_mismatch": preview.is_bound_source_mismatch,
        "source_note": preview.source_note,
        "bound_import_source": preview.bound_import_source,
        "can_apply": preview.can_apply,
        "num_cylinders_proposed": preview.num_cylinders_proposed,
    }


def _tier_selections_from(source):
    """Build the tier_selections dict from a query/JSON source (only valid tiers)."""
    tiers = {}
    for concept, param in (("cht", "tier_cht"), ("egt", "tier_egt"), ("oil_temp", "tier_oil")):
        val = source.get(param)
        if val is not None and str(val).lower() in ("caution", "redline"):
            tiers[concept] = str(val).lower()
    return tiers


def _selected_keys_from_query(args):
    """Parse selected_keys from GET query args into a set, or None if absent.

    Accepts a comma-separated ``selected_keys`` value and/or repeated
    ``selected_keys`` params. Absent entirely => None => all rows selected
    (fresh all-checked). Present (even empty) => a concrete set the workflow
    honors as the selected subset (Req 16.1/16.7).
    """
    if "selected_keys" not in args:
        return None
    keys: set = set()
    for raw in args.getlist("selected_keys"):
        for part in str(raw).split(","):
            part = part.strip()
            if part:
                keys.add(part)
    return keys


def _selected_keys_from_body(body):
    """Parse selected_keys from a JSON apply body into a set, or None if absent.

    Absent => None => all changes written (backward compat). A present list
    (including an empty list) => a set the workflow honors (empty => deselect-all
    no-op, Req 16.7).
    """
    if "selected_keys" not in body:
        return None
    raw = body.get("selected_keys")
    if raw is None:
        return None
    return {str(k).strip() for k in raw if str(k).strip()}


@app.route("/api/settings-import/status")
def api_settings_import_status():
    """Detect whether a newer EFIS settings backup should be offered (Req 9.1, 14, 15).

    Thin wrapper over ``Import_Workflow.detect_new_backup``; the ``parsed``
    backup is never serialized. Returns ``{"available": false, ...}`` for the
    no-backup and every suppressed case (stale n-1, non-primary, bound-source
    mismatch), each with a human-readable ``reason``.
    """
    from efis_data_manager.efis_settings import Import_Workflow
    try:
        info = Import_Workflow.detect_new_backup(load_config(), _settings_archive_root())
    except Exception as exc:  # noqa: BLE001 — soft error, keep the badge hidden
        logger.warning("settings-import status failed: %s", exc)
        return jsonify({"available": False, "reason": f"error: {exc}", "error": str(exc)})
    if info is None:
        return jsonify({"available": False, "reason": "no settings backup found"})
    return jsonify(_import_info_to_dict(info))


@app.route("/api/settings-import/preview")
def api_settings_import_preview():
    """Build the import preview diff table for the currently detected candidate.

    Query params ``tier_cht`` / ``tier_egt`` / ``tier_oil`` (each
    ``caution``|``redline``) drive the CHT/EGT/Oil tier choice; changing them
    re-runs the caution<redline guard (Req 8, 9.2, 9.3). Returns a preview with
    ``can_apply=false`` and a reason when no eligible candidate exists.
    """
    from efis_data_manager.efis_settings import Import_Workflow
    config = load_config()
    tier_selections = _tier_selections_from(request.args)
    selected_keys = _selected_keys_from_query(request.args)
    try:
        info = Import_Workflow.detect_new_backup(config, _settings_archive_root())
    except Exception as exc:  # noqa: BLE001
        logger.warning("settings-import preview detect failed: %s", exc)
        return jsonify({"can_apply": False, "reason": f"error: {exc}", "error": str(exc)})
    if info is None or info.parsed is None:
        return jsonify({"can_apply": False, "reason": "no settings backup found"})
    if not info.available:
        return jsonify({"can_apply": False, "reason": info.reason})
    preview = Import_Workflow.build_preview(
        info.parsed, config, tier_selections, selected_keys=selected_keys)
    return jsonify(_preview_to_dict(preview))


@app.route("/api/settings-import/apply", methods=["POST"])
def api_settings_import_apply():
    """Apply the previewed import behind a double-confirm (Req 9.4-9.6, 10.3).

    Body ``{confirm1, confirm2, tier_cht, tier_egt, tier_oil}``. Rebuilds the
    preview from the current detected candidate + tier selections, then calls
    ``Import_Workflow.apply`` (persisting via the default ``save_config``).
    On success returns the updated thresholds; otherwise a status/reason.
    """
    from efis_data_manager.efis_settings import Import_Workflow
    body = request.get_json(silent=True) or {}
    confirm1 = bool(body.get("confirm1"))
    confirm2 = bool(body.get("confirm2"))
    tier_selections = _tier_selections_from(body)
    selected_keys = _selected_keys_from_body(body)
    config = load_config()
    try:
        info = Import_Workflow.detect_new_backup(config, _settings_archive_root())
    except Exception as exc:  # noqa: BLE001
        logger.warning("settings-import apply detect failed: %s", exc)
        return jsonify({"status": "error", "reason": f"error: {exc}", "error": str(exc)})
    if info is None or info.parsed is None:
        return jsonify({"status": "error", "reason": "no settings backup found"})
    if not info.available:
        return jsonify({"status": "declined", "reason": info.reason})

    preview = Import_Workflow.build_preview(
        info.parsed, config, tier_selections, selected_keys=selected_keys)
    result = Import_Workflow.apply(
        preview, confirm1, confirm2, config, selected_keys=selected_keys)
    if not result.applied:
        return jsonify({"status": "declined", "reason": result.reason})
    return jsonify({
        "status": "ok",
        "reason": result.reason,
        "updated_thresholds": result.updated_thresholds,
        "thresholds": get_thresholds(),
    })


# ---------------------------------------------------------------------------
# Settings-management explorer endpoints (read-only: history / view)
# ---------------------------------------------------------------------------
#
# These serve the dashboard settings explorer for the settings-management spec.
# All three are READ-ONLY: they list configured devices, scan a device's
# per-family archive history, and parse one archived snapshot into labeled
# SID/value rows. None of them ever writes, moves, or deletes an archived file
# or a drive file (Req 2.4, 3.4, 9.1). A parse failure is reported as an error
# and the snapshot file is left unchanged (Req 3.5, 9.3).

# Settings families the explorer can read a history for. Settings is the only
# family the restore/populate write path touches, but the history/view are
# read-only and therefore available for all archived families.
_ARCHIVED_FAMILIES = ("Settings", "WP", "Plan")


def _configured_devices(config):
    """Return [{link_id, label}] for the managed aircraft's configured devices.

    Reuses ``config.managed_aircraft`` / ``config.device_label`` (R10.4). An
    unlearned aircraft (no devices map) yields an empty list. Ordered by
    Link_ID so the explorer lists devices deterministically (R2.2).
    """
    from efis_data_manager.config import managed_aircraft, device_label
    aircraft = managed_aircraft(config)
    if not isinstance(aircraft, dict):
        return []
    devices = aircraft.get("devices")
    if not isinstance(devices, dict):
        return []
    out = []
    for link_id in sorted(devices.keys()):
        device = devices[link_id]
        if not isinstance(device, dict):
            continue
        out.append({"link_id": link_id, "label": device_label(device)})
    return out


@app.route("/api/settings/devices")
def api_settings_devices():
    """List the managed aircraft's configured devices (label + Link_ID) (R2.2).

    Read-only. Drives the explorer's per-device picker. An unlearned aircraft
    returns an empty list without error.
    """
    return jsonify({"devices": _configured_devices(load_config())})


@app.route("/api/settings/history")
def api_settings_history():
    """Return a device-and-family's archived history, newest to oldest (R2.1).

    Query params ``link_id`` (device Link_ID; blank/omitted => the unresolved
    location) and ``family`` (default ``Settings``). Each item carries its
    Archive_Date, sequence suffix, family, and filename (R2.2). A device/family
    with no snapshots returns an empty list without error (R2.3). Read-only:
    the archive directory is scanned, never modified (R2.4).
    """
    from efis_data_manager import settings_archive
    link_id = request.args.get("link_id") or None
    family = request.args.get("family", "Settings")
    if family not in _ARCHIVED_FAMILIES:
        return jsonify({"error": f"unknown family {family!r}"}), 400
    try:
        history = settings_archive.family_history(
            _settings_archive_root(), link_id, family)
    except Exception as exc:  # noqa: BLE001 — report, leave files unchanged
        logger.warning("settings history scan failed: %s", exc)
        return jsonify({"error": str(exc)}), 500
    return jsonify({
        "link_id": link_id,
        "family": family,
        "snapshots": [
            {
                "archive_date": snap.archive_date.isoformat(),
                "sequence": snap.sequence,
                "family": snap.family,
                "filename": snap.path.name,
            }
            for snap in history
        ],
    })


@app.route("/api/settings/view")
def api_settings_view():
    """Parse one archived snapshot into labeled SID/value rows (R3).

    Query params ``link_id`` (device Link_ID), ``family`` (default
    ``Settings``), and ``name`` (the archived snapshot filename within that
    device/family directory). Returns ``[{sid, label, value, unlabeled}]`` via
    the shared ``efis_settings`` SID vocabulary; unrecognized SIDs are returned
    with their raw value and ``unlabeled=true`` (R3.2, R3.3). Read-only; a
    parse failure is reported and the file is left unchanged (R3.4, R3.5).

    ``name`` is validated to be a bare filename inside the resolved device
    directory so the view cannot be pointed outside the archive.
    """
    from efis_data_manager import settings_archive
    from efis_data_manager.efis_settings import snapshot_view, SettingsParseError
    link_id = request.args.get("link_id") or None
    family = request.args.get("family", "Settings")
    name = request.args.get("name", "")
    if family not in _ARCHIVED_FAMILIES:
        return jsonify({"error": f"unknown family {family!r}"}), 400
    if not name or os.path.basename(name) != name:
        return jsonify({"error": "a bare snapshot filename is required"}), 400

    device_dir = settings_archive.device_settings_dir(
        _settings_archive_root(), link_id)
    snapshot_path = device_dir / name
    if not snapshot_path.is_file():
        return jsonify({"error": f"snapshot {name!r} not found"}), 404

    try:
        rows = snapshot_view(str(snapshot_path))
    except SettingsParseError as exc:
        # Reported failure; the snapshot file is left unchanged (R3.5, R9.3).
        logger.warning("settings snapshot parse failed: %s", exc)
        return jsonify({"error": str(exc)}), 422

    return jsonify({
        "link_id": link_id,
        "family": family,
        "name": name,
        "sids": [
            {
                "sid": row.sid,
                "label": row.label,
                "value": row.value,
                "unlabeled": row.unlabeled,
                "grt_name": row.grt_name,
                "value_meaning": row.value_meaning,
            }
            for row in rows
        ],
    })


@app.route("/api/settings/compare")
def api_settings_compare():
    """Compare two settings versions into a labeled per-SID diff (R4).

    Query params ``link_id`` (device Link_ID), ``family`` (default
    ``Settings``), ``a`` (the first archived snapshot filename), and ``b``.
    ``b`` is either a second archived snapshot filename for the same device and
    family, or the literal ``"drive"`` to diff ``a`` against the current slot of
    the returning drive bound to this device (R4.5).

    The comparison is on parsed SID/value content with the volatile ``UPDATE=``
    / ``CHECKSIZE=`` / ``CHECKSUM=`` keys excluded (R4.2). For each differing
    SID the response carries the human label, the value in version ``a``, and
    the value in version ``b``; a SID present in only one version is reported as
    added/removed via a ``null`` value on the absent side (R4.1, R4.3). When the
    two versions share a Content_Hash the response reports them equivalent
    (R4.4). Read-only: both sources are parsed, never modified (R9.1).

    ``a`` (and ``b`` when not ``"drive"``) must be a bare filename inside the
    resolved per-device directory so the compare cannot be pointed outside the
    archive.
    """
    from efis_data_manager import grt_sids, settings_archive
    from efis_data_manager.efis_settings import (
        Settings_Parser,
        SettingsParseError,
        content_hash,
        recognized_sid_labels,
        _HASH_EXCLUDED_KEYS,
    )

    link_id = request.args.get("link_id") or None
    family = request.args.get("family", "Settings")
    a_name = request.args.get("a", "")
    b_name = request.args.get("b", "")

    if family not in _ARCHIVED_FAMILIES:
        return jsonify({"error": f"unknown family {family!r}"}), 400
    if not a_name or os.path.basename(a_name) != a_name:
        return jsonify({"error": "a bare snapshot filename is required for 'a'"}), 400
    if not b_name:
        return jsonify({"error": "'b' must be a snapshot filename or 'drive'"}), 400

    device_dir = settings_archive.device_settings_dir(
        _settings_archive_root(), link_id)

    a_path = device_dir / a_name
    if not a_path.is_file():
        return jsonify({"error": f"snapshot {a_name!r} not found"}), 404

    # Resolve side B: either a second archived snapshot or the current slot of
    # the returning drive bound to this device (R4.5).
    compare_drive = b_name == "drive"
    if not compare_drive and os.path.basename(b_name) != b_name:
        return jsonify({"error": "a bare snapshot filename is required for 'b'"}), 400

    try:
        a_parsed = Settings_Parser.parse(str(a_path))
    except SettingsParseError as exc:
        logger.warning("settings compare parse failed for a=%s: %s", a_name, exc)
        return jsonify({"error": str(exc)}), 422

    if compare_drive:
        mount_point, reason = _bound_drive_for_link_id(load_config(), link_id)
        if mount_point is None:
            return jsonify({"status": "no-drive", "error": reason}), 409
        from efis_data_manager.archiver import _current_settings_slot
        b_parsed = _current_settings_slot(mount_point, family)
        if b_parsed is None:
            return jsonify({
                "error": "the bound drive carries no parseable "
                         f"{family} slot to compare against",
            }), 422
    else:
        b_path = device_dir / b_name
        if not b_path.is_file():
            return jsonify({"error": f"snapshot {b_name!r} not found"}), 404
        try:
            b_parsed = Settings_Parser.parse(str(b_path))
        except SettingsParseError as exc:
            logger.warning("settings compare parse failed for b=%s: %s", b_name, exc)
            return jsonify({"error": str(exc)}), 422

    # R4.4: equal Content_Hash => the two versions are equivalent in content.
    equivalent = content_hash(a_parsed) == content_hash(b_parsed)

    # R4.2: compare parsed content, excluding the volatile keys.
    a_sids = {s: v for s, v in a_parsed.sids.items() if s not in _HASH_EXCLUDED_KEYS}
    b_sids = {s: v for s, v in b_parsed.sids.items() if s not in _HASH_EXCLUDED_KEYS}
    labels = recognized_sid_labels()

    differences = []
    def _sid_order(s):
        # Numeric SIDs first in numeric order; non-numeric last, by string.
        return (0, int(s), s) if s.isdigit() else (1, 0, s)

    for sid in sorted(set(a_sids) | set(b_sids), key=_sid_order):
        a_val = a_sids.get(sid)
        b_val = b_sids.get(sid)
        if a_val == b_val:
            continue  # R4.1: report only differing SIDs
        # R4.3: present in one, absent in the other => added/removed.
        if a_val is None:
            change = "added"
        elif b_val is None:
            change = "removed"
        else:
            change = "changed"
        label = labels.get(sid)
        info = grt_sids.sid_info(sid)
        differences.append({
            "sid": sid,
            "label": label,
            "unlabeled": label is None,
            "grt_name": info.grt_name if info else None,
            "a": a_val,
            "b": b_val,
            "a_meaning": grt_sids.value_meaning(sid, a_val),
            "b_meaning": grt_sids.value_meaning(sid, b_val),
            "change": change,
        })

    return jsonify({
        "link_id": link_id,
        "family": family,
        "a": a_name,
        "b": b_name,
        "equivalent": equivalent,
        "differences": differences,
    })


def _parse_drive_settings(mount_point):
    """Parse the drive's current Settings slot for identity resolution, or None.

    Mirrors the DM's ``_find_drive_settings`` / the archiver slot selection: a
    GRT settings backup at the mount root is ``Settings.bak`` / ``Settings.dat``;
    either slot carries the same Link_ID / Mode_S identity, so the first file
    that parses is enough to resolve which device the drive is bound to. A
    parse failure is treated as absent (read-only, never mutates the drive).
    """
    from efis_data_manager.efis_settings import Settings_Parser, SettingsParseError
    for ext in (".bak", ".dat"):
        path = os.path.join(mount_point, f"Settings{ext}")
        if not os.path.isfile(path):
            continue
        try:
            return Settings_Parser.parse(path)
        except SettingsParseError as exc:
            logger.debug("Settings%s on %s did not parse, ignoring: %s",
                         ext, mount_point, exc)
    return None


def _bound_drive_for_link_id(config, link_id):
    """Resolve the mount point of the currently inserted drive bound to a device.

    Restoration writes only to a drive bound to the target device (R5.2),
    resolving the binding via the existing drive-to-device resolution. This
    scans ``/Volumes`` for a mounted managed drive, resolves it through
    ``device_resolution.resolve_drive`` (the authoritative Link_ID path shared by
    the DM), and returns its mount point only when the resolved Link_ID matches
    the requested ``link_id``.

    Returns ``(mount_point, None)`` on a confident match, or ``(None, reason)``
    describing why no bound drive is available (no managed drive mounted, the
    mounted drive does not resolve, or it resolves to a different device).
    """
    from efis_data_manager import device_resolution
    from efis_data_manager.device_resolution import ResolutionStatus
    from efis_data_manager.drive_updater import read_identity
    from efis_data_manager.usb_monitor import find_mounted_managed_drive

    mount_point = find_mounted_managed_drive()
    if mount_point is None:
        return None, "no managed EFIS drive is currently mounted"

    try:
        parsed = _parse_drive_settings(mount_point)
        recorded_identity = read_identity(mount_point)
        outcome = device_resolution.resolve_drive(parsed, recorded_identity, config)
    except Exception as exc:  # noqa: BLE001 — report, write nothing
        logger.warning("restore drive resolution failed: %s", exc)
        return None, f"could not resolve the mounted drive: {exc}"

    if outcome.status != ResolutionStatus.RESOLVED or outcome.link_id is None:
        return None, (
            "the mounted drive does not resolve to a configured device "
            f"({outcome.message or outcome.status.name})"
        )
    if str(outcome.link_id) != str(link_id):
        return None, (
            f"the mounted drive is bound to device Link_ID {outcome.link_id}, "
            f"not the requested device {link_id}"
        )
    return mount_point, None


def _write_result_to_dict(result):
    """Serialize a settings_writer.WriteResult for the restore route."""
    return {
        "status": result.status,
        "written_path": result.written_path,
        "removed_slots": result.removed_slots,
        "error": result.error,
    }


@app.route("/api/settings/restore", methods=["POST"])
def api_settings_restore():
    """Restore a chosen archived Settings version onto the bound drive (R5).

    Body ``{link_id, family:"Settings", name}``. This is the dashboard-SELECT /
    DM-WRITE handoff: the dashboard resolves the archived file and the drive
    bound to the target device, then the DM write path
    (``settings_writer.restore_version``) performs the byte-verbatim sole-slot
    write against that drive. The archived file's own bytes are written
    unchanged (checksum wall, R8).

    Restoration is Settings only (R5.9): any other family is rejected with 400.
    The write happens only on this explicit POST, never automatically on drive
    insert (R5.7), and is a WRITE-NOW one-shot against the currently bound drive
    (R5.8). ``name`` must be a bare filename inside the resolved per-device
    directory; a missing snapshot is a 404. When no drive bound to the device is
    mounted, the request is rejected and nothing is written.

    Returns the ``WriteResult`` (status, written_path, removed_slots, error).
    """
    from efis_data_manager import settings_archive, settings_writer

    body = request.get_json(silent=True) or {}
    link_id = body.get("link_id") or None
    family = body.get("family", "Settings")
    name = body.get("name", "")

    # R5.9: only the Settings family is ever restored; WP/Plan are rejected.
    if family != "Settings":
        return jsonify({
            "status": "rejected",
            "error": f"restoration is Settings-only; refusing family {family!r}",
        }), 400

    if not name or os.path.basename(name) != name:
        return jsonify({
            "status": "rejected",
            "error": "a bare snapshot filename is required",
        }), 400

    config = load_config()
    device_dir = settings_archive.device_settings_dir(
        _settings_archive_root(), link_id)
    archived_path = device_dir / name
    if not archived_path.is_file():
        return jsonify({
            "status": "rejected",
            "error": f"snapshot {name!r} not found",
        }), 404

    # R5.2: write only to a drive bound to the target device, resolving the
    # binding via the existing drive-to-device resolution.
    mount_point, reason = _bound_drive_for_link_id(config, link_id)
    if mount_point is None:
        return jsonify({"status": "no-drive", "error": reason}), 409

    # Hand off to the DM writer: byte-verbatim, verify-then-sole-slot (R5.3-R5.6).
    result = settings_writer.restore_version(str(archived_path), mount_point)
    status_code = 200 if result.status == "written" else 500
    return jsonify(_write_result_to_dict(result)), status_code


@app.route("/api/param-catalog")
def api_param_catalog():
    """Expose the fixed aux parameter catalog for the Settings mapping UI.

    Returns an ordered list of {key, label, unit, precision}. "none" and
    "custom" are included so the client can build the per-channel dropdown.
    The default_label for "custom" is empty (label/unit come from the user).
    """
    catalog = [{"key": "none", "label": "None", "unit": "", "precision": 0}]
    for key, (label, unit, precision) in PARAM_CATALOG.items():
        catalog.append({
            "key": key,
            "label": label if label is not None else "Custom",
            "unit": unit if unit is not None else "",
            "precision": precision,
        })
    return jsonify({"catalog": catalog, "channels": list(AUX_CHANNELS)})


@app.route("/api/export/flights")
def api_export_flights():
    """Export flight summaries CSV."""
    path = export_flight_summaries()
    return send_file(path, as_attachment=True)


@app.route("/api/export/raw/<int:operation_id>")
def api_export_raw(operation_id):
    """Export raw FDL data for a flight."""
    try:
        path = export_flight_raw(operation_id)
        return send_file(path, as_attachment=True)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


@app.route("/api/export/oil")
def api_export_oil():
    """Export oil consumption CSV."""
    path = export_oil_consumption()
    return send_file(path, as_attachment=True)


def create_app():
    """Create and configure the Flask app."""
    return app


def run_dashboard():
    """Run the dashboard web server.

    Set FLASK_DEBUG=1 in the environment to enable auto-reload on source/
    template changes (for development). Defaults to production mode.
    """
    config = load_config()
    port = config.get("dashboard_port", 5050)
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")

    # Auto-refresh Jinja templates in debug so template edits show on refresh
    app.jinja_env.auto_reload = debug
    app.config["TEMPLATES_AUTO_RELOAD"] = debug

    logger.info(f"Starting dashboard on http://localhost:{port} (debug={debug})")
    app.run(host="127.0.0.1", port=port, debug=debug, use_reloader=debug)
