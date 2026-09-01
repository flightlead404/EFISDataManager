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
                      aux1, aux2, aux3
               FROM fdl_data
               WHERE operation_id = ? ORDER BY timestamp""",
            (flight["operation_id"],)
        ).fetchall()

        # Build response as column arrays
        data = {
            "timestamps": [],
            "point_count": len(rows),
            "engine": {
                "rpm": [], "map": [],
                "cht1": [], "cht2": [], "cht3": [], "cht4": [],
                "egt1": [], "egt2": [], "egt3": [], "egt4": [],
                "fuel_flow": [], "oil_temp": [], "oil_pressure": [],
                "eis_volts": [], "amps": [], "fuel_pressure": [],
            },
            "flight": {
                "ias": [], "tas": [], "ground_speed": [],
                "pressure_alt": [], "gps_alt": [], "density_alt": [],
                "vertical_speed": [], "oat": [], "g_load": [],
                "roll": [], "pitch": [], "mag_heading": [], "track": [],
            },
        }

        for row in rows:
            data["timestamps"].append(row["timestamp"])
            data["engine"]["rpm"].append(row["rpm1"])
            # MAP is on aux2 for this install (the Internal MAP column is empty)
            data["engine"]["map"].append(row["aux2"])
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
            data["engine"]["amps"].append(row["aux1"])            # aux1 = amps
            data["engine"]["fuel_pressure"].append(row["aux3"])   # aux3 = fuel pressure
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
    Field resolution matches /api/flight/<id>/data (aux1=amps, aux2=MAP, aux3=FP).
    """
    from efis_data_manager.database import get_db_connection
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """SELECT timestamp, rpm1, aux2 AS map, aux1 AS amps, eis_volts,
                      fuel_flow, oil_temp, oil_pressure, indicated_airspeed,
                      cht1, cht2, cht3, cht4, cht5, cht6,
                      egt1, egt2, egt3, egt4, egt5, egt6
               FROM fdl_data WHERE operation_id = ? ORDER BY timestamp""",
            (flight_id,),
        ).fetchall()
        if not rows:
            return jsonify([])

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

        # (key, label, unit, computation)
        specs = [
            ("max_cht",   "Max CHT",        "°F",  lambda: extreme_cyl(cht_cols, 100)),
            ("max_egt",   "Max EGT",        "°F",  lambda: extreme_cyl(egt_cols, 100)),
            ("max_cht_spread", "Max CHT Spread", "°F", lambda: extreme_spread(cht_cols, 200)),
            ("max_oil_temp", "Max Oil Temp", "°F", lambda: extreme_single("oil_temp", "max")),
            ("min_oil_press", "Min Oil Press", "psi", lambda: extreme_single("oil_pressure", "min", min_valid=1)),
            ("max_oil_press", "Max Oil Press", "psi", lambda: extreme_single("oil_pressure", "max")),
            ("max_rpm",   "Max RPM",        "",    lambda: extreme_single("rpm1", "max")),
            ("max_map",   "Max MAP",        '"',   lambda: extreme_single("map", "max")),
            ("max_ff",    "Max Fuel Flow",  "gph", lambda: extreme_single("fuel_flow", "max")),
            ("max_ias",   "Max IAS",        "kt",  lambda: extreme_single("indicated_airspeed", "max")),
            ("min_volts", "Min Volts",      "V",   lambda: extreme_single("eis_volts", "min", min_valid=1)),
            ("max_volts", "Max Volts",      "V",   lambda: extreme_single("eis_volts", "max")),
            ("min_amps",  "Min Amps",       "A",   lambda: extreme_single("amps", "min")),
            ("max_amps",  "Max Amps",       "A",   lambda: extreme_single("amps", "max")),
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
    from efis_data_manager.analysis import get_oil_changes
    from efis_data_manager.database import get_oil_events
    cutoff = config.get("oil_cutoff_date", "")
    return jsonify({
        "consumption": compute_oil_consumption_rolling(window),
        "changes": get_oil_changes(),
        "events": get_oil_events(cutoff_date=cutoff),
        "cutoff_date": cutoff,
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
