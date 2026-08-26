"""SQLite database for EFIS flight data storage and analysis.

Schema design:
- operations: One row per FDL file (engine start/stop cycle)
- flights: One row per airborne operation (subset of operations that flew)
- fdl_data: Time-series engine/flight data at 1-second resolution
- logbook: Imported GRT logbook entries

Indexes optimized for:
- Time-range queries (flight duration, trend windows)
- Per-flight aggregation (GROUP BY operation_id)
- Engine parameter analysis (CHT, EGT, oil)
"""

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from efis_data_manager.config import load_config
from efis_data_manager.fdl_parser import FDLFile, FDLRecord

logger = logging.getLogger(__name__)

# Database lives alongside logs
DB_DIR = Path(os.path.expanduser("~/EFIS/DataManagerLogs"))
DB_PATH = DB_DIR / "efis_data.sqlite"

SCHEMA_VERSION = 1

SCHEMA_SQL = """
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_info (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Operations: one per FDL file (each engine start/stop cycle)
CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_filename TEXT NOT NULL,
    start_time TEXT NOT NULL,           -- ISO 8601
    end_time TEXT NOT NULL,             -- ISO 8601
    duration_seconds INTEGER NOT NULL,
    record_count INTEGER NOT NULL,
    has_flight INTEGER NOT NULL DEFAULT 0,  -- 1 if airborne segment detected
    date TEXT NOT NULL,                 -- YYYY-MM-DD for easy grouping
    imported_at TEXT NOT NULL,          -- when this was imported
    UNIQUE(source_filename, start_time)
);

-- Flights: summary for operations that include airborne time
CREATE TABLE IF NOT EXISTS flights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id INTEGER NOT NULL REFERENCES operations(id),
    date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL,
    airborne_seconds INTEGER NOT NULL,
    -- GPS/nav
    max_gps_altitude REAL,
    max_pressure_altitude REAL,
    max_ground_speed REAL,
    max_indicated_airspeed REAL,
    -- Engine
    max_rpm REAL,
    avg_rpm_cruise REAL,
    max_cht REAL,
    avg_cht_cruise REAL,
    max_egt REAL,
    avg_egt_cruise REAL,
    max_oil_temp REAL,
    min_oil_pressure REAL,
    -- Fuel
    fuel_used REAL,                     -- gallons (fuel_total delta)
    avg_fuel_flow_cruise REAL,
    -- Hourmeter
    hourmeter_start REAL,
    hourmeter_end REAL,
    UNIQUE(operation_id)
);

-- Time-series data: 1-second FDL samples
CREATE TABLE IF NOT EXISTS fdl_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id INTEGER NOT NULL REFERENCES operations(id),
    timestamp TEXT NOT NULL,            -- ISO 8601
    tick INTEGER,
    -- Position
    latitude REAL,
    longitude REAL,
    ground_speed REAL,
    track REAL,
    gps_altitude REAL,
    -- Attitude/nav
    roll REAL,
    pitch REAL,
    mag_heading REAL,
    pressure_altitude REAL,
    indicated_altitude REAL,
    vertical_speed REAL,
    density_altitude REAL,
    indicated_airspeed REAL,
    true_airspeed REAL,
    g_load REAL,
    -- Environment
    oat REAL,
    wind_speed REAL,
    wind_direction REAL,
    -- Engine
    rpm1 REAL,
    rpm2 REAL,
    cht1 REAL,
    cht2 REAL,
    cht3 REAL,
    cht4 REAL,
    cht5 REAL,
    cht6 REAL,
    egt1 REAL,
    egt2 REAL,
    egt3 REAL,
    egt4 REAL,
    egt5 REAL,
    egt6 REAL,
    fuel_flow REAL,
    fuel_total REAL,
    oil_temp REAL,
    oil_pressure REAL,
    -- Electrical
    eis_volts REAL,
    volts1 REAL,
    -- Other
    hourmeter REAL,
    internal_map REAL
);

-- Logbook entries (imported from Logbook.csv)
CREATE TABLE IF NOT EXISTS logbook (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    origin TEXT,
    destination TEXT,
    duration_str TEXT,
    duration_hours REAL,
    fuel_used REAL,
    departure_time TEXT,
    arrival_time TEXT,
    hourmeter REAL,
    flight_type TEXT,
    passengers INTEGER,
    fuel_added REAL,
    oil_added REAL,
    imported_at TEXT NOT NULL,
    UNIQUE(date, departure_time, origin)
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_fdl_data_operation ON fdl_data(operation_id);
CREATE INDEX IF NOT EXISTS idx_fdl_data_timestamp ON fdl_data(timestamp);
CREATE INDEX IF NOT EXISTS idx_fdl_data_op_ts ON fdl_data(operation_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_operations_date ON operations(date);
CREATE INDEX IF NOT EXISTS idx_flights_date ON flights(date);
CREATE INDEX IF NOT EXISTS idx_logbook_date ON logbook(date);
"""


