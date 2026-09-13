# Implementation Plan: Percent Power

## Overview

Reproduce the GRT HXr EFIS's exact percent-power computation post-flight for
every logged FDL sample, so Percent_Power can be plotted and summarized in the
dashboard even though it is not carried in the FDL feed. Work is ordered
bottom-up: pure math helpers first (`interp`, `isa_std_oat_f`,
`oat_to_fahrenheit`), then the `PowerMap` data model + `build_power_map` + the
SID-layout constants, then the eight-step `percent_power()` pipeline, then the
MAP-column resolver `map_column_for`. Only after the pure core is testable do we
add the single settings integration point (`load_current_settings_sids()` in the
existing `efis_settings.py`) plus `power.py`'s `_read_settings`/`load_power_map`
adapters, then the per-flight cruise-average stat in `analysis.py`, then the
dashboard route + descriptor wiring, then the flight-page display, and finally a
full-suite checkpoint.

All new pure logic lives in a single new module `src/efis_data_manager/power.py`;
the only edit to an existing pure/parse module is the one integration helper in
`efis_settings.py`. Property-based tests use **Hypothesis** (min 100 iterations
each, tagged `# Feature: percent-power, Property N`), and each design property
(Properties 1–10) is implemented as exactly one property-based test.
Example/reference tests pin the 8-step pipeline to hand-computed values from this
aircraft's real Power_Map (RATED_HP = 190) and pin the SID layout; Flask
integration tests cover the route/stats wiring by patching `load_power_map`.

There is no approximate fallback and no database schema change: Percent_Power is
computed on the fly from existing `fdl_data` columns plus the Power_Map, and the
whole flight shows no Percent_Power when the Power_Map or RATED_HP is
unavailable.

## Tasks

- [x] 1. Create `power.py` with the pure interpolation and temperature helpers
  - [x] 1.1 Implement `interp`, `isa_std_oat_f`, and `oat_to_fahrenheit`
    - Create `src/efis_data_manager/power.py` (no Flask/DB/IO imports).
    - Implement `interp(xs, ys, x)`: `xs` ascending, paired with `ys`, len >= 2;
      linear interpolation on the containing segment, and 2-point linear
      extrapolation from the first two points below `xs[0]` and the last two
      points above `xs[-1]`; `interp(xs, ys, xs[k]) == ys[k]`.
    - Implement `isa_std_oat_f(pressure_alt_ft)` returning
      `59.0 - 3.564 * (pressure_alt_ft / 1000.0)` (equivalently
      `(15 - 1.98*h/1000)*9/5 + 32`).
    - Implement `oat_to_fahrenheit(oat, temp_units_celsius)`: `oat*9/5+32` when
      Celsius, else `oat` unchanged.
    - _Requirements: 3.1, 3.2, 2.8, 4.2, 4.3_

  - [x]* 1.2 Write property test for interpolation and extrapolation
    - **Property 1: Interpolation and extrapolation are piecewise-linear**
    - **Validates: Requirements 3.1, 3.2, 2.2, 2.3, 2.6**

  - [x]* 1.3 Write property test for ISA standard OAT
    - **Property 3: ISA standard OAT is linear in altitude and 59 °F at sea level**
    - **Validates: Requirements 2.8, 4.2**

  - [x]* 1.4 Write property test for OAT unit conversion
    - **Property 5: OAT unit conversion**
    - **Validates: Requirements 4.3**

