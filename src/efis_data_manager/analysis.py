# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Flight data analysis engine.

Provides:
- Per-flight detailed statistics (cylinder-level, fuel, performance)
- Rolling trend computation over configurable hour windows
- Anomaly detection with altitude/OAT normalization
- Alert generation for engine parameter exceedances
"""

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from efis_data_manager.database import get_db_connection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configurable thresholds (defaults — can be overridden via config)
# ---------------------------------------------------------------------------

DEFAULT_THRESHOLDS = {
    # CHT limits (degrees F)
    "cht_caution": 380,       # Yellow arc start
    "cht_redline": 430,       # Never exceed
    "cht_spread_caution": 50, # Max CHT spread across cylinders

    # EGT limits (degrees F)
    "egt_caution": 1450,
    "egt_redline": 1500,
    "egt_spread_caution": 100,  # Max EGT spread

    # Oil limits
    "oil_temp_caution": 220,    # degrees F
    "oil_temp_redline": 245,
    "oil_pressure_low": 25,     # psi (hot idle minimum)
    "oil_pressure_low_cruise": 55,  # psi (cruise minimum)
    "oil_pressure_high": 95,    # psi (upper normal limit)

    # Fuel
    "fuel_flow_lean_threshold": 6.0,  # GPH — below this, likely LOP
    "fuel_pressure_low": 15,    # psi (minimum)
    "fuel_pressure_high": 35,   # psi (maximum)

    # Episode detection hysteresis: a parameter must recover past the limit by
    # its deadband AND stay recovered for episode_min_gap_s before an episode is
    # considered ended. Prevents one noisy excursion from spawning many alerts.
    "episode_min_gap_s": 30,
    "episode_deadband_temp": 10,      # deg F (CHT/EGT/oil temp)
    "episode_deadband_oil_press": 5,  # psi
    "episode_deadband_fuel_press": 3, # psi
    "episode_deadband_tas": 5,        # kt (airspeed / Vne)
    "episode_deadband_g": 0.2,        # g

    # G-load limits. Positive and negative each have a caution and a limit
    # (exceeding the limit is a warning). Set to your aircraft/category values
    # (RV-8: +6/-3 g at aerobatic gross; +4.4 g at higher weight).
    "g_pos_caution": 3.0,       # positive-G caution
    "g_pos_limit": 3.8,         # positive-G limit (warning at/above)
    "g_neg_caution": -1.5,      # negative-G caution
    "g_neg_limit": -3.0,        # negative-G limit (warning at/below)

    # Vne (never-exceed), checked against TRUE airspeed because Vne is a
    # flutter/TAS limit on many aircraft (incl. the RV-8). Value in KNOTS TAS.
    # The EFIS handles the descending indicated-Vne in the cockpit; here we
    # simply flag any logged TAS at/above this limit as a warning.
    # Default 200 KTAS = RV-8 Vne of 230 mph TAS. Set to your aircraft's value.
    "vne_tas_redline": 200,     # KTAS (RV-8: 230 mph TAS = ~200 KTAS)

    # IAS-based Vne companion to vne_tas_redline. Exactly one Vne key is active
    # at a time (the other holds the 9999 disabled sentinel). Ships disabled so
    # today's TAS-based Vne behavior is preserved until an import flips it.
    "vne_ias_redline": 9999,    # KIAS; 9999 = disabled

    # RPM redline (engine over-speed). Set from the EFIS Max RPM on import.
    "rpm_redline": 2700,        # RPM (typical Lycoming redline)

    # CHT shock-cooling rate limit, degrees F per minute (SID 150). GRT EIS4000
    # expresses this in °/min. Any faster cooling flags a shock-cooling event.
    "cht_cooling_rate_max": 60, # °F/min

    # Trend detection
    "trend_cht_rise_rate": 5.0,   # degrees F per flight hour (rolling avg)
    "trend_oil_consumption_high": 0.15,  # quarts per hour

    # Voltage
    "voltage_low": 13.0,
    "voltage_high": 15.0,
}


def get_thresholds() -> dict:
    """Load analysis thresholds from config, falling back to defaults."""
    from efis_data_manager.config import load_config
    config = load_config()
    thresholds = dict(DEFAULT_THRESHOLDS)
    # Override with any user-configured values
    user_thresholds = config.get("analysis_thresholds", {})
    thresholds.update(user_thresholds)
    return thresholds


# ---------------------------------------------------------------------------
# Per-flight detailed statistics
# ---------------------------------------------------------------------------

@dataclass
class CylinderStats:
    """Per-cylinder statistics for a flight."""
    number: int
    max_cht: Optional[float] = None
    avg_cht_cruise: Optional[float] = None
    max_egt: Optional[float] = None
    avg_egt_cruise: Optional[float] = None
    min_egt_cruise: Optional[float] = None  # For GAMI spread detection


@dataclass
class FlightStats:
    """Comprehensive per-operation statistics (flight or ground)."""
    flight_id: int
    operation_id: int
    date: str
    duration_seconds: int
    airborne_seconds: int
    has_flight: bool = True

    # Performance
    max_ground_speed: Optional[float] = None
    max_indicated_airspeed: Optional[float] = None
    max_true_airspeed: Optional[float] = None
    avg_ias_cruise: Optional[float] = None
    max_altitude: Optional[float] = None
    max_g_load: Optional[float] = None
    min_g_load: Optional[float] = None

    # Engine summary
    max_rpm: Optional[float] = None
    avg_rpm_cruise: Optional[float] = None
    max_cht: Optional[float] = None
    max_egt: Optional[float] = None
    cht_spread_max: Optional[float] = None  # Max spread between cylinders
    egt_spread_max: Optional[float] = None
    # Max windowed CHT cooling rate over the flight, °/min (positive = cooling).
    # Computed on the hottest-cylinder CHT series over a W-second window.
    max_cht_cooling_rate: Optional[float] = None

    # Oil
    max_oil_temp: Optional[float] = None
    min_oil_pressure_cruise: Optional[float] = None
    avg_oil_temp_cruise: Optional[float] = None
    avg_oil_pressure_cruise: Optional[float] = None

    # Fuel
    fuel_used: Optional[float] = None
    avg_fuel_flow_cruise: Optional[float] = None
    max_fuel_flow: Optional[float] = None

    # Percent power (computed from the EFIS Power_Map; None when unavailable)
    avg_percent_power_cruise: Optional[float] = None

    # Electrical
    min_voltage: Optional[float] = None
    avg_voltage: Optional[float] = None

    # Hourmeter
    hourmeter_start: Optional[float] = None
    hourmeter_end: Optional[float] = None

    # Per-cylinder
    cylinders: list[CylinderStats] = field(default_factory=list)

    # Alerts generated for this flight
    alerts: list[str] = field(default_factory=list)


def get_flight_stats(operation_id: int) -> Optional[FlightStats]:
    """Compute detailed statistics for a specific operation (flight or ground).

    Queries the raw FDL data from the database and computes comprehensive
    per-operation metrics including cylinder-level analysis.

    Args:
        operation_id: The operations.id to analyze.

    Returns:
        FlightStats dataclass or None if operation not found.
    """
    conn = get_db_connection()
    try:
        # Get operation metadata
        op_row = conn.execute(
            "SELECT * FROM operations WHERE id = ?", (operation_id,)
        ).fetchone()
        if not op_row:
            return None

        # Get all FDL data for this operation
        rows = conn.execute(
            """SELECT * FROM fdl_data WHERE operation_id = ?
               ORDER BY timestamp""",
            (operation_id,)
        ).fetchall()

        if not rows:
            return None

        stats = FlightStats(
            flight_id=operation_id,
            operation_id=operation_id,
            date=op_row["date"],
            duration_seconds=op_row["duration_seconds"],
            airborne_seconds=op_row["airborne_seconds"],
            hourmeter_start=op_row["hourmeter_start"],
            hourmeter_end=op_row["hourmeter_end"],
            fuel_used=op_row["fuel_used"],
        )
        stats.has_flight = bool(op_row["has_flight"])

        # Classify records
        airborne = [r for r in rows if (r["indicated_airspeed"] or 0) > 40]
        cruise = [
            r for r in airborne
            if (r["vertical_speed"] is not None and abs(r["vertical_speed"]) < 300
                and r["rpm1"] is not None and r["rpm1"] > 1800)
        ]

        # Performance
        stats.max_ground_speed = _max_col(rows, "ground_speed")
        stats.max_indicated_airspeed = _max_col(rows, "indicated_airspeed")
        stats.max_true_airspeed = _max_col(rows, "true_airspeed")
        stats.avg_ias_cruise = _avg_col(cruise, "indicated_airspeed")
        stats.max_altitude = _max_col(rows, "pressure_altitude")
        stats.max_g_load = _max_col(rows, "g_load")
        stats.min_g_load = _min_col(rows, "g_load")

        # Engine
        stats.max_rpm = _max_col(rows, "rpm1")
        stats.avg_rpm_cruise = _avg_col(cruise, "rpm1")
        stats.max_fuel_flow = _max_col(rows, "fuel_flow")
        stats.avg_fuel_flow_cruise = _avg_col(cruise, "fuel_flow")

        # Percent power (cruise average). Computed from the EFIS Power_Map via
        # the exact 8-step algorithm; None for the whole flight when the map is
        # unavailable, and per-sample None values are excluded from the average
        # (Req 6.2, 5.1, 5.2). Imported lazily to avoid an import cycle.
        stats.avg_percent_power_cruise = _avg_percent_power_cruise(cruise)

        # Oil
        stats.max_oil_temp = _max_col(rows, "oil_temp", min_valid=50)
        stats.min_oil_pressure_cruise = _min_col(cruise, "oil_pressure", min_valid=1)
        stats.avg_oil_temp_cruise = _avg_col(cruise, "oil_temp", min_valid=50)
        stats.avg_oil_pressure_cruise = _avg_col(cruise, "oil_pressure", min_valid=1)

        # Electrical
        volts_vals = [r["eis_volts"] for r in airborne if r["eis_volts"] and r["eis_volts"] > 10]
        if volts_vals:
            stats.min_voltage = min(volts_vals)
            stats.avg_voltage = sum(volts_vals) / len(volts_vals)

        # Per-cylinder analysis
        cht_fields = ["cht1", "cht2", "cht3", "cht4", "cht5", "cht6"]
        egt_fields = ["egt1", "egt2", "egt3", "egt4", "egt5", "egt6"]

        from efis_data_manager.config import load_config
        num_cyl = load_config().get("num_cylinders", 4)

        for i, (cht_f, egt_f) in enumerate(zip(cht_fields[:num_cyl], egt_fields[:num_cyl]), start=1):
            cyl = CylinderStats(number=i)
            cyl.max_cht = _max_col(rows, cht_f, min_valid=100)
            cyl.avg_cht_cruise = _avg_col(cruise, cht_f, min_valid=100)
            cyl.max_egt = _max_col(rows, egt_f, min_valid=100)
            cyl.avg_egt_cruise = _avg_col(cruise, egt_f, min_valid=100)
            # Min EGT in cruise (for GAMI spread — peak EGT during lean)
            egt_cruise_vals = [r[egt_f] for r in cruise if r[egt_f] and r[egt_f] > 100]
            cyl.min_egt_cruise = min(egt_cruise_vals) if egt_cruise_vals else None
            stats.cylinders.append(cyl)

        # CHT/EGT spread (max difference between any two cylinders at same instant)
        # Use higher min_valid to exclude unused/dummy cylinders on 4-cyl engines
        stats.max_cht = max((c.max_cht for c in stats.cylinders if c.max_cht), default=None)
        stats.max_egt = max((c.max_egt for c in stats.cylinders if c.max_egt), default=None)
        stats.cht_spread_max = _max_spread(cruise, cht_fields[:num_cyl], min_valid=200)
        stats.egt_spread_max = _max_spread(cruise, egt_fields[:num_cyl], min_valid=500)

        # Max windowed CHT cooling rate (°/min) over the flight. Built from the
        # same hottest-cylinder CHT series used by detect_episodes so the
        # flight-summary line and the episode agree (Req 12.3).
        cht_cols_present = [c for c in cht_fields[:num_cyl] if c in rows[0].keys()]
        if cht_cols_present:
            hottest = []
            for r in rows:
                vals = [r[c] for c in cht_cols_present
                        if r[c] is not None and r[c] >= 100]
                hottest.append((r["timestamp"], max(vals) if vals else None))
            rate_s = _cht_cooling_rate_series(hottest)
            rates = [rt for _, rt in rate_s]
            stats.max_cht_cooling_rate = max(rates) if rates else None

        # Generate alerts
        _check_alerts(stats, get_thresholds())

        return stats

    finally:
        conn.close()


def get_all_flight_stats() -> list[FlightStats]:
    """Get stats for all flights in the database."""
    conn = get_db_connection()
    try:
        flight_ids = [
            row["id"] for row in
            conn.execute("SELECT id FROM operations ORDER BY date DESC").fetchall()
        ]
    finally:
        conn.close()

    return [s for fid in flight_ids if (s := get_flight_stats(fid)) is not None]


# ---------------------------------------------------------------------------
# Rolling trends
# ---------------------------------------------------------------------------

@dataclass
class TrendPoint:
    """A single data point in a trend series."""
    date: str
    flight_id: int
    hourmeter: Optional[float]
    value: float


@dataclass
class TrendSeries:
    """A named trend series with rolling average."""
    name: str
    unit: str
    points: list[TrendPoint] = field(default_factory=list)
    rolling_avg: list[Optional[float]] = field(default_factory=list)


def compute_trends(window_hours: float = 25.0) -> dict[str, TrendSeries]:
    """Compute rolling trends for key engine parameters.

    Uses a rolling window based on engine hours (hourmeter delta) rather
    than calendar time, since flight frequency varies.

    Args:
        window_hours: Size of rolling window in engine hours (default 25).

    Returns:
        Dict of parameter_name -> TrendSeries.
    """
    conn = get_db_connection()
    try:
        # Trends only include flights, not ground operations
        flights = conn.execute(
            """SELECT id, date, hourmeter_end, max_cht, avg_cht_cruise,
                      max_egt, avg_egt_cruise, max_oil_temp,
                      min_oil_pressure, avg_fuel_flow_cruise, fuel_used,
                      airborne_seconds
               FROM operations
               WHERE has_flight = 1
               ORDER BY date, hourmeter_end""",
        ).fetchall()
    finally:
        conn.close()

    if not flights:
        return {}

    # Build trend series
    series_defs = [
        ("max_cht", "Max CHT", "°F"),
        ("avg_cht_cruise", "Avg CHT (cruise)", "°F"),
        ("max_egt", "Max EGT", "°F"),
        ("avg_egt_cruise", "Avg EGT (cruise)", "°F"),
        ("max_oil_temp", "Max Oil Temp", "°F"),
        ("min_oil_pressure", "Min Oil Pressure (cruise)", "psi"),
        ("avg_fuel_flow_cruise", "Avg Fuel Flow (cruise)", "GPH"),
    ]

    trends = {}
    for col, name, unit in series_defs:
        series = TrendSeries(name=name, unit=unit)
        for f in flights:
            val = f[col]
            if val is not None:
                series.points.append(TrendPoint(
                    date=f["date"],
                    flight_id=f["id"],
                    hourmeter=f["hourmeter_end"],
                    value=val,
                ))
        # Compute rolling average by hour window
        series.rolling_avg = _rolling_avg_by_hours(series.points, window_hours)
        trends[col] = series

    return trends


def _rolling_avg_by_hours(points: list[TrendPoint], window_hours: float) -> list[Optional[float]]:
    """Compute rolling average using engine-hour window."""
    if not points:
        return []

    result = []
    for i, p in enumerate(points):
        if p.hourmeter is None:
            result.append(None)
            continue

        # Find all points within window_hours before this point
        window_vals = []
        for j in range(i, -1, -1):
            pj = points[j]
            if pj.hourmeter is None:
                continue
            if p.hourmeter - pj.hourmeter > window_hours:
                break
            window_vals.append(pj.value)

        if window_vals:
            result.append(sum(window_vals) / len(window_vals))
        else:
            result.append(None)

    return result


# ---------------------------------------------------------------------------
# Anomaly detection
# ---------------------------------------------------------------------------

@dataclass
class Anomaly:
    """A detected anomaly in flight data."""
    flight_id: int
    date: str
    parameter: str
    severity: str       # "caution" or "warning"
    message: str
    value: Optional[float] = None
    threshold: Optional[float] = None
    timestamp: Optional[str] = None  # ISO 8601 timestamp of the exceedance


def detect_anomalies(window_hours: float = 25.0) -> list[Anomaly]:
    """Detect anomalies across all flights.

    Looks for:
    - Absolute exceedances (CHT/EGT/oil beyond limits)
    - Trend anomalies (values rising significantly above rolling average)
    - Spread anomalies (cylinder imbalance)

    Args:
        window_hours: Rolling window for trend-based detection.

    Returns:
        List of Anomaly objects sorted by date descending.
    """
    thresholds = get_thresholds()
    anomalies = []

    conn = get_db_connection()
    try:
        # Alerts cover all operations (flights and ground ops)
        flights = conn.execute(
            """SELECT * FROM operations ORDER BY date DESC""",
        ).fetchall()
    finally:
        pass  # Keep conn open for timestamp lookups below

    trends = compute_trends(window_hours)

    for flight in flights:
        fid = flight["id"]
        fdate = flight["date"]
        op_id = flight["id"]
        is_ground = not flight["has_flight"]
        ground_note = " (ground op)" if is_ground else ""

        # --- Absolute exceedances (with timestamp lookup) ---
        if flight["max_cht"] and flight["max_cht"] >= thresholds["cht_redline"]:
            ts = _find_exceedance_timestamp(conn, op_id, "cht", thresholds["cht_redline"])
            anomalies.append(Anomaly(
                flight_id=fid, date=fdate, parameter="CHT",
                severity="warning",
                message=f"CHT Redline Exceeded ({flight['max_cht']:.0f}°F max, redline {thresholds['cht_redline']:.0f}°F){ground_note}",
                value=flight["max_cht"], threshold=thresholds["cht_redline"],
                timestamp=ts,
            ))
        elif flight["max_cht"] and flight["max_cht"] >= thresholds["cht_caution"]:
            ts = _find_exceedance_timestamp(conn, op_id, "cht", thresholds["cht_caution"])
            anomalies.append(Anomaly(
                flight_id=fid, date=fdate, parameter="CHT",
                severity="caution",
                message=f"CHT Caution Limit Exceeded ({flight['max_cht']:.0f}°F max, caution {thresholds['cht_caution']:.0f}°F){ground_note}",
                value=flight["max_cht"], threshold=thresholds["cht_caution"],
                timestamp=ts,
            ))

        if flight["max_egt"] and flight["max_egt"] >= thresholds["egt_redline"]:
            ts = _find_exceedance_timestamp(conn, op_id, "egt", thresholds["egt_redline"])
            anomalies.append(Anomaly(
                flight_id=fid, date=fdate, parameter="EGT",
                severity="warning",
                message=f"EGT Redline Exceeded ({flight['max_egt']:.0f}°F max, redline {thresholds['egt_redline']:.0f}°F){ground_note}",
                value=flight["max_egt"], threshold=thresholds["egt_redline"],
                timestamp=ts,
            ))
        elif flight["max_egt"] and flight["max_egt"] >= thresholds["egt_caution"]:
            ts = _find_exceedance_timestamp(conn, op_id, "egt", thresholds["egt_caution"])
            anomalies.append(Anomaly(
                flight_id=fid, date=fdate, parameter="EGT",
                severity="caution",
                message=f"EGT Caution Limit Exceeded ({flight['max_egt']:.0f}°F max, caution {thresholds['egt_caution']:.0f}°F){ground_note}",
                value=flight["max_egt"], threshold=thresholds["egt_caution"],
                timestamp=ts,
            ))

        if flight["max_oil_temp"] and flight["max_oil_temp"] >= thresholds["oil_temp_caution"]:
            sev = "warning" if flight["max_oil_temp"] >= thresholds["oil_temp_redline"] else "caution"
            ts = _find_exceedance_timestamp(conn, op_id, "oil_temp", thresholds["oil_temp_caution"])
            anomalies.append(Anomaly(
                flight_id=fid, date=fdate, parameter="Oil Temp",
                severity=sev,
                message=f"Oil temp reached {flight['max_oil_temp']:.0f}°F{ground_note}",
                value=flight["max_oil_temp"], threshold=thresholds["oil_temp_caution"],
                timestamp=ts,
            ))

        # Oil pressure: use appropriate context (cruise for flights, running for ground)
        oil_press_context = "in cruise" if not is_ground else "while running"
        if flight["min_oil_pressure"] and flight["min_oil_pressure"] < thresholds["oil_pressure_low_cruise"]:
            sev = "warning" if flight["min_oil_pressure"] < thresholds["oil_pressure_low"] else "caution"
            ts = _find_min_timestamp(conn, op_id, "oil_pressure", thresholds["oil_pressure_low_cruise"])
            anomalies.append(Anomaly(
                flight_id=fid, date=fdate, parameter="Oil Pressure",
                severity=sev,
                message=f"Oil pressure dropped to {flight['min_oil_pressure']:.0f} psi {oil_press_context}{ground_note}",
                value=flight["min_oil_pressure"], threshold=thresholds["oil_pressure_low_cruise"],
                timestamp=ts,
            ))

        # --- Cylinder spread (CHT/EGT imbalance) ---
        # Spread isn't stored on the operations row, so compute it from the raw
        # data. Timestamp points to the instant of maximum spread.
        for label, prefix, thr_key, min_valid in (
            ("CHT", "cht", "cht_spread_caution", 200),
            ("EGT", "egt", "egt_spread_caution", 500),
        ):
            spread_val, spread_ts = _find_max_spread_timestamp(
                conn, op_id, prefix, min_valid)
            if spread_val is not None and spread_val >= thresholds[thr_key]:
                anomalies.append(Anomaly(
                    flight_id=fid, date=fdate, parameter=f"{label} Spread",
                    severity="caution",
                    message=f"{label} Spread Caution Limit Exceeded "
                            f"({spread_val:.0f}°F max, caution {thresholds[thr_key]:.0f}°F){ground_note}",
                    value=spread_val, threshold=thresholds[thr_key],
                    timestamp=spread_ts,
                ))

    # --- Trend anomalies ---
    # Flag flights where a parameter is > 2 std deviations above rolling avg
    for param_key, series in trends.items():
        if len(series.points) < 5:
            continue  # Need enough data for meaningful trends

        for i, (point, avg) in enumerate(zip(series.points, series.rolling_avg)):
            if avg is None:
                continue
            # Compute local std dev from the window
            window_vals = [
                p.value for j, p in enumerate(series.points[:i+1])
                if series.rolling_avg[j] is not None
                and (point.hourmeter and p.hourmeter and
                     point.hourmeter - p.hourmeter <= window_hours)
            ]
            if len(window_vals) < 3:
                continue
            mean = sum(window_vals) / len(window_vals)
            variance = sum((v - mean) ** 2 for v in window_vals) / len(window_vals)
            std_dev = variance ** 0.5

            if std_dev > 0 and (point.value - avg) > 2 * std_dev:
                anomalies.append(Anomaly(
                    flight_id=point.flight_id, date=point.date,
                    parameter=series.name, severity="caution",
                    message=(f"{series.name} at {point.value:.1f}{series.unit} "
                             f"is {(point.value - avg)/std_dev:.1f}σ above "
                             f"rolling avg ({avg:.1f}{series.unit})"),
                    value=point.value, threshold=avg + 2 * std_dev,
                ))

    # Sort by date descending, then severity
    severity_order = {"warning": 0, "caution": 1}
    anomalies.sort(key=lambda a: (a.date, severity_order.get(a.severity, 2)), reverse=True)

    # --- Oil consumption anomalies ---
    oil_rolling = compute_oil_consumption_rolling(window_hours)
    if oil_rolling:
        threshold = thresholds.get("trend_oil_consumption_high", 0.15)
        for entry in oil_rolling:
            if entry["rate"] > threshold:
                anomalies.append(Anomaly(
                    flight_id=0,  # Not tied to a specific flight
                    date=entry["date"],
                    parameter="Oil Consumption",
                    severity="caution",
                    message=(f"Oil consumption {entry['rate']:.3f} qt/hr "
                             f"({entry['oil_added']} qt over {entry['hours_since_last']:.1f} hr) "
                             f"exceeds threshold ({threshold} qt/hr)"),
                    value=entry["rate"],
                    threshold=threshold,
                ))

    # Re-sort after adding oil entries
    anomalies.sort(key=lambda a: (a.date, severity_order.get(a.severity, 2)), reverse=True)

    conn.close()
    return anomalies


# ---------------------------------------------------------------------------
# Alert checking for a single flight
# ---------------------------------------------------------------------------

def _check_alerts(stats: FlightStats, thresholds: dict):
    """Check a flight's stats against thresholds and populate alerts list."""
    if stats.max_cht and stats.max_cht >= thresholds["cht_caution"]:
        sev = "WARNING" if stats.max_cht >= thresholds["cht_redline"] else "CAUTION"
        stats.alerts.append(f"{sev}: Max CHT {stats.max_cht:.0f}°F")

    if stats.max_egt and stats.max_egt >= thresholds["egt_caution"]:
        sev = "WARNING" if stats.max_egt >= thresholds["egt_redline"] else "CAUTION"
        stats.alerts.append(f"{sev}: Max EGT {stats.max_egt:.0f}°F")

    if stats.cht_spread_max and stats.cht_spread_max >= thresholds["cht_spread_caution"]:
        stats.alerts.append(f"CAUTION: CHT spread {stats.cht_spread_max:.0f}°F")

    if stats.egt_spread_max and stats.egt_spread_max >= thresholds["egt_spread_caution"]:
        stats.alerts.append(f"CAUTION: EGT spread {stats.egt_spread_max:.0f}°F")

    if stats.max_oil_temp and stats.max_oil_temp >= thresholds["oil_temp_caution"]:
        stats.alerts.append(f"CAUTION: Oil temp {stats.max_oil_temp:.0f}°F")

    if stats.min_oil_pressure_cruise and stats.min_oil_pressure_cruise < thresholds["oil_pressure_low_cruise"]:
        stats.alerts.append(f"CAUTION: Oil pressure {stats.min_oil_pressure_cruise:.0f} psi in cruise")

    if stats.min_voltage and stats.min_voltage < thresholds["voltage_low"]:
        stats.alerts.append(f"CAUTION: Voltage dropped to {stats.min_voltage:.1f}V")

    # Positive G: caution + limit (warning).
    if stats.max_g_load and stats.max_g_load >= thresholds["g_pos_caution"]:
        sev = "WARNING" if stats.max_g_load >= thresholds["g_pos_limit"] else "CAUTION"
        stats.alerts.append(f"{sev}: Max +G {stats.max_g_load:.2f}g")

    # Negative G: caution + limit (warning). More-negative is worse.
    gnc = thresholds.get("g_neg_caution")
    gnl = thresholds.get("g_neg_limit")
    if gnc is not None and stats.min_g_load is not None and stats.min_g_load <= gnc:
        sev = "WARNING" if (gnl is not None and stats.min_g_load <= gnl) else "CAUTION"
        stats.alerts.append(f"{sev}: Max -G {stats.min_g_load:.2f}g")

    # Vne (warning only) against TRUE airspeed; disabled at 9999 placeholder.
    vne = thresholds.get("vne_tas_redline", 9999)
    if stats.max_true_airspeed and vne < 9999 and stats.max_true_airspeed >= vne:
        stats.alerts.append(f"WARNING: Max TAS {stats.max_true_airspeed:.0f} kt (Vne {vne:.0f} TAS)")

    # Vne (warning only) against INDICATED airspeed; mirror of the TAS line.
    # The mapper keeps the two Vne keys mutually exclusive (Req 5.6).
    vne_ias = thresholds.get("vne_ias_redline", 9999)
    if stats.max_indicated_airspeed and vne_ias < 9999 and stats.max_indicated_airspeed >= vne_ias:
        stats.alerts.append(f"WARNING: Max IAS {stats.max_indicated_airspeed:.0f} kt (Vne {vne_ias:.0f} IAS)")

    # RPM redline (warning only). Gated on rpm_redline being set (Req 11.2, 11.3).
    rpm_redline = thresholds.get("rpm_redline")
    if rpm_redline is not None and stats.max_rpm and stats.max_rpm >= rpm_redline:
        stats.alerts.append(f"WARNING: Max RPM {stats.max_rpm:.0f} (redline {rpm_redline:.0f})")

    # CHT shock-cooling rate (caution). Gated on cht_cooling_rate_max being set
    # (Req 12.3, 12.4). max_cht_cooling_rate is the max windowed °/min cooling
    # observed over the flight (positive = cooling).
    cool_max = thresholds.get("cht_cooling_rate_max")
    if (cool_max is not None and stats.max_cht_cooling_rate is not None
            and stats.max_cht_cooling_rate > cool_max):
        stats.alerts.append(
            f"CAUTION: CHT cooling {stats.max_cht_cooling_rate:.0f}°/min "
            f"(max {cool_max:.0f}°/min)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _max_col(rows: list, col: str, min_valid: float = None) -> Optional[float]:
    """Get max value of a column from rows, optionally filtering below min_valid."""
    vals = []
    for r in rows:
        v = r[col]
        if v is not None:
            if min_valid is None or v >= min_valid:
                vals.append(v)
    return max(vals) if vals else None


def _min_col(rows: list, col: str, min_valid: float = None) -> Optional[float]:
    """Get min value of a column from rows, optionally filtering below min_valid."""
    vals = []
    for r in rows:
        v = r[col]
        if v is not None:
            if min_valid is None or v >= min_valid:
                vals.append(v)
    return min(vals) if vals else None


def _avg_col(rows: list, col: str, min_valid: float = None) -> Optional[float]:
    """Get average of a column from rows."""
    vals = []
    for r in rows:
        v = r[col]
        if v is not None:
            if min_valid is None or v >= min_valid:
                vals.append(v)
    return sum(vals) / len(vals) if vals else None


def _avg_percent_power_cruise(cruise: list) -> Optional[float]:
    """Cruise-average Percent_Power over the given cruise rows (Req 6.2).

    Obtains the EFIS Power_Map once; when unavailable returns None (the whole
    flight shows no Percent_Power, Req 5.2). Otherwise computes percent_power
    per cruise row from rpm1, the resolved MAP column, pressure_altitude, and
    OAT (converted to Fahrenheit via the settings Temp_Units flag), averages the
    defined (non-None) values, and returns None when no cruise sample yields a
    defined value (Req 5.1).

    Imports are lazy to keep analysis.py free of an import cycle with power.py.
    """
    from efis_data_manager import power
    from efis_data_manager.aux_map import resolve_aux

    settings = power._read_settings()
    if settings is None:
        return None
    sids, temp_units_celsius = settings
    power_map = power.build_power_map(sids)
    if power_map is None:
        return None

    map_col = power.map_column_for(resolve_aux())

    keys = cruise[0].keys() if cruise else []
    values = []
    for r in cruise:
        rpm = r["rpm1"]
        map_inhg = r[map_col] if map_col in keys else None
        palt = r["pressure_altitude"]
        oat_raw = r["oat"]
        if oat_raw is None:
            oat_f = None
        else:
            oat_f = power.oat_to_fahrenheit(oat_raw, temp_units_celsius)
        pp = power.percent_power(rpm, map_inhg, palt, oat_f, power_map)
        if pp is not None:
            values.append(pp)

    return sum(values) / len(values) if values else None


def _max_spread(rows: list, fields: list[str], min_valid: float = 0) -> Optional[float]:
    """Compute maximum spread (max - min) across multiple columns at any instant."""
    max_spread = 0
    for r in rows:
        vals = [r[f] for f in fields if r[f] is not None and r[f] >= min_valid]
        if len(vals) >= 2:
            spread = max(vals) - min(vals)
            if spread > max_spread:
                max_spread = spread
    return max_spread if max_spread > 0 else None


def _find_exceedance_timestamp(conn, operation_id: int, param_type: str,
                                threshold: float) -> Optional[str]:
    """Find the first timestamp where a parameter exceeded a threshold.

    For multi-cylinder params (cht, egt), checks all cylinder columns.
    """
    from efis_data_manager.config import load_config
    num_cyl = load_config().get("num_cylinders", 4)

    if param_type == "cht":
        cols = [f"cht{i}" for i in range(1, num_cyl + 1)]
    elif param_type == "egt":
        cols = [f"egt{i}" for i in range(1, num_cyl + 1)]
    else:
        cols = [param_type]

    # Build WHERE clause: any column >= threshold
    conditions = " OR ".join(f"{c} >= ?" for c in cols)
    params = [threshold] * len(cols)

    row = conn.execute(
        f"""SELECT timestamp FROM fdl_data
            WHERE operation_id = ? AND ({conditions})
            ORDER BY timestamp LIMIT 1""",
        [operation_id] + params,
    ).fetchone()

    return row["timestamp"] if row else None


def _find_max_spread_timestamp(conn, operation_id: int, prefix: str,
                               min_valid: float) -> tuple[Optional[float], Optional[str]]:
    """Find the maximum cylinder spread and the timestamp at which it occurred.

    prefix is 'cht' or 'egt'; scans cht1..cht6 / egt1..egt6 present in the data.
    Returns (max_spread, timestamp) or (None, None) if not computable.
    """
    cols = [f"{prefix}{i}" for i in range(1, 7)]
    existing = {r[1] for r in conn.execute("PRAGMA table_info(fdl_data)")}
    cols = [c for c in cols if c in existing]
    if len(cols) < 2:
        return None, None

    rows = conn.execute(
        f"SELECT timestamp, {', '.join(cols)} FROM fdl_data "
        f"WHERE operation_id = ? ORDER BY timestamp",
        (operation_id,),
    ).fetchall()

    best_spread = 0.0
    best_ts = None
    for r in rows:
        vals = [r[c] for c in cols if r[c] is not None and r[c] >= min_valid]
        if len(vals) >= 2:
            spread = max(vals) - min(vals)
            if spread > best_spread:
                best_spread = spread
                best_ts = r["timestamp"]
    return (best_spread, best_ts) if best_spread > 0 else (None, None)


def _find_min_timestamp(conn, operation_id: int, column: str,
                         threshold: float) -> Optional[str]:
    """Find the first timestamp where a parameter dropped below a threshold."""
    row = conn.execute(
        f"""SELECT timestamp FROM fdl_data
            WHERE operation_id = ? AND {column} IS NOT NULL
                  AND {column} > 0 AND {column} < ?
            ORDER BY timestamp LIMIT 1""",
        (operation_id, threshold),
    ).fetchone()

    return row["timestamp"] if row else None


# ---------------------------------------------------------------------------
# Oil consumption tracking
# ---------------------------------------------------------------------------

@dataclass
class OilConsumptionPeriod:
    """One oil consumption measurement period (between two oil events)."""
    start_date: str
    end_date: str
    start_hourmeter: float
    end_hourmeter: float
    hours_between: float            # interval since the previous event (drives rate)
    hours_since_change: Optional[float]  # hours since the most recent oil change (None if none on record)
    oil_consumed_quarts: float      # oil consumed during this period
    rate_quarts_per_hour: float
    end_event_type: str             # 'change' or 'addition'


def compute_oil_consumption() -> list[OilConsumptionPeriod]:
    """Compute oil consumption rate between consecutive oil events.

    Uses the oil_events table (respecting the configured cutoff date).
    Oil consumed during a period = what was replenished at the period's end:
      - addition: quarts_added
      - change:   quarts_low (how low it had run before the change; the fresh
                  fill is not "consumption")

    Returns:
        List of OilConsumptionPeriod sorted by hourmeter.
    """
    from efis_data_manager.database import get_oil_events
    from efis_data_manager.config import load_config

    cutoff = load_config().get("oil_cutoff_date", "")
    events = get_oil_events(cutoff_date=cutoff)

    if len(events) < 2:
        return []

    # Track the hourmeter of the most recent oil change so we can report, for
    # each event, how many engine hours have accumulated since that change.
    # Seed from the first event if it is itself a change.
    last_change_hm = events[0]["hourmeter"] if events[0]["event_type"] == "change" else None

    periods = []
    for i in range(1, len(events)):
        prev = events[i - 1]
        curr = events[i]

        hours_between = curr["hourmeter"] - prev["hourmeter"]
        if hours_between <= 0:
            # Still advance the last-change marker on a (zero/negative-interval)
            # change so later rows reference the right change.
            if curr["event_type"] == "change":
                last_change_hm = curr["hourmeter"]
            continue

        # Oil consumed since previous event = what was replenished now
        if curr["event_type"] == "change":
            consumed = curr["quarts_low"]
        else:
            consumed = curr["quarts_added"]

        rate = consumed / hours_between

        # Hours since the most recent oil change. A change row resets to 0.
        # If there is no oil change on record yet, leave it None so the UI shows
        # "-" rather than a misleading 0.
        if curr["event_type"] == "change":
            hours_since_change = 0.0
        elif last_change_hm is not None:
            hours_since_change = curr["hourmeter"] - last_change_hm
        else:
            hours_since_change = None  # no prior change on record to measure from

        periods.append(OilConsumptionPeriod(
            start_date=prev["date"],
            end_date=curr["date"],
            start_hourmeter=prev["hourmeter"],
            end_hourmeter=curr["hourmeter"],
            hours_between=hours_between,
            hours_since_change=hours_since_change,
            oil_consumed_quarts=consumed,
            rate_quarts_per_hour=rate,
            end_event_type=curr["event_type"],
        ))

        # Update the last-change marker AFTER computing this row's value.
        if curr["event_type"] == "change":
            last_change_hm = curr["hourmeter"]

    return periods


def compute_oil_consumption_rolling(window_hours: float = 25.0) -> list[dict]:
    """Compute rolling average oil consumption rate.

    Args:
        window_hours: Rolling window in engine hours.

    Returns:
        List of dicts with per-period rate, rolling avg, and event type.
    """
    periods = compute_oil_consumption()
    if not periods:
        return []

    results = []
    for i, p in enumerate(periods):
        # Sum consumption + hours within the rolling window
        window_hours_total = 0
        window_oil_total = 0
        for j in range(i, -1, -1):
            pj = periods[j]
            if p.end_hourmeter - pj.start_hourmeter > window_hours:
                break
            window_hours_total += pj.hours_between
            window_oil_total += pj.oil_consumed_quarts

        rolling_rate = window_oil_total / window_hours_total if window_hours_total > 0 else None

        results.append({
            "date": p.end_date,
            "hourmeter": p.end_hourmeter,
            "rate": p.rate_quarts_per_hour,
            "rolling_avg_rate": rolling_rate,
            "oil_added": p.oil_consumed_quarts,
            "hours_since_last": p.hours_between,
            "hours_since_change": p.hours_since_change,
            "event_type": p.end_event_type,
        })

    return results


def get_oil_changes() -> list[dict]:
    """Return oil-change events with hours-since-last-change, for chart markers.

    Respects the configured cutoff date.

    Returns:
        List of dicts: {date, hourmeter, quarts_added, quarts_low, note,
                        hours_since_last_change}.
    """
    from efis_data_manager.database import get_oil_events
    from efis_data_manager.config import load_config

    cutoff = load_config().get("oil_cutoff_date", "")
    events = get_oil_events(cutoff_date=cutoff)
    changes = [e for e in events if e["event_type"] == "change"]

    result = []
    prev_change_hm = None
    for c in changes:
        hours_since = (c["hourmeter"] - prev_change_hm) if prev_change_hm is not None else None
        result.append({
            "date": c["date"],
            "hourmeter": c["hourmeter"],
            "quarts_added": c["quarts_added"],
            "quarts_low": c["quarts_low"],
            "note": c["note"],
            "hours_since_last_change": hours_since,
        })
        prev_change_hm = c["hourmeter"]

    return result


# ---------------------------------------------------------------------------
# Oil-age warning (dashboard-computed oil hours since the last oil change)
# ---------------------------------------------------------------------------

def compute_oil_age(
    operations_max_hourmeter: Optional[float],
    oil_change_events: list[dict],
    threshold_hours: float,
) -> dict:
    """Classify oil age from dashboard data. PURE — no DB, no config, no I/O.

    Args:
        operations_max_hourmeter: Current_Engine_Hours (max hourmeter_end), or
            None when no operation has a non-null hourmeter_end (Req 2.1, 2.6).
        oil_change_events: oil events already filtered by cutoff date. Only
            events with event_type == "change" are considered; any others are
            ignored (Req 3.1, 3.2). Each considered event must carry a numeric
            "hourmeter". The list need not be sorted.
        threshold_hours: oil_age_warning_hours, already sanitized to >= 0.
            <= 0 means the warning is disabled (Req 1.3, 1.4, 4.5).

    Returns a status dict:
        {
          "status": "ok" | "exceeded" | "no_change_on_record" | "not_computable",
          "oil_hours_since_change": float | None,
          "threshold": float,
          "limit_exceeded": bool,
        }

    Classification (in order):
      1. threshold_hours <= 0 -> "ok", hours None, limit_exceeded False
         (disabled; Req 1.3, 4.5).
      2. no "change" event among oil_change_events -> "no_change_on_record",
         hours None (Req 2.5).
      3. operations_max_hourmeter is None -> "not_computable", hours None
         (Req 2.6).
      4. otherwise H = max hourmeter among "change" events (Req 2.2, 5.2);
         hours = operations_max_hourmeter - H (Req 2.3):
           - hours <= 0 (rolled-back/out-of-order): "ok", hours reported,
             limit_exceeded False (Req 5.1);
           - 0 < hours <= threshold: "ok", limit_exceeded False (Req 4.3);
           - hours > threshold: "exceeded", limit_exceeded True (Req 4.1).

    "threshold" in the returned dict is always the sanitized threshold_hours.
    """
    # Step 1: disabled threshold — do no work, never render a banner.
    if threshold_hours <= 0:
        return {
            "status": "ok",
            "oil_hours_since_change": None,
            "threshold": threshold_hours,
            "limit_exceeded": False,
        }

    # Consider only oil-change events; additions/top-offs are ignored (Req 3).
    change_hours = [
        e["hourmeter"] for e in oil_change_events
        if e.get("event_type") == "change" and e.get("hourmeter") is not None
    ]

    # Step 2: no oil change on record.
    if not change_hours:
        return {
            "status": "no_change_on_record",
            "oil_hours_since_change": None,
            "threshold": threshold_hours,
            "limit_exceeded": False,
        }

    # Step 3: no current engine hours -> not computable.
    if operations_max_hourmeter is None:
        return {
            "status": "not_computable",
            "oil_hours_since_change": None,
            "threshold": threshold_hours,
            "limit_exceeded": False,
        }

    # Step 4: compute age against the highest-hourmeter change (Req 2.2, 5.2).
    recent = max(change_hours)
    hours = operations_max_hourmeter - recent
    exceeded = hours > threshold_hours  # strictly greater (Req 4.1, 4.3)
    return {
        "status": "exceeded" if exceeded else "ok",
        "oil_hours_since_change": hours,
        "threshold": threshold_hours,
        "limit_exceeded": exceeded,
    }


def get_oil_age_status() -> dict:
    """Compute the oil-age status from live dashboard data (Req 2, 3, 5).

    Reads oil_age_warning_hours and oil_cutoff_date from config, loads
    cutoff-filtered oil events via database.get_oil_events(cutoff_date=...),
    reads Current_Engine_Hours via database.get_max_operation_hourmeter(), then
    delegates to compute_oil_age(). Sanitizes a negative / non-numeric /
    absent oil_age_warning_hours to 0 (Req 1.2, 1.4) before passing it in.

    Returns the same status dict shape as compute_oil_age().
    """
    from efis_data_manager.config import load_config
    from efis_data_manager.database import (
        get_oil_events,
        get_max_operation_hourmeter,
    )

    config = load_config()
    # Sanitize threshold: coerce to float, clamp negative/non-numeric to 0.
    try:
        threshold = float(config.get("oil_age_warning_hours", 0))
    except (TypeError, ValueError):
        threshold = 0.0
    if threshold < 0:
        threshold = 0.0

    cutoff = config.get("oil_cutoff_date", "")
    events = get_oil_events(cutoff_date=cutoff)
    max_hm = get_max_operation_hourmeter()
    return compute_oil_age(max_hm, events, threshold)


# ---------------------------------------------------------------------------
# Per-episode exceedance detection (hysteresis)
# ---------------------------------------------------------------------------

@dataclass
class Episode:
    """One continuous exceedance episode of a parameter beyond a limit."""
    parameter: str          # e.g. "CHT", "Oil Pressure"
    direction: str          # "high" or "low"
    severity: str           # "caution" or "warning" (worst reached in episode)
    start_timestamp: str    # first sample of the excursion (jump target)
    peak_timestamp: str     # sample of worst value
    peak_value: float       # worst value reached (max for high, min for low)
    threshold: float        # the limit that was crossed
    duration_s: float       # seconds from start to end of episode
    message: str = ""


def _elapsed_s(t_from: str, t_to: str) -> float:
    """Seconds between two naive ISO timestamps."""
    return (datetime.fromisoformat(t_to) - datetime.fromisoformat(t_from)).total_seconds()


# Shock-cooling window. GRT EIS4000 expresses SID 150 (Max CHT Cooling Rate) in
# degrees-per-minute (design Req 12.1). The FDL is ~1 Hz, so raw per-sample
# first differences are dominated by sensor noise. We instead measure the rate
# over a W-second window: rate(t) = (cht[t-W] - cht[t]) / (W/60), in °/min,
# positive = cooling. A multi-second window suppresses single-sample noise while
# still catching a genuine shock-cooling ramp.
_CHT_COOLING_WINDOW_S = 10          # W: window length in seconds
_CHT_COOLING_WINDOW_TOL_S = 2.0     # how close a sample must be to t-W to pair


_CHT_COOLING_SMOOTH_S = 5           # trailing smoothing window (seconds)


def _cht_cooling_rate_series(cht_series):
    """Derive a (timestamp, cooling_rate_deg_per_min) series from a CHT series.

    `cht_series` is a list of (iso_timestamp, cht_value-or-None). To reject 1 Hz
    sensor noise the raw CHT is first smoothed with a short trailing average
    (_CHT_COOLING_SMOOTH_S seconds), then a cooling rate is measured over a
    W-second window against the nearest earlier smoothed sample:

        rate(t) = (cht_smooth[t-W] - cht_smooth[t]) / (dt/60)   [°/min, + = cool]

    where dt is the ACTUAL elapsed time between the paired samples. The paired
    sample must lie within _CHT_COOLING_WINDOW_TOL_S of t - W AND cover at least
    (W - tol) seconds, so the very start of the series (where a full window does
    not yet exist) produces no rate sample rather than an amplified noisy one.
    Gaps / missing values simply produce no rate sample rather than raising.
    """
    pts = [(ts, v) for ts, v in cht_series if v is not None]
    if not pts:
        return []
    base = pts[0][0]
    elapsed = [_elapsed_s(base, ts) for ts, _ in pts]

    # Trailing moving average to suppress single-sample jitter (design: "smooth
    # with a short trailing window").
    smooth = []
    for j in range(len(pts)):
        lo = elapsed[j] - _CHT_COOLING_SMOOTH_S
        win = [pts[k][1] for k in range(j, -1, -1) if elapsed[k] >= lo]
        smooth.append(sum(win) / len(win))

    out = []
    W = _CHT_COOLING_WINDOW_S
    tol = _CHT_COOLING_WINDOW_TOL_S
    for j in range(len(pts)):
        target = elapsed[j] - W  # elapsed seconds at t - W
        # Walk back to the sample nearest `target` elapsed seconds.
        best_k = None
        best_err = None
        for k in range(j - 1, -1, -1):
            err = abs(elapsed[k] - target)
            if best_err is None or err < best_err:
                best_err = err
                best_k = k
            if elapsed[k] < target - tol:
                break
        if best_k is None or best_err is None or best_err > tol:
            continue
        dt = elapsed[j] - elapsed[best_k]
        if dt < W - tol:      # window not yet fully spanned (series start / gap)
            continue
        if elapsed[best_k] < _CHT_COOLING_SMOOTH_S:
            # The earlier endpoint lacks a full trailing smoothing window, so its
            # smoothed value is biased by series-start truncation — skip it to
            # avoid a spurious rate from that transient.
            continue
        rate = (smooth[best_k] - smooth[j]) / (dt / 60.0)
        out.append((pts[j][0], rate))
    return out


def _detect_episodes_series(samples, direction, threshold, deadband,
                            min_gap_s, redline=None):
    """Core hysteresis engine over a list of (timestamp, value) samples.

    An episode opens when value crosses `threshold` (>= for high, <= for low).
    It stays open until value recovers past `threshold -/+ deadband` AND remains
    recovered for at least `min_gap_s` seconds; only then can a new episode open.
    This prevents one noisy excursion from producing many episodes.

    If `redline` is given (high direction only), an episode that reaches redline
    is marked severity "warning"; otherwise "caution". One episode per excursion
    (no nesting).

    Returns a list of dicts describing each episode.
    """
    def exceeds(v):
        return v >= threshold if direction == "high" else v <= threshold

    def recovered(v):
        if direction == "high":
            return v < (threshold - deadband)
        return v > (threshold + deadband)

    episodes = []
    in_episode = False
    ep = None
    recovering_since = None  # timestamp when the value first became "recovered"

    for ts, v in samples:
        if v is None:
            continue
        if not in_episode:
            if exceeds(v):
                in_episode = True
                ep = {"start": ts, "peak_ts": ts, "peak": v, "reached_redline": False}
                if redline is not None and v >= redline:
                    ep["reached_redline"] = True
                recovering_since = None
        else:
            # Track worst value
            better = (v > ep["peak"]) if direction == "high" else (v < ep["peak"])
            if better:
                ep["peak"] = v
                ep["peak_ts"] = ts
            if redline is not None and v >= redline:
                ep["reached_redline"] = True

            if recovered(v):
                if recovering_since is None:
                    recovering_since = ts
                elif _elapsed_s(recovering_since, ts) >= min_gap_s:
                    # Episode ended at the point recovery began.
                    ep["end"] = recovering_since
                    episodes.append(ep)
                    in_episode = False
                    ep = None
                    recovering_since = None
            else:
                # Popped back above the limit before the gap elapsed — same episode.
                recovering_since = None

    # Close a still-open episode at the last sample.
    if in_episode and ep is not None:
        ep["end"] = samples[-1][0]
        episodes.append(ep)

    return episodes


def detect_episodes(operation_id: int) -> list[Episode]:
    """Detect all exceedance episodes for one flight/operation.

    Computed on the fly from the raw data (not cached), so it always reflects
    the current thresholds. Scoped to a single operation — cheap.

    Covers: CHT (high), EGT (high), Oil Temp (high), Oil Pressure (low+high),
    Fuel Pressure (low+high). CHT/EGT use the hottest cylinder at each instant
    and support caution+redline (one episode at worst severity, no nesting).
    """
    t = get_thresholds()
    from efis_data_manager.config import load_config
    num_cyl = load_config().get("num_cylinders", 4)
    min_gap = t.get("episode_min_gap_s", 30)
    db_temp = t.get("episode_deadband_temp", 10)
    db_oilp = t.get("episode_deadband_oil_press", 5)
    db_fp = t.get("episode_deadband_fuel_press", 3)
    db_tas = t.get("episode_deadband_tas", 5)
    db_g = t.get("episode_deadband_g", 0.2)
    db_rpm = t.get("episode_deadband_rpm", 50)          # RPM
    db_cool = t.get("episode_deadband_cht_cooling", 5)  # °/min (rate deadband)

    # Resolve the aux mapping through the single resolver. Fuel-pressure
    # episodes run only when fuel_pressure is mapped, and read the resolved
    # channel column instead of a hardcoded aux3 (Req 2.2, 2.3).
    from efis_data_manager.aux_map import resolve_aux
    resolved_aux = resolve_aux()
    fuel_press_channel = None
    if "fuel_pressure" in resolved_aux:
        fuel_press_channel = resolved_aux["fuel_pressure"]["channel"]

    conn = get_db_connection()
    try:
        existing = {r[1] for r in conn.execute("PRAGMA table_info(fdl_data)")}
        cht_cols = [f"cht{i}" for i in range(1, num_cyl + 1) if f"cht{i}" in existing]
        egt_cols = [f"egt{i}" for i in range(1, num_cyl + 1) if f"egt{i}" in existing]
        # Select the mapped fuel-pressure channel (if any) aliased to a stable
        # name, so the episode logic below is channel-agnostic.
        fp_select = (
            f"{fuel_press_channel} AS fuel_pressure"
            if fuel_press_channel
            else "NULL AS fuel_pressure"
        )
        rows = conn.execute(
            f"""SELECT timestamp, oil_temp, oil_pressure, {fp_select},
                      indicated_airspeed, true_airspeed, g_load, rpm1,
                      cht1, cht2, cht3, cht4, cht5, cht6,
                      egt1, egt2, egt3, egt4, egt5, egt6
               FROM fdl_data WHERE operation_id = ? ORDER BY timestamp""",
            (operation_id,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return []

    def cyl_series(cols, min_valid):
        """Series of (timestamp, hottest-cylinder-value) ignoring dummies."""
        out = []
        for r in rows:
            vals = [r[c] for c in cols if r[c] is not None and r[c] >= min_valid]
            out.append((r["timestamp"], max(vals) if vals else None))
        return out

    def col_series(col, min_valid=None):
        out = []
        for r in rows:
            v = r[col]
            if v is not None and min_valid is not None and v < min_valid:
                v = None
            out.append((r["timestamp"], v))
        return out

    results: list[Episode] = []

    def add(param, direction, raw_eps, threshold, unit, deadband_label):
        for e in raw_eps:
            sev = "warning" if e.get("reached_redline") else "caution"
            peak = e["peak"]
            dur = _elapsed_s(e["start"], e["end"])
            if direction == "high":
                verb = "exceeded" if sev == "caution" else "REDLINE exceeded"
                msg = (f"{param} {verb} ({peak:.0f}{unit} peak, "
                       f"limit {threshold:.0f}{unit}, {dur:.0f}s)")
            else:
                msg = (f"{param} below limit ({peak:.0f}{unit} min, "
                       f"limit {threshold:.0f}{unit}, {dur:.0f}s)")
            results.append(Episode(
                parameter=param, direction=direction, severity=sev,
                start_timestamp=e["start"], peak_timestamp=e["peak_ts"],
                peak_value=peak, threshold=threshold, duration_s=dur, message=msg,
            ))

    # CHT (high, caution + redline)
    if cht_cols:
        s = cyl_series(cht_cols, 100)
        eps = _detect_episodes_series(s, "high", t["cht_caution"], db_temp,
                                      min_gap, redline=t["cht_redline"])
        add("CHT", "high", eps, t["cht_caution"], "°F", "temp")

        # CHT shock-cooling rate (caution). Build a windowed cooling-rate series
        # (°/min) from the hottest-cylinder CHT and open a high-direction episode
        # whenever the cooling rate exceeds cht_cooling_rate_max. Gated on the
        # threshold being set — missing/None means the feature is off (Req 12.4).
        cool_max = t.get("cht_cooling_rate_max")
        if cool_max is not None:
            rate_s = _cht_cooling_rate_series(s)
            eps = _detect_episodes_series(rate_s, "high", cool_max, db_cool, min_gap)
            add("CHT cooling", "high", eps, cool_max, "°/min", "cht_cooling")

    # EGT (high, caution + redline)
    if egt_cols:
        s = cyl_series(egt_cols, 100)
        eps = _detect_episodes_series(s, "high", t["egt_caution"], db_temp,
                                      min_gap, redline=t["egt_redline"])
        add("EGT", "high", eps, t["egt_caution"], "°F", "temp")

    # Oil temp (high, caution + redline)
    s = col_series("oil_temp", min_valid=50)
    eps = _detect_episodes_series(s, "high", t["oil_temp_caution"], db_temp,
                                  min_gap, redline=t["oil_temp_redline"])
    add("Oil Temp", "high", eps, t["oil_temp_caution"], "°F", "temp")

    # Oil pressure (low, single severity; and high, single severity)
    s = col_series("oil_pressure", min_valid=1)
    eps = _detect_episodes_series(s, "low", t["oil_pressure_low_cruise"], db_oilp, min_gap)
    add("Oil Pressure", "low", eps, t["oil_pressure_low_cruise"], "psi", "oil_press")
    eps = _detect_episodes_series(s, "high", t["oil_pressure_high"], db_oilp, min_gap)
    add("Oil Pressure", "high", eps, t["oil_pressure_high"], "psi", "oil_press")

    # Fuel pressure (low and high, single severity) — only when fuel_pressure
    # is mapped to a channel. Unmapped => skip entirely (Req 2.3).
    if fuel_press_channel:
        s = col_series("fuel_pressure", min_valid=0)
        eps = _detect_episodes_series(s, "low", t["fuel_pressure_low"], db_fp, min_gap)
        add("Fuel Pressure", "low", eps, t["fuel_pressure_low"], "psi", "fuel_press")
        eps = _detect_episodes_series(s, "high", t["fuel_pressure_high"], db_fp, min_gap)
        add("Fuel Pressure", "high", eps, t["fuel_pressure_high"], "psi", "fuel_press")

    # Vne (high, warning only) checked against TRUE airspeed. On many aircraft
    # (incl. the RV-8) Vne is a flutter/TAS limit, so IAS would be non-
    # conservative at altitude. 9999 placeholder disables until configured.
    # No caution tier for airspeed (Vne is a hard limit) — the episode is
    # marked warning by passing redline == threshold.
    vne = t.get("vne_tas_redline", 9999)
    if vne < 9999:
        s = col_series("true_airspeed", min_valid=0)
        eps = _detect_episodes_series(s, "high", vne, db_tas, min_gap, redline=vne)
        add("Vne (TAS)", "high", eps, vne, " kt", "tas")

    # Vne (IAS) — mirror of the TAS path against INDICATED airspeed. The mapper
    # makes the two Vne keys mutually exclusive (the inactive one holds the 9999
    # sentinel), so at most one of the TAS/IAS blocks fires (Req 5.5, 5.6).
    vne_ias = t.get("vne_ias_redline", 9999)
    if vne_ias < 9999:
        s = col_series("indicated_airspeed", min_valid=0)
        eps = _detect_episodes_series(s, "high", vne_ias, db_tas, min_gap, redline=vne_ias)
        add("Vne (IAS)", "high", eps, vne_ias, " kt", "tas")

    # RPM redline (engine over-speed), warning-only like Vne (redline == the
    # threshold). Gated on rpm_redline being SET — a missing/None value means the
    # feature is off (Req 11.1, 11.3). SID 123's Disabled_Value is mapped by
    # omitting the key from config, which surfaces here as None.
    rpm_redline = t.get("rpm_redline")
    if rpm_redline is not None:
        s = col_series("rpm1", min_valid=0)
        eps = _detect_episodes_series(s, "high", rpm_redline, db_rpm, min_gap,
                                      redline=rpm_redline)
        add("RPM", "high", eps, rpm_redline, " RPM", "rpm")

    # Positive G (high): caution + limit (warning at/above limit).
    s = col_series("g_load")
    eps = _detect_episodes_series(s, "high", t["g_pos_caution"], db_g, min_gap,
                                  redline=t["g_pos_limit"])
    add("G-load (+)", "high", eps, t["g_pos_caution"], "g", "g")

    # Negative G (low): caution + limit. _detect_episodes_series has no low-
    # direction redline, so open episodes at the caution and mark warning when
    # the episode's minimum reaches the (more negative) limit.
    gcaut = t.get("g_neg_caution")
    glim = t.get("g_neg_limit")
    if gcaut is not None:
        eps = _detect_episodes_series(s, "low", gcaut, db_g, min_gap)
        if glim is not None:
            for e in eps:
                if e["peak"] <= glim:   # peak is the min value for a low episode
                    e["reached_redline"] = True
        add("G-load (-)", "low", eps, gcaut, "g", "g")

    # Sort by start time
    results.sort(key=lambda e: e.start_timestamp)
    return results
