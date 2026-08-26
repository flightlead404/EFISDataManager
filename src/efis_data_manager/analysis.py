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
    "cht_redline": 420,       # Never exceed
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

    # Fuel
    "fuel_flow_lean_threshold": 6.0,  # GPH — below this, likely LOP

    # Performance
    "g_load_caution": 3.0,
    "g_load_limit": 3.8,       # Utility category

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
    """Comprehensive per-flight statistics."""
    flight_id: int
    operation_id: int
    date: str
    duration_seconds: int
    airborne_seconds: int

    # Performance
    max_ground_speed: Optional[float] = None
    max_indicated_airspeed: Optional[float] = None
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

    # Oil
    max_oil_temp: Optional[float] = None
    min_oil_pressure_cruise: Optional[float] = None
    avg_oil_temp_cruise: Optional[float] = None
    avg_oil_pressure_cruise: Optional[float] = None

    # Fuel
    fuel_used: Optional[float] = None
    avg_fuel_flow_cruise: Optional[float] = None
    max_fuel_flow: Optional[float] = None

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


def get_flight_stats(flight_id: int) -> Optional[FlightStats]:
    """Compute detailed statistics for a specific flight.

    Queries the raw FDL data from the database and computes comprehensive
    per-flight metrics including cylinder-level analysis.

    Args:
        flight_id: The flights.id to analyze.

    Returns:
        FlightStats dataclass or None if flight not found.
    """
    conn = get_db_connection()
    try:
        # Get flight metadata
        flight_row = conn.execute(
            "SELECT * FROM flights WHERE id = ?", (flight_id,)
        ).fetchone()
        if not flight_row:
            return None

        operation_id = flight_row["operation_id"]

        # Get all FDL data for this operation
        rows = conn.execute(
            """SELECT * FROM fdl_data WHERE operation_id = ?
               ORDER BY timestamp""",
            (operation_id,)
        ).fetchall()

        if not rows:
            return None

        stats = FlightStats(
            flight_id=flight_id,
            operation_id=operation_id,
            date=flight_row["date"],
            duration_seconds=flight_row["duration_seconds"],
            airborne_seconds=flight_row["airborne_seconds"],
            hourmeter_start=flight_row["hourmeter_start"],
            hourmeter_end=flight_row["hourmeter_end"],
            fuel_used=flight_row["fuel_used"],
        )

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
        stats.avg_ias_cruise = _avg_col(cruise, "indicated_airspeed")
        stats.max_altitude = _max_col(rows, "pressure_altitude")
        stats.max_g_load = _max_col(rows, "g_load")
        stats.min_g_load = _min_col(rows, "g_load")

        # Engine
        stats.max_rpm = _max_col(rows, "rpm1")
        stats.avg_rpm_cruise = _avg_col(cruise, "rpm1")
        stats.max_fuel_flow = _max_col(rows, "fuel_flow")
        stats.avg_fuel_flow_cruise = _avg_col(cruise, "fuel_flow")

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
            conn.execute("SELECT id FROM flights ORDER BY date DESC").fetchall()
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
        flights = conn.execute(
            """SELECT f.id, f.date, f.hourmeter_end, f.max_cht, f.avg_cht_cruise,
                      f.max_egt, f.avg_egt_cruise, f.max_oil_temp,
                      f.min_oil_pressure, f.avg_fuel_flow_cruise, f.fuel_used,
                      f.airborne_seconds
               FROM flights f
               ORDER BY f.date, f.hourmeter_end""",
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
        flights = conn.execute(
            """SELECT f.*, o.source_filename
               FROM flights f JOIN operations o ON f.operation_id = o.id
               ORDER BY f.date DESC""",
        ).fetchall()
    finally:
        pass  # Keep conn open for timestamp lookups below

    trends = compute_trends(window_hours)

    for flight in flights:
        fid = flight["id"]
        fdate = flight["date"]
        op_id = flight["operation_id"]

        # --- Absolute exceedances (with timestamp lookup) ---
        if flight["max_cht"] and flight["max_cht"] >= thresholds["cht_redline"]:
            ts = _find_exceedance_timestamp(conn, op_id, "cht", thresholds["cht_redline"])
            anomalies.append(Anomaly(
                flight_id=fid, date=fdate, parameter="CHT",
                severity="warning",
                message=f"CHT reached {flight['max_cht']:.0f}°F (redline {thresholds['cht_redline']}°F)",
                value=flight["max_cht"], threshold=thresholds["cht_redline"],
                timestamp=ts,
            ))
        elif flight["max_cht"] and flight["max_cht"] >= thresholds["cht_caution"]:
            ts = _find_exceedance_timestamp(conn, op_id, "cht", thresholds["cht_caution"])
            anomalies.append(Anomaly(
                flight_id=fid, date=fdate, parameter="CHT",
                severity="caution",
                message=f"CHT reached {flight['max_cht']:.0f}°F (caution {thresholds['cht_caution']}°F)",
                value=flight["max_cht"], threshold=thresholds["cht_caution"],
                timestamp=ts,
            ))

        if flight["max_egt"] and flight["max_egt"] >= thresholds["egt_redline"]:
            ts = _find_exceedance_timestamp(conn, op_id, "egt", thresholds["egt_redline"])
            anomalies.append(Anomaly(
                flight_id=fid, date=fdate, parameter="EGT",
                severity="warning",
                message=f"EGT reached {flight['max_egt']:.0f}°F (redline {thresholds['egt_redline']}°F)",
                value=flight["max_egt"], threshold=thresholds["egt_redline"],
                timestamp=ts,
            ))

        if flight["max_oil_temp"] and flight["max_oil_temp"] >= thresholds["oil_temp_caution"]:
            sev = "warning" if flight["max_oil_temp"] >= thresholds["oil_temp_redline"] else "caution"
            ts = _find_exceedance_timestamp(conn, op_id, "oil_temp", thresholds["oil_temp_caution"])
            anomalies.append(Anomaly(
                flight_id=fid, date=fdate, parameter="Oil Temp",
                severity=sev,
                message=f"Oil temp reached {flight['max_oil_temp']:.0f}°F",
                value=flight["max_oil_temp"], threshold=thresholds["oil_temp_caution"],
                timestamp=ts,
            ))

        if flight["min_oil_pressure"] and flight["min_oil_pressure"] < thresholds["oil_pressure_low_cruise"]:
            sev = "warning" if flight["min_oil_pressure"] < thresholds["oil_pressure_low"] else "caution"
            ts = _find_min_timestamp(conn, op_id, "oil_pressure", thresholds["oil_pressure_low_cruise"])
            anomalies.append(Anomaly(
                flight_id=fid, date=fdate, parameter="Oil Pressure",
                severity=sev,
                message=f"Oil pressure dropped to {flight['min_oil_pressure']:.0f} psi in cruise",
                value=flight["min_oil_pressure"], threshold=thresholds["oil_pressure_low_cruise"],
                timestamp=ts,
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

    if stats.max_g_load and stats.max_g_load >= thresholds["g_load_caution"]:
        stats.alerts.append(f"WARNING: G-load {stats.max_g_load:.2f}g")


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
    """One oil consumption measurement period (between two oil additions)."""
    start_date: str
    end_date: str
    start_hourmeter: float
    end_hourmeter: float
    hours_between: float
    oil_added_quarts: float
    rate_quarts_per_hour: float


def compute_oil_consumption() -> list[OilConsumptionPeriod]:
    """Compute oil consumption rate between each oil addition.

    Uses logbook entries where Oil Added > 0. The consumption rate for each
    period is: quarts_added / hours_flown_since_last_addition.

    Returns:
        List of OilConsumptionPeriod sorted by date.
    """
    conn = get_db_connection()
    try:
        # Get all logbook entries with oil additions, ordered by hourmeter
        oil_entries = conn.execute(
            """SELECT date, hourmeter, oil_added
               FROM logbook
               WHERE oil_added IS NOT NULL AND oil_added > 0
                     AND hourmeter IS NOT NULL
               ORDER BY hourmeter""",
        ).fetchall()
    finally:
        conn.close()

    if len(oil_entries) < 2:
        return []

    periods = []
    for i in range(1, len(oil_entries)):
        prev = oil_entries[i - 1]
        curr = oil_entries[i]

        hours_between = curr["hourmeter"] - prev["hourmeter"]
        if hours_between <= 0:
            continue

        # The oil added at 'curr' is what was consumed since 'prev'
        rate = curr["oil_added"] / hours_between

        periods.append(OilConsumptionPeriod(
            start_date=prev["date"],
            end_date=curr["date"],
            start_hourmeter=prev["hourmeter"],
            end_hourmeter=curr["hourmeter"],
            hours_between=hours_between,
            oil_added_quarts=curr["oil_added"],
            rate_quarts_per_hour=rate,
        ))

    return periods


def compute_oil_consumption_rolling(window_hours: float = 25.0) -> list[dict]:
    """Compute rolling average oil consumption rate.

    Args:
        window_hours: Rolling window in engine hours.

    Returns:
        List of dicts with {"date", "hourmeter", "rate", "rolling_avg_rate"}.
    """
    periods = compute_oil_consumption()
    if not periods:
        return []

    results = []
    for i, p in enumerate(periods):
        # Find all periods within window
        window_rates = []
        window_hours_total = 0
        window_oil_total = 0
        for j in range(i, -1, -1):
            pj = periods[j]
            if p.end_hourmeter - pj.start_hourmeter > window_hours:
                break
            window_rates.append(pj.rate_quarts_per_hour)
            window_hours_total += pj.hours_between
            window_oil_total += pj.oil_added_quarts

        # Rolling average: total oil consumed / total hours in window
        rolling_rate = window_oil_total / window_hours_total if window_hours_total > 0 else None

        results.append({
            "date": p.end_date,
            "hourmeter": p.end_hourmeter,
            "rate": p.rate_quarts_per_hour,
            "rolling_avg_rate": rolling_rate,
            "oil_added": p.oil_added_quarts,
            "hours_since_last": p.hours_between,
        })

    return results
