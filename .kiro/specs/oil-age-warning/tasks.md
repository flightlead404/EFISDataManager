# Implementation Plan: Oil Age Warning

## Overview

Add a self-contained oil-age warning to the dashboard: a configurable threshold
(`oil_age_warning_hours`, `0` = disabled) and a banner at the top of the Oil page
when engine hours since the most recent oil change exceed that threshold. The
figure is computed entirely from dashboard-recorded data (oil-change events plus
operation hourmeters); the EFIS State counter is out of scope.

Work is ordered bottom-up so the testable core comes first. The
subtraction/selection/classification lives in a PURE function
`compute_oil_age(operations_max_hourmeter, oil_change_events, threshold_hours)`
in `analysis.py`, which is where all six correctness properties are driven by
Hypothesis. Around it: a one-line DB helper `get_max_operation_hourmeter()` in
`database.py`, a thin `get_oil_age_status()` wrapper that reads config +
cutoff-filtered events + max hourmeter and delegates to the pure function, a new
`oil_age` key on the existing `/api/oil` response, the top banner in `oil.html`,
and the `oil_age_warning_hours` input in `settings.html` wired through the
existing `/api/config` plumbing.

Same-file writers are serialized (config default, then the DB helper; the pure
`compute_oil_age` before its `get_oil_age_status` wrapper). The two template
edits touch independent files and can run in parallel. Property-based tests use
**Hypothesis** (already in `requirements.txt`, min 100 iterations each, tagged
`# Feature: oil-age-warning, Property N`), and each design property (Properties
1–6) is implemented as exactly one property-based test. There is no schema
change: oil age is recomputed on the fly from the existing `oil_events` and
`operations` tables.

## Tasks

- [x] 1. Add the `oil_age_warning_hours` config key
  - [x] 1.1 Add `oil_age_warning_hours` to `DEFAULT_CONFIG`
    - In `src/efis_data_manager/config.py`, add `"oil_age_warning_hours": 0` to
      `DEFAULT_CONFIG` with a comment: engine hours since the most recent oil
      change above which the Oil page shows a banner; `0` disables it (the
      default, so existing installs are unaffected until the user opts in); a
      negative value is treated as `0`.
    - Default `0` ensures `load_config()` merges it in automatically on upgrade.
    - _Requirements: 1.1, 1.2, 1.3_

  - [x]* 1.2 Write example tests for the config default
    - `DEFAULT_CONFIG["oil_age_warning_hours"] == 0`; a `load_config()` over a
      saved config lacking the key yields `0`.
    - _Requirements: 1.1, 1.2_

- [x] 2. Add the `get_max_operation_hourmeter` DB helper
  - [x] 2.1 Implement `get_max_operation_hourmeter()`
    - In `src/efis_data_manager/database.py`, add
      `get_max_operation_hourmeter() -> Optional[float]` running
      `SELECT MAX(hourmeter_end) FROM operations`. `MAX(...)` ignores NULLs and
      yields `NULL` (→ Python `None`) when no operation has a non-null
      `hourmeter_end`, which is the "not computable" input (Current_Engine_Hours).
    - _Requirements: 2.1, 2.6_

  - [x]* 2.2 Write example tests for NULL handling
    - Insert operations with a mix of numeric and `NULL` `hourmeter_end`; the
      helper returns the max. With no rows / all-NULL it returns `None`.
    - _Requirements: 2.1, 2.6_

