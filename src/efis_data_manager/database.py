# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

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

import hashlib
import logging
import os
import sqlite3
from dataclasses import fields as dataclass_fields
from datetime import datetime
from pathlib import Path
from typing import Optional

from efis_data_manager.config import load_config
from efis_data_manager.fdl_parser import (
    FDLFile,
    FDLRecord,
    Operation,
    segment_records,
)

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
    -- Informational provenance label of the contributing filename(s). NOT part
    -- of any identity/dedup key: GRT FDL filenames start at 0000 and cycle on
    -- every fresh USB. Nullable.
    source_filename TEXT,
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
    destination TEXT
);

-- Time-series data: 1-second FDL samples
CREATE TABLE IF NOT EXISTS fdl_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id INTEGER NOT NULL REFERENCES operations(id),
    timestamp TEXT NOT NULL,            -- ISO 8601
    -- Per-record content fingerprint (hash of all FDLRecord field values except
    -- the timestamp). Record identity = (timestamp, content_hash); re-import is
    -- a per-record no-op via INSERT OR IGNORE on that key. Filename-agnostic.
    content_hash TEXT,
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
    -- EIS aux channels. Meaning is user-configurable (see aux_map.py); the
    -- legacy default is aux1=amps, aux2=MAP, aux3=fuel pressure. aux4-aux6 are
    -- stored so owners with other wiring can map them.
    aux1 REAL,
    aux2 REAL,
    aux3 REAL,
    aux4 REAL,
    aux5 REAL,
    aux6 REAL
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
-- NOTE: the record-identity index UNIQUE(timestamp, content_hash) and the
-- operations derived-key index are created in _ensure_schema AFTER the
-- content_hash column is ensured, so legacy DBs (which add the column via
-- ALTER) don't fail here on a column that doesn't exist yet.
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
    for c in ("aux1", "aux2", "aux3", "aux4", "aux5", "aux6"):
        if fdl_cols and c not in fdl_cols:
            conn.execute(f"ALTER TABLE fdl_data ADD COLUMN {c} REAL")

    # FDL operation segmentation migration: add the per-record content_hash
    # column so dedup is record-level by (timestamp, content_hash).
    if fdl_cols and "content_hash" not in fdl_cols:
        conn.execute("ALTER TABLE fdl_data ADD COLUMN content_hash TEXT")

    # Rebuild the operations table if it still carries the legacy
    # UNIQUE(source_filename, start_time) constraint. SQLite cannot drop a
    # constraint in place, so create-copy-swap. Operation identity is now
    # derived from the deduped record stream, so the legacy filename-based
    # uniqueness is both wrong (filenames cycle) and can actively block correct
    # re-segmentation (one file -> many ops with the same source_filename).
    _rebuild_operations_table_if_legacy(conn)

    # Record-level and derived-key indexes (idempotent). Created here as well as
    # in SCHEMA_SQL so existing DBs that pre-date them get them on open.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_fdl_data_record_identity "
        "ON fdl_data(timestamp, content_hash)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_operations_derived_key "
        "ON operations(start_time, end_time, record_count)"
    )

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

    # One-time gap-based re-segmentation of already-imported data. Guarded by a
    # schema_info flag so it runs once and is safe to re-run. Operates on the
    # passed connection (no new connection -> no recursion).
    migrate_resegment_operations(conn)