- [x] 2. Implement the `PowerMap` model, SID-layout constants, and `build_power_map`
  - [x] 2.1 Add SID constants, the `PowerMap` dataclass, and `build_power_map`
    - In `power.py`, add the string-keyed SID constants: `SID_RATED_HP = "167"`,
      `SID_RPM_COLUMN = [str(s) for s in range(168, 178)]`, `SID_MAP55_COLUMN`
      (178–187), `SID_MAP75_COLUMN` (188–197), `SID_ALTITUDE_COLUMN` (198–207),
      `SID_DELTA_HP_COLUMN` (208–217).
    - Add the frozen `PowerMap` dataclass (`rated_hp`, `rpm`, `map55`, `map75`,
      `altitude`, `delta_hp`), holding only valid rows sorted ascending.
    - Implement `build_power_map(sids: dict[str, str])`: read cells with STRING
      keys (`sids.get("167")`, `sids.get(str(s))`), treating absent/non-numeric
      cells as blank; drop Sea_Level_Rows where `rpm <= 0`, keep a Sea_Level_Row
      only when rpm, map55, map75 are ALL `> 0`; drop Altitude_Rows where
      `altitude <= 0` (delta_hp may be any value); sort sea-level rows by rpm and
      altitude rows by altitude, preserving pairing. Return `None` when RATED_HP
      is missing or `<= 0`, or there is no valid Sea_Level_Row, or no valid
      Altitude_Row.
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_

  - [x]* 2.2 Write property test for sea-level row validity
    - **Property 6: Sea-level row validity**
    - **Validates: Requirements 1.3, 1.4**

  - [x]* 2.3 Write property test for altitude row validity
    - **Property 7: Altitude row validity**
    - **Validates: Requirements 1.5**

  - [x]* 2.4 Write property test for map unavailability
    - **Property 8: Map unavailability**
    - **Validates: Requirements 1.6, 1.7**

  - [x]* 2.5 Write example tests for the SID layout and RATED_HP read
    - Build from the worked-table SID dict (RATED_HP = 190); assert each column
      array (`rpm`/`map55`/`map75`/`altitude`/`delta_hp`) equals the expected
      values in order. SID `"167"` present → `rated_hp` equals it; absent → the
      map is unavailable (`None`).
    - _Requirements: 1.1, 1.2_

