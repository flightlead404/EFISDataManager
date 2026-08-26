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

logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder="templates")


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

@app.route("/api/flights")
def api_flights():
    """Get all flights with summary data."""
    conn = get_db_connection()
    try:
        flights = conn.execute(
            """SELECT f.*, o.source_filename
               FROM flights f
               JOIN operations o ON f.operation_id = o.id
               ORDER BY f.date DESC, f.start_time DESC"""
        ).fetchall()
        return jsonify([dict(f) for f in flights])
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
    """Get time-series data for a flight's dual charts.

    Downsamples to ~1500 points max for responsive chart rendering.
    """
    conn = get_db_connection()
    try:
        # Get operation_id for this flight
        flight = conn.execute(
            "SELECT operation_id FROM flights WHERE id = ?", (flight_id,)
        ).fetchone()
        if not flight:
            return jsonify({"error": "Flight not found"}), 404

        rows = conn.execute(
            """SELECT timestamp, indicated_airspeed, true_airspeed, ground_speed,
                      pressure_altitude, gps_altitude, density_altitude,
                      vertical_speed, oat, g_load,
                      rpm1, cht1, cht2, cht3, cht4,
                      egt1, egt2, egt3, egt4,
                      fuel_flow, oil_temp, oil_pressure, eis_volts,
                      internal_map
               FROM fdl_data
               WHERE operation_id = ?
               ORDER BY timestamp""",
            (flight["operation_id"],)
        ).fetchall()

        # Downsample if needed (target ~1500 points for smooth charts)
        max_points = 1500
        if len(rows) > max_points:
            step = len(rows) / max_points
            indices = [int(i * step) for i in range(max_points)]
            # Always include first and last
            if indices[-1] != len(rows) - 1:
                indices.append(len(rows) - 1)
            rows = [rows[i] for i in indices]

        # Build response as column arrays
        data = {
            "timestamps": [],
            "engine": {
                "rpm": [], "map": [],
                "cht1": [], "cht2": [], "cht3": [], "cht4": [],
                "egt1": [], "egt2": [], "egt3": [], "egt4": [],
                "fuel_flow": [], "oil_temp": [], "oil_pressure": [],
                "eis_volts": [],
            },
            "flight": {
                "ias": [], "tas": [], "ground_speed": [],
                "pressure_alt": [], "gps_alt": [], "density_alt": [],
                "vertical_speed": [], "oat": [], "g_load": [],
            },
        }

        for row in rows:
            data["timestamps"].append(row["timestamp"])
            # Engine
            data["engine"]["rpm"].append(row["rpm1"])
            data["engine"]["map"].append(row["internal_map"])
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
            # Flight
            data["flight"]["ias"].append(row["indicated_airspeed"])
            data["flight"]["tas"].append(row["true_airspeed"])
            data["flight"]["ground_speed"].append(row["ground_speed"])
            data["flight"]["pressure_alt"].append(row["pressure_altitude"])
            data["flight"]["gps_alt"].append(row["gps_altitude"])
            data["flight"]["density_alt"].append(row["density_altitude"])
            data["flight"]["vertical_speed"].append(row["vertical_speed"])
            data["flight"]["oat"].append(row["oat"])
            data["flight"]["g_load"].append(row["g_load"])

        return jsonify(data)

    finally:
        conn.close()


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
    """Get oil consumption data."""
    config = load_config()
    window = config.get("trend_window_hours", 25)
    data = compute_oil_consumption_rolling(window)
    return jsonify(data)


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
    """Run the dashboard web server."""
    config = load_config()
    port = config.get("dashboard_port", 5050)
    logger.info(f"Starting dashboard on http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
