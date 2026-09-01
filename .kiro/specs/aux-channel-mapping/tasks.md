# Tasks — Configurable EIS Aux Channel Mapping

- [x] 1. Add config schema + parameter catalog + resolver
  - Add `engine_category` and `aux_mapping` to `DEFAULT_CONFIG` (config.py),
    defaults preserving amps/MAP/fuel-pressure on aux1/2/3, aux4-6 = none.
  - Create `PARAM_CATALOG` and `get_aux_mapping()` / `resolve_aux()` in a new
    `aux_map.py` (or config.py section). Backfill per-channel defaults; make
    parameter→channel unique (first wins + log on duplicate).
  - Unit tests: default resolution, custom mapping, duplicate handling, None
    channels excluded.
  - _Requirements: 1.5, 2.1, 2.3, 6.1, 6.2_

- [x] 2. Persist aux4-aux6 in the database
  - Add aux4/aux5/aux6 to SCHEMA_SQL fdl_data CREATE.
  - Extend `_ensure_schema` migration loop to add aux4-6 (no-op-safe).
  - Add r.aux4/aux5/aux6 to `_insert_fdl_data` tuple/columns/placeholders.
  - Verify migration on existing DB adds columns with no row loss.
  - _Requirements: 5.1, 5.2, 5.3_

- [x] 3. Route `/api/flight/<id>/data` through the resolver
  - Replace hardcoded map/amps/fuel_pressure with dynamic engine-bucket keys
    from `resolve_aux()`; SELECT aux1-6; emit one series per mapped parameter.
  - Add `data["aux_params"]` (key/label/group/precision) for the client.
  - Verify against running dashboard with default + remapped config.
  - _Requirements: 2.1, 2.2, 3.1, 3.2, 6.1_

- [x] 4. Route extremes + episodes through the resolver
  - `api_flight_extremes`: build amps/MAP extreme specs only when mapped.
  - `detect_episodes`: run fuel-pressure episodes only when fuel_pressure is
    mapped; read the resolved channel, not hardcoded aux3.
  - Verify fuel-pressure episode present with default config, absent when
    fuel_pressure unmapped.
  - _Requirements: 2.2, 2.3, 6.1_

- [x] 5. Make flight.html PARAMS dynamic
  - On flight-data load, merge `aux_params` into PARAMS (label + precision).
  - Ensure Power panel shows MAP only when mapped; "+ add parameter" lists only
    mapped aux parameters; extreme buttons skip unmapped.
  - _Requirements: 3.1, 3.2, 3.3_

- [x] 6. Settings: engine category selector + reorder
  - Add Engine Category select as first control; Traditional shows Engine Type
    then Number of Cylinders; Water-Cooled hides them and shows the Rotax
    tester-invite note and hides the aux-mapping section.
  - Persist engine_category via the existing flat POST.
  - _Requirements: 4.1, 4.2, 4.3, 4.4_

- [x] 7. Settings: aux channel mapping UI
  - One row per aux1-6: parameter select (catalog), plus label+unit inputs shown
    only for Custom. Load from config.aux_mapping; save full object in POST.
  - Add help tooltips (channel purpose, Custom option, EIS applies scale/offset).
  - Round-trip test: set a mapping, save, reload, confirm persisted + applied.
  - _Requirements: 1.1, 1.2, 1.3, 1.4_

- [-] 8. Regression + release
  - With default config, confirm flight 3 Power panel, extreme buttons, and
    episodes match current output (no regression).
  - Bump version + DASHBOARD_VERSION, update README clone tag, commit, tag,
    push, refresh release ZIP + GitHub release.
  - _Requirements: 6.1, 6.2_
