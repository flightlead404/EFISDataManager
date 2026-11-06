# FDL Storage & FlightsTo Push — Investigation Findings

Read-only codebase investigation. No code was changed.

---

## Summary Answers

**Q1 — How is the FDL stored?** GRT FDL CSV files arrive on the EFIS USB card
with a header row plus one row per time sample. `fdl_parser.py` maps the GRT
header columns to clean attribute names and builds one `FDLRecord` per row. On
archive, the archiver moves the raw `.csv` off the USB into a dated archive
folder on disk (files are **retained**, not discarded), then imports the parsed
records into a local SQLite DB. Records land in the `fdl_data` table (one row
per sample) keyed by `(timestamp, content_hash)` via `INSERT OR IGNORE`
(record-level dedup). The record stream is segmented at timestamp gaps > 180 s
into **operations** (one engine start/stop cycle); an operation with an airborne
segment (IAS > 40 kt) is flagged `has_flight = 1`, which the dashboard calls a
"flight." Everything is local; nothing leaves the machine.

**Q2 — Can the dashboard push a flight to FlightsTo? NO.** There is zero
FlightsTo integration anywhere in the codebase (no string match for `flightsto`
in any file). The closest existing capability is a **single-flight raw CSV
export** downloaded through the browser ("Export Raw CSV" button on the flight
page → `/api/export/raw/<operation_id>`). There is no GPX export, no network
upload of flight data, no external flight-logging/logbook API client, and no
auth/API-key plumbing for such a service. The project is deliberately local and
offline for flight data (README: flight data stays on the Mac, "it uploads
nothing").

---

## Q1 — FDL Storage Pipeline (end to end)

### 1. Raw FDL file format & the parser (`src/efis_data_manager/fdl_parser.py`)

- **File shape:** first row is a header of GRT column names; each subsequent row
  is one time sample of all flight/engine parameters. Fields may be empty when
  data is unavailable (e.g. IAS at low speed). Module docstring describes each
  file as roughly one engine start/stop cycle.
- **`COLUMN_MAP`** maps ~75 GRT header labels (e.g. `"Indicated Airspeed"`,
  `"Normal Acceleration"`, `"RPM1/N1"`, `"CHT1".."CHT6"`, `"EGT1".."EGT6"`,
  `"Aux1".."Aux6"`, `"Analog1".."Analog8"`, `"Volts1".."Volts3"`) to stable
  Python attribute names, insulating the code from minor firmware header
  changes. `"Normal Acceleration"` → `g_load`.
- **`FDLRecord`** (dataclass) holds one sample: a required `timestamp`
  (`datetime`), an `int` `tick`, and ~70 `Optional[float]` fields covering
  position, attitude/nav, environment, engine, fuel, oil, electrical, aux and
  analog channels. Convenience properties: `engine_running` (`rpm1 > 400`) and
  `airborne` (`indicated_airspeed > 40.0`).
- **`parse_fdl_file(filepath)`** opens the CSV, reads the header, and builds
  `field_indices` (attr → column index) only for columns present in
  `COLUMN_MAP`. It **requires** `Date` and `Time` columns (raises `ValueError`
  otherwise). Returns an `FDLFile` (source filename/path + list of records).
- **`_parse_row`** combines `Date` + `Time` with
  `strptime("%Y-%m-%d %H:%M:%S")` into the timestamp (row skipped if
  date/time missing or unparseable), parses `tick` as int, and every
  `FLOAT_FIELDS` member as float (blank/invalid → `None`).
- **Segmentation primitive:** `segment_records(records, gap_threshold=180)` is a
  pure function that cuts the timestamp-ordered stream wherever consecutive
  samples differ by **more than** `GAP_THRESHOLD_SECONDS` (180 s / 3 min; a gap
  of exactly 180 s stays in one run). This defines operation boundaries.
- **Sampling-rate note (relevant to the open backlog item):** the parser
  assumes 1 Hz throughout — the docstrings say "1-second samples," `airborne`
  seconds and summary stats are computed as `len(records)` (1 sample == 1 s),
  and timestamps are parsed at whole-second resolution (`%H:%M:%S`, no
  sub-second/millisecond field). A 200 ms recording interval would break those
  1-sample-==-1-second assumptions and collapse sub-second samples onto
  duplicate whole-second timestamps.

### 2. Database (`src/efis_data_manager/database.py`)

- **Physical location on disk:** `DB_DIR = ~/EFIS/DataManagerLogs`,
  `DB_PATH = ~/EFIS/DataManagerLogs/efis_data.sqlite` (module constants).
  SQLite opened WAL mode, `foreign_keys=ON`.
- **Tables:** `operations` (one per segmented engine cycle), `fdl_data`
  (per-sample time series), `oil_events`, `schema_info`. (A legacy `logbook`
  table is dropped on open — the logbook CSV is now a transient enrichment
  source, not stored.)

#### `fdl_data` schema (one row per sample)

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | row id |
| `operation_id` | INTEGER NOT NULL | FK → `operations(id)` |
| `timestamp` | TEXT NOT NULL | ISO 8601; half of the identity key |
| `content_hash` | TEXT | SHA-256 of all `FDLRecord` fields except timestamp; other half of identity key |
| `tick` | INTEGER | |
| `latitude` | REAL | |
| `longitude` | REAL | |
| `ground_speed` | REAL | |
| `track` | REAL | |
| `gps_altitude` | REAL | |
| `roll` | REAL | |
| `pitch` | REAL | |
| `mag_heading` | REAL | |
| `pressure_altitude` | REAL | |
| `indicated_altitude` | REAL | |
| `vertical_speed` | REAL | |
| `density_altitude` | REAL | |
| `indicated_airspeed` | REAL | |
| `true_airspeed` | REAL | |
| `g_load` | REAL | |
| `oat` | REAL | |
| `wind_speed` | REAL | |
| `wind_direction` | REAL | |
| `rpm1` | REAL | |
| `rpm2` | REAL | |
| `cht1`–`cht6` | REAL | 6 cylinder-head temps |
| `egt1`–`egt6` | REAL | 6 exhaust-gas temps |
| `fuel_flow` | REAL | |
| `fuel_total` | REAL | fuel-remaining gauge (counts down; steps up on refuel reset) |
| `oil_temp` | REAL | |
| `oil_pressure` | REAL | |
| `eis_volts` | REAL | |
| `volts1` | REAL | |
| `hourmeter` | REAL | |
| `internal_map` | REAL | |
| `aux1`–`aux6` | REAL | user-configurable EIS aux channels (see `aux_map.py`) |

Note: not every parsed `FDLRecord` field is persisted. Fields such as
`mag_variation`, `desired_track`, `cross_track_error`, `mag_field_heading`,
`carb_temp`, `coolant_temp`, `r_n1/r_n2`, `analog1-8`, `volts2/volts3`,
`wind_*`-adjacent extras are parsed into the record (and so feed the
`content_hash`) but have no dedicated `fdl_data` column — they are dropped at
insert time (`_insert_fdl_data` lists the exact persisted column set).

#### Indexes on `fdl_data`
- `idx_fdl_data_operation` on `(operation_id)`
- `idx_fdl_data_timestamp` on `(timestamp)`
- `idx_fdl_data_op_ts` on `(operation_id, timestamp)`
- `idx_fdl_data_record_identity` — **UNIQUE** on `(timestamp, content_hash)`
  (created in `_ensure_schema` after the `content_hash` column is guaranteed).

#### Record identity, content_hash, and insert behavior
- **Identity key = `(timestamp, content_hash)`.** `record_content_hash(record)`
  (SHA-256 over every `FDLRecord` field value **except** `timestamp`; filename
  and row/operation id deliberately excluded) produces `content_hash`.
- **Insert = `INSERT OR IGNORE`** on that unique index (`_insert_fdl_data`), so
  re-importing the same samples — even under a different filename — adds no
  rows (returns count of newly inserted rows). `_attach_run_records` then points
  the run's rows at the correct `operation_id`.

#### `operations` table & flight/operation segmentation
- One row per **operation** (a maximal contiguous run with all consecutive gaps
  ≤ 180 s). An operation may span multiple rotated files (merged) or be one of
  several runs cut from a single multi-session file (split).
- **Operation convenience/derived key:** `(start_time, end_time, record_count)`.
  `import_operation` finds-or-creates on that key (no separate unique
  constraint; a legacy `UNIQUE(source_filename, start_time)` is rebuilt away
  because GRT filenames cycle and are not identity).
- Columns include: `id`, `source_filename` (informational provenance label of
  contributing filename(s), nullable, **not** an identity key), `start_time`,
  `end_time`, `duration_seconds`, `record_count`, `has_flight`
  (1 if any airborne sample), `airborne_seconds`, `date`, `imported_at`, plus
  computed summaries (`max_gps_altitude`, `max_pressure_altitude`,
  `max_ground_speed`, `max_indicated_airspeed`, `max_rpm`, `avg_rpm_cruise`,
  `max_cht`, `avg_cht_cruise`, `max_egt`, `avg_egt_cruise`, `max_oil_temp`,
  `min_oil_pressure`, `fuel_used`, `avg_fuel_flow_cruise`, `hourmeter_start`,
  `hourmeter_end`) and logbook enrichment (`origin`, `destination`).
- Indexes: `idx_operations_date (date)`, `idx_operations_flight (has_flight)`,
  `idx_operations_derived_key (start_time, end_time, record_count)`.
- `GAP_THRESHOLD_SECONDS = 180` lives in `fdl_parser.py`. `duration_seconds` =
  end − start; `airborne_seconds` = count of samples with `airborne` True
  (assumes 1 Hz).

#### Import entry points
- `import_fdl_files(fdl_files)` (used by the archiver): pools all records across
  candidate files, **stable-sorts by timestamp only** (never by filename),
  dedups exact duplicates, segments, and imports each run as one operation.
- `import_fdl_file(fdl)`: single-file wrapper over the same segmentation.
- Two guarded one-time migrations run on DB open: `migrate_resegment_operations`
  (re-segments already-imported rows from `fdl_data`, the source of truth) and
  `migrate_recompute_fuel_used` (reset-safe fuel recompute).

### 3. Raw file retention / archiving (`src/efis_data_manager/archiver.py`)

- Raw source files are **kept**. `archive_efis_drive` moves
  `GRT FDL*.csv` from the USB mount into
  `<archive_path>/FDL/<YYYY-MM-DD>/` with a SHA-256 copy-verify
  (`_move_file`), then deletes from USB only after the archived copy is
  validated. `archive_path` default (`config.py` `DEFAULT_CONFIG`) is
  `~/Documents/EFIS_Archive`.
- Immediately after archiving, `_import_fdl_to_database` parses the archived
  files and calls `database.import_fdl_files`. So on disk you end up with **both**
  the retained raw CSVs under `~/Documents/EFIS_Archive/FDL/<date>/` **and** the
  parsed samples in `~/EFIS/DataManagerLogs/efis_data.sqlite`.
- `manifest.py` is unrelated to FDL retention — it implements SHA-256 content
  manifests for the **chart-drive sync** engine (`ManifestEntry`,
  `FamilyManifest`, `CardManifest`, `Manifest_Builder/Store`, `diff_family`),
  not flight-data archiving.

### 4. Data model summary
- **One `fdl_data` row = one sample** (nominally one second).
- **Operation** = one engine start/stop cycle = a contiguous run of samples with
  no > 180 s gap. Stored in `operations`.
- **Flight** = an operation whose samples went airborne (IAS > 40 kt) →
  `has_flight = 1`. Not a separate table; it is a flagged subset of
  `operations`. The dashboard's "flight list" is the operations list.
- **Persisted per sample:** timestamp + content hash + tick + the ~45 columns in
  the `fdl_data` table above (position, attitude/nav, environment, engine
  (rpm/CHT×6/EGT×6), fuel, oil, electrical, hourmeter, internal MAP, aux1–6).

---

## Q2 — Can the dashboard push a flight to FlightsTo?

### Current capability: **NO.**

- A full-codebase search for `flightsto` returns **no matches** in any file
  (source, templates, tests, docs). There is no FlightsTo client, no
  "share"/"publish"/"send to" feature, and no OAuth/API-key configuration for
  any external flight-logging service anywhere.
- The only network egress in the project is **inbound fetching** of chart/nav
  updates from Seattle Avionics and GRT (`requests`, `cloudscraper`,
  `playwright` in `requirements.txt`; `drive_updater.py`). No code posts flight
  data outward. `README.md` states flight data stays local and the tool
  "uploads nothing."

### Closest existing capability (what a user can do today)

- **Single-flight raw CSV export.** The flight page
  (`dashboard/templates/flight.html`) has an **"Export Raw CSV"** button whose
  href is wired to `/api/export/raw/${stats.operation_id}`. That route
  (`dashboard/app.py` `api_export_raw` → `export.export_flight_raw`) streams a
  browser download via Flask `send_file(as_attachment=True)`.
- `export.export_flight_raw(operation_id)` writes every `fdl_data` row for the
  operation (all columns except `id`/`operation_id`) in `timestamp` order to
  `~/EFIS/Exports/fdl_raw_<date>_op<id>.csv`.
- Other exports exist but are not per-flight-to-service: `export_flight_summaries`
  (all operations, one row each), `export_trends`, `export_oil_consumption` —
  all **CSV, all local file downloads** (`/api/export/flights`, `/api/export/oil`).
- **No GPX / KML / track export exists.** Export is CSV only. Position columns
  (`latitude`, `longitude`, `gps_altitude`, `timestamp`) are present in the raw
  CSV, so a GPS track is derivable, but nothing emits a track-format file.

### Gap between "today" and one-click "push this flight to FlightsTo"

1. **No destination integration.** No FlightsTo API client, endpoint, or any
   outbound POST of flight data. This would be net-new (`requests` is already a
   dependency, so no new runtime dep is strictly required).
2. **No auth plumbing.** No storage or UI for a FlightsTo account token/API key.
   `config.py` has no field for it; a secure credential store + settings UI
   would be needed.
3. **Data-format mismatch (unverified against FlightsTo).** The app emits CSV
   (and the per-flight raw CSV is EFIS-column-shaped, not a standard track
   format). FlightsTo's expected upload format was **not** researched per the
   brief; a mapping/transform step (likely to GPX/KML or whatever FlightsTo
   accepts) would be required. Position + timestamp data to build a track does
   exist in `fdl_data`.
4. **No UI affordance.** The flight page has only "Export Raw CSV" / "GAMI Lean
   Test" / "Back to Flights". A "Push to FlightsTo" button and a backing route
   (e.g. `POST /api/flight/<id>/push-flightsto`) would be new.
5. **Network-egress posture.** The project is intentionally local/offline for
   flight data and advertises "uploads nothing." Adding outbound flight upload
   is a deliberate posture change that should be explicit/opt-in and documented.

---

## Conclusions & recommendations (nothing implemented)

- **Q1:** The storage pipeline is clear and local: raw FDL CSV → parser →
  `import_fdl_files` (timestamp-pooled, gap-segmented, record-deduped) →
  `operations` + `fdl_data` in `~/EFIS/DataManagerLogs/efis_data.sqlite`; raw
  CSVs retained under `~/Documents/EFIS_Archive/FDL/<date>/`. One row per
  sample; flight = airborne operation. Worth flagging for the separate
  variable-sampling-rate backlog item: the 1 Hz assumption is baked into
  timestamp resolution (`%H:%M:%S`), `airborne_seconds`/duration math, and the
  "1-second sample" semantics — a 200 ms interval would need those addressed.
- **Q2:** No FlightsTo push exists. If the user wants it, the smallest credible
  increment is: (a) add a track exporter (GPX/KML) derived from the existing
  lat/lon/alt/time columns, reusing the `export.py` pattern; (b) add a
  FlightsTo client + credential storage + settings UI; (c) add a
  "Push to FlightsTo" button on `flight.html` and a backing `POST` route; (d)
  confirm FlightsTo's accepted upload format and auth model (explicitly out of
  scope for this codebase-only investigation). Until then, the manual path is
  "Export Raw CSV and upload it yourself."