def _operations_has_legacy_unique(conn: sqlite3.Connection) -> bool:
    """True if the ``operations`` table still has UNIQUE(source_filename,...)."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='operations'"
    ).fetchone()
    if not row or not row[0]:
        return False
    # The legacy table declared `source_filename TEXT NOT NULL` and
    # `UNIQUE(source_filename, start_time)`. The rebuilt table has neither.
    # Compare whitespace-insensitively so formatting differences don't matter.
    sql = row[0].replace(" ", "").replace("\n", "")
    return "UNIQUE(source_filename" in sql or "source_filenameTEXTNOTNULL" in sql


def _rebuild_operations_table_if_legacy(conn: sqlite3.Connection):
    """Create-copy-swap the operations table to drop the legacy unique key.

    Preserves every operations row and the fdl_data -> operations relationship
    (the ``id`` values are kept). Idempotent: a no-op once the table has already
    been rebuilt.
    """
    if not _operations_has_legacy_unique(conn):
        return

    logger.info("Rebuilding operations table to drop legacy "
                "UNIQUE(source_filename, start_time) constraint.")

    # The new table definition (mirrors SCHEMA_SQL, minus the legacy UNIQUE and
    # with a nullable source_filename). foreign_keys may be ON; defer them for
    # the swap so re-pointing fdl_data via the preserved ids stays valid.
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("""
            CREATE TABLE operations_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_filename TEXT,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                duration_seconds INTEGER NOT NULL,
                record_count INTEGER NOT NULL,
                has_flight INTEGER NOT NULL DEFAULT 0,
                airborne_seconds INTEGER NOT NULL DEFAULT 0,
                date TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                max_gps_altitude REAL,
                max_pressure_altitude REAL,
                max_ground_speed REAL,
                max_indicated_airspeed REAL,
                max_rpm REAL,
                avg_rpm_cruise REAL,
                max_cht REAL,
                avg_cht_cruise REAL,
                max_egt REAL,
                avg_egt_cruise REAL,
                max_oil_temp REAL,
                min_oil_pressure REAL,
                fuel_used REAL,
                avg_fuel_flow_cruise REAL,
                hourmeter_start REAL,
                hourmeter_end REAL,
                origin TEXT,
                destination TEXT
            )
        """)

        # Copy existing rows by explicit column list (order-independent, and
        # tolerant of older DBs missing origin/destination which were just
        # added above).
        old_cols = {r[1] for r in conn.execute("PRAGMA table_info(operations)")}
        new_cols = [
            "id", "source_filename", "start_time", "end_time",
            "duration_seconds", "record_count", "has_flight",
            "airborne_seconds", "date", "imported_at",
            "max_gps_altitude", "max_pressure_altitude", "max_ground_speed",
            "max_indicated_airspeed", "max_rpm", "avg_rpm_cruise", "max_cht",
            "avg_cht_cruise", "max_egt", "avg_egt_cruise", "max_oil_temp",
            "min_oil_pressure", "fuel_used", "avg_fuel_flow_cruise",
            "hourmeter_start", "hourmeter_end", "origin", "destination",
        ]
        copy_cols = [c for c in new_cols if c in old_cols]
        collist = ", ".join(copy_cols)
        conn.execute(
            f"INSERT INTO operations_new ({collist}) SELECT {collist} FROM operations"
        )

        conn.execute("DROP TABLE operations")
        conn.execute("ALTER TABLE operations_new RENAME TO operations")

        # Recreate the operations indexes dropped with the old table.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_operations_date ON operations(date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_operations_flight ON operations(has_flight)")
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


# Record identity: all FDLRecord fields EXCEPT the timestamp (which is the
# other half of the (timestamp, content_hash) key). Computed once from the
# dataclass definition so it stays in sync if fields are added.
_CONTENT_HASH_FIELDS = tuple(
    f.name for f in dataclass_fields(FDLRecord) if f.name != "timestamp"
)


def record_content_hash(record: FDLRecord) -> str:
    """Compute a stable content fingerprint for an FDL record.

    Hashes all FDLRecord field VALUES except the timestamp (the timestamp is the
    other component of the record identity key (timestamp, content_hash)). Two
    records are "the same sample" iff they share a timestamp AND an identical
    content hash. Filename and database row/operation id are deliberately NOT
    part of the hash.
    """
    parts = []
    for name in _CONTENT_HASH_FIELDS:
        parts.append(f"{name}={getattr(record, name)!r}")
    payload = "|".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def import_fdl_file(fdl: FDLFile) -> Optional[int]:
    """Import a parsed FDL file into the database (backward-compatible wrapper).

    Segments the file's records by timestamp gap and imports each contiguous
    run as its own operation (a single-session contiguous file still yields
    exactly one operation, preserving the prior behavior). Dedup is now
    record-level by (timestamp, content_hash), so re-importing the same samples
    — even under a different filename — is a no-op.

    Args:
        fdl: Parsed FDLFile object.

    Returns:
        The operation_id of the (first) imported operation, or None if the file
        was empty or every record was already present (no new operation).
    """
    if not fdl.records:
        logger.warning(f"Skipping empty FDL file: {fdl.source_filename}")
        return None

    first_op_id = None
    for run in segment_records(fdl.records):
        op_id = import_operation(run, source_label=fdl.source_filename)
        if op_id is not None and first_op_id is None:
            first_op_id = op_id
    return first_op_id


def import_fdl_files(fdl_files: list[FDLFile]) -> list[int]:
    """Import a candidate set of FDL files with cross-file pooling.

    This is the entry point the archiver uses. It implements the fixed importer
    F' (gap-based segmentation over the concatenated, timestamp-ordered
    candidate record set):

    1. Concatenate ALL records across the candidate files, tagging each with its
       source filename for provenance only.
    2. Sort by timestamp ascending (NEVER by filename - GRT filenames cycle and
       are not monotonic with time). A stable sort keeps same-timestamp records
       in file order.
    3. Segment the pooled stream into contiguous runs with ``segment_records``.
       A run may span a file boundary (rotated flight -> merged) or be one of
       several cut out of a single multi-session file (-> split).
    4. Import each run as one operation (record-level dedup handles boundary
       overlap and re-runs).

    Args:
        fdl_files: Parsed candidate ``FDLFile`` objects.

    Returns:
        The operation ids that received new data (pure no-op re-imports are
        omitted, matching ``import_operation``'s ``None`` return).
    """
    # Pool records, remembering each record's provenance filename.
    tagged: list[tuple[FDLRecord, str]] = []
    for fdl in fdl_files:
        for r in fdl.records:
            tagged.append((r, fdl.source_filename))

    if not tagged:
        return []

    # Stable sort by timestamp only (ties keep file order). Filenames are never
    # used for ordering.
    tagged.sort(key=lambda rt: rt[0].timestamp)

    # De-duplicate EXACT duplicate records (same timestamp + identical content)
    # before segmenting, so a record present in both the tail of one file and
    # the head of the next (rotated-file boundary overlap) is not counted twice
    # in a run's record_count. Keeps the first occurrence (its file label).
    deduped: list[tuple[FDLRecord, str]] = []
    seen_keys: set[tuple[str, str]] = set()
    for r, name in tagged:
        key = (r.timestamp.isoformat(), record_content_hash(r))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append((r, name))

    records = [r for r, _ in deduped]
    labels = [name for _, name in deduped]

    op_ids: list[int] = []
    index = 0
    for run in segment_records(records):
        run_labels = labels[index:index + len(run)]
        index += len(run)
        # Provenance label: the distinct contributing filename(s), in first-seen
        # order. Informational only (not an identity key).
        seen: list[str] = []
        for name in run_labels:
            if name and name not in seen:
                seen.append(name)
        source_label = ", ".join(seen) if seen else None

        op_id = import_operation(run, source_label=source_label)
        if op_id is not None:
            op_ids.append(op_id)
    return op_ids


def import_operation(records: list[FDLRecord],
                     source_label: Optional[str] = None) -> Optional[int]:
    """Import one operation (a contiguous run of records) into the database.

    Record-level dedup: each record is inserted with INSERT OR IGNORE on
    (timestamp, content_hash), so re-importing the same samples adds no rows.
    Operation identity is DERIVED from the deduped records: its convenience key
    is (start_time, end_time, record_count). If an operation with that key
    already exists it is reused (its summary refreshed); otherwise it is created.

    Args:
        records: A contiguous run of FDL records (one engine start/stop cycle),
            ordered ascending by timestamp.
        source_label: Informational provenance label (contributing filename(s)).
            NOT part of any identity key.

    Returns:
        The operation_id, or None if the run was empty or every record was
        already present under an existing operation (pure no-op re-import).
    """
    if not records:
        return None

    op = Operation(records=list(records), source_label=source_label)
    start_iso = op.start_time.isoformat()
    end_iso = op.end_time.isoformat()
    record_count = op.record_count

    conn = get_db_connection()
    try:
        # Find or create the operation on its derived key FIRST, so records can
        # be inserted with a valid operation_id (FK constraint is enabled).
        existing = conn.execute(
            """SELECT id FROM operations
               WHERE start_time = ? AND end_time = ? AND record_count = ?""",
            (start_iso, end_iso, record_count),
        ).fetchone()

        if existing is not None:
            operation_id = existing["id"]
        else:
            now = datetime.now().isoformat()
            cur = conn.execute(
                """INSERT INTO operations
                   (source_filename, start_time, end_time, duration_seconds,
                    record_count, has_flight, date, imported_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    source_label,
                    start_iso,
                    end_iso,
                    op.duration_seconds,
                    record_count,
                    1 if op.has_flight else 0,
                    op.date.isoformat(),
                    now,
                ),
            )
            operation_id = cur.lastrowid

        # Insert records (record-level dedup). Count how many are new.
        inserted = _insert_fdl_data(conn, operation_id, records)

        # Ensure every record in this run is attached to this operation (covers
        # the case where a record already existed under another operation but
        # now belongs here, and the dedup-skipped rows on re-import).
        _attach_run_records(conn, operation_id, records)

        # Compute summary stats for ALL operations (flights and ground ops).
        _compute_operation_summary(conn, operation_id, op)

        conn.commit()
        if existing is not None and inserted == 0:
            # Pure no-op re-import: the operation already existed and every
            # record was already present. Signal "nothing new" with None so
            # callers (archiver import counter) don't double-count.
            logger.info(
                f"Re-import no-op: op_id={operation_id} ({record_count} "
                f"records already present)"
            )
            return None
        logger.info(
            f"Imported operation op_id={operation_id} "
            f"(label={source_label}): {record_count} records, "
            f"flight={op.has_flight}, {inserted} new rows"
        )
        return operation_id

    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to import operation (label={source_label}): {e}")
        raise
    finally:
        conn.close()


