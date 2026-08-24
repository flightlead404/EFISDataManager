# Phase 0 — USB Drive Inspection Findings

**Drive:** `/Volumes/EFIS` — 30 GB FAT32, 18 GB used, 12 GB free  
**Aircraft:** N488BF (from Settings.bak field 1062)  
**Date of inspection:** August 2026

---

## 1. Top-Level Drive Structure

```
/Volumes/EFIS/
├── DEMO-YYYYMMDD-HHMMSS.LOG        # Demo recording sessions (base file)
├── DEMO-YYYYMMDD-HHMMSS+N.LOG      # Continuation files for same session
├── Settings.bak                      # EFIS settings backup (key=value text, checksummed)
├── State.bak                         # EFIS state snapshot (key=value text, checksummed)
├── WP.bak                            # Waypoint backup
├── Plan.bak                          # Flight plan backup (lat/lon waypoints with names)
├── HHXRUp-proc.dat                   # EFIS firmware update file (7.6 MB)
├── NAV-proc.DB                       # Nav database "processed" (9.9 MB)
├── NAV.DB                            # Nav database (7.9 MB)
├── SNAP####.PNG                       # Pilot-saved screenshots (manual save from EFIS)
├── GRTCHARTS/                        # GRT chart marker directory (required flag, contents disposable)
│   └── E:GRTCHARTS/                  # Empty directory (Windows artifact, safe to remove)
├── ChartData/                        # Seattle Avionics chart data (~17 GB)
│   ├── ScannedCharts.sqlite          # Chart metadata DB (cycle, expiry, URLs, passwords)
│   ├── LO/                           # IFR Low Altitude chart tiles (~1.0 GB)
│   │   └── W062..W180/              # Longitude-based tile directories
│   ├── SEC/                          # VFR Sectional chart tiles (~3.8 GB)
│   │   └── W060..W180/
│   ├── Plates/                       # Approach plates/airport diagrams (~3.6 GB)
│   │   ├── Plates.sqlite            # Plate index DB (airports, charts, geo-ref)
│   │   ├── Plates.sqlite.chk
│   │   ├── Airports.txt, Charts.txt, Cities.txt, Plates.txt, States.txt, Plates.xml
│   │   ├── US/                       # US plate images (PNG)
│   │   ├── MG/, MH/, MM/, MN/, MR/, MS/, MZ/  # Mexico/other region subdirs
│   │   └── (no FG/ directory present)
│   └── E:ChartData/                  # Duplicate/alternate structure (~8.4 GB)
│       ├── ScannedCharts.sqlite
│       ├── LO/, SEC/, Plates/       # Same structure as parent
│       └── (appears to be an older or backup copy)
└── System Volume Information/        # Windows system folder (FAT32 artifact)
```

## 2. DEMO File Convention (Engine Telemetry)

### Naming Pattern
```
DEMO-{YYYYMMDD}-{HHMMSS}.LOG          # Base file for a recording session
DEMO-{YYYYMMDD}-{HHMMSS}+{N}.LOG      # Continuation file N (1-based)
```

- Date/time in filename = recording start time (local time, based on EFIS clock)
- Base file (no `+N`) is always smaller (~270-310 KB) — contains full settings dump as header
- Continuation files are capped at ~6 MB each (6,291,000–6,294,000 bytes typical)
- Final continuation file in a session is typically smaller (partial fill)

### Session Summary (29 sessions on drive)

| Date | Files | Est. Duration | Notes |
|------|-------|---------------|-------|
| 2025-11-12 | 16 (base + 15) | ~1.5 hr | First session on drive |
| 2026-02-15 | 17 (base + 16) | ~1.5 hr | |
| 2026-03-07 | 5 | ~30 min | |
| 2026-03-13 | 2 sessions (1 + 18 files) | short + ~1.5 hr | |
| 2026-03-15 | 22 | ~2 hr | Longest session |
| 2026-03-21 | 13 | ~1 hr | |
| 2026-03-26 | 3 sessions (4+11+6) | Multiple flights | |
| 2026-03-27 | 6 sessions (2+3+11+10+3+16) | Multiple flights | Busy day |
| 2026-03-28 | 5 sessions (1+3+13+2+4) | Multiple flights | |
| 2026-04-24 | 9 | ~45 min | |
| 2026-05-24 | 2 sessions (1+1) | Very short | |
| 2026-05-30 | 1 | Very short | |
| 2026-06-06 | 3 sessions (1+7+9) | ~1 hr total | Most recent |

