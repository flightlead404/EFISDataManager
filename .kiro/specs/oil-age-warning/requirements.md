# Requirements Document

## Introduction

The GRT EFIS records engine oil hours internally (State value `STID_OIL_HOURS`)
but never displays that value to the pilot, so oil age is tracked manually by
tach time. This user also does not use the EFIS "reset oil hours on change"
option, which means the EFIS counter is stale and effectively per-drive — it
cannot be trusted as a source of truth.

The EFIS Data Manager dashboard already holds everything needed to compute oil
age independently: it records the engine hourmeter at each oil-change event and
records `hourmeter_end` for every operation. This feature adds a small,
self-contained dashboard capability: a configurable oil-age warning threshold
(in engine hours) and a prominent banner on the oil page that appears when the
oil hours accumulated since the most recent oil change exceed that threshold.

The computation uses only dashboard-recorded data (oil-change events plus
operation hourmeters). Reading or parsing EFIS State counters is explicitly out
of scope.

## Glossary

- **Dashboard**: The EFIS Data Manager web dashboard (Flask app in
  `src/efis_data_manager/dashboard/app.py`).
- **Oil_Page**: The dashboard oil view served at route `/oil`, rendered from
  `oil.html`, and populated by the `/api/oil` endpoint.
- **Oil_API**: The `/api/oil` endpoint (`api_oil`), which currently returns a
  JSON object with keys `consumption`, `changes`, `events`, and `cutoff_date`.
- **Oil_Change_Event**: An oil event whose `event_type` is `"change"`, as
  returned by `database.get_oil_events(cutoff_date=...)`. Each event carries at
  least `event_type`, `date`, and `hourmeter`.
- **Oil_Addition_Event**: An oil event whose `event_type` is not `"change"`
  (e.g. an oil top-up/addition). Additions are recorded in the same
  `oil_events` table but do NOT represent an oil change.
- **Operation**: A row in the `operations` table. Each operation may carry a
  numeric `hourmeter_end` (nullable). `hourmeter_end` is the engine hourmeter
  value at the end of that operation.
- **Current_Engine_Hours**: The maximum non-null `hourmeter_end` across all
  recorded operations. This represents the most recent known engine time.
- **Most_Recent_Change**: The Oil_Change_Event with the highest `hourmeter`
  among all Oil_Change_Events that pass the configured cutoff-date filter. When
  multiple changes exist, this is the newest one by hourmeter.
- **Oil_Hours_Since_Change**: `Current_Engine_Hours` MINUS the `hourmeter` of
  the Most_Recent_Change. This is the age of the current oil, in engine hours,
  computed entirely from dashboard data.
- **Oil_Cutoff_Date**: Config key `oil_cutoff_date` (format `YYYY-MM-DD`, empty
  string means "use all events"). Oil events dated before this value are
  excluded from oil computations. Confirmed default is `""` in `config.py`.
- **Oil_Age_Threshold**: New config key `oil_age_warning_hours`, a number of
  engine hours. When Oil_Hours_Since_Change exceeds this value, the warning is
  shown. A value of `0` (or unset) disables the warning.
- **Config**: The dashboard configuration store loaded via
  `config.load_config()`, where `oil_cutoff_date` already lives and where
  `oil_age_warning_hours` will be added.

## Known Inputs (verified against code)

These facts were confirmed by reading the source and are the data model the
requirements below rely on:

- `analysis.get_oil_changes()` returns oil-CHANGE events as dicts with keys
  `date`, `hourmeter`, `quarts_added`, `quarts_low`, `note`, and
  `hours_since_last_change`, filtered by the configured `oil_cutoff_date`.
  (`src/efis_data_manager/analysis.py`, `get_oil_changes`)
- `database.get_oil_events(cutoff_date="")` returns all oil events ordered by
  `hourmeter`; when `cutoff_date` is set it returns only events with
  `date >= cutoff_date`. Rows include `event_type`, `date`, and `hourmeter`.
  (`src/efis_data_manager/database.py`, `get_oil_events`)
- The `operations` table has a nullable `hourmeter_end REAL` column; the latest
  operation's `hourmeter_end` is the current engine time.
  (`src/efis_data_manager/database.py` schema; `analysis.py` operation model)
- `config.py` defines `oil_cutoff_date` (default `""`); this is the correct
  place to add `oil_age_warning_hours`. (`src/efis_data_manager/config.py`)
- The Oil_Page is served at `/oil` from `oil.html`, and `/api/oil` already
  returns `{consumption, changes, events, cutoff_date}`.
  (`src/efis_data_manager/dashboard/app.py`, `oil_page` and `api_oil`)

## Out of Scope

- Reading, parsing, or displaying the EFIS State counter `STID_OIL_HOURS`.
- Any change to how oil events or operations are recorded/imported.
- Notifications outside the Oil_Page (email, menu bar, push, etc.).

## Requirements

### Requirement 1: Configurable oil-age warning threshold

**User Story:** As a pilot, I want to configure an oil-age warning threshold in
engine hours, so that the dashboard can alert me when my oil is due for a change.

#### Acceptance Criteria

1. THE Config SHALL provide a key `oil_age_warning_hours` that stores a number
   of engine hours.
