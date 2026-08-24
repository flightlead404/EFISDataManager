# Implementation Tasks

## Phase 1: Menu Bar App Shell ✅

- [x] Project structure (pyproject.toml, src layout, venv, dependencies)
- [x] rumps menu bar app with status, settings, quit
- [x] Native folder picker for archive path and USB image path
- [x] JSON config persistence in ~/Library/Application Support/EFISDataManager
- [x] Seattle Avionics credential entry (stored in macOS Keychain)
- [x] launchd plist for auto-start on login
- [x] Verified running in menu bar

## Phase 2: USB Detection ✅

- [x] 2.1 USB mount/unmount detection (polling /Volumes/ every 2s — simpler than DiskArbitration)
- [x] 2.2 Create `usb_monitor.py` module with start/stop lifecycle
- [x] 2.3 Implement EFIS drive identification (volume label "EFIS" or GRTCHARTS/ present)
- [x] 2.4 Integrate USB monitor with app.py (background thread, main-thread dispatch for UI)
- [x] 2.5 Drive status in menu ("Drive: Connected" / "Drive: Not connected") + title indicator (● EFIS)
- [x] 2.6 macOS notification on EFIS drive detection and ejection
- [x] 2.7 Eject Drive menu item (diskutil eject)
- [x] 2.8 Tested with actual EFIS USB drive insertion/ejection

## Phase 3: Currency Downloads (Local)

- [x] 3.1 Implement macOS Keychain credential storage for Seattle Avionics
- [x] 3.2 Implement Seattle Avionics login (ASP.NET form POST with ViewState)
- [x] 3.3 Parse Installation.aspx download table (de-duplicate by URL)
- [x] 3.4 Compare remote cycle dates against local cycle metadata
- [x] 3.5 Download new chart zips when available (plain HTTP, large files)
- [x] 3.6 Extract zips with per-file passwords into correct USB image subdirectories
- [x] 3.7 Verify zipfile vs pyzipper for extraction (ZipCrypto vs AES)
- [x] 3.8 Implement GRT nav DB version checking and download (Playwright for Sucuri bypass)
- [x] 3.9 Download new GRT nav DB files to USB image (proc + non-proc, both EFIS units)
- [x] 3.10 Background scheduler (12hr charts, 24hr nav DB) using rumps.Timer
- [x] 3.11 Notifications on successful download / failure alerts
- [x] 3.12 Handle "page layout changed" detection (scrape breakage alert)

**Note:** HXr/Mini A/P EFIS software updates are manual-check only (grtavionics.com is behind Sucuri bot protection, and software updates are infrequent). Nav DB is fully automated via Playwright.

## Phase 4: Auto-Archive EFIS Data ✅

- [x] 4.1 Create `archiver.py` module with file type identification
- [x] 4.2 Implement FDL CSV file detection and move (GRT FDL*.csv)
- [x] 4.3 Implement DEMO file detection and move (DEMO-*.LOG)
- [x] 4.4 Implement snapshot detection and move (SNAP*.PNG)
- [x] 4.5 Implement logbook CSV detection and copy (Logbook*.csv)
- [x] 4.6 Implement settings backup copy with date stamp (*.bak)
- [x] 4.7 SHA-256 validation before USB file deletion
- [x] 4.8 Skip already-archived files (filename+size dedup)
- [x] 4.9 Clean GRTCHARTS/ contents, remove E:ChartData/ if present
- [x] 4.10 Integrate with USB monitor (trigger archive on drive detection)
- [x] 4.11 Status updates during archive
- [x] 4.12 Completion notification with summary
- [x] 4.13 Error handling: partial archive on drive pull, corrupt files
- [x] 4.14 Logging to stderr.log via Python logging

## Phase 5: Auto-Update Drive & Prepare Drive ✅

- [x] 5.1 Create `drive_updater.py` module
- [x] 5.2 Implement currency check (compare USB vs local image — size-based comparison)
- [x] 5.3 Implement delta sync (rsync --checksum for ChartData, shutil.copy2 for individual files)
- [x] 5.4 Trigger auto-update after archive completes (sequential pipeline)
- [x] 5.5 Status updates and completion notification
- [x] 5.6 Implement "Prepare Drive" — format FAT32, label "EFIS", create GRTCHARTS/, populate
- [x] 5.7 Tested full pipeline: insert → archive → update → notify (ChartData rsync ~1hr, all files synced)

## Phase 6: Analysis

- [ ] 6.1 Create `fdl_parser.py` — parse FDL CSV into structured records
- [ ] 6.2 Design SQLite schema for time-series engine data
- [ ] 6.3 Flight segmentation (split FDL data into individual flights by GPS/time gaps)
- [ ] 6.4 Import FDL data into SQLite on archive
- [ ] 6.5 Create `analysis.py` — per-flight summary statistics
- [ ] 6.6 Rolling trend computation (10/25/50 hour windows)
- [ ] 6.7 Anomaly detection with altitude/OAT normalization
- [ ] 6.8 Configurable thresholds (stored in config)
- [ ] 6.9 Oil consumption tracking (logbook CSV import + manual entry)
- [ ] 6.10 Oil consumption rate calculation (25+ hour rolling window)
- [ ] 6.11 Alert generation (engine anomalies + oil consumption spikes)
- [ ] 6.12 Alert display in menu bar popover
- [ ] 6.13 CSV/Excel export of per-flight data and trend reports

## Phase 7: Natural Language Reporting

- [ ] 7.1 Evaluate implementation approach (local LLM vs API vs structured NLQ)
- [ ] 7.2 Design query interface (text input → SQL generation → result formatting)
- [ ] 7.3 Implement query execution against SQLite time-series DB
- [ ] 7.4 Result presentation (text, tables, charts)
- [ ] 7.5 Integration with menu bar UI (chat window/popover)
- [ ] 7.6 Example queries: EGT spread, oil consumption, fuel flow trends, leaning events