**Total:** 210 files, ~2.1 GB

### Binary Format Analysis

The DEMO file is a **binary record stream**, not a flat binary blob. Record structure observed:

**Record header:** `{type:1}{unknown:2}{seq_id:2}{flags:3}{payload_len:1}{payload:N}`

- **Type 0x07** — Settings/configuration (key=value ASCII strings)
  - These appear at the beginning of every file (base and continuation)
  - Contain the full EFIS Settings.bak content embedded in record format
  - The `seq_id` field increments (e.g., `0x0d6c`, `0x0d6d`, ...)
  
- **Type 0x02** — Engine data packet (packed binary)
  - Appears after GPS sentences
  - Contains multi-byte packed values for engine parameters
  - Example decoded (tentative): `49 feff fe 05 42 00 fd 01 11 01 15 01 0f 00 55 00 59 04 c7 04 bc 04 cc 04 dd 00 53 00 92...`
  - Pattern suggests: RPM(2) + multiple 2-byte channel values (CHT×4, EGT×4, oil, fuel, etc.)

- **Type 0x09** — Short data records (5-byte payload typical)
  - Appear frequently between other record types
  - Likely individual sensor updates or status frames

- **Type 0x13** — Longer records (often 19-20 bytes)
  - Contains `7e6a8d` pattern (appears to be a marker/signature)
  - Possibly encrypted/compressed AHRS data or multi-sensor fusion

- **Type 0x0b/0x14** — Variable-length records containing NMEA GPS sentences

- **Type 0x04** — Short (36-byte) records, possibly display/status updates

- **Type 0x01** — Variable records (often contain `7ffffe` or `7ffffd` patterns)
  - Likely AHRS/attitude data (pitch, roll, heading, airspeed, altitude)

### GPS Data Confirmed Present

NMEA sentences embedded in the data stream:
- **$GPGGA** — Fix data: lat, lon, altitude (MSL), fix quality, HDOP
- **$GPRMC** — Position, speed, course, date
- **$GPGSA** — DOP and active satellites
- **$GPGSV** — Satellites in view

Example from data:
```
$GPGGA,172609.000,3318.8278,N,08446.4126,W,2,9,0.91,304.2,M,-30.3,M,0000,0000*6C
$GPRMC,172609.000,A,3318.8278,N,08446.4126,W,0.00,0.00,211006,,,D*75
$GPGSA,A,3,21,29,11,20,05,25,12,18,15,,,,1.27,0.91,0.88*08
```

This confirms: **lat/lon, GPS altitude, ground speed, ground track, and date/time are all available** in every demo file. Route reconstruction is feasible.

## 3. Logbook Data — Not on Drive (Manual Save Required)

**Finding:** No logbook CSV is currently present on this USB drive. This is expected —
the GRT HXr requires the pilot to manually trigger a logbook save from the EFIS menu.
When saved, it produces a full-history CSV file (e.g., `Logbook 2025-01-15.csv`).

**Implication for FR-4.1:** The logbook CSV is a complete dump of all flight history
each time it's saved (not incremental). It contains structured flight records with
origin, destination, times, fuel used, hourmeter, and oil-added fields. See Section 7
below for full format analysis.

**Dual-source approach for flight records:**
- **Primary:** Logbook CSV when available (complete, structured, includes fuel data)
- **Supplemental:** DEMO file sessions can fill gaps between logbook saves (derive
  flight start/end from filename timestamps + GPS position for airports)

## 4. Chart Data Structure

### Key Architecture Insight

The EFIS reads chart data from `/ChartData/` at the root of the USB/SD card. The directory structure is:

- **`/ChartData/ScannedCharts.sqlite`** — Master database with `RegionalExpDates` table containing cycle info, passwords, download URLs for all chart types. Also has an `Info` table with overall cycle/version data. **Current cycle on drive: 2411 (Oct-Dec 2024) — significantly outdated.**
- **`/ChartData/LO/W{nnn}/`** — IFR Low chart tiles organized by west longitude
- **`/ChartData/SEC/W{nnn}/`** — VFR Sectional tiles, same scheme
- **`/ChartData/Plates/`** — Approach plates with SQLite index
- **`/ChartData/Plates/US/`** — US plate PNGs (~146,975 files total across all chart dirs)

### `E:ChartData` Mystery

The `E:ChartData` subdirectory inside `/ChartData/` appears to be **a duplicate left behind by the Windows-based ChartData Manager** when it was last used. It references the Windows drive letter (`E:`) that the SD card was mounted as. It contains the same structure (LO, SEC, Plates, ScannedCharts.sqlite) but is ~8.4 GB — less than the full 17 GB, suggesting it may be a partial/older copy.

**Decision needed:** Should our automation preserve `E:ChartData`, remove it (reclaiming ~8.4 GB), or ignore it? The EFIS likely reads from the root `/ChartData/` path, not the `E:` prefixed one.

### SQLite Metadata (FR-2.13 Opportunity)

`ScannedCharts.sqlite` contains `RegionalExpDates` with columns: Region, Subregion, FileType, ExpDate, Cycle, Pwd, URL, FriendlyName, TMSTilesURL, MBTilesURL, StartDate, UserSelected.

`Plates.sqlite` `Info` table shows: `Version=1, Start_Date=2026-05-14, End_Date=2026-06-10` — **plates are cycle 2606, much newer than the scanned charts (cycle 2411).** This suggests charts were partially updated at some point.

## 5. EFIS System Files

| File | Size | Purpose |
|------|------|---------|
| `HHXRUp-proc.dat` | 7.6 MB | EFIS firmware/software update file (processed format) |
| `NAV-proc.DB` | 9.9 MB | Navigation database (processed for EFIS) |
| `NAV.DB` | 7.9 MB | Navigation database (source/raw) |
| `Settings.bak` | 8.4 KB | Full EFIS settings dump |
| `State.bak` | 800 B | Current EFIS state |
| `WP.bak` | 52 B | User waypoints |
| `Plan.bak` | 1.6 KB | Flight plans (ICAO idents + lat/lon) |

**Settings.bak notable fields:**
- `1062=N488BF  ` — Tail number
- `1063=A60670` — (possibly FAA registration/hex code)
- Engine configuration data (CHT/EGT limits, aux channel names, etc.)

**Plan.bak** contains real waypoints with ICAO identifiers (KGYY, KVUJ, KIPT, 19A/KJCA, etc.) — confirms this is an active, configured aircraft.

## 6. Implications for Design

### What's Confirmed
- Demo files are the **single source** for both engine telemetry AND flight records
- GPS data (lat/lon/alt/speed/track/date) is embedded in demo files
- No separate logbook file exists on USB
- Chart data structure matches Seattle Avionics installation instructions exactly
- SQLite databases provide metadata for both currency checking and delta-copy logic
- File naming convention is deterministic and parseable

### What Needs Further Work
1. **DEMO binary format decoder** — The record structure is identified but field mapping within engine data packets (type 0x02) needs reverse-engineering against known parameter values. Community tools on Vans Airforce and GRT Avionics forums should be sourced as references.
2. **`E:ChartData` handling** — Safe to remove (reclaims ~8.4 GB). EFIS reads from root `/ChartData/`, not the `E:` prefixed path. Confirm deletion is non-destructive before automating.
3. **Logbook CSV import** — Format is known and straightforward. Import is idempotent (full dump each time). Oil Added field provides sparse historical data to bootstrap oil consumption tracking.
4. **Continuation file assembly** — Multiple `+N` files form one logical recording session. Parser must treat `DEMO-{date}-{time}.LOG` + all matching `+N` files as a single flight's data stream.
5. **Chart data staleness** — ScannedCharts shows cycle 2411 (late 2024), but Plates shows 2606 (May 2026). Partial updates have occurred. Currency tracking needs to check each chart type independently.
6. **GRTCHARTS cleanup** — Directory is a required flag; contents (E:GRTCHARTS) are disposable Windows artifacts.

