# GRT HXr Ground Support Automation — Design Document

## 1. System Architecture

### Overview

A macOS menu bar application (`rumps`-based) that runs as a persistent background service,
automating ground support for a GRT Horizon HXr EFIS installation on N488BF (IO-360, 4-cyl).

### Components

```
┌─────────────────────────────────────────────────────────┐
│                    Menu Bar App (rumps)                  │
│  - Status display & color-coded icon                    │
│  - Settings UI (folder pickers)                         │
│  - Manual actions (Prepare Drive, oil change entry)       │
│  - Notification dispatch                                │
└────────┬──────────────┬──────────────┬──────────────────┘
         │              │              │
    ┌────▼────┐   ┌────▼────┐   ┌────▼────┐
    │   USB   │   │Currency │   │Analysis │
    │ Monitor │   │ Updater │   │ Engine  │
    └────┬────┘   └────┬────┘   └────┬────┘
         │              │              │
         │              │              │
    ┌────▼────┐   ┌────▼────┐   ┌────▼────┐
    │Archiver │   │  Chart  │   │  FDL    │
    │(capture)│   │Download │   │ Parser  │
    └─────────┘   └─────────┘   └─────────┘
         │              │              │
         ▼              ▼              ▼
    ┌─────────────────────────────────────┐
    │         Local Storage               │
    │  - Archive (FDL, demo, logbook,     │
    │    snapshots, settings by date)     │
    │  - USB Image (mirror of drive root) │
    │  - Config (App Support/JSON)        │
    │  - Time-series DB (SQLite)          │
    └─────────────────────────────────────┘
```

### Module Layout

```
src/efis_data_manager/
├── __init__.py              # Package version
├── app.py                   # Menu bar app (rumps), UI, notifications
├── config.py                # Settings load/save, path management
├── usb_monitor.py           # DiskArbitration-based USB detection
├── archiver.py              # File capture logic (move/copy/validate/clean)
├── currency.py              # Seattle Avionics + GRT update checking/downloading
├── drive_updater.py         # Sync local USB image → physical USB drive
├── fdl_parser.py            # FDL CSV parsing into time-series records
├── analysis.py              # Trending, anomaly detection, oil consumption
├── reporting.py             # Excel/CSV export, flight list
└── nlq.py                   # Natural language query interface (Phase 7)
```

## 2. Data Flow

### On USB Insertion (sequential)

