# GRT HXr Ground Support Automation — Requirements Document (Draft v0.1)

## 1. Purpose

Build a Mac-based system that automates ground support for a GRT Horizon HXr EFIS
installation, covering four functions:

1. **Capture** — auto-archive logbook, FDL (flight data logger CSV), demo files,
   snapshots, and settings from USB drives on insertion. Maintain local history
   with timestamps to track changes over time (e.g. settings revisions).
2. **Currency** — keep chart data (Seattle Avionics), EFIS/AHRS software, and nav
   database current, and stage updates to USB for transfer to the aircraft.
3. **Analysis** — parse FDL (flight data logger) CSV data to trend engine performance and flag anomalies.
4. **Clean USB** — keep the USB drive clean and preserve space by moving (not
   copying) FDL, demo, logbook, and snapshot files off the drive during capture,
   with validation that the move completed correctly (all bytes transferred intact)
   before the files are removed from the USB drive.

## 2. Known Constraints (from research — please confirm/correct)

These materially affect what's achievable and should be validated before design starts.
Note: per your own long-term observation, the Seattle Avionics download mechanism
has been stable for 15+ years — this doesn't guarantee continued stability, but it
meaningfully lowers the practical risk of the "fragile to site/format changes" caveat
below. Build in failure detection/alerting (FR-2.7, NFR-6) as cheap insurance rather
than treating it as a high-probability concern.

| Constraint | Detail | Impact |
|---|---|---|
| Seattle Avionics ChartData Manager is Windows-only | No official Mac client, no public API, no FTP. **However, Seattle Avionics officially supports a Mac-oriented manual workflow**: log into `https://www.seattleavionics.com/promo_g/` (URL is static), navigate to "Click here for manual download and installation instructions" (`https://seattleavionics.com/ChartData/Installation.aspx`), which lists per-cycle zip download links and passwords in a table, each mapped to a specific target subdirectory. This is a documented, sanctioned path — not a fragile undocumented scrape. | Fully achievable natively on Mac. Still needs to be automated (session login, table scrape, per-file download, password-protected extraction into the exact directory structure below), and the page could still change layout over time, but the mechanism itself is stable and intended for this use case. See FR-2.8–2.12 and the directory table below. |
| EFIS software / AHRS firmware / nav database updates are manual downloads | Distributed as files from grtavionics.com; nav database is on a 28-day cycle. No public API for "what's the latest version." | "Automatically maintain" likely means: periodically check the GRT site for new files (scraping, since no API) and download/stage them — not push-button firmware flashing, which GRT documentation suggests is done at the EFIS itself. |
| USB drive is shared across purposes | Same drive may carry FDL/demo/logbook data (outbound from EFIS) and chart/software/nav updates (inbound to EFIS). Need to avoid clobbering EFIS-required folder structures (e.g. `GRTCHARTS`, `ChartData`) when writing archive logic. | Drive content in each direction must be handled by distinct, non-destructive logic. |

**Confirmed via direct test (Aug 2026):** Login is fully scriptable with plain
POST requests — no CAPTCHA or 2FA present on the form. The actual login form
lives inside an iframe on the `promo_g/` landing page
(`https://seattleavionics.com/ChartData/default.aspx?TargetDevice=GRT`), not on
that page directly — automation should target the iframe URL, not the wrapper.
It's a standard ASP.NET WebForms login (`txtLoginEmail`, `txtLoginPassword`,
`cmdLogin`, plus the usual `__VIEWSTATE`/`__EVENTVALIDATION` hidden fields that
must be echoed back on POST). A scripted login followed by a GET on
`Installation.aspx` in the same session successfully retrieved the download
table. This subsystem's biggest open risk is now closed.