### Drive Identification for FR-1.2

The EFIS drive can be identified by the presence of:
- Volume name: `EFIS`
- AND/OR presence of `GRTCHARTS/` directory at root
- AND/OR presence of `DEMO-*.LOG` files at root
- AND/OR presence of `HHXRUp-proc.dat` or `NAV.DB` at root

Any of these is sufficient; together they form a confident signature.

## 7. Logbook Data (Manually Saved CSV)

**Key finding:** The GRT HXr does NOT automatically write logbook data to USB. The pilot
must manually trigger a save from the EFIS. When saved, it produces a CSV file.

**Sample file:** `/Users/mwalker/Library/CloudStorage/Dropbox/Flying/Logbooks/Logbook 2025-01-15.csv`

### CSV Format

```csv
Date,Origin,Destination,Length,Length (hours),Fuel Used,Departure,Arrival,Hourmeter,Type,Passengers,Fuel Added,Oil Added
3/19/16,KLZU,KAND,0:29,0.4,0,15:13:14,15:43:10,49.6,VFR,,,
```

| Column | Type | Notes |
|--------|------|-------|
| Date | M/D/YY | US short date format |
| Origin | ICAO/FAA ID | Airport identifier, sometimes blank (interrupted flights) |
| Destination | ICAO/FAA ID | Sometimes blank |
| Length | H:MM | Duration as hours:minutes |
| Length (hours) | Decimal | Same duration as decimal hours |
| Fuel Used | Decimal | Gallons consumed on that leg |
| Departure | HH:MM:SS | Time of departure (UTC based on GPS) |
| Arrival | HH:MM:SS | Time of arrival |
| Hourmeter | Decimal | Running hourmeter total (tach hours) — this is the cumulative value |
| Type | String | Always "VFR" in this dataset |
| Passengers | Integer | Sparse — only a few entries populated |
| Fuel Added | Decimal | Gallons added (very sparse) |
| Oil Added | Decimal | Quarts added (very sparse — values seen: 1, 6) |

### Key Observations

- **~350 flight records** covering Mar 2016 – Jan 2025 (356.5 tach hours total)
- **Home base:** KLZU (Gwinnett County/Briscoe Field, GA)
- **Hourmeter** provides continuous tach-hour reference for oil consumption tracking (FR-4.4)
- **Oil Added** column exists but is extremely sparse — only a handful of entries have values. This confirms FR-4.2's manual-entry approach is correct; the logbook CSV alone is insufficient for oil trending.
- **Fuel Used** is populated on most legs — useful for SFC trending (FR-5.4)
- Some rows have blank Origin or Destination (GPS signal loss, aborted takeoffs, or multi-leg flights where the system lost track)
- Last two rows are summary/blank (empty separator + total hours)
- File is a snapshot of ALL history (not incremental) — each save overwrites/replaces the full logbook

### Import Strategy

Since each logbook CSV is a complete dump (not incremental):
1. Import should be idempotent — re-importing the same or newer CSV replaces old data
2. Match on Date + Departure time as the unique key per flight record
3. Oil Added entries from the CSV can seed historical oil data, but going forward, the menu-bar manual entry (FR-4.2) is the primary source
4. The hourmeter value at each entry provides the tach-hours baseline for oil consumption rate calculation

## 8. Screenshots (SNAP####.PNG)

Pilot can manually save screenshots from the EFIS display. These appear as:
```
SNAP0001.PNG
SNAP0002.PNG
...
```

- Numbered sequentially
- Located at USB root
- No processing required — archive as-is with date context
- Should be moved off USB (cleaned) after archive, same as DEMO files
- No screenshots currently on this drive (presumably cleared previously or not yet taken on this cycle)

## 9. Settings Files (Manual EFIS Settings Backup)

The EFIS allows manual backup of settings to USB. Files present:
- `Settings.bak` — Full EFIS configuration (key=value, checksummed)
- `State.bak` — Current EFIS state
- `WP.bak` — User waypoints
- `Plan.bak` — Flight plans