- [x] 3. Implement the pure `compute_oil_age` core
  - [x] 3.1 Implement `compute_oil_age(operations_max_hourmeter, oil_change_events, threshold_hours)`
    - In `src/efis_data_manager/analysis.py`, add the PURE function (no DB, no
      config, no I/O) returning
      `{"status", "oil_hours_since_change", "threshold", "limit_exceeded"}`.
    - Classify in order: (1) `threshold_hours <= 0` → `status "ok"`, hours
      `None`, `limit_exceeded False` (disabled); (2) no `event_type == "change"`
      event → `"no_change_on_record"`, hours `None`; (3)
      `operations_max_hourmeter is None` → `"not_computable"`, hours `None`;
      (4) otherwise `H = max hourmeter among "change" events`,
      `hours = operations_max_hourmeter - H`: `hours <= 0` → `"ok"`, hours
      reported, `limit_exceeded False`; `0 < hours <= threshold` → `"ok"`,
      `limit_exceeded False`; `hours > threshold` → `"exceeded"`,
      `limit_exceeded True`. `"threshold"` is always the sanitized threshold.
    - Consider only `event_type == "change"` events; ignore all others.
    - _Requirements: 2.2, 2.3, 2.5, 2.6, 3.1, 3.2, 4.1, 4.3, 4.4, 5.1, 5.2_

  - [x]* 3.2 Write property test for age = current minus highest-hourmeter change
    - **Property 1: Age is current hours minus the highest-hourmeter change**
    - **Validates: Requirements 2.2, 2.3, 5.2**

  - [x]* 3.3 Write property test that oil additions are ignored
    - **Property 2: Oil additions are ignored**
    - **Validates: Requirements 3.1, 3.2**

  - [x]* 3.4 Write property test for missing-data states
    - **Property 3: Missing-data states produce no number and no warning**
    - **Validates: Requirements 2.5, 2.6, 4.4**

  - [x]* 3.5 Write property test that a non-positive threshold disables the warning
    - **Property 4: A non-positive threshold disables the warning**
    - **Validates: Requirements 1.3, 4.5**

  - [x]* 3.6 Write property test for the exceeded boundary
    - **Property 5: Exceeded iff hours strictly greater than threshold**
    - **Validates: Requirements 4.1, 4.3**

  - [x]* 3.7 Write property test that negative age never warns
    - **Property 6: Negative age never raises a warning**
    - **Validates: Requirements 5.1**

  - [x]* 3.8 Write example tests for concrete `compute_oil_age` states
    - Pin: disabled (`threshold 0`, arbitrary data → `"ok"`, hours `None`);
      exceeded (`max_hm 100`, change at `50`, `threshold 45` → hours `50.0`,
      `"exceeded"`, `limit_exceeded True`); at-threshold (same, `threshold 50`
      → `"ok"`); no change (only additions → `"no_change_on_record"`, hours
      `None`); not computable (`max_hm None`, one change → `"not_computable"`);
      negative (`max_hm 40`, change at `50`, `threshold 5` → `"ok"`,
      `limit_exceeded False`).
    - _Requirements: 2.5, 2.6, 4.1, 4.3, 5.1_

- [x] 4. Implement the `get_oil_age_status` wrapper
  - [x] 4.1 Implement `get_oil_age_status()`
    - In `src/efis_data_manager/analysis.py`, add
      `get_oil_age_status() -> dict`: read `oil_age_warning_hours` and
      `oil_cutoff_date` from config; coerce the threshold to `float` and clamp a
      negative or non-numeric value to `0` (sanitize before delegating); load
      cutoff-filtered events via `database.get_oil_events(cutoff_date=...)`; read
      Current_Engine_Hours via `database.get_max_operation_hourmeter()`; delegate
      to `compute_oil_age(...)` and return its dict.
    - _Requirements: 1.4, 2.1, 2.2, 2.7, 2.8, 3.1, 3.2, 5.2_

  - [x]* 4.2 Write tests for sanitization and cutoff passthrough
    - Negative configured value → behaves as disabled (`"ok"`,
      `limit_exceeded False`) and returned `threshold` is `0`. With events on
      both sides of a cutoff date, the most-recent change is selected among only
      the on/after-cutoff `"change"` events; empty cutoff includes all.
    - _Requirements: 1.4, 2.7, 2.8_

- [x] 5. Checkpoint - pure core, DB helper, and wrapper
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Extend `/api/oil` with the `oil_age` object
  - [x] 6.1 Add `oil_age` to the `api_oil` response
    - In `src/efis_data_manager/dashboard/app.py` `api_oil`, import
      `get_oil_age_status` from `analysis` and add
      `"oil_age": get_oil_age_status()` to the returned JSON. Leave the existing
      `consumption`, `changes`, `events`, and `cutoff_date` keys unchanged.
    - _Requirements: 2.1, 4.1_

  - [x]* 6.2 Write Flask integration tests for the `oil_age` field
    - With a seeded DB (operations + change events + a set
      `oil_age_warning_hours`), `GET /api/oil` returns an `oil_age` object with
      the expected `status`, `oil_hours_since_change`, `threshold`, and
      `limit_exceeded`; the other four keys are unchanged. With the default
      config (`0`), `oil_age.status == "ok"` and `limit_exceeded False`.
    - _Requirements: 1.3, 2.1, 4.1_

