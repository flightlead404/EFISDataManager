"""Data export for EFIS flight analysis.

Provides CSV export of:
- Per-flight summary data
- Trend reports
- Oil consumption history
- Raw FDL data for a specific flight
"""

import csv
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from efis_data_manager.database import get_db_connection

logger = logging.getLogger(__name__)

# Default export directory
EXPORT_DIR = Path(os.path.expanduser("~/EFIS/Exports"))


def export_flight_summaries(output_path: Optional[str] = None) -> str:
    """Export all flight summaries to CSV.

    Columns: date, duration_min, airborne_min, max_alt, max_ias, max_gs,
    max_rpm, max_cht, max_egt, max_oil_temp, min_oil_press, fuel_used,
    avg_ff_cruise, hourmeter_start, hourmeter_end.

    Args:
        output_path: Optional output file path. Defaults to ~/EFIS/Exports/.

    Returns:
        Path to the exported file.
    """
    if output_path is None:
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = str(EXPORT_DIR / f"flight_summaries_{timestamp}.csv")

    conn = get_db_connection()
    try:
        flights = conn.execute(
            """SELECT f.*, o.source_filename
               FROM flights f
               JOIN operations o ON f.operation_id = o.id
               ORDER BY f.date"""
        ).fetchall()

        with open(output_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Date", "Source File", "Duration (min)", "Airborne (min)",
                "Max Altitude (ft)", "Max IAS (kts)", "Max GS (kts)",
                "Max RPM", "Max CHT (°F)", "Avg CHT Cruise (°F)",
                "Max EGT (°F)", "Avg EGT Cruise (°F)",
                "Max Oil Temp (°F)", "Min Oil Press (psi)",
                "Fuel Used (gal)", "Avg FF Cruise (GPH)",
                "Hourmeter Start", "Hourmeter End",
            ])
            for flight in flights:
                writer.writerow([
                    flight["date"],
                    flight["source_filename"] if "source_filename" in flight.keys() else "",
                    round(flight["duration_seconds"] / 60, 1),
                    round(flight["airborne_seconds"] / 60, 1),
                    flight["max_pressure_altitude"],
                    flight["max_indicated_airspeed"],
                    flight["max_ground_speed"],
                    flight["max_rpm"],
                    flight["max_cht"],
                    flight["avg_cht_cruise"],
                    flight["max_egt"],
                    flight["avg_egt_cruise"],
                    flight["max_oil_temp"],
                    flight["min_oil_pressure"],
                    flight["fuel_used"],
                    flight["avg_fuel_flow_cruise"],
                    flight["hourmeter_start"],
                    flight["hourmeter_end"],
                ])

        logger.info(f"Exported {len(flights)} flight summaries to {output_path}")
        return output_path

    finally:
        conn.close()


def export_trends(output_path: Optional[str] = None) -> str:
    """Export trend data to CSV.

    One row per flight with all tracked trend parameters.

    Args:
        output_path: Optional output file path.

    Returns:
        Path to the exported file.
    """
    from efis_data_manager.analysis import compute_trends

    if output_path is None:
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = str(EXPORT_DIR / f"trends_{timestamp}.csv")

    trends = compute_trends()
    if not trends:
        logger.warning("No trend data available for export.")
        return output_path

    # Build a unified table: one row per flight, columns for each trend param
    # Use the first series to get flight IDs and dates
    first_series = next(iter(trends.values()))
    flight_data = {}  # flight_id -> {date, hourmeter, param: value, param_avg: value}

    for param_key, series in trends.items():
        for point, avg in zip(series.points, series.rolling_avg):
            if point.flight_id not in flight_data:
                flight_data[point.flight_id] = {
                    "date": point.date,
                    "hourmeter": point.hourmeter,
                }
            flight_data[point.flight_id][param_key] = point.value
            flight_data[point.flight_id][f"{param_key}_rolling_avg"] = avg

    # Sort by date
    sorted_flights = sorted(flight_data.values(), key=lambda x: x["date"])

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        # Header
        param_headers = []
        for key, series in trends.items():
            param_headers.append(f"{series.name} ({series.unit})")
            param_headers.append(f"{series.name} Rolling Avg")
        writer.writerow(["Date", "Hourmeter"] + param_headers)

        # Data rows
        for fd in sorted_flights:
            row = [fd["date"], fd.get("hourmeter")]
            for key in trends:
                row.append(fd.get(key))
                row.append(fd.get(f"{key}_rolling_avg"))
            writer.writerow(row)

    logger.info(f"Exported trend data ({len(sorted_flights)} flights) to {output_path}")
    return output_path


def export_oil_consumption(output_path: Optional[str] = None) -> str:
    """Export oil consumption history to CSV.

    Args:
        output_path: Optional output file path.

    Returns:
        Path to the exported file.
    """
    from efis_data_manager.analysis import compute_oil_consumption_rolling

    if output_path is None:
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = str(EXPORT_DIR / f"oil_consumption_{timestamp}.csv")

    data = compute_oil_consumption_rolling()

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Date", "Hourmeter", "Oil Added (qt)", "Hours Since Last",
            "Rate (qt/hr)", "Rolling Avg Rate (qt/hr)",
        ])
        for entry in data:
            writer.writerow([
                entry["date"],
                entry["hourmeter"],
                entry["oil_added"],
                round(entry["hours_since_last"], 1),
                round(entry["rate"], 4),
                round(entry["rolling_avg_rate"], 4) if entry["rolling_avg_rate"] else "",
            ])

    logger.info(f"Exported oil consumption ({len(data)} periods) to {output_path}")
    return output_path


def export_flight_raw(operation_id: int, output_path: Optional[str] = None) -> str:
    """Export raw FDL data for a specific flight/operation.

    Full 1-second time-series data for detailed analysis in external tools.

    Args:
        operation_id: The operation to export.
        output_path: Optional output file path.

    Returns:
        Path to the exported file.
    """
    conn = get_db_connection()
    try:
        op = conn.execute(
            "SELECT * FROM operations WHERE id = ?", (operation_id,)
        ).fetchone()
        if not op:
            raise ValueError(f"Operation {operation_id} not found")

        if output_path is None:
            EXPORT_DIR.mkdir(parents=True, exist_ok=True)
            date_str = op["date"]
            output_path = str(EXPORT_DIR / f"fdl_raw_{date_str}_op{operation_id}.csv")

        rows = conn.execute(
            """SELECT * FROM fdl_data WHERE operation_id = ? ORDER BY timestamp""",
            (operation_id,)
        ).fetchall()

        if not rows:
            raise ValueError(f"No data for operation {operation_id}")

        # Get column names (skip id and operation_id)
        columns = [desc[0] for desc in conn.execute(
            "SELECT * FROM fdl_data LIMIT 1"
        ).description]
        export_cols = [c for c in columns if c not in ("id", "operation_id")]

        with open(output_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(export_cols)
            for row in rows:
                writer.writerow([row[c] for c in export_cols])

        logger.info(
            f"Exported {len(rows)} raw records for operation {operation_id} to {output_path}"
        )
        return output_path

    finally:
        conn.close()