**Archive strategy:** These should be archived with a date stamp appended to the filename (e.g., `Settings.bak` → `Settings-20260809.bak`) since they're overwritten in-place on each save. History of settings changes has diagnostic value (e.g., if an EFIS setting change correlates with a performance anomaly).

**Do not delete from USB** — unlike DEMO and SNAP files, the EFIS may need these for restore.

## 10. GRTCHARTS Directory

Per pilot testing: the `GRTCHARTS/` folder itself is **required** as a presence flag (the EFIS checks for it), but its contents are not needed. The `E:GRTCHARTS` subdirectory inside it is a Windows artifact and can be safely removed.

**Automation rule:** Preserve `GRTCHARTS/` directory. Remove any contents within it (stale Windows artifacts). Never write chart data into this folder — charts go in `/ChartData/`.

## 11. Confirmed Aircraft Details

| Item | Value |
|------|-------|
| Tail Number | N488BF |
| Engine | Lycoming IO-360 (4-cylinder, fuel-injected) |
| Cylinders | 4 (CHT×4, EGT×4) |
| EFIS | GRT Horizon HXr |
| Configuration | Single EFIS, single aircraft |
| Home Base | KLZU (Gwinnett County/Briscoe Field, GA) |

## 12. Updated Capture Rules (FR-1.2 Refined)

On USB insertion, identify and handle these file types:

| Pattern | Type | Action |
|---------|------|--------|
| `DEMO-*.LOG` | Engine telemetry | Move to archive (validate then delete from USB) |
| `SNAP*.PNG` | Screenshots | Move to archive (validate then delete from USB) |
| `Logbook*.csv` | Logbook export | Copy to archive (do NOT delete — pilot may re-export) |
| `Settings.bak` | EFIS config | Copy to archive with date stamp (do NOT delete) |
| `State.bak` | EFIS state | Copy to archive with date stamp (do NOT delete) |
| `WP.bak` | Waypoints | Copy to archive with date stamp (do NOT delete) |
| `Plan.bak` | Flight plans | Copy to archive with date stamp (do NOT delete) |
| `HHXRUp-proc.dat` | EFIS firmware | Leave untouched |
| `NAV*.DB` | Nav database | Leave untouched |
| `ChartData/` | Chart data | Leave untouched (managed by currency subsystem) |
| `GRTCHARTS/` | Flag directory | Leave untouched (clean internal contents only) |

## 13. DEMO File Format Research (Community & Official Sources)

### Key Discovery: Two Recording Mechanisms Available

The GRT HXr has **two distinct data recording features** (per GRT forum and documentation):

1. **DEMO Recording** (currently in use) — Binary format, records full system state for
   replay on the EFIS itself. This is what produces the `DEMO-*.LOG` files on the drive.
   Contains settings, AHRS, GPS, EIS engine data, and display state. Undocumented binary format.

2. **USB Flight Data Logger (FDL)** — Available on HXr/HX/SX/EX/Mini. Outputs **CSV files**
   directly (`GRT FDL {number}.csv`). Configurable recording interval (200ms–30000ms,
   default 1000ms). Includes AHRS and GPS data alongside engine parameters. Records
   automatically when: airspeed valid, ground speed >5 kts, RPM >0, or fuel flow >0.
   **Not currently enabled on this aircraft** (no FDL CSV files found on drive).

**Recommendation:** Enable the USB FDL feature on the EFIS (under SET MENU > General Setup >
Demo Settings). Set recording interval to 1000ms (1 Hz, good balance of resolution vs. file
size). This gives us **structured CSV data without needing to reverse-engineer the binary
DEMO format** — dramatically simplifying Phase 2 engine data analysis. The DEMO recording
can remain enabled as well for the full-fidelity replay capability, but the CSV FDL files
become the primary data source for trending and analysis.

**Configuration recommended:**
- USB Flight Data Logger: ON
- USB FDL Record Interval: 1000 ms (1 second)
- USB FDL Save Interval: 60 s (writes to USB every 60 seconds)

### DEMO Binary Format (for archived files already on drive)

Based on hex analysis and community research:

**Sources identified:**
- [MATLAB GRTLogFileReader](https://mathworks.com/matlabcentral/fileexchange/45289) by Russell Carpenter — parses both EIS and AHRS data from GRT LOG files. Author obtained format documentation directly from GRT with permission. Available at `git@gitlab.com:carpenaut1/GRTLogFileReader.git`.
- [GRT EIS Log software](https://grtavionics.com/horizon-hx-software/) — Official GRT Windows tool that decodes EIS engine data from DEMO files to spreadsheet/CSV format. Confirms that the EIS data subset is extractable.
- [Walter's Flight Data Recorder](http://www.iflyez.com/EFISRecorder.shtml) — Windows tool that decodes GRT EIS 2000/4000/6000 serial data in real-time. Documents the EIS serial protocol (header `FE FF FE`, 9600 baud, 8N1).
- Vans Airforce forum thread on [GRT EIS4000 serial data decoding](https://vansairforce.net/threads/grt-eis4000-serial-data-decoding.183147/) — confirms the `FE FF FE` header for EIS data frames and that different firmware versions may use `FE FE FE`.

**EIS Serial Data Protocol (relevant for record type 0x02 in DEMO files):**
- Header: `0xFE 0xFF 0xFE` (or `0xFE 0xFE 0xFE` for rare old firmware)
- 9600 baud, 8N1 on serial; captured directly in DEMO binary as record type 0x02
- Payload contains packed 16-bit values for: RPM, CHT×4/6/9, EGT×4/6/9, oil temp,
  oil pressure, fuel flow, fuel pressure, volts, amps, manifold pressure, tach hours,
  and aux channels
- GRT's own EIS Log tool can decode this portion from DEMO files

**AHRS Data (record type 0x01/0x13/0x14 in DEMO files):**
- Contains pitch, roll, yaw/heading, airspeed, altitude (pressure + density),
  vertical speed, angle of attack, G-force, OAT, wind, turn rate
- The MATLAB GRTLogFileReader handles this, suggesting the format is documented
  (author received it from GRT upon request)

**GPS Data (NMEA sentences embedded in records):**
- Standard NMEA-0183: $GPGGA, $GPRMC, $GPGSA, $GPGSV
- Provides: lat, lon, GPS altitude, ground speed, ground track, satellite info, date/time
- Already confirmed extractable from hex inspection

### Strategy for Engine Data Analysis

**Phase 2 approach (recommended):**
1. **Going forward:** Enable USB FDL → parse the CSV files directly (trivial)
2. **Historical data (existing DEMO files):** Use GRT's official EIS Log tool (Windows, or
   via Wine/Crossover) to batch-decode the EIS portion to CSV, OR port the MATLAB
   GRTLogFileReader to Python for direct binary parsing of both EIS and AHRS data

**Fallback/advanced:** If full binary parsing is needed (for AHRS normalization per FR-3.7),
contact GRT Avionics to request the serial interface documentation (they've provided it
to others per the MATLAB author's note), then build a native Python parser.

### EIS 4000 Parameters (4-cylinder IO-360)

Based on EIS 4000 documentation and Walter's Flight Data Recorder field list:

| Parameter | Resolution | Notes |
|-----------|-----------|-------|
| RPM | 1 RPM | Via tach pickup |
| CHT 1-4 | 1°F | All 4 cylinders |
| EGT 1-4 | 1°F | All 4 cylinders |
| Oil Temperature | 1°F | |
| Oil Pressure | 0.1 PSI | |
| Fuel Flow | 0.1 GPH | Via fuel flow transducer |
| Fuel Pressure | 0.1 PSI | |
| Bus Voltage | 0.1 V | |
| Bus Current (Amps) | 0.1 A | |
| Manifold Pressure | 0.1 inHg | Internal sensor on EIS 4000 |
| Tach Time | 0.1 hr | Accumulated |
| Aux 1-6 | Configurable | User-defined (OAT, carb temp, etc.) |
| Fuel Quantity L/R | Gallons | If sensor installed |

Plus AHRS/flight data (if available from EFIS records):
- Pressure Altitude, Density Altitude
- OAT (Outside Air Temperature)
- Indicated Airspeed, True Airspeed
- Vertical Speed
- Pitch, Roll, Heading (magnetic)
- G-force (vertical, lateral)