### Key file/symbol citations
- `src/efis_data_manager/fdl_parser.py`: `COLUMN_MAP`, `FLOAT_FIELDS`,
  `FDLRecord`, `Operation`, `GAP_THRESHOLD_SECONDS`, `segment_records`,
  `parse_fdl_file`, `_parse_row`.
- `src/efis_data_manager/database.py`: `DB_DIR`/`DB_PATH`, `SCHEMA_SQL`
  (`operations`, `fdl_data`), `record_content_hash`, `import_fdl_files`,
  `import_operation`, `_insert_fdl_data`, `_compute_operation_summary`.
- `src/efis_data_manager/archiver.py`: `archive_efis_drive`, `_move_file`,
  `_import_fdl_to_database`.
- `src/efis_data_manager/export.py`: `export_flight_raw`,
  `export_flight_summaries`, `export_trends`, `export_oil_consumption`,
  `EXPORT_DIR`.
- `src/efis_data_manager/dashboard/app.py`: `api_export_raw`
  (`/api/export/raw/<operation_id>`), `api_export_flights`, `api_export_oil`.
- `src/efis_data_manager/dashboard/templates/flight.html`: `#export-raw` button.
- `src/efis_data_manager/config.py`: `DEFAULT_CONFIG["archive_path"]`.
- `README.md`: "uploads nothing" / flight data stays local.