1. **Detect** — `usb_monitor.py` fires callback on mount event
2. **Identify** — Check volume label ("EFIS") + presence of `GRTCHARTS/`
3. **Archive** (all reads from USB first):
   - Move `GRT FDL*.csv` → archive by date
   - Move `DEMO-*.LOG` → archive by date
   - Move `SNAP*.PNG` → archive by date
   - Copy `Logbook*.csv` → archive (don't delete from USB)
   - Copy `*.bak` → archive with date stamp (don't delete from USB)
   - Validate each moved file (checksum), delete USB copy only after validation
   - Clean `GRTCHARTS/` contents, remove `E:ChartData/` if present
4. **Update** (all writes to USB second):
   - Compare USB drive currency against local USB image (via metadata/SQLite cycle check)
   - If stale, rsync changed files from local image → USB
5. **Notify** — macOS notification with summary

### Scheduled (independent of USB)

- Every 12 hours: Check Seattle Avionics for new chart cycle, download to local USB image
- Every 24 hours: Check grtavionics.com for software/nav DB updates, download to local USB image

### On FDL Import (after archive)

- Parse new FDL CSV files into time-series SQLite database
- Run anomaly detection against historical baselines
- Surface alerts if thresholds exceeded

## 3. Storage Layout

### Archive Directory (user-configurable, default ~/Documents/EFIS_Archive)

```
EFIS_Archive/
├── FDL/
│   └── 2026-06-06/
│       ├── GRT FDL 001.csv
│       └── GRT FDL 002.csv
├── Demo/
│   └── 2026-06-06/
│       ├── DEMO-20260606-122536.LOG
│       └── DEMO-20260606-122536+1.LOG
├── Logbook/
│   └── Logbook 2025-01-15.csv
│   └── Logbook 2026-08-09.csv
├── Snapshots/
│   └── 2026-06-06/
│       └── SNAP0001.PNG
├── Settings/
│   └── Settings-20260809.bak
│   └── State-20260809.bak
│   └── WP-20260809.bak
│   └── Plan-20260809.bak
└── database.sqlite          # Time-series engine data + oil consumption
```

### USB Image Directory (user-configurable, local mirror of drive root)

```
EFIS_USBImage/
├── GRTCHARTS/               # Flag directory (empty)
├── ChartData/
│   ├── ScannedCharts.sqlite
│   ├── LO/
│   ├── SEC/
│   └── Plates/
├── HHXRUp-proc.dat          # EFIS firmware
├── NAV-proc.DB              # Nav database (processed)
└── NAV.DB                   # Nav database
```

## 4. Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Sequential archive→update on USB insert | Flash drives perform better with pure read then pure write; avoids controller thrash |
| FDL CSV as primary analysis source | Structured, human-readable, no reverse engineering needed vs binary DEMO format |
| SQLite for time-series storage | Single-file, no server, good query performance for the data volumes involved |
| `rsync --checksum` for drive updates | Simple, correct, fast enough for single-digit GB on USB 3.0 |
| Metadata marker for quick currency check | Avoid full rsync comparison on every insertion; check cycle field in ScannedCharts.sqlite or a flag file |
| `DiskArbitration` framework for USB detection | Native macOS API, reliable, no polling required |
| `rumps` for menu bar | Lightweight Python-native menu bar framework, no Electron/Swift bridge needed |
| `launchd` for persistence | Standard macOS mechanism for login-item agents |
| Oil consumption tracking separate from FDL | Oil additions come from logbook CSV; oil changes (date + tach time) are manual entry. Neither is derivable from flight data. |
| Currency checks non-blocking to USB ops | Background scheduler runs independently; USB ops use whatever is current in local image |

## 5. Phase 2: USB Detection — Detailed Design

### Approach

Use `pyobjc` to access macOS `DiskArbitration` framework directly. Register a callback
that fires on volume mount/unmount events. Filter for removable media with FAT32 filesystem.

### Identification Logic

A mounted volume is an "EFIS drive" if ANY of:
- Volume label is "EFIS"
- `GRTCHARTS/` directory exists at volume root

### Integration with App

- `usb_monitor.py` runs on a background thread, started when the app launches
- On EFIS drive detection, posts event to main thread → triggers archive + update pipeline
- Status menu item updates to reflect: "Drive detected", "Archiving...", "Updating...", "Complete"
- On ejection, status returns to "Idle"

### Error Handling

- If drive is pulled mid-archive: log partial state, alert user, do not delete any USB files that weren't validated
- If drive is read-only: skip archive (can't delete), attempt update only, warn user
- If drive detection fails: degrade gracefully, app continues running, log error

## 6. Phase 3: Currency Downloads — Detailed Design

### Seattle Avionics Chart Data

1. Login to `https://seattleavionics.com/ChartData/default.aspx?TargetDevice=GRT`
2. GET `Installation.aspx`, parse download table (de-duplicate by URL)
3. Compare valid dates against locally stored cycle info
4. If new cycle available: download zips, extract with password into USB image `/ChartData/`
5. Skip IFR High Altitude Charts

### GRT Software / Nav Database

1. Scrape grtavionics.com for current software versions:
   - Horizon HXr EFIS software
   - Mini A/P EFIS software
   - AHRS firmware
   - Nav database (28-day cycle)
2. Compare against local versions
3. Download new files to USB image root

### Credentials

- Seattle Avionics login: macOS Keychain (`security` CLI or `keyring` library)
- No credentials needed for GRT downloads (public)

## 7. Phases 4-7: Summary Design Notes

### Phase 4: Auto-Archive
- Implements `archiver.py` — the file identification, validation, move/copy logic
- Checksum validation (SHA-256 of source vs destination before USB delete)
- Idempotent: re-inserting same drive doesn't re-archive already-captured files

### Phase 5: Auto-Update Drive
- Implements `drive_updater.py` — compare USB currency, rsync delta
- "Prepare Drive" formats FAT32, labels, creates GRTCHARTS/, triggers update
- Uses `diskutil` for format operations (requires user confirmation)

### Phase 6: Analysis
- Implements `fdl_parser.py` — CSV → SQLite time-series
- Implements `analysis.py` — rolling stats, anomaly detection, oil trending
- Flight segmentation from FDL data (GPS start/stop position → airport lookup)
- Configurable thresholds stored in config.json

### Phase 7: Natural Language Reporting
- Implements `nlq.py` — query interface over SQLite data
- Implementation TBD (local LLM, API-based, or structured NLQ)
- Returns text, tables, or chart images
