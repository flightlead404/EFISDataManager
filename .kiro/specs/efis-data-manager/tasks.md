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

- [x] 6.1 Create `fdl_parser.py` — parse FDL CSV into structured records
- [x] 6.2 Design SQLite schema for time-series engine data
- [x] 6.3 Flight segmentation (each FDL file = one operation; flight = IAS > threshold)
- [x] 6.4 Import FDL data into SQLite on archive
- [x] 6.5 Create `analysis.py` — per-flight summary statistics
- [x] 6.6 Rolling trend computation (configurable engine-hour window, default 25hr)
- [x] 6.7 Anomaly detection (absolute exceedances + 2σ statistical)
- [x] 6.8 Configurable thresholds (stored in config)
- [x] 6.9 Oil consumption tracking (oil_events table + logbook enrichment + manual entry)
- [x] 6.10 Oil consumption rate calculation (rolling window)
- [x] 6.11 Alert generation (engine anomalies + oil consumption spikes)
- [x] 6.12 Alert display in menu bar (count + Recent Errors + Diagnostics)
- [x] 6.13 CSV export of per-operation data, trends, oil consumption, raw FDL

## Phase 6.5: Web Dashboard ✅

- [x] Flask dashboard (localhost) launched from menu bar, browser-based
- [x] Operations list with Flight/Ground badge and "flights only" filter
- [x] Dual time-synced charts (engine + flight) with WebGL, progressive resolution
- [x] Clickable alerts jump to timestamp on flight detail charts
- [x] Trends, alerts, oil pages
- [x] Oil events: record changes/additions, cutoff date, red vertical change markers
- [x] Settings page (num_cylinders, thresholds, flight detection, dashboard)
- [x] Logbook CSV as transient enrichment: oil additions → oil_events,
      origin/destination → matched to operations by hourmeter range

## DEFERRED — needs real data

- [ ] **Multi-file FDL stitching for long flights.** GRT rotates to a new FDL
      file when a max size is hit (DEMO files cap ~6.29 MB with +N suffix; FDL
      likely similar). A single long flight (up to ~5 hr) may span multiple FDL
      files, which currently import as SEPARATE operations — splitting one
      flight in two. Need to detect continuation files (small time gap at
      rotation + engine running continuously across the boundary; a new file
      starting with engine already hot = continuation) and merge them into one
      operation. **Blocked until we have a real multi-file flight** to confirm
      rotation behavior: exact time gap at rotation, whether tick counter
      continues or resets, and whether GRT uses +N suffix or sequential numbers.

## Phase 7: Natural Language Reporting

- [ ] 7.1 Evaluate implementation approach (local LLM vs API vs structured NLQ)
- [ ] 7.2 Design query interface (text input → SQL generation → result formatting)
- [ ] 7.3 Implement query execution against SQLite time-series DB
- [ ] 7.4 Result presentation (text, tables, charts)
- [ ] 7.5 Integration with menu bar UI (chat window/popover)
- [ ] 7.6 Example queries: EGT spread, oil consumption, fuel flow trends, leaning events