2. WHERE `oil_age_warning_hours` is absent from stored configuration, THE
   Config SHALL treat the value as `0`.
3. WHERE `oil_age_warning_hours` equals `0`, THE Oil_Page SHALL treat the
   oil-age warning as disabled and SHALL omit the warning banner.
4. IF `oil_age_warning_hours` is set to a negative number, THEN THE Config SHALL
   treat the value as `0` (disabled).
5. THE Settings page SHALL present an input for `oil_age_warning_hours` that
   accepts a non-negative number of engine hours.
6. WHEN a user submits a value for `oil_age_warning_hours` on the Settings page,
   THE Dashboard SHALL persist the submitted value to Config.

### Requirement 2: Compute oil hours since last change from dashboard data

**User Story:** As a pilot, I want the dashboard to compute oil hours since my
last oil change from its own records, so that the figure is accurate even though
I never reset the EFIS oil-hours counter.

#### Acceptance Criteria

1. THE Dashboard SHALL compute Current_Engine_Hours as the maximum non-null
   `hourmeter_end` value across all recorded operations.
2. THE Dashboard SHALL select the Most_Recent_Change as the Oil_Change_Event
   with the highest `hourmeter` among Oil_Change_Events that pass the
   Oil_Cutoff_Date filter.
3. WHEN a Most_Recent_Change and a Current_Engine_Hours both exist, THE
   Dashboard SHALL compute Oil_Hours_Since_Change as Current_Engine_Hours minus
   the `hourmeter` of the Most_Recent_Change.
4. THE Dashboard SHALL compute Oil_Hours_Since_Change using dashboard-recorded
   oil-change events and operation hourmeters, and SHALL NOT use the EFIS State
   counter `STID_OIL_HOURS`, because the pilot does not use the EFIS "reset oil
   hours on change" option and the State counter is stale and per-drive.
5. IF no Oil_Change_Event passes the Oil_Cutoff_Date filter, THEN THE Dashboard
   SHALL report a distinct "no oil change on record" state and SHALL NOT compute
   Oil_Hours_Since_Change.
6. IF no operation has a non-null `hourmeter_end`, THEN THE Dashboard SHALL
   report Oil_Hours_Since_Change as not computable and SHALL NOT produce a
   numeric value.
7. THE Dashboard SHALL exclude Oil_Change_Events dated before Oil_Cutoff_Date
   when Oil_Cutoff_Date is a non-empty `YYYY-MM-DD` value.
8. WHERE Oil_Cutoff_Date is an empty string, THE Dashboard SHALL include all
   Oil_Change_Events in the Most_Recent_Change selection.

### Requirement 3: Only oil changes reset the oil-age clock

**User Story:** As a pilot, I want oil top-ups to be ignored when computing oil
age, so that adding a quart does not make the dashboard think the oil is fresh.

#### Acceptance Criteria

1. WHEN selecting the Most_Recent_Change, THE Dashboard SHALL consider only
   events whose `event_type` is `"change"`.
2. THE Dashboard SHALL exclude Oil_Addition_Events from Most_Recent_Change
   selection so that recording an oil addition does not reset
   Oil_Hours_Since_Change.

### Requirement 4: Warning banner on the oil page

**User Story:** As a pilot, I want a prominent banner at the top of the oil page
when my oil is overdue, so that I notice it without hunting through charts.

#### Acceptance Criteria

1. WHERE `oil_age_warning_hours` is greater than `0` AND Oil_Hours_Since_Change
   is computable AND Oil_Hours_Since_Change is greater than
   `oil_age_warning_hours`, THE Oil_Page SHALL display a warning banner at the
   top of the page.
2. THE warning banner SHALL state the computed Oil_Hours_Since_Change to one
   decimal place and the configured `oil_age_warning_hours` limit, using wording
   of the shape "Oil age threshold exceeded: NN.N oil hours since last change
   (limit MM)".
3. WHERE Oil_Hours_Since_Change is less than or equal to
   `oil_age_warning_hours`, THE Oil_Page SHALL omit the warning banner entirely.
4. WHERE Oil_Hours_Since_Change is not computable, THE Oil_Page SHALL omit the
   warning banner entirely and SHALL NOT display a zero or placeholder value.
5. WHERE `oil_age_warning_hours` equals `0`, THE Oil_Page SHALL omit the warning
   banner entirely.
6. WHEN the warning banner is omitted, THE Oil_Page SHALL render its existing
   oil content unchanged.

### Requirement 5: Robustness to hourmeter anomalies

**User Story:** As a pilot, I want the oil-age computation to behave sensibly
when hourmeter values are inconsistent, so that I do not see nonsensical or
false warnings.

#### Acceptance Criteria

1. IF Current_Engine_Hours is less than the `hourmeter` of the Most_Recent_Change
   (a rolled-back or out-of-order hourmeter, yielding a negative
   Oil_Hours_Since_Change), THEN THE Oil_Page SHALL omit the warning banner.
2. WHEN multiple Oil_Change_Events pass the Oil_Cutoff_Date filter, THE
   Dashboard SHALL base Oil_Hours_Since_Change only on the Most_Recent_Change
   (highest `hourmeter`) and SHALL ignore earlier changes.
