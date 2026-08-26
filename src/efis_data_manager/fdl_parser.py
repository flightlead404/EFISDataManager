"""FDL (Flight Data Logger) CSV parser for GRT HXr EFIS.

Parses GRT FDL CSV files into structured records suitable for database import
and analysis. Each FDL file represents one engine start/stop cycle (operation).

File format:
- First row: header with column names
- Subsequent rows: 1-second samples of all flight parameters
- Fields may be empty when data is unavailable (e.g. IAS at low speeds)
"""

import csv
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, date, time, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Canonical column names mapped to clean Python attribute names.
# This handles the GRT header naming conventions and provides stable keys
# regardless of minor firmware header changes.
COLUMN_MAP = {
    "Version": "version",
    "Tick": "tick",
    "Date": "date",
    "Time": "time",
    "Latitude": "latitude",
    "Longitude": "longitude",
    "Ground Speed": "ground_speed",
    "Track": "track",
    "Magnetic Variation": "mag_variation",
    "GPS Altitude": "gps_altitude",
    "Desired Track": "desired_track",
    "Cross Track Error": "cross_track_error",
    "Roll": "roll",
    "Pitch": "pitch",
    "Magnetic Heading": "mag_heading",
    "Magnetic Field Heading": "mag_field_heading",
    "Pressure Altitude": "pressure_altitude",
    "Indicated Altitude": "indicated_altitude",
    "Vertical Speed": "vertical_speed",
    "Density Altitude": "density_altitude",
    "Indicated Airspeed": "indicated_airspeed",
    "True Airspeed": "true_airspeed",
    "Normal Acceleration": "g_load",
    "OAT": "oat",
    "Wind Speed": "wind_speed",
    "Wind Direction": "wind_direction",
    "RPM1/N1": "rpm1",
    "RPM2/N2": "rpm2",
    "CHT1": "cht1",
    "CHT2": "cht2",
    "CHT3": "cht3",
    "CHT4": "cht4",
    "CHT5": "cht5",
    "CHT6": "cht6",
    "EGT1": "egt1",
    "EGT2": "egt2",
    "EGT3": "egt3",
    "EGT4": "egt4",
    "EGT5": "egt5",
    "EGT6": "egt6",
    "Fuel Flow": "fuel_flow",
    "Fuel Total": "fuel_total",
    "Carb Temp": "carb_temp",
    "Oil Temp": "oil_temp",
    "Oil Pressure": "oil_pressure",
    "Coolant Temp": "coolant_temp",
    "Hourmeter": "hourmeter",
    "EIS Volts": "eis_volts",
    "Aux1": "aux1",
    "Aux2": "aux2",
    "Aux3": "aux3",
    "Aux4": "aux4",
    "Aux5": "aux5",
    "Aux6": "aux6",
    "Internal MAP": "internal_map",
    "R-N1": "r_n1",
    "R-N2": "r_n2",
    "Analog1": "analog1",
    "Analog2": "analog2",
    "Analog3": "analog3",
    "Analog4": "analog4",
    "Analog5": "analog5",
    "Analog6": "analog6",
    "Analog7": "analog7",
    "Analog8": "analog8",
    "Volts1": "volts1",
    "Volts2": "volts2",
    "Volts3": "volts3",
}

# Fields that should be parsed as floats (everything numeric except version/tick/date/time)
FLOAT_FIELDS = {
    "latitude", "longitude", "ground_speed", "track", "mag_variation",
    "gps_altitude", "desired_track", "cross_track_error", "roll", "pitch",
    "mag_heading", "mag_field_heading", "pressure_altitude", "indicated_altitude",
    "vertical_speed", "density_altitude", "indicated_airspeed", "true_airspeed",
    "g_load", "oat", "wind_speed", "wind_direction",
    "rpm1", "rpm2",
    "cht1", "cht2", "cht3", "cht4", "cht5", "cht6",
    "egt1", "egt2", "egt3", "egt4", "egt5", "egt6",
    "fuel_flow", "fuel_total", "carb_temp", "oil_temp", "oil_pressure",
    "coolant_temp", "hourmeter", "eis_volts",
    "aux1", "aux2", "aux3", "aux4", "aux5", "aux6",
    "internal_map", "r_n1", "r_n2",
    "analog1", "analog2", "analog3", "analog4",
    "analog5", "analog6", "analog7", "analog8",
    "volts1", "volts2", "volts3",
}

# Integer fields
INT_FIELDS = {"version", "tick"}

# Minimum IAS (knots) to consider airborne
AIRBORNE_IAS_THRESHOLD = 40.0

# Minimum RPM to consider engine running
ENGINE_RUNNING_RPM = 400