- [x] 3. Implement the eight-step `percent_power` pipeline
  - [x] 3.1 Implement `percent_power(rpm, map_inhg, pressure_alt, oat_f, power_map)`
    - In `power.py`, implement GRT's 8-step algorithm: (1) `m55 =
      interp(rpm, map55, rpm_in)`, (2) `m75 = interp(rpm, map75, rpm_in)`,
      (3) sea-level % = `55 + (map_inhg - m55) * 20 / (m75 - m55)`, (4) sea-level
      HP = `pct/100 * rated_hp`, (5) `delta_hp = interp(altitude, delta_hp,
      pressure_alt)`, (6) alt-corrected HP = sea-level HP + delta_hp, (7) multiply
      by OAT_Correction `sqrt((460 + isa_std_oat_f(pressure_alt)) / (460 +
      oat_f))`, (8) corrected HP / rated_hp * 100. Return `None` when any input
      is `None` or non-finite (`NaN`/`inf`), when `m75 == m55` (degenerate
      anchor), or when `460 + oat_f <= 0`. Do NOT clamp the result.
    - _Requirements: 2.1, 2.4, 2.5, 2.7, 2.9, 4.1, 5.1_

  - [x]* 3.2 Write property test for the affine sea-level anchor
    - **Property 2: Sea-level power percentage is affine through the 55%/75% anchors**
    - **Validates: Requirements 2.4**

  - [x]* 3.3 Write property test for the OAT correction
    - **Property 4: OAT correction matches its Fahrenheit formula and is monotonic**
    - **Validates: Requirements 2.8, 4.1**

  - [x]* 3.4 Write property test for undefined output on missing/invalid inputs
    - **Property 9: Percent_Power is undefined on missing or invalid inputs**
    - **Validates: Requirements 5.1**

  - [x]* 3.5 Write reference/example tests for the 8-step pipeline
    - Recompute the anchors from the imported worked table (RATED_HP = 190) and
      assert `percent_power(2400, 22.0, 6000, 40.0, pm)` is within ±0.5% of the
      hand-computed value (~68.3%); repeat for several points including at least
      one extrapolation case (RPM below 2000 or above 2700, altitude above
      14000, or MAP outside the 55–75% band). Add a degenerate-anchor case where
      `map55 == map75` at some RPM → that sample returns `None`.
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.9, 3.2, 7.1_

- [x] 4. Implement the MAP-column resolver
  - [x] 4.1 Implement `map_column_for(resolved_aux)`
    - In `power.py`, implement `map_column_for(resolved_aux: dict) -> str`:
      return `resolved_aux["manifold_pressure"]["channel"]` when a channel is
      mapped to `manifold_pressure`, otherwise fall back to `"internal_map"`.
      Pure — takes the already-resolved dict, keeping `aux_map` the one place aux
      meaning is derived.
    - _Requirements: 2.1_

  - [x]* 4.2 Write example tests for `map_column_for`
    - Resolved dict with a `manifold_pressure` channel → returns that channel;
      empty/absent mapping → returns `"internal_map"`.
    - _Requirements: 2.1_

- [x] 5. Add the settings integration point and the `power.py` load adapters
  - [x] 5.1 Implement `load_current_settings_sids()` in `efis_settings.py`
    - In the existing `src/efis_data_manager/efis_settings.py`, add
      `load_current_settings_sids() -> Optional[tuple[dict[str, str], bool]]`:
      `archive_root = Path(load_config()["archive_path"])`; glob
      `<archive_root>/Settings/Settings*.bak` and `*.dat`; parse each via
      `Settings_Parser` (skipping parse failures); select the current one via
      `Settings_Selector`; on success return `(parsed.sids,
      temp_units_celsius)` where `temp_units_celsius = parsed.sids.get("345",
      "").strip() == "1"`. Return `None` when no archived backup exists, none
      parses, or `SelectionResult.current` is `None`.
    - _Requirements: 1.1, 1.2, 4.3, 5.2_

  - [x] 5.2 Implement `_read_settings` and `load_power_map` in `power.py`
    - Add `_read_settings()`: `try` importing
      `efis_data_manager.efis_settings.load_current_settings_sids`, catching
      `ImportError` and returning `None`; otherwise forward its
      `(sids, temp_units_celsius)` tuple (or `None`).
    - Add `load_power_map() -> Optional[PowerMap]`: call `_read_settings()`;
      return `None` on `None`, otherwise unpack `(sids, temp_units_celsius)` and
      return `build_power_map(sids)`. Callers treat `None` as "Percent_Power
      unavailable for the flight".
    - _Requirements: 1.1, 1.2, 5.2_

  - [x]* 5.3 Write integration tests for the settings read + graceful degradation
    - `load_current_settings_sids()` over a temp archive dir: returns
      `(sids, temp_units_celsius)` for a valid Settings backup and `None` when no
      backup/selection. `load_power_map()` returns a `PowerMap` for a valid map
      and `None` when `_read_settings` yields `None` (patched) — no crash.
    - _Requirements: 1.1, 1.2, 5.2_

- [x] 6. Checkpoint - pure core and settings read
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Add the cruise-average Percent_Power statistic in `analysis.py`
  - [x] 7.1 Add `avg_percent_power_cruise` to `FlightStats` and `get_flight_stats`
    - In `src/efis_data_manager/analysis.py`, add
      `avg_percent_power_cruise: Optional[float]` to `FlightStats`. In
      `get_flight_stats`, obtain `load_power_map()` once; when non-`None`, compute
      `percent_power` per row over the existing `cruise` row classification
      (inputs `rpm1`, the resolved MAP column, `pressure_altitude`, `oat`),
      average the defined (non-`None`) values, and set the field to that mean
      (or `None` when no cruise sample has a defined value or the map is
      unavailable).
    - _Requirements: 6.2, 5.1, 5.2_

  - [x]* 7.2 Write property test for the cruise-average rule
    - **Property 10: Cruise-average percent power ignores undefined samples**
    - **Validates: Requirements 6.2**

- [x] 8. Wire Percent_Power into the dashboard data route
  - [x] 8.1 Compute the `percent_power` series and append the descriptor in `api_flight_data`
    - In `src/efis_data_manager/dashboard/app.py` `api_flight_data`: obtain
      `load_power_map()` once; when non-`None`, resolve the MAP column via
      `map_column_for(resolve_aux())`, compute `percent_power` per row into
      `data["engine"]["percent_power"]` (aligned with `timestamps`; `null` when a
      sample lacks a valid input), and append the descriptor
      `{"key": "percent_power", "label": "% Power", "group": "engine",
      "precision": 0}` to `data["aux_params"]`. When `load_power_map()` is
      `None`, omit BOTH the series key and the descriptor.
    - _Requirements: 6.1, 6.3, 5.1, 5.2_

  - [x] 8.2 Add `avg_percent_power_cruise` to the flight-detail JSON in `api_flight_detail`
    - In `api_flight_detail`, include `stats.avg_percent_power_cruise` in the
      returned JSON (numeric when computable, `None` otherwise).
    - _Requirements: 6.2, 6.3_

  - [x]* 8.3 Write Flask integration tests for the route and stats wiring
    - With `load_power_map` patched to a valid `PowerMap`: `GET
      /api/flight/<id>/data` returns `engine.percent_power` aligned to
      `timestamps` and a `percent_power` descriptor in `aux_params`, and the
      detail route reports a numeric `avg_percent_power_cruise`. With
      `load_power_map` patched to `None`: the data route omits both the series
      and the descriptor and `avg_percent_power_cruise` is `None`.
    - _Requirements: 6.1, 6.2, 6.3, 5.2_

- [x] 9. Display the cruise-average Percent_Power on the flight page
  - [x] 9.1 Show `avg_percent_power_cruise` in the flight summary in `flight.html`
    - In `src/efis_data_manager/dashboard/templates/flight.html`, render the
      cruise-average Percent_Power in the summary when the value is non-`None`,
      and omit the row entirely when it is `None` (no zeros/placeholders). The
      plottable `percent_power` series flows in via the existing
      `mergeAuxParams()` path from the appended descriptor, so no static `PARAMS`
      edit is required.
    - _Requirements: 6.1, 6.2, 6.3_

- [x] 10. Final checkpoint - full suite
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test tasks and can be skipped for a faster
  MVP; core implementation tasks are never optional.
- Each task references specific requirement sub-clauses and, for test tasks, the
  exact design property number it implements.
- All ten design properties map to exactly one property-based test:
  P1 → 1.2; P3 → 1.3; P5 → 1.4; P6 → 2.2; P7 → 2.3; P8 → 2.4; P2 → 3.2;
  P4 → 3.3; P9 → 3.4; P10 → 7.2.
- Property-based tests use Hypothesis (`max_examples >= 100`) and carry a
  `# Feature: percent-power, Property N: …` tag. Hypothesis is ALREADY in
  `requirements.txt` (added by the `efis-settings-import` spec), so no
  dependency-bump task is needed.
