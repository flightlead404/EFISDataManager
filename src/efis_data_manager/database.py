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
-- Operations: one per FDL file (each engine start/stop cycle).
-- Summary stats computed for ALL operations (flights and ground ops).
-- Cruise averages are only populated for flights (has_flight=1).
CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_filename TEXT NOT NULL,
    start_time TEXT NOT NULL,           -- ISO 8601
    end_time TEXT NOT NULL,             -- ISO 8601
    duration_seconds INTEGER NOT NULL,
    record_count INTEGER NOT NULL,
    has_flight INTEGER NOT NULL DEFAULT 0,  -- 1 if airborne segment detected
    airborne_seconds INTEGER NOT NULL DEFAULT 0,
    date TEXT NOT NULL,                 -- YYYY-MM-DD for easy grouping
    imported_at TEXT NOT NULL,          -- when this was imported
    -- GPS/nav (mostly relevant for flights)
    max_gps_altitude REAL,
    max_pressure_altitude REAL,
    max_ground_speed REAL,
    max_indicated_airspeed REAL,
    -- Engine (computed for all operations)
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
    -- Logbook enrichment (matched by hourmeter range on logbook import)
    origin TEXT,
    destination TEXT,
    UNIQUE(source_filename, start_time)
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
    internal_map REAL,
    -- EIS aux channels (this install: aux1=amps, aux2=MAP, aux3=fuel pressure)
    aux1 REAL,
    aux2 REAL,
    aux3 REAL
);

-- Oil events: single source of truth for oil tracking (changes + additions).
-- The logbook CSV is a transient enrichment source (not stored): its oil
-- additions feed this table, and its origin/destination enrich operations.
CREATE TABLE IF NOT EXISTS oil_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,                 -- YYYY-MM-DD
    hourmeter REAL NOT NULL,            -- tach/hourmeter at the event
    event_type TEXT NOT NULL,           -- 'change' or 'addition'
    quarts_added REAL NOT NULL DEFAULT 0,
    quarts_low REAL NOT NULL DEFAULT 0, -- how low before a change (counts as consumed)
    note TEXT,
    source TEXT NOT NULL DEFAULT 'manual',  -- 'manual' or 'logbook'
    created_at TEXT NOT NULL,
    UNIQUE(date, hourmeter, source, event_type)
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_oil_events_hourmeter ON oil_events(hourmeter);
CREATE INDEX IF NOT EXISTS idx_fdl_data_operation ON fdl_data(operation_id);
CREATE INDEX IF NOT EXISTS idx_fdl_data_timestamp ON fdl_data(timestamp);
CREATE INDEX IF NOT EXISTS idx_fdl_data_op_ts ON fdl_data(operation_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_operations_date ON operations(date);
CREATE INDEX IF NOT EXISTS idx_operations_flight ON operations(has_flight);
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

    # Lightweight migrations: add columns that may be missing on older DBs.
    # ALTER TABLE ADD COLUMN is a no-op-safe pattern guarded by a column check.
    def _columns(table):
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

    op_cols = _columns("operations")
    if "origin" not in op_cols:
        conn.execute("ALTER TABLE operations ADD COLUMN origin TEXT")
    if "destination" not in op_cols:
        conn.execute("ALTER TABLE operations ADD COLUMN destination TEXT")

    oil_cols = _columns("oil_events")
    if oil_cols and "source" not in oil_cols:
        conn.execute("ALTER TABLE oil_events ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")

    fdl_cols = _columns("fdl_data")
    for c in ("aux1", "aux2", "aux3"):
        if fdl_cols and c not in fdl_cols:
            conn.execute(f"ALTER TABLE fdl_data ADD COLUMN {c} REAL")

    # Drop the legacy logbook table if it exists (now a transient source)
    conn.execute("DROP TABLE IF EXISTS logbook")

    conn.commit()

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

        # Insert operation (stats filled in by _compute_operation_summary)
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

        # Compute summary stats for ALL operations (flights and ground ops).
        # Cruise averages are only meaningful for flights.
        _compute_operation_summary(conn, operation_id, fdl)

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
            r.aux1, r.aux2, r.aux3,
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
            eis_volts, volts1, hourmeter, internal_map,
            aux1, aux2, aux3)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )


def _compute_operation_summary(conn: sqlite3.Connection, operation_id: int,
                               fdl: FDLFile):
    """Compute and store summary statistics for an operation.

    Runs for ALL operations (flights and ground ops). Max/min values and
    fuel/hourmeter are computed for everything. Cruise averages are only
    computed for flights (has_flight=1) since ground ops have no cruise phase.
    """
    records = fdl.records
    is_flight = fdl.has_flight

    # Find airborne segment (empty for ground ops)
    airborne_records = [r for r in records if r.airborne]
    airborne_seconds = len(airborne_records)  # 1-second samples

    # Cruise: airborne and RPM stable (exclude climb/descent by VS threshold).
    # Only relevant for flights; ground ops never have airborne records.
    cruise_records = [
        r for r in airborne_records
        if r.vertical_speed is not None and abs(r.vertical_speed) < 300
        and r.rpm1 is not None and r.rpm1 > 1800
    ] if is_flight else []

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

    # For ground ops, min_oil_pressure uses all running records (no airborne set)
    if not is_flight:
        oil_press_running = [
            r.oil_pressure for r in records
            if r.oil_pressure and r.oil_pressure > 0
            and r.rpm1 and r.rpm1 > 400
        ]
        min_oil_pressure = min(oil_press_running) if oil_press_running else None

    conn.execute(
        """UPDATE operations SET
            airborne_seconds = ?,
            max_gps_altitude = ?, max_pressure_altitude = ?,
            max_ground_speed = ?, max_indicated_airspeed = ?,
            max_rpm = ?, avg_rpm_cruise = ?,
            max_cht = ?, avg_cht_cruise = ?,
            max_egt = ?, avg_egt_cruise = ?,
            max_oil_temp = ?, min_oil_pressure = ?,
            fuel_used = ?, avg_fuel_flow_cruise = ?,
            hourmeter_start = ?, hourmeter_end = ?
           WHERE id = ?""",
        (
            airborne_seconds,
            max_gps_alt, max_press_alt, max_gs, max_ias,
            max_rpm, avg_rpm_cruise, max_cht, avg_cht_cruise,
            max_egt, avg_egt_cruise, max_oil_temp, min_oil_pressure,
            fuel_used, avg_ff_cruise, hm_start, hm_end,
            operation_id,
        )
    )


# ---------------------------------------------------------------------------
# Logbook import
# ---------------------------------------------------------------------------

def import_logbook_csv(filepath: str) -> dict:
    """Import a GRT Logbook.csv as an enrichment source (not stored as a table).

    The logbook is used for two things:
      1. Oil additions -> oil_events (source='logbook'), deduped
      2. Origin/destination -> matched to the corresponding operation by
         hourmeter range and stored on the operations row. Multiple legs that
         fall within one operation's hourmeter span have their airports
         concatenated (e.g. "KCCO -> KLZU -> KCCO").

    Args:
        filepath: Path to Logbook.csv file.

    Returns:
        Dict with counts: {"oil_events_created", "operations_enriched",
                           "legs_read", "errors"}
    """
    import csv

    results = {"oil_events_created": 0, "operations_enriched": 0,
               "legs_read": 0, "legs_skipped_old": 0, "errors": []}
    conn = get_db_connection()
    now = datetime.now().isoformat()

    # Only import rows above the watermark (highest hourmeter previously imported).
    # Handles 'download all' vs 'download new only' and avoids reprocessing.
    watermark = get_logbook_watermark()
    max_hourmeter_seen = watermark

    try:
        # Preload operations with a valid hourmeter range for matching
        ops = conn.execute(
            """SELECT id, hourmeter_start, hourmeter_end
               FROM operations
               WHERE hourmeter_start IS NOT NULL AND hourmeter_end IS NOT NULL"""
        ).fetchall()

        # legs_by_op accumulates (origin, destination) per operation id
        legs_by_op = {}

        with open(filepath, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row_num, row in enumerate(reader, start=2):
                try:
                    date = row.get("Date", "").strip()
                    if not date:
                        continue

                    origin = row.get("Origin", "").strip() or None
                    destination = row.get("Destination", "").strip() or None
                    hourmeter = _parse_float(row.get("Hourmeter", ""))
                    oil_added = _parse_float(row.get("Oil Added", ""))

                    # Skip entries at/below the watermark (already imported)
                    if hourmeter is not None and hourmeter <= watermark:
                        results["legs_skipped_old"] += 1
                        continue

                    results["legs_read"] += 1
                    if hourmeter is not None and hourmeter > max_hourmeter_seen:
                        max_hourmeter_seen = hourmeter

                    # 1. Oil additions -> oil_events (deduped by unique constraint)
                    if oil_added and oil_added > 0 and hourmeter is not None:
                        conn.execute(
                            """INSERT OR IGNORE INTO oil_events
                               (date, hourmeter, event_type, quarts_added,
                                quarts_low, note, source, created_at)
                               VALUES (?,?,?,?,?,?,?,?)""",
                            (date, hourmeter, "addition", oil_added, 0.0,
                             "from logbook", "logbook", now)
                        )
                        if conn.total_changes:
                            results["oil_events_created"] += 1

                    # 2. Match leg to an operation by hourmeter range
                    if hourmeter is not None and (origin or destination):
                        for op in ops:
                            if op["hourmeter_start"] <= hourmeter <= op["hourmeter_end"]:
                                legs_by_op.setdefault(op["id"], []).append(
                                    (origin, destination)
                                )
                                break

                except Exception as e:
                    results["errors"].append(f"Row {row_num}: {e}")

        # Apply enrichment: concatenate airports per operation
        for op_id, legs in legs_by_op.items():
            airports = []
            for origin, destination in legs:
                if origin and (not airports or airports[-1] != origin):
                    airports.append(origin)
                if destination:
                    airports.append(destination)
            route = " -> ".join(airports) if airports else None
            first_origin = legs[0][0]
            last_dest = legs[-1][1]
            conn.execute(
                "UPDATE operations SET origin = ?, destination = ? WHERE id = ?",
                (first_origin, last_dest, op_id)
            )
            # Store full route in destination if multiple legs
            if len(legs) > 1 and route:
                conn.execute(
                    "UPDATE operations SET destination = ? WHERE id = ?",
                    (route, op_id)
                )
            results["operations_enriched"] += 1

        conn.commit()
        logger.info(
            f"Logbook enrichment: {results['legs_read']} new legs read "
            f"({results['legs_skipped_old']} below watermark {watermark}), "
            f"{results['oil_events_created']} oil events created, "
            f"{results['operations_enriched']} operations enriched"
        )
    except Exception as e:
        conn.rollback()
        results["errors"].append(str(e))
        conn.close()
        return results
    finally:
        conn.close()

    # Advance the watermark to the highest hourmeter seen (after successful commit)
    if max_hourmeter_seen > watermark:
        set_logbook_watermark(max_hourmeter_seen)
        results["watermark"] = max_hourmeter_seen

    return results


def _parse_float(val: str) -> Optional[float]:
    """Parse a string to float, returning None for empty/invalid."""
    if not val or not val.strip():
        return None
    try:
        return float(val.strip())
    except ValueError:
        return None


def _parse_int(val: str) -> Optional[int]:
    """Parse a string to int, returning None for empty/invalid."""
    if not val or not val.strip():
        return None
    try:
        return int(val.strip())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Oil events
# ---------------------------------------------------------------------------

def get_logbook_watermark() -> float:
    """Return the highest hourmeter imported from a logbook (0.0 if none).

    Logbook imports only process rows with hourmeter above this watermark,
    which handles both 'download all' and 'download new only' cases and avoids
    re-processing already-imported entries.
    """
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT value FROM schema_info WHERE key = 'logbook_hourmeter_watermark'"
        ).fetchone()
        if row and row["value"]:
            try:
                return float(row["value"])
            except ValueError:
                return 0.0
        return 0.0
    finally:
        conn.close()