@dataclass
class FDLRecord:
    """A single 1-second FDL sample."""
    timestamp: datetime
    tick: int = 0
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    ground_speed: Optional[float] = None
    track: Optional[float] = None
    mag_variation: Optional[float] = None
    gps_altitude: Optional[float] = None
    desired_track: Optional[float] = None
    cross_track_error: Optional[float] = None
    roll: Optional[float] = None
    pitch: Optional[float] = None
    mag_heading: Optional[float] = None
    mag_field_heading: Optional[float] = None
    pressure_altitude: Optional[float] = None
    indicated_altitude: Optional[float] = None
    vertical_speed: Optional[float] = None
    density_altitude: Optional[float] = None
    indicated_airspeed: Optional[float] = None
    true_airspeed: Optional[float] = None
    g_load: Optional[float] = None
    oat: Optional[float] = None
    wind_speed: Optional[float] = None
    wind_direction: Optional[float] = None
    rpm1: Optional[float] = None
    rpm2: Optional[float] = None
    cht1: Optional[float] = None
    cht2: Optional[float] = None
    cht3: Optional[float] = None
    cht4: Optional[float] = None
    cht5: Optional[float] = None
    cht6: Optional[float] = None
    egt1: Optional[float] = None
    egt2: Optional[float] = None
    egt3: Optional[float] = None
    egt4: Optional[float] = None
    egt5: Optional[float] = None
    egt6: Optional[float] = None
    fuel_flow: Optional[float] = None
    fuel_total: Optional[float] = None
    carb_temp: Optional[float] = None
    oil_temp: Optional[float] = None
    oil_pressure: Optional[float] = None
    coolant_temp: Optional[float] = None
    hourmeter: Optional[float] = None
    eis_volts: Optional[float] = None
    aux1: Optional[float] = None
    aux2: Optional[float] = None
    aux3: Optional[float] = None
    aux4: Optional[float] = None
    aux5: Optional[float] = None
    aux6: Optional[float] = None
    internal_map: Optional[float] = None
    r_n1: Optional[float] = None
    r_n2: Optional[float] = None
    analog1: Optional[float] = None
    analog2: Optional[float] = None
    analog3: Optional[float] = None
    analog4: Optional[float] = None
    analog5: Optional[float] = None
    analog6: Optional[float] = None
    analog7: Optional[float] = None
    analog8: Optional[float] = None
    volts1: Optional[float] = None
    volts2: Optional[float] = None
    volts3: Optional[float] = None

    @property
    def engine_running(self) -> bool:
        """True if engine RPM indicates running."""
        return (self.rpm1 or 0) > ENGINE_RUNNING_RPM

    @property
    def airborne(self) -> bool:
        """True if IAS suggests airborne."""
        return (self.indicated_airspeed or 0) > AIRBORNE_IAS_THRESHOLD


@dataclass
class FDLFile:
    """Parsed contents of a single FDL CSV file (one operation)."""
    source_filename: str
    source_path: str
    records: list[FDLRecord] = field(default_factory=list)

    @property
    def record_count(self) -> int:
        return len(self.records)

    @property
    def start_time(self) -> Optional[datetime]:
        return self.records[0].timestamp if self.records else None

    @property
    def end_time(self) -> Optional[datetime]:
        return self.records[-1].timestamp if self.records else None

    @property
    def duration_seconds(self) -> int:
        if self.start_time and self.end_time:
            return int((self.end_time - self.start_time).total_seconds())
        return 0

    @property
    def has_flight(self) -> bool:
        """True if any records show airborne (IAS above threshold)."""
        return any(r.airborne for r in self.records)

    @property
    def date(self) -> Optional[date]:
        return self.start_time.date() if self.start_time else None


def parse_fdl_file(filepath: str) -> FDLFile:
    """Parse a GRT FDL CSV file into structured records.

    Args:
        filepath: Path to the FDL CSV file.

    Returns:
        FDLFile with all parsed records.

    Raises:
        ValueError: If the file has no valid header or is empty.
    """
    filepath = str(filepath)
    filename = os.path.basename(filepath)

    records = []
    field_indices = {}  # column_attr_name -> column_index

    with open(filepath, "r", newline="") as f:
        reader = csv.reader(f)

        # Parse header
        header = next(reader, None)
        if not header:
            raise ValueError(f"Empty file: {filename}")

        # Map header columns to our attribute names
        for idx, col_name in enumerate(header):
            col_name = col_name.strip()
            if col_name in COLUMN_MAP:
                field_indices[COLUMN_MAP[col_name]] = idx

        if "date" not in field_indices or "time" not in field_indices:
            raise ValueError(
                f"FDL file missing required Date/Time columns: {filename}"
            )

        # Parse data rows
        for row_num, row in enumerate(reader, start=2):
            try:
                record = _parse_row(row, field_indices)
                if record is not None:
                    records.append(record)
            except Exception as e:
                logger.warning(f"{filename} row {row_num}: parse error: {e}")
                continue

    logger.info(
        f"Parsed {filename}: {len(records)} records"
        f"{', ' + str(records[-1].timestamp - records[0].timestamp) if len(records) > 1 else ''}"
    )
    return FDLFile(source_filename=filename, source_path=filepath, records=records)


def _parse_row(row: list[str], field_indices: dict[str, int]) -> Optional[FDLRecord]:
    """Parse a single CSV row into an FDLRecord.

    Returns None if the row can't be parsed (missing date/time).
    """
    def _get(attr: str) -> str:
        idx = field_indices.get(attr)
        if idx is None or idx >= len(row):
            return ""
        return row[idx].strip()

    # Parse timestamp (required)
    date_str = _get("date")
    time_str = _get("time")
    if not date_str or not time_str:
        return None

    try:
        timestamp = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None

    # Parse tick
    tick_str = _get("tick")
    tick = int(tick_str) if tick_str else 0

    # Parse all float fields
    kwargs = {"timestamp": timestamp, "tick": tick}
    for attr in FLOAT_FIELDS:
        val_str = _get(attr)
        if val_str:
            try:
                kwargs[attr] = float(val_str)
            except ValueError:
                kwargs[attr] = None
        else:
            kwargs[attr] = None

    return FDLRecord(**kwargs)


def parse_fdl_filename(filename: str) -> Optional[dict]:
    """Extract metadata from an FDL filename.

    GRT FDL naming: "GRT FDL NNNN.CSV" where NNNN is a zero-padded sequence number.

    Returns:
        Dict with {"sequence": int} or None if not a valid FDL filename.
    """
    match = re.match(r"GRT FDL (\d+)\.CSV", filename, re.IGNORECASE)
    if match:
        return {"sequence": int(match.group(1))}
    return None