- Reference/example tests (task 3.5) pin the 8-step pipeline to hand-computed
  values from this aircraft's real Power_Map (RATED_HP = 190) with ±0.5%
  tolerance and include at least one extrapolation case.
- Flask integration tests (tasks 5.3, 8.3) patch `load_power_map` rather than
  standing up a real settings backup, exercising both the available and
  unavailable branches.
- The computation does NOT clamp: GRT extrapolates beyond the table ends
  (Req 3.2), so values may exceed 100% or fall below 0%; the descriptor's
  `precision: 0` only rounds the display.
- **Deferred manual validation (Requirement 7):** validating the computed
  Percent_Power against real EFIS-displayed values for a recorded engine state is
  a documented manual check to be performed once new flight data and settings are
  available (expected the following evening). It is intentionally NOT a blocking
  automated test; task 3.5 asserts against hand-computed reference values now, and
  a known-EFIS-value assertion can be added to it later when a reference value
  exists.
- This change is dashboard/analysis-only (plus one helper in `efis_settings.py`);
  on release, bump versions per the versioning policy.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "1.4", "2.1", "5.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "2.4", "2.5", "3.1"] },
    { "id": 3, "tasks": ["3.2", "3.3", "3.4", "3.5", "4.1"] },
    { "id": 4, "tasks": ["4.2", "5.2"] },
    { "id": 5, "tasks": ["5.3", "7.1"] },
    { "id": 6, "tasks": ["7.2", "8.1"] },
    { "id": 7, "tasks": ["8.2"] },
    { "id": 8, "tasks": ["8.3", "9.1"] }
  ]
}
```