- [x] 7. Add the oil-age banner to `oil.html`
  - [x] 7.1 Render the top banner from `data.oil_age`
    - In `src/efis_data_manager/dashboard/templates/oil.html`, add an empty
      `<div id="oil-age-banner" style="display:none;"></div>` at the very top of
      the content block, before `<h1>Oil Consumption</h1>`. In `loadOil()`, after
      receiving `data`, render the banner defensively from `data.oil_age` ONLY
      when `age.status === 'exceeded'`: reuse the `.note` block style recolored
      warning-red (`#ef5350`) and set the text to the shape "Oil age threshold
      exceeded: NN.N oil hours since last change (limit MM)" with hours to one
      decimal. In every other state hide the banner and clear its text (no
      placeholder), leaving the rest of the oil content unchanged.
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6_

- [x] 8. Add the threshold input to `settings.html`
  - [x] 8.1 Add and wire the `oil_age_warning_hours` input
    - In `src/efis_data_manager/dashboard/templates/settings.html`, add a
      non-negative numeric input `#oil_age_warning_hours`
      (`type="number" min="0" step="1"`) inside the existing "Dashboard" card
      next to `trend_window_hours`, with help text explaining it and that `0`
      disables it. On config load populate it from
      `config.oil_age_warning_hours ?? 0`; in `saveSettings()` include it in the
      posted `config`, clamping a negative/blank value to `0` client-side
      (`Math.max(0, parseFloat(...) || 0)`). Flows through the existing
      `/api/config` GET/POST path — no new endpoint.
    - _Requirements: 1.5, 1.6_

  - [x]* 8.2 Write Flask integration tests for the config round-trip
    - `POST /api/config` with `{"oil_age_warning_hours": 45}` then
      `GET /api/config` returns `45`; posting a negative value is stored but
      `get_oil_age_status()` still treats it as disabled (sanitized at read time).
    - _Requirements: 1.5, 1.6_

- [x] 9. Final checkpoint - full suite
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test tasks and can be skipped for a faster
  MVP; core implementation tasks are never optional.
- Each task references specific requirement sub-clauses and, for test tasks, the
  exact design property number it implements.
- All six design properties map to exactly one property-based test:
  P1 → 3.2; P2 → 3.3; P3 → 3.4; P4 → 3.5; P5 → 3.6; P6 → 3.7.
- Property-based tests use Hypothesis (`max_examples >= 100`) and carry a
  `# Feature: oil-age-warning, Property N: …` tag. Hypothesis is ALREADY in
  `requirements.txt`, so no dependency-bump task is needed. Use
  `math.isclose`/tolerance where generated floats are subtracted and compared.
- The pure `compute_oil_age` is the property-tested core; the DB read
  (`MAX(hourmeter_end)`), config sanitization, cutoff SQL filter, settings
  input, and banner wording are covered by example/integration/UI checks
  instead, because they exercise SQLite/Flask/DOM behavior that does not vary
  meaningfully with generated input.
- Banner wording (Req 4.2) and omission (Req 4.3–4.6) are verified by inspecting
  the rendered DOM / a lightweight JS assertion; the settings input (Req 1.5) is
  verified via the `/api/config` round-trip test.
- No schema change: oil age is recomputed on the fly from `oil_events` and
  `operations`, consistent with the rest of the oil analysis.
- This change is dashboard/analysis-only; on release, bump versions per the
  versioning policy (dashboard changed, so `DASHBOARD_VERSION`).

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "2.1"] },
    { "id": 2, "tasks": ["2.2", "3.1"] },
    { "id": 3, "tasks": ["3.2", "3.3", "3.4", "3.5", "3.6", "3.7", "3.8", "4.1"] },
    { "id": 4, "tasks": ["4.2", "6.1", "7.1", "8.1"] },
    { "id": 5, "tasks": ["6.2", "8.2"] }
  ]
}
```