def get_db_connection() -> sqlite3.Connection:
    """Get a connection to the EFIS data database, creating if needed."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection):
    """Create tables if they don't exist, handle migrations."""
    conn.executescript(SCHEMA_SQL)

    # Check/set schema version
    cur = conn.execute(
        "SELECT value FROM schema_info WHERE key = 'schema_version'"
    )
    row = cur.fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_info (key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION))
        )
        conn.commit()


def import_fdl_file(fdl: FDLFile) -> Optional[int]:
    """Import a parsed FDL file into the database.

    Creates an operation record, imports all time-series data, and if the
    operation includes a flight, computes and stores flight summary stats.

    Args:
        fdl: Parsed FDLFile object.

    Returns:
        The operation_id if imported, None if already exists or empty.
    """
    if not fdl.records:
        logger.warning(f"Skipping empty FDL file: {fdl.source_filename}")
        return None

    conn = get_db_connection()
    try:
        # Check for duplicate
        cur = conn.execute(
            "SELECT id FROM operations WHERE source_filename = ? AND start_time = ?",
            (fdl.source_filename, fdl.start_time.isoformat())
        )
        if cur.fetchone():
            logger.info(f"Already imported: {fdl.source_filename}")
            return None

        # Insert operation
        now = datetime.now().isoformat()
        cur = conn.execute(
            """INSERT INTO operations
               (source_filename, start_time, end_time, duration_seconds,
                record_count, has_flight, date, imported_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fdl.source_filename,
                fdl.start_time.isoformat(),
                fdl.end_time.isoformat(),
                fdl.duration_seconds,
                fdl.record_count,
                1 if fdl.has_flight else 0,
                fdl.date.isoformat(),
                now,
            )
        )
        operation_id = cur.lastrowid

        # Batch insert time-series data
        _insert_fdl_data(conn, operation_id, fdl.records)

        # If this operation includes a flight, compute summary
        if fdl.has_flight:
            _compute_flight_summary(conn, operation_id, fdl)

        conn.commit()
        logger.info(
            f"Imported {fdl.source_filename}: op_id={operation_id}, "
            f"{fdl.record_count} records, flight={fdl.has_flight}"
        )
        return operation_id

    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to import {fdl.source_filename}: {e}")
        raise
    finally:
        conn.close()


def _insert_fdl_data(conn: sqlite3.Connection, operation_id: int,
                     records: list[FDLRecord]):
    """Batch insert FDL records into fdl_data table."""
    rows = []
    for r in records:
        rows.append((
            operation_id, r.timestamp.isoformat(), r.tick,
            r.latitude, r.longitude, r.ground_speed, r.track, r.gps_altitude,
            r.roll, r.pitch, r.mag_heading,
            r.pressure_altitude, r.indicated_altitude, r.vertical_speed,
            r.density_altitude, r.indicated_airspeed, r.true_airspeed, r.g_load,
            r.oat, r.wind_speed, r.wind_direction,
            r.rpm1, r.rpm2,
            r.cht1, r.cht2, r.cht3, r.cht4, r.cht5, r.cht6,
            r.egt1, r.egt2, r.egt3, r.egt4, r.egt5, r.egt6,
            r.fuel_flow, r.fuel_total, r.oil_temp, r.oil_pressure,
            r.eis_volts, r.volts1, r.hourmeter, r.internal_map,
        ))

    conn.executemany(
        """INSERT INTO fdl_data
           (operation_id, timestamp, tick,
            latitude, longitude, ground_speed, track, gps_altitude,
            roll, pitch, mag_heading,
            pressure_altitude, indicated_altitude, vertical_speed,
            density_altitude, indicated_airspeed, true_airspeed, g_load,
            oat, wind_speed, wind_direction,
            rpm1, rpm2,
            cht1, cht2, cht3, cht4, cht5, cht6,
            egt1, egt2, egt3, egt4, egt5, egt6,
            fuel_flow, fuel_total, oil_temp, oil_pressure,
            eis_volts, volts1, hourmeter, internal_map)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )


def _compute_flight_summary(conn: sqlite3.Connection, operation_id: int,
                            fdl: FDLFile):
    """Compute and store flight summary statistics."""
    from efis_data_manager.fdl_parser import AIRBORNE_IAS_THRESHOLD

    records = fdl.records

    # Find airborne segment
    airborne_records = [r for r in records if r.airborne]
    airborne_seconds = len(airborne_records)  # 1-second samples

    # Cruise: airborne and RPM stable (exclude climb/descent by VS threshold)
    cruise_records = [
        r for r in airborne_records
        if r.vertical_speed is not None and abs(r.vertical_speed) < 300
        and r.rpm1 is not None and r.rpm1 > 1800
    ]

    # Compute stats
    max_gps_alt = max((r.gps_altitude for r in records if r.gps_altitude), default=None)
    max_press_alt = max((r.pressure_altitude for r in records if r.pressure_altitude), default=None)
    max_gs = max((r.ground_speed for r in records if r.ground_speed), default=None)
    max_ias = max((r.indicated_airspeed for r in records if r.indicated_airspeed), default=None)
    max_rpm = max((r.rpm1 for r in records if r.rpm1), default=None)

    # CHT/EGT max across all cylinders
    all_chts = []
    all_egts = []
    for r in records:
        for v in [r.cht1, r.cht2, r.cht3, r.cht4, r.cht5, r.cht6]:
            if v is not None and v > 100:  # Filter noise/cold readings
                all_chts.append(v)
        for v in [r.egt1, r.egt2, r.egt3, r.egt4, r.egt5, r.egt6]:
            if v is not None and v > 100:
                all_egts.append(v)

    max_cht = max(all_chts) if all_chts else None
    max_egt = max(all_egts) if all_egts else None

    # Cruise averages
    avg_rpm_cruise = None
    avg_cht_cruise = None
    avg_egt_cruise = None
    avg_ff_cruise = None
    if cruise_records:
        rpm_vals = [r.rpm1 for r in cruise_records if r.rpm1]
        avg_rpm_cruise = sum(rpm_vals) / len(rpm_vals) if rpm_vals else None

        cht_vals = []
        egt_vals = []
        for r in cruise_records:
            for v in [r.cht1, r.cht2, r.cht3, r.cht4, r.cht5, r.cht6]:
                if v is not None and v > 100:
                    cht_vals.append(v)
            for v in [r.egt1, r.egt2, r.egt3, r.egt4, r.egt5, r.egt6]:
                if v is not None and v > 100:
                    egt_vals.append(v)
        avg_cht_cruise = sum(cht_vals) / len(cht_vals) if cht_vals else None
        avg_egt_cruise = sum(egt_vals) / len(egt_vals) if egt_vals else None

        ff_vals = [r.fuel_flow for r in cruise_records if r.fuel_flow and r.fuel_flow > 0]
        avg_ff_cruise = sum(ff_vals) / len(ff_vals) if ff_vals else None

    # Oil
    max_oil_temp = max((r.oil_temp for r in records if r.oil_temp and r.oil_temp > 50), default=None)
    oil_press_airborne = [r.oil_pressure for r in airborne_records if r.oil_pressure and r.oil_pressure > 0]
    min_oil_pressure = min(oil_press_airborne) if oil_press_airborne else None

    # Fuel used (delta of fuel_total)
    fuel_totals = [r.fuel_total for r in records if r.fuel_total is not None]
    fuel_used = None
    if len(fuel_totals) >= 2:
        fuel_used = fuel_totals[0] - fuel_totals[-1]  # fuel_total counts down
        if fuel_used < 0:
            fuel_used = abs(fuel_used)  # handle either direction

    # Hourmeter
    hourmeters = [r.hourmeter for r in records if r.hourmeter is not None]
    hm_start = hourmeters[0] if hourmeters else None
    hm_end = hourmeters[-1] if hourmeters else None

    conn.execute(
        """INSERT INTO flights
           (operation_id, date, start_time, end_time, duration_seconds,
            airborne_seconds, max_gps_altitude, max_pressure_altitude,
            max_ground_speed, max_indicated_airspeed, max_rpm, avg_rpm_cruise,
            max_cht, avg_cht_cruise, max_egt, avg_egt_cruise,
            max_oil_temp, min_oil_pressure, fuel_used, avg_fuel_flow_cruise,
            hourmeter_start, hourmeter_end)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            operation_id, fdl.date.isoformat(),
            fdl.start_time.isoformat(), fdl.end_time.isoformat(),
            fdl.duration_seconds, airborne_seconds,
            max_gps_alt, max_press_alt, max_gs, max_ias,
            max_rpm, avg_rpm_cruise, max_cht, avg_cht_cruise,
            max_egt, avg_egt_cruise, max_oil_temp, min_oil_pressure,
            fuel_used, avg_ff_cruise, hm_start, hm_end,
        )
    )
