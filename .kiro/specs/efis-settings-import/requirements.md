# Requirements Document

## Introduction

The EFIS Settings Import feature reads the GRT HXr EFIS's own settings backup
files (Settings.bak / Settings.dat) and populates the analysis dashboard's
alert thresholds (caution / redline limits) from the values configured in the
aircraft. Today those dashboard thresholds are hand-entered; this feature makes
them derive automatically from the aircraft's actual configuration. The import
also adds new alert thresholds the dashboard does not have today (an RPM
redline, a CHT shock-cooling rate limit, and an IAS-based Vne companion to the
existing TAS-based Vne) and can auto-set the analysis cylinder count from the
EFIS settings.

Because these thresholds drive safety alerts, the import is READ-ONLY (it never
modifies the aircraft's backup files) and is never applied silently. When a
first or new settings backup is detected, the system offers a preview of every
threshold change (current value → imported value) and requires two explicit
confirmations before overwriting.

Percent-power / engine power-map import is explicitly OUT OF SCOPE for this
feature (deferred to the separate `percent-power` spec, pending GRT documenting
the power-map SIDs).

## Glossary

- **EFIS**: The GRT HXr Electronic Flight Instrument System in the aircraft.
- **Settings_Backup**: A GRT settings file written to the EFIS USB drive, named
  `Settings.bak` or `Settings.dat`. Plain ASCII, one `KEY=VALUE` per line.
- **SID (Setting_Identifier)**: The numeric key on the left of a `KEY=VALUE`
  line in a Settings_Backup (e.g. `151` in `151=400`).
- **UPDATE_Value**: The integer value of the `UPDATE=` line in a
  Settings_Backup. UPDATE_Value is monotonic only WITHIN a single EFIS display;
  each display maintains its own counter, so UPDATE_Value is not comparable
  across two displays. UPDATE_Value can also be low or reset (e.g. `1`), so it
  is a reliable ordering signal only when comparing backups from the same
  Source_Key.
- **Mode_S_Address**: The ICAO 24-bit aircraft address read from SID 1063
  (e.g. `A60670`), globally unique per aircraft.
- **Flight_ID**: The Flight ID / N-number read from SID 1062.
- **Link_ID**: The Inter-Display Link ID (also called DULINK_ID,
  SID_DULINK_ID) read from SID 387 (`0`=Auto, `1`=Primary, `2`-`254`=other),
  which distinguishes the displays within one aircraft.
- **Primary_Display**: The GRT display whose Settings_Backup reports SID 387
  (Link_ID / DULINK_ID) equal to `1`. Primary_Display status is the sole,
  device-agnostic discriminator for import eligibility: it does NOT depend on
  the display model or class (Sport, Horizon, HXr, Mini, or any future model).
  A display reporting SID 387 in `{0, 2-254}` is not a Primary_Display.
- **Bound_Import_Source**: The Source_Key that becomes the authoritative source
  for threshold imports, bound to the Primary_Display's Source_Key on the first
  successful import. After binding, imports are offered only from the
  Bound_Import_Source until the user explicitly changes it.
- **Source_Key**: The identity that uniquely identifies the EFIS display that
  wrote a Settings_Backup, formed by combining the Mode_S_Address (SID 1063)
  and the Link_ID (SID 387). WHERE an identity SID is missing, the Source_Key
  falls back to the available identity SIDs, and WHERE no identity SID is
  readable the Source_Key is undetermined.
- **Content_Hash**: A hash computed over the parsed SID/value content of a
  Settings_Backup, used as a secondary signal to detect whether a backup's
  content differs from the last imported content.
- **Import_Marker**: A per-Source_Key record persisted after a successful
  import, storing that source's last-imported UPDATE_Value, Content_Hash, and
  import timestamp. Import_Markers are tracked per Source_Key, not globally.
- **Checksum_Lines**: The `CHECKSIZE=` and `CHECKSUM=` lines a Settings_Backup
  carries for integrity verification.
- **Settings_Parser**: The component that reads a Settings_Backup file and
  produces a map of SID → value.
- **Settings_Selector**: The component that, given a `.bak` and/or `.dat` pair,
  chooses the current (newer, integrity-verified) Settings_Backup.
- **Settings_Mapper**: The component that translates parsed SID values into
  dashboard threshold keys, applying units, rounding, and disabled-value rules.
- **Import_Workflow**: The user-facing flow that previews and applies imported
  thresholds to the dashboard.
- **Archiver**: The existing component (`archiver.py`) that copies EFIS files
  from the USB drive to the date-stamped local archive.
- **Dashboard_Thresholds**: The analysis alert-threshold set stored under the
  `analysis_thresholds` config key (defaults in `analysis.py`
  `DEFAULT_THRESHOLDS`), e.g. `cht_caution`, `cht_redline`, `vne_tas_redline`.
- **Caution_Tier**: A dashboard threshold that raises a lower-severity caution.
- **Redline_Tier**: A dashboard threshold that raises a higher-severity warning.
- **Disabled_Value**: A SID value that the EFIS uses to mean "limit not set"
  (e.g. `0` for Max EGT Span or Min RPM), which the system treats as "skip".
- **Temp_Units**: Temperature units in effect, read from SID 345
  (`0`=Fahrenheit, `1`=Celsius).
- **RPM_Redline_Threshold**: A new Dashboard_Threshold (`rpm_redline`) that
  raises a Redline_Tier warning when engine RPM is at or above the imported
  Max RPM value.
- **CHT_Cooling_Rate_Threshold**: A new Dashboard_Threshold
  (`cht_cooling_rate_max`) that raises an alert when CHT falls faster than the
  imported maximum cooling rate (shock-cooling). Per GRT EIS4000 documentation,
  SID 150 is expressed in **degrees per minute** (degrees in the EFIS's
  configured Temp_Units).
- **IAS_Vne_Threshold**: A Dashboard_Threshold (`vne_ias_redline`) that raises
  a Redline_Tier warning when indicated airspeed is at or above the imported
  Vne, used when the EFIS is configured for an IAS-based Vne.
- **Num_Cylinders_Config**: The existing dashboard config value `num_cylinders`
  used throughout analysis, which this feature can auto-set from the EIS
  cylinder-count settings.
- **Archive_Date**: The calendar date encoded in an archived Settings_Backup's
  filename datestamp (`Settings-YYYY-MM-DD`), stamped by the Archiver at copy
  time. Archive_Date is the only reliable cross-capture "when" signal, because
  UPDATE_Value is not globally monotonic on this EFIS and EFIS-written file
  modification times are unreliable FAT timestamps.
- **Selected_Changes**: The subset of proposed threshold changes (and the
  Num_Cylinders_Config change) that the user has marked for import in the
  current import preview session. Defaults to all proposed changes and is not
  persisted between import sessions.

## Requirements

### Requirement 1: Parse a Settings backup file

**User Story:** As a pilot doing ground support, I want the app to read the
EFIS settings backup file, so that its configured limits can be used without
hand-entering them.

#### Acceptance Criteria

1. WHEN a Settings_Backup file is provided, THE Settings_Parser SHALL read the
   file as plain ASCII and produce a map of SID to value from each valid
   `KEY=VALUE` line.
2. WHEN a Settings_Backup line is not a well-formed `KEY=VALUE` pair, THE
   Settings_Parser SHALL skip that line and continue parsing remaining lines.
3. WHEN a Settings_Backup contains a SID that the system does not recognize,
   THE Settings_Parser SHALL retain the line during parsing and THE
   Settings_Mapper SHALL ignore the unrecognized SID.
4. THE Settings_Parser SHALL read the `UPDATE=` line as the UPDATE_Value of the
   file.
5. THE Settings_Parser SHALL read the `CHECKSIZE=` and `CHECKSUM=` lines as the
   Checksum_Lines of the file.
6. IF a Settings_Backup file cannot be read or contains no valid `KEY=VALUE`
   lines, THEN THE Settings_Parser SHALL report a parse failure and SHALL leave
   the source file unmodified.

### Requirement 2: Select the current backup from the .bak/.dat pair

**User Story:** As a pilot, I want the app to use my most recent EFIS settings,
so that the imported limits reflect the aircraft's current configuration.

#### Acceptance Criteria

1. WHEN both a `.bak` and a `.dat` Settings_Backup are present, THE
   Settings_Selector SHALL parse both and SHALL select the file with the higher
   UPDATE_Value as the current Settings_Backup.
2. WHEN only one of the `.bak` or `.dat` Settings_Backup is present, THE
   Settings_Selector SHALL select that file as the current Settings_Backup.
3. WHEN evaluating a Settings_Backup for selection, THE Settings_Selector SHALL
   verify the file against its Checksum_Lines.
4. IF a candidate Settings_Backup fails Checksum_Lines verification AND another
   candidate passes, THEN THE Settings_Selector SHALL select the candidate that
   passes verification.
5. IF all candidate Settings_Backup files fail Checksum_Lines verification,
   THEN THE Settings_Selector SHALL report a verification failure and SHALL NOT
   select a file for import.

### Requirement 3: Archive both .bak and .dat settings files

**User Story:** As a pilot, I want every settings backup snapshot preserved in
the archive, so that I keep a dated history of the aircraft's configuration.

#### Acceptance Criteria

1. WHEN the Archiver processes the EFIS USB drive, THE Archiver SHALL copy each
   present `Settings.dat`, `State.dat`, `Plan.dat`, and `WP.dat` file to the
   date-stamped Settings archive in addition to the existing `.bak` files.
2. THE Archiver SHALL copy each Settings_Backup file without deleting or
   modifying the source file on the USB drive.
3. WHEN a date-stamped copy of a Settings_Backup with identical size already
   exists in the archive, THE Archiver SHALL skip re-copying that file.

### Requirement 4: Map engine limit SIDs to dashboard thresholds

**User Story:** As a pilot, I want the EFIS engine limits imported into the
dashboard, so that engine alert thresholds match the aircraft.

#### Acceptance Criteria

1. THE Settings_Mapper SHALL map oil pressure SIDs to Dashboard_Thresholds:
   SID 120 (Min Cruise Oil Press) to `oil_pressure_low_cruise` and SID 118
   (Max Oil Press) to `oil_pressure_high`.
2. THE Settings_Mapper SHALL map SID 121 (Max Oil Temp) to the Oil Temp limit
   selected by Requirement 8.
3. THE Settings_Mapper SHALL map SID 151 (Max CHT) to the CHT limit selected by
   Requirement 8.
4. THE Settings_Mapper SHALL map SID 144 (Max EGT) to the EGT limit selected by
   Requirement 8.
5. THE Settings_Mapper SHALL map SID 147 (Max EGT Span) to `egt_spread_caution`.
6. THE Settings_Mapper SHALL map SID 1463 (Min Fuel Press) to
   `fuel_pressure_low` and SID 1464 (Max Fuel Press) to `fuel_pressure_high`.
7. THE Settings_Mapper SHALL map SID 353 (Min V Bus1) to `voltage_low` and SID
   354 (Max V Bus1) to `voltage_high`.
8. THE Settings_Mapper SHALL map SID 123 (Max RPM) to the
   RPM_Redline_Threshold (`rpm_redline`).
9. THE Settings_Mapper SHALL map SID 150 (Max CHT Cooling Rate) to the
   CHT_Cooling_Rate_Threshold (`cht_cooling_rate_max`).
10. WHERE a mapped engine SID holds a Disabled_Value, THE Settings_Mapper SHALL
    skip that SID and SHALL leave the corresponding Dashboard_Threshold
    unchanged.

### Requirement 5: Map airspeed SIDs to dashboard thresholds

**User Story:** As a pilot, I want the EFIS airspeed limits imported, so that
airspeed alerts match the aircraft.

#### Acceptance Criteria

1. THE Settings_Mapper SHALL read SID 22 (Vne) as the never-exceed speed in
   knots.
2. THE Dashboard_Thresholds SHALL provide both a true-airspeed Vne limit
   (`vne_tas_redline`) and an IAS_Vne_Threshold (`vne_ias_redline`).
3. WHEN SID 889 (Convert Vne from TAS to IAS) equals `1`, THE Settings_Mapper
   SHALL store the imported Vne in `vne_tas_redline` and SHALL clear
   `vne_ias_redline` so that only the true-airspeed limit is active.
4. WHEN SID 889 equals `0`, THE Settings_Mapper SHALL store the imported Vne in
   `vne_ias_redline` and SHALL clear `vne_tas_redline` so that only the
   indicated-airspeed limit is active.
5. WHILE `vne_tas_redline` is the active airspeed limit, THE Dashboard SHALL
   raise the Vne Redline_Tier warning when true airspeed is at or above
   `vne_tas_redline`.
6. WHILE `vne_ias_redline` is the active airspeed limit, THE Dashboard SHALL
   raise the Vne Redline_Tier warning when indicated airspeed is at or above
   `vne_ias_redline`.
7. WHERE an imported airspeed SID holds a Disabled_Value, THE Settings_Mapper
   SHALL skip that SID and SHALL leave the corresponding Dashboard_Threshold
   unchanged.

### Requirement 6: Map G-meter SIDs to dashboard thresholds

**User Story:** As a pilot, I want the EFIS G-load limits imported, so that
G-load alerts match the aircraft.

#### Acceptance Criteria

1. THE Settings_Mapper SHALL map SID 331 (G Caution Max) to `g_pos_caution` and
   SID 329 (G Max) to `g_pos_limit`.
2. THE Settings_Mapper SHALL map SID 332 (G Caution Min) to `g_neg_caution` and
   SID 330 (G Min) to `g_neg_limit`.
3. THE Settings_Mapper SHALL preserve the sign of negative-G SID values so that
   `g_neg_caution` and `g_neg_limit` are stored as negative numbers.
4. WHERE an imported G-meter SID holds a Disabled_Value, THE Settings_Mapper
   SHALL skip that SID and SHALL leave the corresponding Dashboard_Threshold
   unchanged.

### Requirement 7: Apply units and rounding to imported values

**User Story:** As a pilot, I want imported values to be clean and in the units
I work in, so that the dashboard thresholds are readable and correct.

#### Acceptance Criteria

1. THE Settings_Mapper SHALL round imported G-load thresholds to one decimal
   place.
2. THE Settings_Mapper SHALL round imported voltage thresholds to one decimal
   place.
3. THE Settings_Mapper SHALL round imported temperature and speed thresholds to
   the nearest integer.
4. WHEN SID 345 (Temp Units) equals `1` (Celsius), THE Settings_Mapper SHALL
   convert imported temperature values to the dashboard's Fahrenheit thresholds.
5. WHEN SID 345 equals `0` (Fahrenheit), THE Settings_Mapper SHALL apply
   imported temperature values as Fahrenheit without conversion.

### Requirement 8: Prompt the user for caution-vs-redline mapping

**User Story:** As a pilot, I want to choose whether a single EFIS limit becomes
a dashboard caution or redline, so that the imported thresholds match how I
intend the alerts to work.

#### Acceptance Criteria

1. WHERE the EFIS provides a single limit for a concept that has both a
   Caution_Tier and a Redline_Tier in the dashboard (CHT, EGT, Oil Temp), THE
   Import_Workflow SHALL ask the user whether to map that limit to the
   Caution_Tier or the Redline_Tier.
2. IF a chosen mapping would set a Caution_Tier value at or above its
   corresponding Redline_Tier value, THEN THE Import_Workflow SHALL warn the
   user and SHALL NOT apply that mapping until the user resolves the conflict.
3. THE Import_Workflow SHALL map both tiers directly from the EFIS for the
   G-meter, applying caution SIDs to caution thresholds and max/min SIDs to
   limit thresholds without prompting for tier selection.

### Requirement 9: Preview and confirm before applying

**User Story:** As a pilot, I want to see exactly what will change and confirm
it, so that I never silently overwrite my safety-alert thresholds.

#### Acceptance Criteria

1. WHEN a Settings_Backup is detected during archive or import AND that backup
   qualifies as genuinely newer for its Source_Key per Requirement 14, THE
   Import_Workflow SHALL offer to apply the imported thresholds.
2. WHEN the user chooses to review an import, THE Import_Workflow SHALL present
   a preview listing each affected Dashboard_Threshold with its current value
   and the proposed imported value.
3. THE Import_Workflow SHALL inform the user that applying the import will
   overwrite the current Dashboard_Thresholds.
4. WHEN the user requests to apply the import, THE Import_Workflow SHALL require
   a second confirmation before writing any Dashboard_Threshold.
5. WHEN the user confirms twice, THE Import_Workflow SHALL update the
   Dashboard_Thresholds with the imported values and SHALL leave thresholds
   with no imported value unchanged.
6. IF the user declines at any confirmation step, THEN THE Import_Workflow SHALL
   leave all Dashboard_Thresholds unchanged.

### Requirement 10: Read-only and fault-tolerant behavior

**User Story:** As a pilot, I want the import to be safe and robust, so that a
bad backup file never crashes the app or corrupts my settings.

#### Acceptance Criteria

1. THE Settings_Parser SHALL treat every Settings_Backup as read-only and SHALL
   NOT modify the source file.
2. WHEN a Settings_Backup contains malformed lines, unknown SIDs, missing
   Checksum_Lines, or a missing UPDATE_Value, THE Settings_Parser SHALL handle
   the condition and SHALL continue without terminating abnormally.
3. IF an import cannot be completed, THEN THE Import_Workflow SHALL report the
   reason and SHALL leave all Dashboard_Thresholds unchanged.

### Requirement 11: Alert on RPM redline

**User Story:** As a pilot, I want the dashboard to flag when engine RPM reaches
the aircraft's Max RPM, so that over-speed events are surfaced in analysis.

#### Acceptance Criteria

1. WHILE the RPM_Redline_Threshold holds an imported value, THE Dashboard SHALL
   raise a Redline_Tier alert during per-flight episode detection for each
   sample where engine RPM is at or above the RPM_Redline_Threshold.
2. WHILE the RPM_Redline_Threshold holds an imported value, THE Dashboard SHALL
   include an RPM redline entry in the flight-summary alerts when engine RPM is
   at or above the RPM_Redline_Threshold during the flight.
3. WHERE SID 123 (Max RPM) holds a Disabled_Value, THE Settings_Mapper SHALL
   leave the RPM_Redline_Threshold unset and THE Dashboard SHALL NOT raise an
   RPM redline alert.

### Requirement 12: Alert on CHT shock-cooling rate

**User Story:** As a pilot, I want the dashboard to flag when a cylinder cools
faster than the aircraft's configured maximum cooling rate, so that
shock-cooling events are surfaced in analysis.

#### Acceptance Criteria

1. THE CHT_Cooling_Rate_Threshold SHALL be expressed in degrees per minute,
   matching GRT's SID 150 definition (EIS4000 documentation).
2. WHILE the CHT_Cooling_Rate_Threshold holds an imported value, THE Dashboard
   SHALL raise an alert during per-flight episode detection when the CHT rate
   of decrease exceeds the CHT_Cooling_Rate_Threshold (in degrees per minute).
3. WHILE the CHT_Cooling_Rate_Threshold holds an imported value, THE Dashboard
   SHALL include a CHT cooling-rate entry in the flight-summary alerts when the
   CHT cooling rate exceeds the CHT_Cooling_Rate_Threshold during the flight.
4. IF SID 150 (Max CHT Cooling Rate) holds a Disabled_Value, THEN THE
   Settings_Mapper SHALL leave the CHT_Cooling_Rate_Threshold unset and THE
   Dashboard SHALL NOT raise a CHT cooling-rate alert.

### Requirement 13: Auto-set cylinder count from EIS settings

**User Story:** As a pilot, I want the dashboard to know how many cylinders my
engine has from the EFIS settings, so that per-cylinder analysis is correct
without hand-entering the count.

#### Acceptance Criteria

1. WHEN SID 1055 (Num CHT) or SID 1054 (Num EGT) is present in the current
   Settings_Backup, THE Settings_Mapper SHALL set the Num_Cylinders_Config from
   that value.
2. WHEN neither SID 1055 nor SID 1054 is present AND SID 58 (Num Cylinders) is
   present, THE Settings_Mapper SHALL set the Num_Cylinders_Config from SID 58.
3. THE Import_Workflow SHALL include the Num_Cylinders_Config change in the
   import preview and double-confirmation flow, showing the current value and
   the proposed imported value like any other change.
4. WHERE none of SID 1055, SID 1054, or SID 58 is present, THE Settings_Mapper
   SHALL leave the Num_Cylinders_Config unchanged.

### Requirement 14: Source-keyed, regression-proof import detection

**User Story:** As a pilot who rotates USB drives between two GRT EFIS displays
and keeps an n and n-1 drive in rotation, I want the app to only offer an
import when a backup is genuinely newer than what I last imported from that same
display, so that seeing an older spare drive never regresses my dashboard
thresholds to a stale configuration.

#### Acceptance Criteria

1. WHEN the Import_Workflow evaluates a candidate Settings_Backup, THE
   Import_Workflow SHALL determine the candidate's Source_Key from the
   Mode_S_Address (SID 1063) and Link_ID (SID 387).
2. WHEN an import completes successfully, THE Import_Workflow SHALL persist an
   Import_Marker for the candidate's Source_Key that records that source's
   last-imported UPDATE_Value, Content_Hash, and import timestamp.
3. WHEN no Import_Marker exists for the candidate's Source_Key, THE
   Import_Workflow SHALL treat the candidate as a first import for that source
   and SHALL offer to apply the imported thresholds.
4. WHEN an Import_Marker exists for the candidate's Source_Key AND the
   candidate's UPDATE_Value is strictly greater than that Import_Marker's
   last-imported UPDATE_Value, THE Import_Workflow SHALL offer to apply the
   imported thresholds.
5. IF an Import_Marker exists for the candidate's Source_Key AND the candidate's
   UPDATE_Value is less than or equal to that Import_Marker's last-imported
   UPDATE_Value, THEN THE Import_Workflow SHALL NOT offer to apply the imported
   thresholds and SHALL suppress any new-backup prompt for that candidate.
6. IF the candidate's Source_Key differs from every Source_Key that has a prior
   Import_Marker, THEN THE Import_Workflow SHALL treat the candidate as a
   distinct source and SHALL NOT overwrite the current Dashboard_Thresholds
   without the preview and double-confirmation flow of Requirement 9.
7. WHERE the Import_Workflow offers an import from a Source_Key that differs
   from the Source_Key of the most recent prior import, THE Import_Workflow
   SHALL indicate in the preview that the candidate backup originates from a
   different display or aircraft than the last import.
8. IF the candidate's Source_Key is undetermined because identity SIDs are
   missing or unreadable, THEN THE Import_Workflow SHALL compare the candidate's
   Content_Hash against the last-imported Content_Hash and SHALL offer an import
   only when the candidate's Content_Hash differs from the last-imported
   Content_Hash and the candidate's UPDATE_Value is greater than the
   last-imported UPDATE_Value.
9. THE Import_Workflow SHALL apply the source-keyed detection of this
   requirement only to determine WHEN an import is offered or allowed, and SHALL
   still require the preview and double-confirmation flow of Requirement 9
   before writing any Dashboard_Threshold.

### Requirement 15: Primary-display-only import gate

**User Story:** As a pilot who may run any mix of GRT displays (Sport, Horizon,
HXr, one or two Minis, or future models), I want only the backup written by my
Primary display to be offered for threshold import, so that a non-primary
display's backup never populates my safety-alert thresholds regardless of which
display models are installed.

#### Acceptance Criteria

1. THE Import_Workflow SHALL treat a candidate Settings_Backup as
   import-eligible only when the candidate is a Primary_Display, that is when
   SID 387 (Link_ID / DULINK_ID) equals `1`.
2. WHEN a candidate Settings_Backup is not a Primary_Display (SID 387 in
   `{0, 2-254}`), THE Import_Workflow SHALL NOT offer that candidate for import
   and SHALL report the candidate as unavailable with a reason indicating the
   candidate is not the primary display, while leaving Archiver behavior
   unaffected.
3. WHEN the first import completes successfully, THE Import_Workflow SHALL bind
   the Bound_Import_Source to the Primary_Display's Source_Key.
4. WHILE a Bound_Import_Source is bound, THE Import_Workflow SHALL offer imports
   only from candidates whose Source_Key equals the Bound_Import_Source.
5. WHEN a candidate is a Primary_Display whose Source_Key differs from the
   Bound_Import_Source, THE Import_Workflow SHALL NOT offer or apply that
   candidate until the user explicitly changes the Bound_Import_Source, and
   SHALL still require the preview and double-confirmation flow of
   Requirement 9 before writing any Dashboard_Threshold.
6. IF no available or archived candidate Settings_Backup reports a
   Primary_Display (every candidate's SID 387 is in `{0, 2-254}`), THEN THE
   Import_Workflow SHALL offer no import and SHALL NOT select a non-primary
   candidate as the import source.
7. WHERE no candidate reports a Primary_Display, THE Import_Workflow MAY allow
   the user to explicitly designate an import source, and SHALL require that
   explicit user designation before offering an import from a non-primary
   candidate.
8. THE Import_Workflow SHALL evaluate the Primary_Display gate of this
   requirement as an additional precondition before or together with the
   source-keyed detection of Requirement 14, and SHALL apply the
   newer-for-source and stale n-1 rules of Requirement 14 only among candidates
   that are import-eligible under this requirement.


### Requirement 16: Per-threshold import selection

**User Story:** As a pilot, I want to choose which individual thresholds get
imported rather than accepting all of them, so that I can import some limits
(e.g. a new CHT redline) while skipping others (e.g. a Vne change).

#### Acceptance Criteria

1. WHEN the user opens the import preview, THE Import_Workflow SHALL present
   each proposed threshold change as an individually selectable row with a
   checkbox, and SHALL default every row to selected.
2. WHEN the user opens the import preview, THE Import_Workflow SHALL present the
   Num_Cylinders_Config change as an individually selectable row with a
   checkbox, defaulting to selected, in the same manner as a threshold-change
   row.
3. THE Import_Workflow SHALL present Vne as a single selectable row that, when
   selected, imports both `vne_tas_redline` and `vne_ias_redline` together,
   preserving the SID 889 mutual exclusivity of Requirement 5 (exactly one Vne
   limit active and the other holding its Disabled_Value sentinel).
4. THE Import_Workflow SHALL NOT allow the user to select only one of
   `vne_tas_redline` or `vne_ias_redline`, so that a partial Vne selection
   cannot produce an inconsistent Vne state.
5. WHEN the user applies the import, THE Import_Workflow SHALL write only the
   Selected_Changes and SHALL leave every unselected threshold at its current
   Dashboard_Threshold value.
6. IF a change in the Selected_Changes would set a Caution_Tier value at or
   above the current-or-selected value of its paired Redline_Tier, THEN THE
   Import_Workflow SHALL warn the user and SHALL NOT apply the import until the
   user resolves the conflict, consistent with Requirement 8's block-on-conflict
   behavior.
7. IF the user deselects every row before applying, THEN THE Import_Workflow
   SHALL treat the apply as a no-op that changes no Dashboard_Threshold and
   SHALL report a reason indicating there is nothing selected to import.
8. WHEN the user opens the import preview, THE Import_Workflow SHALL initialize
   the selection fresh with all rows selected, and SHALL NOT persist
   per-threshold selection between import sessions.
9. WHEN the user applies the Selected_Changes, THE Import_Workflow SHALL still
   require the preview and double-confirmation flow of Requirement 9 before
   writing any Dashboard_Threshold, so that per-threshold selection does not
   bypass either confirmation.

### Requirement 17: Cross-date backup ordering

**User Story:** As a pilot with repeated settings backups (some unchanged, some
changed) archived over many dates, I want the system to import from the
genuinely newest backup for my primary display, so that a stale copy is never
imported over a newer one.

#### Acceptance Criteria

1. WHEN ordering archived Settings_Backup files for the same Source_Key across
   different Archive_Dates, THE Import_Workflow SHALL use the Archive_Date as
   the primary ordering key and SHALL select the latest-dated backup as the
   current Settings_Backup.
2. THE Import_Workflow SHALL NOT use the UPDATE_Value to order backups across
   different Archive_Dates, because UPDATE_Value is not globally monotonic on
   this EFIS and can be identical across many captures.
3. WHERE a `.bak` and a `.dat` Settings_Backup share the same concept and the
   same Archive_Date, THE Settings_Selector MAY use the UPDATE_Value only to
   choose the newer slot of that pair, consistent with Requirement 2.1.
4. THE Import_Workflow SHALL NOT use EFIS-written file modification times for
   ordering backups, because they are unreliable FAT timestamps.
5. WHEN the Import_Workflow compares the current backup for a Source_Key against
   that Source_Key's last-imported backup, THE Import_Workflow SHALL detect a
   content change using the Content_Hash (equivalently the GRT CHECKSUM), and
   SHALL treat identical Content_Hash as "nothing new to import" regardless of
   Archive_Date, raising no new-backup prompt and causing no regression. This
   refines Requirement 14 so that regression-proof detection is based on
   (Source_Key, Archive_Date, Content_Hash) rather than UPDATE_Value magnitude.
6. WHERE only one backup exists for the Source_Key, THE Settings_Selector SHALL
   select it, unchanged from Requirement 2.2.