**Also confirmed via direct test:** the full download table was successfully
parsed programmatically, matching the 6 real entries exactly (5 in-scope after
excluding IFR High Altitude). One quirk found and worth remembering during
implementation: **the page renders the download table multiple times** (4x in
testing — likely a duplicate/responsive layout), so a naive parse returns
duplicate rows; de-duplication by download URL is required. Download URLs
follow a predictable pattern:
`http://data.seattleavionics.com/OEM/Generic/{cycle}/{cycle}.{type}.zip`
(e.g. `2607.LO.MultiDiskImg.zip`, `2608.Plates.GEO.zip`) — note this is
**plain HTTP, not HTTPS**, for the actual file download, even though login is
HTTPS. A full download-and-extract run (all in-scope files, real password
per file, extracted with Python's `zipfile`) was kicked off against a temp
scratch directory to validate the end-to-end mechanism; final pass/fail
per-file results were still in progress as of this writing and should be
confirmed complete before treating FR-2.9/2.10 as fully validated — in
particular, whether all zips extract cleanly with standard `zipfile` (some
zip encryption schemes, e.g. AES, aren't supported by Python's built-in
`zipfile` and would need `pyzipper` instead).

## 3. Functional Requirements

### 3.1 USB Capture & Archive

Two distinct GRT data sources are involved and should be treated as separate capture
paths, even though both may travel on the same USB drive:

- **FDL files** — USB Flight Data Logger CSV output containing engine telemetry (CHT,
  EGT, RPM, fuel flow, oil temp/pressure, volts/amps, manifold pressure), AHRS data
  (altitude, airspeed, attitude, OAT), and GPS (lat/lon/speed/track). Recorded
  automatically when flying. This is the primary source for engine analysis (Section 3.3).
- **Demo files** — binary EFIS replay recordings (`DEMO-*.LOG`). Archived for
  completeness but not parsed for analysis (FDL CSV replaces this need).
- **Logbook data** — flight times (Hobbs/tach, block/flight time) and oil-added
  annotations. **Confirmed:** logbook is a manually-saved CSV from the EFIS (not
  automatic). May be cumulative (full history) or incremental (new only) depending
  on pilot's save choice on the HXr. Used for oil consumption tracking only (Section 3.4).
- **Screenshots** — pilot-saved EFIS display captures (`SNAP####.PNG`), archived
  without processing.
- **Settings backups** — manually-saved EFIS configuration files (`Settings.bak`,
  `State.bak`, `WP.bak`, `Plan.bak`), archived with date stamps for history tracking.

**Drive identification:** Volume name "EFIS" + presence of `GRTCHARTS/` directory
at root. The `GRTCHARTS/` folder is a required flag for the EFIS — preserve it
but clean any contents (Windows artifacts).

- FR-1.1: Detect USB volume insertion/removal on macOS (e.g. via `diskutil`/`DiskArbitration` or a launchd-triggered watcher).
- FR-1.2: On insertion, identify and handle the following file types on the volume:

  | Pattern | Action | Delete from USB? |
  |---------|--------|-----------------|
  | `DEMO-*.LOG` | Move to archive by date | Yes (after validation) |
  | `GRT FDL*.csv` | Move to archive by date | Yes (after validation) |
  | `SNAP*.PNG` | Move to archive by date | Yes (after validation) |
  | `Logbook*.csv` | Copy to archive | No (pilot may re-export) |
  | `Settings.bak`, `State.bak`, `WP.bak`, `Plan.bak` | Copy to archive with date stamp | No (EFIS needs for restore) |

- FR-1.3: **Move** (not copy) FDL, DEMO, and SNAP files to a local archive, organized by date and/or tail number, to keep the USB drive clean and preserve its space (Purpose #4). **Copy** (not move) logbook CSV and settings .bak files — these must remain on the drive. Skip files already archived (checksum or filename+size comparison) rather than re-moving/duplicating them.
- FR-1.4: Before removing any file from the USB drive, validate the local copy is byte-for-byte identical to the source (e.g. checksum comparison of source vs. destination after write) — only delete the USB-side file once validation confirms an error-free transfer. If validation fails, leave the USB-side file untouched, log the failure, and surface it as an alert rather than silently retrying indefinitely.
- FR-1.5: Log each capture event (files moved/copied, size, type, timestamp, source volume, validation result) to a local run log.
- FR-1.6: Notify the user (macOS notification) on completion, including summary and any errors (e.g. corrupt file, unreadable volume).
- FR-1.7: Detect and avoid double-counting when the same flight/data appears across multiple captured files (e.g. re-inserting the same USB drive later).
- FR-1.8: Archive settings .bak files with a date stamp appended (e.g. `Settings.bak` → `Settings-20260809.bak`) since they are overwritten in-place on each manual save. Maintain history for diagnostic correlation.
- FR-1.9: Clean contents of `GRTCHARTS/` directory (remove any subdirectories like `E:GRTCHARTS`) while preserving the directory itself — it serves as a required flag for the EFIS.
- FR-1.10: Remove `E:ChartData/` subdirectory from `/ChartData/` if present — it's a ~8.4 GB leftover from the Windows ChartData Manager and is not read by the EFIS.

### 3.2 Currency Management (charts, EFIS/AHRS software, nav database)

- FR-2.1: Check for new chart data availability from Seattle Avionics every 12 hours (sufficient given the 28-day cycle).
- FR-2.2: Check grtavionics.com daily for new EFIS/AHRS software releases and nav database updates. **Two EFIS units to track:** Horizon HXr and Mini A/P — each has its own software update path.
- FR-2.3: Download available updates to local storage when on a network connection. Notify the user when EFIS software, AHRS firmware, or nav database has a new version detected and successfully downloaded (distinct notification per item, since they update independently).
- FR-2.4: On USB insertion, if the drive is recognized/designated as an "update" drive, stage current chart/software/database files onto it in the folder structure the HXr expects (`/ChartData/` at drive root — **confirmed via Phase 0 inspection**), without disturbing any FDL/demo/logbook data also present. The `GRTCHARTS/` directory must be preserved as a flag but chart data goes in `/ChartData/`, not `/GRTCHARTS/`. **Only write files that have actually changed** (see FR-2.13) rather than re-copying the full chart set every cycle — this matters for performance given the ~17 GB total chart data size. **For quick currency determination on insertion:** rather than comparing individual chart files, write a local metadata marker (e.g. a small flag file or use the existing `ScannedCharts.sqlite` cycle/date fields) that records the last-staged cycle per chart type. On insertion, compare this marker against the local archive's current cycle — if they match, no copy needed. The SQLite databases already on the drive (`ScannedCharts.sqlite`, `Plates.sqlite`) contain cycle and date fields that may serve this purpose natively. Notify the user when chart staging/copying begins (in-progress indicator, since this may take noticeable time for large chart sets), and again when it completes successfully — or fails, per FR-2.7.
- FR-2.5: Track locally which version is currently staged/on-drive vs. latest available (compare "valid dates" range per data type from the installation page), so the user can see at a glance if the aircraft's data is current.
- FR-2.6: Handle Seattle Avionics account authentication securely (stored credentials in macOS Keychain), POSTing to `https://seattleavionics.com/ChartData/default.aspx?TargetDevice=GRT` (the actual login form, reached via an iframe from the `promo_g/` landing page — confirmed working via scripted login test), and maintaining the resulting session for the subsequent check/download flow.
- FR-2.7: Alert the user if login fails, the installation page layout has changed (scrape breaks), or a scheduled check fails repeatedly — treat "page changed" as a distinct alert type since it needs manual intervention (code update), not just a retry.
- FR-2.8: After login, navigate to `https://seattleavionics.com/ChartData/Installation.aspx` and parse the download table: description, region, valid dates, password (if present — note some entries, e.g. Scanned Charts DB, have no password), and download link. **Confirmed via test:** the table is rendered multiple times on the page — parsing logic must de-duplicate by download URL. **IFR High Altitude Charts are explicitly excluded from download/processing scope** (not needed for this aircraft).
- FR-2.9: Download each zip file listed. **Confirmed:** download URLs are plain HTTP (not HTTPS) at `data.seattleavionics.com/OEM/Generic/{cycle}/{cycle}.{type}.zip`.
- FR-2.10: Extract each zip using its associated password (where applicable) into the exact target directory per the mapping table below — critical detail: some zips (Sectionals, IFR Low, IFR High) contain their own subdirectory structure and must be extracted so that structure lands correctly under `/ChartData`, not flattened. **Confirm during implementation** whether all zip types extract with Python's standard `zipfile` (ZipCrypto) or whether any use AES encryption requiring `pyzipper` instead — not yet fully verified end-to-end.
- FR-2.11: Detect when a new update cycle has published (compare current valid-dates/password vs. last known per data type), so checks don't just re-download unchanged data.
- FR-2.12: Ensure the Scanned Charts DB is always current/present, since the installation page notes it's required for the HI/LO/SEC downloads to function correctly.
- FR-2.13: Determine which files actually changed between the local (already-downloaded) chart set and what's currently staged on the USB drive, and copy only the delta — not a full re-copy each cycle. **Default implementation: `rsync --checksum`** (or equivalent content-based size+mtime comparison) between the local chart directory and the USB target directory — general-purpose, works regardless of internal file structure, and chart data size (single-digit GB) on USB 3.0 makes a full content-based diff acceptably fast without needing a metadata-driven shortcut. If a SQLite index or similar is discovered during Phase 0 inspection of the chart data, it could be evaluated as an optimization, but is not required. Whichever approach is used, the correctness guarantee is the same as FR-1.4: never leave the USB-side data in a partially-updated, inconsistent state — apply changes such that a failure mid-copy doesn't leave stale and updated files mixed within what should be one consistent chart cycle.

- FR-2.14: **Prepare Drive** — provide a user-initiated function (via menu bar) to prepare a fresh USB drive for EFIS use. When selected: (a) prompt the user to choose a currently mounted USB volume, (b) confirm the destructive action, (c) format the drive as FAT32 with an EFIS-compatible volume label (e.g. "EFIS"), (d) create the required `GRTCHARTS/` flag directory, (e) copy all current charts, EFIS/AHRS software, and nav database from local archive onto the drive in the correct directory structure. This provides a one-click way to provision a replacement or additional USB drive without manual file management.

**Target directory mapping** (from Seattle Avionics installation instructions):

| Data Type | Target Directory |
|---|---|
| IFR Low Altitude Charts | `/ChartData` |
| IFR High Altitude Charts | `/ChartData` | **Out of scope** — not needed for this aircraft's operations. |
| Sectionals (VFR) | `/ChartData` |
| IFR Approach Plates and Airport Diagrams (geo-ref data) | `/ChartData/Plates` |
| IFR Approach Plates and Airport Diagrams (images) | `/ChartData/Plates/US` |
| Seattle Avionics Airport Diagrams (geo-ref data) | `/ChartData/FG` |
| Seattle Avionics Diagrams (images) | `/ChartData/FG/US` |
| Scanned Charts Database (required for HI/LO/SEC) | `/ChartData` |

Note: this table lists the *SD-card/USB target*, which per FR-2.4 the system
stages onto the update USB drive after building it locally.

### 3.3 Engine Data Analysis (from FDL data)

The GRT HXr's built-in **USB Flight Data Logger (FDL)** outputs structured CSV files
(`GRT FDL {N}.csv`) directly to USB. This includes EIS engine data, AHRS flight data
(altitude, airspeed, attitude, OAT), and GPS (lat/lon/speed/track). It records
automatically when flying (airspeed valid, ground speed >5 kts, RPM >0, or fuel flow >0).
**Currently not enabled** — will be activated on next flight to obtain sample data.

- FR-3.1: Parse FDL CSV data into structured engine/flight parameters (CHT×4, EGT×4, oil temp/pressure, RPM, fuel flow, manifold pressure, volts/amps, pressure altitude, OAT, airspeed, GPS position, etc.).
- FR-3.2: Maintain a historical time-series database of engine parameters across flights.
- FR-3.3: Compute per-flight summary statistics (min/max/mean, trends over rolling window e.g. last 10/25/50 hours).
- FR-3.4: Detect deviations from established baseline (e.g. CHT spread widening, EGT divergence, oil pressure trending down, mag drop changes) using statistical thresholds (configurable, not hardcoded).
- FR-3.5: Flag anomalies for user review with the relevant flight, timestamp, and parameter trace — not just a binary alert.
- FR-3.6: Present trends visually (charts) and allow export (CSV) for sharing with a mechanic or engine monitor service (e.g. Savvy Aviation).
- FR-3.7: Avoid false-positive fatigue: distinguish expected variation (e.g. altitude/OAT-driven CHT changes, break-in period) from anomalous behavior where feasible. FDL data includes pressure altitude and OAT, so altitude/temperature normalization of CHT/EGT is achievable.
- FR-3.8: Link each FDL flight record to its corresponding logbook entry (Section 3.4) where derivable (matching date/time), so engine trends can be viewed alongside flight time/oil context without merging the two data sources into one file format.

### 3.4 Oil Consumption Tracking (from logbook data + manual entry)

- FR-4.1: Parse GRT logbook CSV for oil addition records only. **Confirmed format:** `Date,Origin,Destination,Length,Length (hours),Fuel Used,Departure,Arrival,Hourmeter,Type,Passengers,Fuel Added,Oil Added`. The Hourmeter column provides tach-hours baseline for oil consumption calculations. The Oil Added column provides historical oil addition data to seed trending. **Note:** logbook CSV may be cumulative (full history) or incremental (new entries only), depending on whether the pilot saves "all" or "new" on the HXr — import logic must handle both cases gracefully (de-duplicate by Date + Departure time). **Re-import behavior:** logbook rows may be edited between imports (e.g. oil addition annotated on a previously-imported flight). Import must update existing rows' Oil Added field rather than skipping them as duplicates. The Oil Added value is annotated on the flight *preceding* the addition (i.e. Flight B's row), but physically added before the *next* flight (Flight C). The Hourmeter on that row is the tach reference for the consumption interval endpoint.
- FR-4.2: Provide a manual entry mechanism (menu-bar quick-entry, since day-to-day interaction is a menu bar app per Section "day to day") for oil changes: date and tach hours at time of change. This marks the start of a new consumption interval (full sump). Oil additions between changes come from the logbook CSV `Oil Added` column.
- FR-4.3: Store oil-addition entries independently of imported logbook data — they must survive re-imports and are never overwritten by file capture.
- FR-4.4: Compute oil consumption rate (e.g. quarts per hour, or per X hours) using tach hours between oil-added entries. **Key timing detail:** oil is added during pre-flight (before the flight starts), so each addition represents consumption during the *preceding* interval — from the most recent prior addition (or oil change, whichever is more recent) up to the current logbook entry's tach hours. The addition quantity is attributed to that preceding interval, not to subsequent flights.
- FR-4.5: Trend oil consumption rate over a rolling window of **25+ engine hours** rather than single intervals between consecutive additions — at typical low consumption rates, a single interval is too noisy (small measurement/topping-off variance dominates the signal) to draw a reliable trend from. Shorter-interval data points still feed the rolling calculation, but the trend/alert logic (FR-4.6) should evaluate against the accumulated 25+ hour window, not point-to-point.
- FR-4.6: Alert when consumption rate changes meaningfully — both a gradual upward trend (evaluated over the 25+ hour rolling window per FR-4.5) and a sudden spike between two consecutive additions (evaluated independently of the rolling window, since a genuine leak can show up in a single short interval and shouldn't wait for 25 hours of data to surface) — since these likely indicate different failure modes (e.g. gradual = normal wear/ring seating changes, sudden spike = potential leak or new issue). Threshold should be configurable, not hardcoded, for both the gradual-trend and spike cases.
- FR-4.7: Feed oil consumption alerts into the same anomaly-review flow as engine telemetry anomalies (FR-3.5), so both surface together rather than as separate, disconnected alert channels.
- FR-4.8: Support export of oil consumption data (CSV at minimum) for use in or reconciliation with a standard pilot logbook (paper or app-based, e.g. ForeFlight/LogTen).
- FR-4.9: Include oil consumption trend in exports/reports alongside engine parameter trends (FR-3.6), since both inform engine health, while keeping the underlying data sources separate.

### 3.5 Reporting (Phase 2 — deferred, high-level scope only for now)

Two distinct report types, both exported to Excel rather than viewed only in-app:

- **Per-flight detail report** — pick one flight, get its raw engine telemetry.
- **Longer-term trend report** — a chosen parameter (or set of parameters) plotted/tabulated over a user-specified time range (months/years), not a single flight.

- FR-5.1: Provide a browsable/filterable list of flights (sourced from FDL data, by date/time) showing: date, start time, end time, departure airport, arrival airport, flight duration.
- FR-5.2: Departure/arrival airport ("from"/"to") derivable from GPS position in FDL data at start/end of each flight (nearest airport lookup).
- FR-5.3: Selecting a flight from the list generates an Excel workbook containing line graphs for all engine and flight parameters over the flight's duration (CHT×4, EGT×4, oil temp/pressure, RPM, fuel flow, manifold pressure, airspeed, altitude, OAT, etc.) — full time-series, not just summary stats.
- FR-5.4: Provide summary/trend reports over a user-specified time range (e.g. last N months/years), exported to Excel with both tabular data and charts/graphs (not tabular-only), covering at minimum:
  - Oil consumption rate over time (ties to FR-4.5's rolling-window trend).
  - Average specific fuel consumption (SFC) over time — FDL data includes fuel flow and RPM/MP.
  - Other engine parameters as available (e.g. CHT/EGT trend over time).
- FR-5.5: Full scope of available trend reports to be finalized once FDL sample data confirms exact columns available.

- HMI-1: Menu bar (task bar) item as the sole persistent UI element — no separate always-open window, no Dock icon required. **Implementation: `rumps` (Python menu-bar framework)** as the UI shell, with the Python backend handling all subsystems directly.
- HMI-2: Menu bar icon is color-coded to reflect system state at a glance (e.g. green = nominal/current, yellow = attention needed such as data stale or minor trend, red = alert such as anomaly detected or chart/software out of date). Also reflects transient in-progress states (e.g. chart copying underway per FR-2.4) distinctly from steady-state alerts, so an in-progress copy isn't mistaken for a fault. Exact color/state mapping TBD once alert types are finalized.
- HMI-3: Clicking the menu bar item opens a popover/window containing:
  - Manual data entry (oil change: date, tach hours — FR-4.2).
  - Summary data (currency status of charts/software/nav database; recent flight/engine summary; oil consumption trend).
  - Active alerts, if any (engine anomalies, oil consumption spikes, stale currency data), each linkable to underlying detail.
  - In-progress operations, if any (e.g. chart copy in progress — FR-2.4), so the popover reflects the same state as the notification, not just a static summary.
- HMI-4: Window closes/collapses back to menu bar on dismiss — not a persistent window cluttering the desktop.
- HMI-5: No separate always-running full app window; background service (per NFR-1) with this popover as the only interactive surface.
- HMI-6: **AI Chat Interface** — provide a natural-language query interface for engine data and oil consumption analysis. The user should be able to ask questions in plain English against the historical FDL and oil consumption data, such as:
  - "What were the EGT spreads between cylinders on all leaning events in the last flight?"
  - "What was my oil consumption over the last 100 hours?"
  - "Show CHT trends for cylinder 3 over the last 6 months"
  - "When was my last oil addition and how many hours since?"
  - "Compare fuel flow at cruise between the last 5 flights"
  
  The system should interpret the query, execute it against the time-series database (FR-3.2) and oil consumption records (FR-4.3), and return results as text, tables, or charts as appropriate. This is a Phase 2+ feature — exact implementation (local LLM, API-based, or structured NLQ) TBD based on available tooling at build time.

## 5. Non-Functional Requirements

- NFR-1: Runs unattended as a background service (per your preference) — no GUI required for normal operation; status/logs accessible on demand. **Note:** The menu bar app (HMI-1) *is* the background service (long-running process, no Dock icon). A `launchd` plist should ensure it restarts on login or crash.
- NFR-2: Safe by deletion-only-after-validation — FDL, demo, and snapshot files on the USB drive (FR-1.3/1.4) are only removed after their local copy is verified byte-for-byte correct; chart/software update source data is never deleted automatically.
- NFR-3: Resilient to malformed/partial files (e.g. USB pulled mid-write).
- NFR-4: All credentials stored in macOS Keychain, not plaintext config.
- NFR-5: Runs on macOS without requiring the aircraft to be network-connected (all connectivity happens hangar/home-side).
- NFR-6: System should degrade gracefully — e.g. if chart automation breaks, capture and analysis continue working independently. Treat the three subsystems as decoupled.

## 6. Open Questions to Resolve Before Design

1. ~~**Sample files (top priority)**~~ **RESOLVED (Phase 0 complete):** Drive inspected. Demo files are binary record streams with embedded settings, GPS NMEA, and engine data. Logbook is a manually-saved CSV. Screenshots are SNAP####.PNG. Settings are .bak files. See Phase0-USB-Inspection-Findings.md for full analysis.
2. ~~Single aircraft/single EFIS, or multiple screens/aircraft to support?~~ **RESOLVED:** Single aircraft (N488BF), single EFIS (GRT Horizon HXr).
3. ~~What's your engine instrumentation — how many cylinders, and which specific parameters does your GRT engine monitor module report?~~ **RESOLVED:** Lycoming IO-360, 4-cylinder fuel-injected. CHT×4, EGT×4, oil temp/pressure, RPM, fuel flow, volts/amps, pressure altitude, OAT. Exact FDL CSV column list to be confirmed once FDL is enabled and sample data collected.
4. Any existing baseline "normal" data you'd want the anomaly detection tuned against, or should it self-calibrate from historical data over time?

## 7. Development Phases

1. **Menu bar app shell** — Background `rumps` process with popover UI on click. User-configurable settings (local archive path, USB image path). `launchd` plist for auto-start on login. No functional automation yet — just the running skeleton.

2. **USB detection** — Automatic drive insertion/ejection detection via `DiskArbitration` or equivalent. Identify EFIS drives by volume label + `GRTCHARTS/` flag. Surface insertion/ejection events to the menu bar (status indicator).

3. **Currency downloads (local)** — Scheduled background tasks: Seattle Avionics chart data check (every 12 hours), GRT software/nav database check (daily). Download updates to local archive when available. Notify user on new downloads. **Runs independently of USB operations** — does not block or depend on steps 4/5, and steps 4/5 do not wait for this to complete before proceeding with whatever is currently in the local archive.

4. **Auto-archive EFIS data** — On EFIS drive insertion, automatically archive FDL, demo, logbook, snapshots, and settings files to local storage per FR-1.2–1.10. Validate-then-delete for FDL/demo/snap files. Date-stamp settings. macOS notification on completion with summary. **Runs sequentially before step 5** — all reads from USB complete before any writes begin (better throughput on flash media, and ensures drive state is stable before update logic inspects it).

5. **Auto-update drive & Prepare Drive** — After archiving (step 4), compare drive's chart/nav/software currency against local archive. If stale, stage updated files onto the drive per FR-2.4/2.13. "Prepare Drive" (FR-2.14) formats a fresh USB as FAT32, labels it, creates the `GRTCHARTS/` flag — at which point the system recognizes it as an EFIS drive and the normal auto-update logic triggers automatically (drive is "out of date" because it has no charts/nav/software yet). macOS notification on completion.

6. **Analysis** — Parse archived FDL CSV data into time-series database. Compute per-flight summaries, rolling trends, anomaly detection (FR-3.1–3.8). Oil consumption tracking from logbook + manual entry (FR-4.1–4.9). Visual trend display in the popover and CSV/Excel export.

7. **Natural language reporting** — AI chat interface (HMI-6) for querying engine data, oil consumption, and flight history in plain English. Returns results as text, tables, or charts.