def set_logbook_watermark(hourmeter: float):
    """Set the logbook import watermark (highest imported hourmeter)."""
    conn = get_db_connection()
    try:
        conn.execute(
            """INSERT INTO schema_info (key, value) VALUES ('logbook_hourmeter_watermark', ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (str(hourmeter),)
        )
        conn.commit()
    finally:
        conn.close()


def reset_logbook_watermark():
    """Reset the logbook watermark to 0 (forces a full re-import next time)."""
    set_logbook_watermark(0.0)


def add_oil_event(date: str, hourmeter: float, event_type: str,
                  quarts_added: float = 0.0, quarts_low: float = 0.0,
                  note: str = "", source: str = "manual") -> int:
    """Add an oil change or addition event.

    Args:
        date: YYYY-MM-DD.
        hourmeter: Tach/hourmeter reading at the event.
        event_type: 'change' or 'addition'.
        quarts_added: Quarts of oil added.
        quarts_low: For a change, how many quarts low it was before (counts
            as consumed since the last event).
        note: Optional free-text note.
        source: 'manual' (user-entered) or 'logbook' (auto from logbook import).

    Returns:
        The new oil_events.id, or -1 if it was a duplicate (ignored).
    """
    if event_type not in ("change", "addition"):
        raise ValueError(f"event_type must be 'change' or 'addition', got {event_type!r}")

    conn = get_db_connection()
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO oil_events
               (date, hourmeter, event_type, quarts_added, quarts_low, note, source, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (date, hourmeter, event_type, quarts_added, quarts_low, note, source,
             datetime.now().isoformat())
        )
        conn.commit()
        return cur.lastrowid if cur.rowcount else -1
    finally:
        conn.close()


def delete_oil_event(event_id: int):
    """Delete an oil event by id."""
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM oil_events WHERE id = ?", (event_id,))
        conn.commit()
    finally:
        conn.close()


def get_oil_events(cutoff_date: str = "") -> list[dict]:
    """Return oil events ordered by hourmeter, optionally filtered by cutoff date.

    Args:
        cutoff_date: If set (YYYY-MM-DD), only events on/after this date.

    Returns:
        List of dicts.
    """
    conn = get_db_connection()
    try:
        if cutoff_date:
            rows = conn.execute(
                "SELECT * FROM oil_events WHERE date >= ? ORDER BY hourmeter",
                (cutoff_date,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM oil_events ORDER BY hourmeter"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