def _attach_run_records(conn: sqlite3.Connection, operation_id: int,
                        records: list[FDLRecord]):
    """Point the fdl_data rows for this run at ``operation_id`` by identity key.

    Records were inserted (or already present) keyed by (timestamp,
    content_hash); here we set their operation_id so the run's rows belong to
    this operation. Safe to call repeatedly (idempotent)."""
    for r in records:
        conn.execute(
            "UPDATE fdl_data SET operation_id = ? WHERE timestamp = ? AND content_hash = ?",
            (operation_id, r.timestamp.isoformat(), record_content_hash(r)),
        )


def _insert_fdl_data(conn: sqlite3.Connection, operation_id: int,
                     records: list[FDLRecord]) -> int:
    """Batch insert FDL records into fdl_data (record-level dedup).

    Uses INSERT OR IGNORE on the (timestamp, content_hash) unique index so a
    record already present is skipped.

    Returns the number of rows actually inserted (new records)."""
    before = conn.total_changes
    rows = []
    for r in records:
        rows.append((
            operation_id,
            r.timestamp.isoformat(), record_content_hash(r), r.tick,
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
            r.aux1, r.aux2, r.aux3, r.aux4, r.aux5, r.aux6,
        ))

    conn.executemany(
        """INSERT OR IGNORE INTO fdl_data
           (operation_id, timestamp, content_hash, tick,
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
            aux1, aux2, aux3, aux4, aux5, aux6)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    return conn.total_changes - before


def _compute_operation_summary(conn: sqlite3.Connection, operation_id: int,
                               fdl):
    """Compute and store summary statistics for an operation.

    Accepts any object exposing ``records`` and ``has_flight`` (an ``FDLFile``
    or an ``Operation`` view over one contiguous run). Logic is unchanged.

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
# Cleanup / re-import migration (gap-based re-segmentation of existing data)
# ---------------------------------------------------------------------------

# schema_info flag that marks the re-segmentation migration as applied. Keyed so
# the migration runs once from the schema path and is safe to re-run manually.
_RESEGMENT_FLAG = "fdl_resegmentation_applied"

# Columns persisted in fdl_data that carry record CONTENT (used to reconstruct
# an FDLRecord and to compute a migration-local content hash). Excludes the row
# id, operation_id, timestamp (the identity's other half) and content_hash.
_FDL_DATA_CONTENT_COLUMNS = (
    "tick",
    "latitude", "longitude", "ground_speed", "track", "gps_altitude",
    "roll", "pitch", "mag_heading",
    "pressure_altitude", "indicated_altitude", "vertical_speed",
    "density_altitude", "indicated_airspeed", "true_airspeed", "g_load",
    "oat", "wind_speed", "wind_direction",
    "rpm1", "rpm2",
    "cht1", "cht2", "cht3", "cht4", "cht5", "cht6",
    "egt1", "egt2", "egt3", "egt4", "egt5", "egt6",
    "fuel_flow", "fuel_total", "oil_temp", "oil_pressure",
    "eis_volts", "volts1", "hourmeter", "internal_map",
    "aux1", "aux2", "aux3", "aux4", "aux5", "aux6",
)


def _row_content_hash(row: sqlite3.Row) -> str:
    """Content hash of a stored fdl_data row (migration-local, reproducible).

    Hashes only the persisted content columns (not every FDLRecord field, which
    the DB does not store). Reproducible from the DB alone, so the migration's
    dedup is internally consistent and stable across re-runs. It is NOT required
    to equal the import-time ``record_content_hash`` (that hashes the full
    FDLRecord); the two never need to collide because FDL files are moved off
    the USB on import.
    """
    present = set(row.keys())
    parts = []
    for col in _FDL_DATA_CONTENT_COLUMNS:
        value = row[col] if col in present else None
        parts.append(f"{col}={value!r}")
    payload = "|".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fdl_record_from_row(row: sqlite3.Row) -> FDLRecord:
    """Reconstruct an FDLRecord from a stored fdl_data row for summary compute.

    Only the persisted columns are populated; non-persisted FDLRecord fields
    stay at their defaults. This is sufficient for ``_compute_operation_summary``
    and ``segment_records`` (which only reads ``timestamp``)."""
    present = set(row.keys())
    kwargs = {"timestamp": datetime.fromisoformat(row["timestamp"])}
    record_field_names = {f.name for f in dataclass_fields(FDLRecord)}
    for col in _FDL_DATA_CONTENT_COLUMNS:
        if col in record_field_names and col in present:
            kwargs[col] = row[col]
    return FDLRecord(**kwargs)


def migrate_resegment_operations(conn: sqlite3.Connection,
                                 force: bool = False) -> dict:
    """Re-segment already-imported fdl_data into correct gap-based operations.

    The ``fdl_data`` rows are the source of truth (already in the DB), so this
    does NOT need the original CSV files. It:

    1. Backfills ``content_hash`` for any row missing it (migration-local hash).
    2. Reads all records ordered by timestamp and de-duplicates exact duplicates
       by (timestamp, content_hash) - handles old per-file double-imports and
       rotated-file boundary overlap. Duplicate rows are deleted (never the
       canonical copy).
    3. Runs ``segment_records`` over the deduped, timestamp-ordered stream to
       compute the correct set of operations.
    4. For each correct operation, finds or creates the operations row on its
       derived key (start_time, end_time, record_count). A healthy operation
       whose derived key already matches is reused unchanged (ids 3, 8, 14, 15,
       16, 17 survive untouched).
    5. Re-points every fdl_data row's operation_id to its correct operation
       (never deletes a fdl_data row - the time series is preserved).
    6. Deletes operations left with no records (the bad mega-ops 18, 19 and any
       other superseded operation), after their rows have been re-pointed.
    7. Recomputes summaries for every (re)created/kept operation.

    Runs inside a transaction and is idempotent: a second run finds every
    operation already at its correct derived key, re-points nothing, deletes
    nothing, and makes no changes. Guarded by a ``schema_info`` flag so the
    automatic (schema-path) invocation runs once; pass ``force=True`` to run it
    explicitly (e.g. the orchestrator's real-DB cleanup) regardless of the flag.

    Args:
        conn: An open connection (the caller owns commit/close). The migration
            commits its own transaction on success.
        force: Run even if the applied flag is already set.

    Returns:
        A dict summary: {"applied", "operations_before", "operations_after",
        "operations_created", "operations_deleted", "duplicate_rows_removed",
        "records_total"}.
    """
    result = {
        "applied": False,
        "operations_before": 0,
        "operations_after": 0,
        "operations_created": 0,
        "operations_deleted": 0,
        "duplicate_rows_removed": 0,
        "records_total": 0,
    }

    flag = conn.execute(
        "SELECT value FROM schema_info WHERE key = ?", (_RESEGMENT_FLAG,)
    ).fetchone()
    already_applied = flag is not None and flag[0] == "1"
    if already_applied and not force:
        logger.info("Re-segmentation migration already applied; skipping.")
        return result

    try:
        result["operations_before"] = conn.execute(
            "SELECT COUNT(*) FROM operations"
        ).fetchone()[0]

        # 1. Backfill content_hash for rows missing it (migration-local hash).
        missing = conn.execute(
            "SELECT * FROM fdl_data WHERE content_hash IS NULL"
        ).fetchall()
        for row in missing:
            conn.execute(
                "UPDATE fdl_data SET content_hash = ? WHERE id = ?",
                (_row_content_hash(row), row["id"]),
            )

        # 2. Read all rows ordered by timestamp; de-duplicate exact duplicates
        #    by (timestamp, content_hash). Keep the lowest id as canonical.
        all_rows = conn.execute(
            "SELECT * FROM fdl_data ORDER BY timestamp, id"
        ).fetchall()

        seen: dict[tuple, int] = {}
        canonical_rows = []
        dup_ids = []
        for row in all_rows:
            key = (row["timestamp"], row["content_hash"])
            if key in seen:
                dup_ids.append(row["id"])
            else:
                seen[key] = row["id"]
                canonical_rows.append(row)

        if dup_ids:
            conn.executemany(
                "DELETE FROM fdl_data WHERE id = ?", [(i,) for i in dup_ids]
            )
        result["duplicate_rows_removed"] = len(dup_ids)
        result["records_total"] = len(canonical_rows)

        # 3. Segment the deduped stream into correct operations.
        records = [_fdl_record_from_row(r) for r in canonical_rows]
        runs = segment_records(records)

        # Pair each run with the fdl_data ids of its (canonical) rows, in order.
        row_ids = [r["id"] for r in canonical_rows]
        now = datetime.now().isoformat()
        correct_op_ids = set()
        idx = 0
        for run in runs:
            run_row_ids = row_ids[idx:idx + len(run)]
            idx += len(run)

            op = Operation(records=run)
            start_iso = op.start_time.isoformat()
            end_iso = op.end_time.isoformat()
            record_count = op.record_count

            # 4. Find or create the operation on its derived key.
            existing = conn.execute(
                """SELECT id FROM operations
                   WHERE start_time = ? AND end_time = ? AND record_count = ?""",
                (start_iso, end_iso, record_count),
            ).fetchone()
            if existing is not None:
                operation_id = existing["id"]
            else:
                cur = conn.execute(
                    """INSERT INTO operations
                       (source_filename, start_time, end_time, duration_seconds,
                        record_count, has_flight, date, imported_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        None,
                        start_iso,
                        end_iso,
                        op.duration_seconds,
                        record_count,
                        1 if op.has_flight else 0,
                        op.date.isoformat(),
                        now,
                    ),
                )
                operation_id = cur.lastrowid
                result["operations_created"] += 1

            correct_op_ids.add(operation_id)

            # 5. Re-point this run's fdl_data rows to the correct operation.
            conn.executemany(
                "UPDATE fdl_data SET operation_id = ? WHERE id = ?",
                [(operation_id, rid) for rid in run_row_ids],
            )

            # 7. Recompute the summary for this operation.
            _compute_operation_summary(conn, operation_id, op)

        # 6. Delete operations that no longer own any records (superseded /
        #    bad mega-ops). Their rows were re-pointed above, so nothing is
        #    lost; this never touches fdl_data.
        empty_ops = conn.execute(
            """SELECT id FROM operations
               WHERE id NOT IN (
                   SELECT DISTINCT operation_id FROM fdl_data
                   WHERE operation_id IS NOT NULL
               )"""
        ).fetchall()
        for (op_id,) in [(r["id"],) for r in empty_ops]:
            conn.execute("DELETE FROM operations WHERE id = ?", (op_id,))
        result["operations_deleted"] = len(empty_ops)

        result["operations_after"] = conn.execute(
            "SELECT COUNT(*) FROM operations"
        ).fetchone()[0]

        # Mark applied (idempotency guard for the automatic invocation).
        conn.execute(
            """INSERT INTO schema_info (key, value) VALUES (?, '1')
               ON CONFLICT(key) DO UPDATE SET value = '1'""",
            (_RESEGMENT_FLAG,),
        )

        conn.commit()
        result["applied"] = True
        logger.info(
            "Re-segmentation migration: "
            f"{result['operations_before']} -> {result['operations_after']} "
            f"operations ({result['operations_created']} created, "
            f"{result['operations_deleted']} deleted), "
            f"{result['duplicate_rows_removed']} duplicate rows removed, "
            f"{result['records_total']} records."
        )
        return result

    except Exception as e:
        conn.rollback()
        logger.error(f"Re-segmentation migration failed: {e}")
        raise


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

def delete_operation(operation_id: int) -> bool:
    """Delete an operation and all its time-series data.

    Returns True if an operation was deleted.
    """
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM fdl_data WHERE operation_id = ?", (operation_id,))
        cur = conn.execute("DELETE FROM operations WHERE id = ?", (operation_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


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


def get_max_operation_hourmeter() -> Optional[float]:
    """Return the maximum non-null ``hourmeter_end`` across all operations.

    This is Current_Engine_Hours (Req 2.1): the most recent known engine time.
    ``MAX(...)`` in SQLite ignores NULLs and yields NULL (-> Python ``None``)
    when no operation has a non-null ``hourmeter_end`` (Req 2.6), which is the
    "not computable" input.

        SELECT MAX(hourmeter_end) FROM operations
    """
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT MAX(hourmeter_end) FROM operations"
        ).fetchone()
        return row[0] if row is not None else None
    finally:
        conn.close()
