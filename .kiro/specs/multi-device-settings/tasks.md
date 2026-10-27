# Implementation Plan: Multi-Device Settings

## Overview

Build the device-identity + DM-setup foundation for a one-to-three-display GRT
aircraft, bottom-up, so each step rests on tested lower layers and nothing is
left orphaned. The pure, flight-critical core comes first: the aircraft-keyed
configuration model, accessors, label helper, and migration in `config.py`;
then the one-field `device_link_id` extension to the per-drive identity file in
`drive_updater.py`. On top of that sits the new pure `device_resolution.py`
module — the resolve/classify state machine for drive insert (identity branch
via `extract_source_key`, Mode_S aircraft verification/learning, device-by-
Link_ID with the settings-file Link_ID authoritative, and the recorded-binding
fallback). Settings-save validation is extracted as a pure function next, then
the PyObjC Settings UI wires those controls in. Only after the pure core is
testable do we touch the I/O-side effects in `app.py`: the `_on_efis_drive_
mounted` resolution hook (proceed / learn-prompt / warn-and-leave-mounted) and
the `prepare_drive` blank-drive device-binding prompt. A final checkpoint runs
the full suite to confirm the 696 existing tests stay green alongside the new
tests.

All new pure logic lives in `src/efis_data_manager/device_resolution.py`, with
additive changes to `config.py`, `drive_updater.py`, `settings_window.py`, and
`app.py`. Property-based tests use **Hypothesis** (min 100 iterations each,
tagged `# Feature: multi-device-settings, Property N`), and each design property
(Properties 1–10) is implemented as exactly one property-based test placed near
the implementation it exercises. Example/integration tests cover the resolution
matrix (the 8-row table in the design's Testing Strategy), identity-file v1
compat, the `load_config` migration chain, Prepare binding (mocked rumps prompt,
asserting the `write_identity` payload), and the extracted Settings-save
validation.

This increment is the FOUNDATION only: it produces no archiving, outbound sync,
settings explore/compare/restore, FDL enforcement, divergence warning, or
forget-aircraft behavior. Those are future consumers and are out of scope here.

## Tasks

- [x] 1. Extend `config.py` with the aircraft-keyed model, accessors, label helper, and migration
  - [x] 1.1 Add the aircraft default, sentinel, accessors, and label helper
    - In `src/efis_data_manager/config.py`, add `aircraft: {}` to
      `DEFAULT_CONFIG` (empty map = managed aircraft not yet learned) and define
      `PENDING_AIRCRAFT_KEY = ":pending"` (a non-hex sentinel that cannot
      collide with a real Mode_S).
    - Add the aircraft-config accessors: `managed_mode_s(config)` (the single
      real, non-sentinel Mode_S key, else `None`), `managed_aircraft(config)`
      (that aircraft record or `None`), `configured_device(config, link_id)`
      (the Device_Config under the managed aircraft for a Link_ID, else `None`),
      and `device_count(config)` (the managed aircraft's `device_count`,
      defaulting to 1 when not learned).
    - Add the `device_label(device)` helper: `Link N` when the friendly name is
      empty or all-whitespace, `{friendly_name} (Link N)` when a non-whitespace
      friendly name is present, where N is the device's `link_id`.
    - _Requirements: 1.1, 1.2, 1.6, 1.7, 5.3, 5.4, 6.2_

  - [x] 1.2 Add `migrate_aircraft_config` and chain it into `load_config`
    - In `config.py`, add `migrate_aircraft_config(config)` modelled on
      `migrate_settings_import`: mutate-and-return, idempotent, crash-safe. When
      `aircraft` is missing or not a dict, set it to `{}`; when it is already a
      map (real Mode_S keys and/or the pending sentinel), leave it untouched.
      Never invent a Mode_S (binding is deferred to the learn-on-insert flow).
    - Call `migrate_aircraft_config(config)` in `load_config` immediately after
      `migrate_settings_import(config)`, following the `{**DEFAULT_CONFIG,
      **saved}` merge.
    - _Requirements: 6.1, 6.2, 6.3_

  - [x]* 1.3 Write property test for migration idempotency
    - **Property 1: Migration idempotency** — generate arbitrary config dicts
      (empty, missing `aircraft`, already-keyed map, pending-sentinel, legacy
      flat shape); assert `migrate_aircraft_config(migrate_aircraft_config(c))
      == migrate_aircraft_config(c)` and that it never raises.
    - `# Feature: multi-device-settings, Property 1`
    - **Validates: Requirements 6.2, 6.3**

  - [x]* 1.4 Write property test for migration key preservation
    - **Property 2: Migration preserves existing keys** — for any config dict,
      assert every non-`aircraft` key is byte-identical after migration and only
      the `aircraft` key is added or normalized.
    - `# Feature: multi-device-settings, Property 2`
    - **Validates: Requirements 6.2, 6.3**

  - [x]* 1.5 Write property test for label composition
    - **Property 3: Label composition round-trips the Link_ID** — generate
      random friendly names (including empty / whitespace-only / Unicode) and
      Link_IDs; assert `device_label` always contains the substring
      `Link {link_id}`, equals `Link {link_id}` exactly when the name is empty
      or all-whitespace, and equals `{friendly_name} (Link {link_id})` exactly
      when a non-whitespace name is present.
    - `# Feature: multi-device-settings, Property 3`
    - **Validates: Requirements 1.6, 1.7, 5.3, 5.4**

  - [x]* 1.6 Write example tests for accessors and the migration chain
    - `managed_mode_s` / `managed_aircraft` / `configured_device` /
      `device_count` over an empty map, a pending-sentinel-only config, and a
      single real-Mode_S record; assert the sentinel is never returned as the
      managed Mode_S and `device_count` defaults to 1 when not learned.
    - Run the full `load_config` chain (defaults merge → `migrate_settings_
      import` → `migrate_aircraft_config`) on a pre-feature config file lacking
      `aircraft`; assert no raise and single-device defaults.
    - _Requirements: 1.1, 1.2, 6.1, 6.2, 6.3_

- [x] 2. Extend the per-drive identity file with `device_link_id`
  - [x] 2.1 Add `device_link_id` to `_new_identity` and bump the schema version
    - In `src/efis_data_manager/drive_updater.py`, add `device_link_id: None`
      to the dict returned by `_new_identity` (preserving every existing field),
      and bump `IDENTITY_SCHEMA_VERSION` from `1` to `2` as an informational
      provenance marker. Do not add any read-time gate on `schema_version`, so
      v1 drives (no `device_link_id`) remain readable via `read_identity` and
      `.get("device_link_id")` returns `None`.
    - _Requirements: 4.3, 6.6_

  - [x]* 2.2 Write example tests for identity-file v1 backward-read compat
    - Assert `_new_identity` includes `device_link_id: None` and
      `schema_version: 2`; assert `read_identity` on a hand-written v1 JSON (no
      `device_link_id`) parses and `.get("device_link_id")` is `None`; assert
      `update_identity_provenance` can add `device_link_id` to a v1 drive while
      preserving `id`, `kind`, `volume_uuid`, and provenance fields.
    - _Requirements: 4.3, 6.6_

- [x] 3. Create `device_resolution.py` — the resolve/classify state machine
  - [x] 3.1 Implement `ResolutionStatus`, `ResolutionOutcome`, and `resolve_drive`
    - Create `src/efis_data_manager/device_resolution.py` with the
      `ResolutionStatus` enum (`RESOLVED`, `LEARN_PROMPT`,
      `AIRCRAFT_UNRECOGNIZED`, `AIRCRAFT_UNDETERMINED`, `DEVICE_NOT_CONFIGURED`,
      `DEVICE_UNDETERMINED`) and the `ResolutionOutcome` dataclass (`status`,
      `mode_s`, `link_id`, `device`, `display_label`, `binding_mismatch`,
      `recorded_link_id`, `message`).
    - Implement `resolve_drive(parsed, recorded_identity, config)` as the pure,
      side-effect-free state machine:
      - **Identity branch** (settings present): call `extract_source_key(parsed)`;
        Mode_S absent → `AIRCRAFT_UNDETERMINED`; no managed aircraft established
        and Mode_S present → `LEARN_PROMPT` carrying `mode_s`; Mode_S matches the
        managed aircraft → device-by-Link_ID with the **settings-file Link_ID
        authoritative** (matching configured device → `RESOLVED`, setting
        `binding_mismatch`/`recorded_link_id` when a differing `device_link_id`
        is recorded but still resolving to the settings Link_ID; no configured
        device → `DEVICE_NOT_CONFIGURED`; Link_ID absent → fall through to the
        recorded-binding branch); Mode_S present but differs → `AIRCRAFT_
        UNRECOGNIZED`.
      - **Recorded-binding branch** (no settings file or no settings Link_ID):
        recorded `device_link_id` matching a configured device → `RESOLVED` via
        the recorded binding; otherwise → `DEVICE_UNDETERMINED`.
    - Populate `display_label` via `config.device_label` on `RESOLVED`.
    - _Requirements: 2.1, 2.2, 2.3, 2.5, 2.6, 3.1, 3.2, 3.4, 3.5, 3.6, 4.1, 4.5, 4.6_

  - [x]* 3.2 Write property test for authoritative settings-file Link_ID
    - **Property 4: Settings-file Link_ID is authoritative** — for any populated
      drive whose settings yield a Link_ID matching a configured device,
      `resolve_drive` resolves to that settings-file device regardless of any
      recorded `device_link_id`, and flags `binding_mismatch` when the recorded
      binding differs.
    - `# Feature: multi-device-settings, Property 4`
    - **Validates: Requirements 2.2, 4.1, 4.6**

  - [x]* 3.3 Write property test for the recorded-binding fallback
    - **Property 5: Recorded binding is used only when no settings Link_ID is
      present** — for any drive with no readable settings file (or settings
      without a Link_ID), `resolve_drive` resolves to the recorded
      `device_link_id` when that Link_ID is configured, and returns
      `DEVICE_UNDETERMINED` when neither a settings Link_ID nor a configured
      recorded binding is available.
    - `# Feature: multi-device-settings, Property 5`
    - **Validates: Requirements 2.5, 2.6, 4.5**

  - [x]* 3.4 Write property test for aircraft-gated device resolution
    - **Property 6: Aircraft verification gates device resolution** — for any
      populated drive, `resolve_drive` returns `RESOLVED` or `LEARN_PROMPT` only
      when the derived Mode_S is present AND (no managed aircraft yet, or the
      Mode_S equals the managed aircraft's); an absent Mode_S yields
      `AIRCRAFT_UNDETERMINED`, a differing one yields `AIRCRAFT_UNRECOGNIZED`,
      and no non-matching/undetermined aircraft ever produces `RESOLVED`.
    - `# Feature: multi-device-settings, Property 6`
    - **Validates: Requirements 3.1, 3.4, 3.5, 3.6**

  - [x]* 3.5 Write property test for unconfigured Link_ID
    - **Property 7: Unconfigured Link_ID never resolves to a device** — for any
      populated drive belonging to the managed aircraft whose settings Link_ID
      matches no configured device, `resolve_drive` returns
      `DEVICE_NOT_CONFIGURED` and never returns a device.
    - `# Feature: multi-device-settings, Property 7`
    - **Validates: Requirements 2.3, 2.4**

  - [x]* 3.6 Write property test for resolution determinism
    - **Property 8: Resolution is deterministic** — for any fixed inputs
      (`parsed`, `recorded_identity`, `config`), two calls to `resolve_drive`
      return the same `ResolutionStatus` and the same resolved device.
    - `# Feature: multi-device-settings, Property 8`
    - **Validates: Requirements 2.3, 3.4, 4.1**

  - [x]* 3.7 Write example tests for the resolution matrix
    - Table-driven example tests over the 8-row matrix in the design's Testing
      Strategy (one row per expected status): RESOLVED; RESOLVED + mismatch;
      DEVICE_NOT_CONFIGURED; LEARN_PROMPT; AIRCRAFT_UNRECOGNIZED; AIRCRAFT_
      UNDETERMINED; RESOLVED via recorded binding; DEVICE_UNDETERMINED.
    - _Requirements: 2.3, 2.4, 2.5, 2.6, 3.2, 3.5, 3.6, 4.5, 4.6_

- [x] 4. Extract the Settings-save validation as a pure function
  - [x] 4.1 Implement `validate_device_config` (reject-and-retain)
    - Add a pure validation function (in `config.py` or a small helper imported
      by `settings_window.py`) that takes the prior valid managed-aircraft
      record and the proposed edits and returns the record to persist plus any
      rejection reasons: device count outside 1–3 is **rejected** and the prior
      valid count retained; each device carries a `friendly_name`, a user-
      declared `chart_capable` boolean (no catalog lookup), and a `link_id` in
      1–3; a Link_ID that duplicates another configured device's Link_ID is
      **rejected** and the prior Link_ID assignments retained; the analysis-data-
      source is a single-select naming one configured device by Link_ID with no
      capability gating (any configured device is selectable regardless of
      `chart_capable`).
    - _Requirements: 1.3, 1.4, 1.5, 1.8, 1.9, 1.10, 1.11, 1.12_

  - [x]* 4.2 Write property test for validation reject-and-retain
    - **Property 10: Device-count and Link_ID validation preserve prior valid
      state** — for any prior valid record and any attempted edit, a device
      count outside 1–3 retains the previous count and a duplicate Link_ID
      retains the previous Link_ID assignments.
    - `# Feature: multi-device-settings, Property 10`
    - **Validates: Requirements 1.4, 1.9, 1.10**

  - [x]* 4.3 Write example tests for validation edge cases
    - Device count boundaries (0/1/3/4); duplicate vs unique Link_ID
      assignments; analysis-source selectable regardless of each device's
      `chart_capable` value.
    - _Requirements: 1.3, 1.5, 1.8, 1.11, 1.12_

- [x] 5. Wire the device-config controls into `settings_window.py`
  - [x] 5.1 Add the device-config UI section and save wiring
    - In `src/efis_data_manager/settings_window.py`, add, using the existing
      `_add_label` / `_add_text_field` / `_add_checkbox` idiom and the running
      `y` cursor: a device-count control (1–3); one per-device row per device
      index (friendly-name text field, chart-capable checkbox, Link_ID
      field/selector constrained to 1–3); and a single-select analysis-data-
      source selector over the configured devices, each labelled via
      `device_label(...)` with no capability gating.
    - In `saveClicked_`, read the controls back, call `validate_device_config`
      (keeping the previous valid values on rejection, mirroring the existing
      "keep previous value" idiom), build the managed aircraft record (editing
      the pending record under `PENDING_AIRCRAFT_KEY` when no Mode_S is learned
      yet), and persist via `save_config` so subsequent drive-insert resolutions
      read the updated config. Grow the window-height constant to fit the new
      section; leave existing fields and save/cancel wiring unchanged.
    - _Requirements: 1.13, 5.1, 5.2, 5.3, 5.4, 5.7_

- [x] 6. Checkpoint - pure core + Settings UI
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Insert the resolution hook into `app.py` `_on_efis_drive_mounted`
  - [x] 7.1 Call `resolve_drive` and map outcomes to side effects
    - In `src/efis_data_manager/app.py` `_on_efis_drive_mounted`, after the
      `_is_provisioning` guard and before the archive is started, parse the
      settings file (if present; a `SettingsParseError` is treated as
      `parsed=None`) and read the recorded identity, then call
      `device_resolution.resolve_drive(...)` and dispatch on the status:
      - `RESOLVED` → proceed to the existing archive/sync behavior unchanged;
        when `binding_mismatch`, post a best-effort `_notify` reporting the
        mismatch but still proceed to the authoritative settings-file device.
        Resolution is automatic and non-blocking.
      - `LEARN_PROMPT` → prompt (rumps) to confirm adopting the derived Mode_S
        as the managed aircraft; on confirm, persist the managed aircraft via
        `save_config` and re-resolve; on decline, leave the drive mounted and
        take no action.
      - `AIRCRAFT_UNRECOGNIZED` / `AIRCRAFT_UNDETERMINED` / `DEVICE_NOT_
        CONFIGURED` / `DEVICE_UNDETERMINED` → reuse the `_on_adoption_candidate`
        pattern: log, `_notify` the specific reason, take no archive/sync/push/
        eject action, and leave the drive mounted.
    - _Requirements: 2.4, 2.6, 3.2, 3.3, 3.5, 3.6, 4.6, 5.5, 5.6_

  - [x]* 7.2 Write property test for learn-only-on-confirmation (pure side)
    - **Property 9: Learn binds the managed aircraft only on confirmation** —
      for any insert while no managed aircraft is established with a present
      derived Mode_S, `resolve_drive` returns `LEARN_PROMPT`, and (the pure
      resolution outcome) never itself writes a real Mode_S key into the
      `aircraft` map.
    - `# Feature: multi-device-settings, Property 9`
    - **Validates: Requirements 3.2, 3.3**

  - [x]* 7.3 Write example/integration test for the learn-prompt persist side
    - With the rumps prompt mocked: confirming a `LEARN_PROMPT` persists the
      managed aircraft under the real Mode_S (via `save_config`) and re-resolves;
      declining leaves the `aircraft` map with no real Mode_S key, takes no
      action, and leaves the drive mounted. Assert the terminal non-confident
      outcomes take no archive/sync/push/eject action.
    - _Requirements: 3.2, 3.3, 5.5, 5.6_

- [x] 8. Add the Prepare-Drive device binding in `app.py`
  - [x] 8.1 Bind blank drives to a device at Prepare time
    - In `app.py` `prepare_drive` (the blank-drive `adopt = False` branch that
      spawns `_do_prepare_drive`), after the identity file is written: when
      `device_count(config) > 1`, prompt the user to choose which configured
      device (picker labelled via `device_label(...)`) and record the chosen
      device's Link_ID; when `device_count(config) == 1`, bind to that single
      device without prompting. Write the binding by setting `device_link_id`
      via `write_identity` / `update_identity_provenance`.
    - _Requirements: 4.2, 4.3, 4.4, 6.4, 6.5_

  - [x]* 8.2 Write example tests for Prepare binding (mocked prompt)
    - With the rumps prompt mocked: a single-device install binds without
      prompting and writes `device_link_id`; a multi-device install records the
      chosen device's Link_ID. Assert on the `write_identity` payload.
    - _Requirements: 4.2, 4.3, 4.4, 6.4, 6.5_

- [x] 9. Final checkpoint - full suite
  - Run `./venv/bin/python -m pytest`; confirm the 696 existing tests still pass
    alongside the new unit and property tests, and clean up any temp artifacts.
    Ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test tasks and can be skipped for a faster
  MVP; core implementation tasks are never optional and are never postfixed `*`.
- Each implementation sub-task references specific requirement sub-clauses, and
  each test task names the exact design property number it implements.
- All design properties (Properties 1–10) map to exactly one property-based
  test: P1 → 1.3; P2 → 1.4; P3 → 1.5; P4 → 3.2; P5 → 3.3; P6 → 3.4; P7 → 3.5;
  P8 → 3.6; P10 → 4.2; P9 → 7.2 (the pure resolution outcome) with its persist-
  on-confirm side covered by the example/integration test 7.3.
- Property-based tests use Hypothesis (`max_examples>=100`) and carry a
  `# Feature: multi-device-settings, Property N` tag.
- Example/integration tests cover the 8-row resolution matrix (3.7), identity-
  file v1 compat (2.2), the `load_config` migration chain (1.6), Prepare binding
  with a mocked rumps prompt asserting the `write_identity` payload (8.2), and
  the extracted Settings-save validation (4.3).
- This increment adds `src/efis_data_manager/device_resolution.py` and makes
  additive changes to `config.py`, `drive_updater.py`, `settings_window.py`, and
  `app.py`. Per the versioning policy, the menu-bar tool changed, so a release
  bumps `MENUBAR_VERSION` (not `DASHBOARD_VERSION`).
- Per-device archiving, outbound sync/push, settings explore/compare/restore,
  FDL enforcement, divergence warning, and forget-aircraft are out of scope here
  and consume this foundation in later increments.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1"] },
    { "id": 1, "tasks": ["1.2", "1.5", "2.2", "4.1"] },
    { "id": 2, "tasks": ["1.3", "1.4", "1.6", "3.1", "4.2", "4.3"] },
    { "id": 3, "tasks": ["3.2", "3.3", "3.4", "3.5", "3.6", "3.7", "5.1"] },
    { "id": 4, "tasks": ["7.1", "8.1"] },
    { "id": 5, "tasks": ["7.2", "7.3", "8.2"] }
  ]
}
```
