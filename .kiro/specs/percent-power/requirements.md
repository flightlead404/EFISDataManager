# Requirements Document

## Introduction

Percent power is a key performance parameter (leaning, cruise performance,
range/endurance analysis) but is NOT present in the GRT HXr FDL flight-data
feed. The EFIS computes and can display it (Dial Source value 5 = "Percent
Power 1") from an engine Power_Map the aircraft carries in its settings backup.
This feature computes percent power post-flight for each logged FDL sample so it
can be plotted and analyzed in the dashboard, matching how the EFIS derives it.

GRT has provided the authoritative power-map setting-identifier layout (from the
`Settings.h` header) and the exact percent-power computation algorithm, so this
feature implements the EFIS method verbatim. There is no approximate fallback.
The Power_Map and rated horsepower are read from the imported EFIS settings
backup (via the `efis-settings-import` spec's parser), not hand-entered.

Aircraft context: a 190 HP high-compression parallel-valve Lycoming IO-360.

## Glossary

- **Percent_Power**: engine output expressed as a percentage of RATED_HP.
- **EFIS**: The GRT HXr Electronic Flight Instrument System in the aircraft.
- **FDL_Sample**: One time-stamped record in the GRT FDL flight-data feed,
  carrying fields such as `rpm1`, `internal_map`, `pressure_altitude`, and
  `oat`.
- **Power_Map**: The engine power table the EFIS interpolates, read from the
  settings backup, comprising the RPM column, the MAP55_Column, the
  MAP75_Column, the Altitude_Column, and the Delta_HP_Column.
- **SID (Setting_Identifier)**: The numeric key of a `KEY=VALUE` line in the
  EFIS settings backup. Power_Map columns occupy 10 consecutive SIDs each.
- **RATED_HP**: The engine's rated maximum horsepower, read from SID 167
  (this aircraft: 190). RATED_HP must be greater than zero to compute
  Percent_Power.
- **RPM_Column**: SIDs 168-177, the engine-speed breakpoints of the Power_Map.
- **MAP55_Column**: SIDs 178-187, the manifold pressure (inHg) that yields 55%
  power at the RPM in the corresponding RPM_Column row.
- **MAP75_Column**: SIDs 188-197, the manifold pressure (inHg) that yields 75%
  power at the RPM in the corresponding RPM_Column row.
- **Altitude_Column**: SIDs 198-207, the pressure-altitude breakpoints of the
  Power_Map altitude correction.
- **Delta_HP_Column**: SIDs 208-217, the horsepower added at the altitude in the
  corresponding Altitude_Column row.
- **Sea_Level_Row**: One row of the RPM/MAP55/MAP75 portion of the Power_Map. A
  Sea_Level_Row is blank/ignored when its RPM value is zero or negative; a valid
  Sea_Level_Row requires all of its columns to be greater than zero.
- **Altitude_Row**: One row of the Altitude/Delta_HP portion of the Power_Map. An
  Altitude_Row is blank/ignored when its altitude value is zero or negative.
- **Standard_OAT**: The ISA standard outside-air temperature at the current
  pressure altitude, expressed in degrees Fahrenheit, used by the OAT
  correction.
- **OAT_Correction**: The multiplier `sqrt((460 + Standard_OAT) / (460 + OAT))`,
  with both temperatures in degrees Fahrenheit.
- **Temp_Units**: Temperature units in effect per the EFIS setting; the FDL
  `oat` field for this aircraft is in degrees Fahrenheit.

## Known inputs available in the FDL feed

- `rpm1` (engine RPM)
- `internal_map` or the mapped MAP aux channel (manifold pressure, inHg)
- `pressure_altitude`
- `oat` (degrees Fahrenheit for this aircraft; the OAT_Correction is defined in
  Fahrenheit, so convert if Temp_Units indicate otherwise)
- (No percent-power or HP field is logged.)

## Requirements

### Requirement 1: Read the Power_Map and RATED_HP from the settings backup

**User Story:** As a pilot, I want percent power to use my aircraft's actual
EFIS power map, so that the computed values match what the EFIS uses without any
hand entry.

#### Acceptance Criteria

1. THE system SHALL read RATED_HP from SID 167 of the imported EFIS settings
   backup obtained through the `efis-settings-import` Settings_Parser.
2. THE system SHALL read the Power_Map from the imported settings backup as five
   ten-entry columns: the RPM_Column (SIDs 168-177), the MAP55_Column (SIDs
   178-187), the MAP75_Column (SIDs 188-197), the Altitude_Column (SIDs
   198-207), and the Delta_HP_Column (SIDs 208-217).
3. WHERE a Sea_Level_Row has an RPM value that is zero or negative, THE system
   SHALL treat that Sea_Level_Row as blank and SHALL exclude it from
   interpolation.
4. WHERE a Sea_Level_Row is not blank, THE system SHALL include that
   Sea_Level_Row only when its RPM, MAP55, and MAP75 values are all greater than
   zero.
5. WHERE an Altitude_Row has an altitude value that is zero or negative, THE
   system SHALL treat that Altitude_Row as blank and SHALL exclude it from
   interpolation.
6. IF RATED_HP is missing or not greater than zero, THEN THE system SHALL treat
   Percent_Power as unavailable.
7. IF the Power_Map contains no valid Sea_Level_Row or no valid Altitude_Row,
   THEN THE system SHALL treat Percent_Power as unavailable.

### Requirement 2: Compute percent power per sample using GRT's algorithm

**User Story:** As a pilot, I want percent power computed for each flight-data
sample using the exact EFIS method, so that I can plot and analyze it like any
other parameter and trust that it matches my EFIS.

#### Acceptance Criteria

1. WHERE the Power_Map and RATED_HP are available AND an FDL_Sample provides
   valid RPM, manifold pressure, pressure altitude, and OAT, THE system SHALL
   compute Percent_Power for that FDL_Sample using the eight-step algorithm of
   Acceptance Criteria 2 through 9.
2. THE system SHALL linearly interpolate or extrapolate across the MAP55_Column
   at the FDL_Sample RPM to determine the sample's 55% manifold pressure.
3. THE system SHALL linearly interpolate or extrapolate across the MAP75_Column
   at the FDL_Sample RPM to determine the sample's 75% manifold pressure.
4. THE system SHALL linearly interpolate or extrapolate between the sample's 55%
   manifold pressure and 75% manifold pressure, using the FDL_Sample actual
   manifold pressure, to determine the sample's sea-level power percentage.
5. THE system SHALL multiply the sea-level power percentage by RATED_HP to
   determine the sample's sea-level horsepower.
6. THE system SHALL linearly interpolate or extrapolate across the
   Delta_HP_Column at the FDL_Sample pressure altitude to determine the sample's
   delta horsepower.
7. THE system SHALL add the delta horsepower to the sea-level horsepower to
   determine the sample's altitude-corrected horsepower.
8. THE system SHALL multiply the altitude-corrected horsepower by the
   OAT_Correction `sqrt((460 + Standard_OAT) / (460 + OAT))`, where Standard_OAT
   is the ISA standard temperature at the FDL_Sample pressure altitude in
   degrees Fahrenheit and OAT is the FDL_Sample outside-air temperature in
   degrees Fahrenheit, to determine the sample's corrected horsepower.
9. THE system SHALL divide the corrected horsepower by RATED_HP to yield the
   FDL_Sample Percent_Power.

### Requirement 3: Linear interpolation and extrapolation

**User Story:** As a pilot, I want percent power to be defined even when my
engine state falls outside the table breakpoints, so that the value tracks the
EFIS across the full flight envelope.

#### Acceptance Criteria

1. WHEN an interpolation input falls between two valid breakpoints, THE system
   SHALL compute the result by linear interpolation between those breakpoints.
2. WHEN an interpolation input falls below the lowest valid breakpoint or above
   the highest valid breakpoint, THE system SHALL compute the result by linear
   extrapolation from the nearest two valid breakpoints, matching GRT's
   interpolate/extrapolate behavior.

### Requirement 4: OAT units correction

**User Story:** As a pilot, I want the temperature correction applied in the
units the algorithm expects, so that percent power stays accurate regardless of
my EFIS temperature-unit setting.

#### Acceptance Criteria

1. THE system SHALL evaluate the OAT_Correction with both Standard_OAT and OAT
   in degrees Fahrenheit.
2. THE system SHALL define Standard_OAT as the ISA standard temperature at the
   FDL_Sample pressure altitude, converted to degrees Fahrenheit.
3. WHERE the FDL `oat` field is not already in degrees Fahrenheit per Temp_Units,
   THE system SHALL convert the FDL_Sample OAT to degrees Fahrenheit before
   evaluating the OAT_Correction.

### Requirement 5: Missing or invalid inputs

**User Story:** As a pilot, I want percent power to be omitted rather than
guessed when data is missing, so that I never mistake a fabricated value for a
real one.

#### Acceptance Criteria

1. IF an FDL_Sample is missing or holds an invalid value for RPM, manifold
   pressure, pressure altitude, or OAT, THEN THE system SHALL leave
   Percent_Power undefined for that FDL_Sample.
2. IF the Power_Map or RATED_HP is unavailable, THEN THE system SHALL leave
   Percent_Power unavailable for the entire flight and SHALL NOT substitute an
   estimated value.

### Requirement 6: Plot and analysis integration

**User Story:** As a pilot, I want percent power available on the flight plots
and summary stats, so that I can correlate it with leaning, CHT/EGT, and cruise
performance.

#### Acceptance Criteria

1. THE system SHALL expose Percent_Power as a selectable, plottable parameter on
   the flight page, consistent with existing FDL parameters.
2. THE system SHALL include a per-flight Percent_Power summary statistic (for
   example, cruise average) when Percent_Power is computable for the flight.
3. WHERE Percent_Power is unavailable for a flight, THE UI SHALL omit
   Percent_Power from the plot and summary rather than displaying zeros or
   placeholder values.

### Requirement 7: Validation against EFIS values

**User Story:** As a pilot, I want the computed percent power to match my EFIS's
displayed value, so that the analysis numbers are trustworthy.

#### Acceptance Criteria

1. WHERE a known EFIS-displayed Percent_Power value is available for a given
   engine state, THE system SHALL produce a Percent_Power within a small
   tolerance of that EFIS-displayed value.
2. THE system SHALL be validated against known EFIS Percent_Power values where
   such reference values are available.

## Design decisions deferred

- Whether Percent_Power is stored at import time or computed on the fly when a
  flight is viewed is a design decision (mirrors the `detect_episodes`
  compute-on-the-fly vs cached-stats choice). Either way, Percent_Power must be
  exposed as a plottable parameter (Requirement 6.1) and included in per-flight
  summary stats (Requirement 6.2).
- Units and precision for display.

## Dependencies

- Depends on the `efis-settings-import` spec to obtain the Power_Map and
  RATED_HP from the settings backup via its Settings_Parser.
