# Implementation Plan: Settings Management

## Overview

Build bottom-up so each layer rests on tested lower layers. First the pure
per-device archive location and history helpers (`settings_archive.py`) and the
byte-preserving sole-slot writer with its divergence predicate
(`settings_writer.py`), which are independent of each other. Then route the
archiver through the per-device location, add the one-time flat-archive
migration, and layer the populate path on the writer. Only after the pure core
is testable do we wire the DM (`app.py`: pass the resolved Link_ID, fire the
divergence notification, run the migration at startup, expose populate) and the
dashboard (read-only history/view/compare endpoints, the restore handoff, and
the `settings.html` UI).

Archiving covers Settings, WP, and Plan; restoration and population are
Settings-only (R5.9, R6.6). The writer never parses-then-reserializes: it only
copies an archived file's original bytes (checksum wall, R8). Divergence is a
pure Content_Hash mismatch, never an older/newer verdict.

Property-based tests use **Hypothesis** (min 100 examples), tagged
`# Feature: settings-management, Property N`, one test per design property
(Properties 1-10). Example/integration tests cover wiring, dashboard endpoints,
and fault tolerance. Existing tests must stay green throughout.

## Tasks

- [x] 1. Create `settings_archive.py` - per-device archive location and history
  - [x] 1.1 Implement `ArchivedSnapshot`, `device_settings_dir`, `family_history`, `latest_version`
    - Create `src/efis_data_manager/settings_archive.py`. Add the
      `ArchivedSnapshot` dataclass (`path`, `family`, `link_id`, `archive_date`,
      `sequence`).
    - `device_settings_dir(archive_root, link_id)` returns
      `archive_root/Settings/link{N}`, or `archive_root/Settings/unresolved`
      when `link_id` is None/empty.
    - `family_history(archive_root, link_id, family)` scans the device dir for
      `<family>-YYYY-MM-DD[-N].<ext>`, reusing `parse_archive_date` and the
      archiver's sequence parsing, and returns newest to oldest by
      (Archive_Date, sequence). Missing dir returns an empty list. Read-only.
    - `latest_version(...)` returns the first history item or None.
    - _Requirements: 1.7, 2.1, 2.2, 2.3, 2.4_

  - [x] 1.2 Write property test for recency ordering
    - **Property 4: Recency ordering ignores mtime and UPDATE** - generate
      arbitrary histories with randomized mtimes and UPDATE values; assert
      `latest_version` equals the max by (Archive_Date, sequence).
    - `# Feature: settings-management, Property 4`
    - **Validates: Requirements 2.1, 1.7**

- [x] 2. Route the archiver through the per-device location
  - [x] 2.1 Add `link_id` to `archive_efis_drive`
    - In `src/efis_data_manager/archiver.py`, add optional `link_id: str | None
      = None` and set `settings_dest = device_settings_dir(archive_root,
      link_id)`. Leave `_archive_settings_family` and all non-settings archiving
      (FDL, logbook, DEMO, snapshots, cleanup) unchanged. Existing callers
      without `link_id` write to `Settings/unresolved/`.
    - _Requirements: 1.1, 1.2, 1.4, 1.5, 1.8, 1.9, 1.10_

  - [x] 2.2 Write property test for per-device isolation
    - **Property 1: Per-device archive isolation** - for arbitrary Link_IDs and
      settings content, archiving under Link_ID X writes only under X's dir.
    - `# Feature: settings-management, Property 1`
    - **Validates: Requirements 1.1**

  - [x] 2.3 Write property test for content-aware dedup per device
    - **Property 2: Content-aware dedup per device** - identical content
      re-archived writes nothing; differing content writes exactly one snapshot.
    - `# Feature: settings-management, Property 2`
    - **Validates: Requirements 1.4, 1.5**

  - [x] 2.4 Write property test for same-date sequencing
    - **Property 3: Non-destructive same-date sequencing** - N distinct
      same-date changes yield N snapshots, none overwritten.
    - `# Feature: settings-management, Property 3`
    - **Validates: Requirements 1.6**

- [x] 3. Add the one-time flat-archive migration
  - [x] 3.1 Implement `migrate_flat_archives` with sentinel
    - In `settings_archive.py`, add `MigrationResult` and
      `migrate_flat_archives(archive_root)`: for each snapshot directly under
      `Settings/` (not in `link*/` or `unresolved/`), parse read-only, derive
      Link_ID via `extract_source_key`, and `shutil.move` it to
      `device_settings_dir(...)` using `_next_available_snapshot_name` for
      collisions. Underivable Link_ID goes to `unresolved/`. Per-file failures
      are logged and the file left in place. Write a sentinel
      (`Settings/.per_device_migrated`) after a complete pass so reruns are
      no-ops.
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6_

  - [x] 3.2 Write property test for migration
    - **Property 10: Migration adoption is faithful and idempotent** - arbitrary
      flat archives are adopted to the content-derived Link_ID (or unresolved),
      bytes/date/sequence preserved, no collision overwritten, second run is a
      no-op.
    - `# Feature: settings-management, Property 10`
    - **Validates: Requirements 11.2, 11.3, 11.4, 11.5, 11.6**

- [x] 4. Create `settings_writer.py` - sole-slot writer, populate, divergence
  - [x] 4.1 Implement `WriteResult`, `sole_slot_write`, `restore_version`
    - Create `src/efis_data_manager/settings_writer.py`. `sole_slot_write(
      archived_path, mount_point, family="Settings")` copies the archived bytes
      to `<mount>/<family><ext>` (the archived file's own extension), verifies
      SHA-256 of target equals source, and only after a passing verify removes
      the other `.bak`/`.dat` slot. On verify failure, unlink the new file,
      leave the other slot intact, return status "failed". Never parse and
      reserialize. `restore_version` calls it with "Settings".
    - _Requirements: 5.2, 5.3, 5.4, 5.5, 5.6, 5.9, 8.1, 8.2, 8.3_

  - [x] 4.2 Write property test for byte preservation
    - **Property 5: Sole-slot write preserves original bytes**
    - `# Feature: settings-management, Property 5`
    - **Validates: Requirements 5.3, 8.1, 8.3**

  - [x] 4.3 Write property test for sole slot
    - **Property 6: Sole-slot write leaves exactly one slot** - across arbitrary
      initial slot states.
    - `# Feature: settings-management, Property 6`
    - **Validates: Requirements 5.4**

  - [x] 4.4 Write property test for failure retention
    - **Property 7: Write failure retains a valid slot** - inject a verify
      failure; assert the pre-existing slot survives and status is "failed".
    - `# Feature: settings-management, Property 7`
    - **Validates: Requirements 5.6**

  - [x] 4.5 Implement `populate_latest`
    - Look up `latest_version(archive_root, link_id, "Settings")`. Return
      "skipped-none" when absent, "skipped-equal" when the drive's current
      Settings slot Content_Hash equals the latest's, otherwise call
      `sole_slot_write`. Settings only; scoped to the given Link_ID.
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_

  - [x] 4.6 Write property test for population idempotency
    - **Property 8: Population idempotency**
    - `# Feature: settings-management, Property 8`
    - **Validates: Requirements 6.1, 6.2**

  - [x] 4.7 Implement `DivergenceResult` and `diverges`
    - Compare the drive's current Settings slot Content_Hash (via the archiver's
      `_current_settings_slot`) with the latest archived version's. No baseline
      or no drive slot returns `diverged=False`. Never consult timestamps or
      UPDATE_Value.
    - _Requirements: 7.1, 7.2, 7.4, 7.5_

  - [x] 4.8 Write property test for divergence
    - **Property 9: Divergence is a pure hash mismatch**
    - `# Feature: settings-management, Property 9`
    - **Validates: Requirements 7.1, 7.4, 7.5**

- [x] 5. Checkpoint - pure core
  - Run `./venv/bin/python -m pytest`; confirm existing tests stay green with the
    new unit and property tests. Ask the user if questions arise.

- [x] 6. Wire the DM (`app.py`)
  - [x] 6.1 Pass Link_ID to the archive and fire the divergence notification
    - `_proceed_resolved` / `_run_archive` pass `outcome.link_id` into
      `archive_efis_drive`. After archiving a resolved device, call
      `settings_writer.diverges(...)`; on divergence `_notify("Settings drift",
      ...)` using `device_label` and the latest Archive_Date. The notification
      is non-blocking. The pre-provisioning archive passes `link_id=None`.
    - _Requirements: 1.1, 1.10, 7.2, 7.3, 10.1_

  - [x] 6.2 Run the migration at DM startup
    - Call `migrate_flat_archives(archive_root)` once on startup, wrapped so any
      failure is logged and never blocks launch.
    - _Requirements: 11.1, 11.6_

  - [x] 6.3 Expose population as a DM action
    - Add a menu action that, for the currently bound/resolved drive, runs
      `populate_latest` and reports the outcome (written / already current / no
      archive) via `_notify`.
    - _Requirements: 6.1, 6.3, 10.1_

  - [x] 6.4 Write example tests for DM wiring
    - With mocked `_notify`: resolved insert archives into `Settings/link{N}/`;
      divergence notifies on mismatch and not on match; unresolved drive takes
      no archive action; migration failure does not block startup; populate
      action reports each outcome.
    - _Requirements: 1.1, 1.10, 6.1, 7.2, 7.4, 11.6_

- [x] 7. Dashboard read-only API and restore handoff
  - [x] 7.1 Add devices, history, and view endpoints
    - In `dashboard/app.py` add `GET /api/settings/devices`,
      `GET /api/settings/history`, and `GET /api/settings/view` (SID, label,
      value, unlabeled flag via `Settings_Mapper`). All read-only; parse
      failures return a reported error and leave files unchanged.
    - _Requirements: 2.1, 2.2, 2.3, 3.1, 3.2, 3.3, 3.4, 3.5, 9.1, 9.2_

  - [x] 7.2 Add the compare endpoint
    - `GET /api/settings/compare` takes two archived names, or `b=drive` for the
      current returning-drive slot. Returns a labeled per-SID diff with added and
      removed SIDs, excludes `UPDATE`/`CHECKSIZE`/`CHECKSUM`, and reports
      "equivalent" when Content_Hash is equal.
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_

  - [x] 7.3 Add the restore POST handoff
    - `POST /api/settings/restore {link_id, family:"Settings", name}` resolves
      the archived file, requires an explicit request (never automatic), and
      invokes `settings_writer.restore_version` against the drive bound to that
      device. Rejects any family other than Settings. Returns the `WriteResult`.
    - _Requirements: 5.1, 5.2, 5.7, 5.8, 5.9, 10.2_

  - [x] 7.4 Write example tests for the dashboard endpoints
    - History ordering, view with labeled and unlabeled SIDs, compare
      (archived vs archived, archived vs drive, equivalent), restore handoff with
      a mocked drive, WP/Plan restore rejected, malformed snapshot reported.
    - _Requirements: 2.1, 3.3, 3.5, 4.4, 5.1, 5.9_

- [x] 8. Dashboard UI
  - [x] 8.1 Add the settings explorer to `settings.html`
    - Per-device history list, read-only value view, two-version compare table,
      and a "Restore this version" control that POSTs the selection. No edit
      controls anywhere.
    - _Requirements: 3.4, 4.1, 5.1, 8.2, 10.2_

- [x] 9. Final checkpoint - full suite
  - Run `./venv/bin/python -m pytest`; confirm all tests pass, clean temp
    artifacts. Per the versioning policy a release bumps `__version__`,
    `pyproject.toml`, `MENUBAR_VERSION`, and `DASHBOARD_VERSION` (both UIs
    change). Ask the user if questions arise.

- [x] 10. Divergence confirmation before archiving (revised R7)
  - [x] 10.1 Extend `diverges` with the history match
    - Add `matched_archive_date` to `DivergenceResult`. When the card diverges
      from the latest archived version, scan the device's older Settings
      snapshots and set it to the Archive_Date of the most recent one whose
      Content_Hash equals the card's, else `None`. Content_Hash only; never
      UPDATE_Value or timestamps.
    - _Requirements: 7.1, 7.3, 7.7_

  - [x] 10.2 Write property test for the history match
    - **Property 11: History match is a pure hash lookup** - the matched date is
      the most recent older snapshot with equal Content_Hash, `None` when no
      snapshot matches, independent of UPDATE_Value and mtimes.
    - `# Feature: settings-management, Property 11`
    - **Validates: Requirements 7.3, 7.7**

  - [x] 10.3 Add `archive_settings` to `archive_efis_drive`
    - New keyword `archive_settings: bool = True`. When `False`, skip the
      Settings/WP/Plan loop entirely; FDL, logbook, DEMO, snapshots, and cleanup
      run unchanged.
    - _Requirements: 7.4, 7.5_

  - [x] 10.4 Move the divergence check before the archive and prompt
    - In the DM mount path, call `diverges` before archiving. When diverged,
      show the blocking confirmation (ok "Archive settings", cancel "Skip
      settings") with the device label, latest Archive_Date, and the history
      match wording. Cancel passes `archive_settings=False`. Remove the
      post-archive "Settings drift" notification. No prompt when not diverged,
      no baseline, or no card slot.
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6_

  - [x] 10.5 Write tests for the prompt flow against a real archive
    - Use a real temp archive and card (do not mock `diverges`): differing card
      prompts and archive proceeds on OK; skip leaves Settings/WP/Plan archive
      unchanged while FDL still archives; matching card, no baseline, and no
      card slot do not prompt; message includes the matched date or the "new
      content" wording.
    - _Requirements: 7.2, 7.3, 7.4, 7.5, 7.6_

- [x] 11. Final checkpoint - full suite
  - Run `./venv/bin/python -m pytest`; confirm all tests pass, clean temp
    artifacts. Ask the user if questions arise.

- [x] 12. Fix existing consumers of the archive after migration (regression)
  - [x] 12.1 Make archived-backup discovery read the per-device layout
    - `efis_settings._select_current_archived_backup` (used by
      `Import_Workflow.detect_new_backup` and `load_current_settings_sids`)
      globs only `<archive_root>/Settings/Settings*.bak|dat`, which is empty
      after migration, so the settings import card reports no backup and
      percent-power loses its Power_Map. Collect `Settings*.bak|dat` candidates
      from `Settings/` and from each immediate subdirectory (`link*/`,
      `unresolved/`), then select exactly as before. Only the `Settings` family
      is considered (not WP, Plan, or State).
    - _Requirements: 11.7, 10.4_

  - [x] 12.2 Write tests against the per-device layout
    - Import detection and `load_current_settings_sids` find a backup when files
      exist only under `link1/`; still find legacy flat files; pick the latest
      Archive_Date across devices; ignore WP/Plan/State files; return None for an
      empty tree.
    - _Requirements: 11.7_

- [x] 13. Final checkpoint - full suite
  - Run `./venv/bin/python -m pytest`; confirm all tests pass, clean temp
    artifacts. Ask the user if questions arise.

- [x] 14. Restrict settings import detection to the primary display
  - [x] 14.1 Filter import candidates to primary-display backups
    - Give `_select_current_archived_backup` an optional `link_id` filter that
      keeps only candidates whose content Link_ID (`extract_source_key`)
      equals it, applied before selection. `Import_Workflow.detect_new_backup`
      passes `"1"`; `load_current_settings_sids` is unchanged. Add tests: a
      newer Link 2 backup does not hide an older Link 1 backup; a primary
      backup in `unresolved/` or flat is found; only non-primary backups gives
      None.
    - _Requirements: 11.8_

- [x] 15. Final checkpoint - full suite
  - Run `./venv/bin/python -m pytest`; confirm all tests pass, clean temp
    artifacts. Ask the user if questions arise.

- [x] 16. Label the power-map SIDs in the settings explorer
  - [x] 16.1 Add power-map labels to `recognized_sid_labels`
    - Label SID 167 ("Rated HP") and the power-map columns 168-217 (RPM,
      MAP for 55%, MAP for 75%, Altitude, Delta HP; rows 1-10), reusing the SID
      constants in `power.py` (GRT `Settings.h` layout) rather than redefining
      them. Applies to the view and compare endpoints. Add tests.
    - _Requirements: 3.2, 10.4_

- [x] 17. Label settings from GRT's own SID reference
  - [x] 17.1 Build a SID dictionary from the GRT reference files
    - New module `grt_sids.py` that reads the verbatim GRT files in
      `resources/grt/` (`Settings.h`, `DEMO-settings-format.txt`) and exposes,
      per SID: the GRT macro name, a readable label, and (where GRT documents
      it) the meaning of each value. Expand `// N ID's used by X` blocks into
      numbered entries; ignore commented-out defines and `STID_` (State-file)
      ids. Label precedence: GRT doc name, then the existing curated labels
      (power map, identity), then a humanized `Settings.h` name. Package the
      resource files.
    - _Requirements: 3.2, 10.4_

  - [x] 17.2 Use the dictionary in the view, compare, and explorer UI
    - `recognized_sid_labels` and the view/compare endpoints use the
      dictionary; rows also carry `grt_name` and, when documented,
      `value_meaning`. The explorer shows the meaning next to the value and the
      GRT name on hover. Update existing label assertions to the GRT names.
    - _Requirements: 3.1, 3.2, 3.3, 4.1_

## Notes

- Tasks marked `*` are optional test tasks; core implementation tasks are never
  optional.
- Each implementation sub-task cites requirement sub-clauses; each property test
  names its design property number.
- Property map: P1 -> 2.2; P2 -> 2.3; P3 -> 2.4; P4 -> 1.2; P5 -> 4.2; P6 ->
  4.3; P7 -> 4.4; P8 -> 4.6; P9 -> 4.8; P10 -> 3.2; P11 -> 10.2.
- Out of scope: staging EFIS/AHRS software, analysis-data-source enforcement,
  editing settings values, multi-aircraft, and restoring or populating WP/Plan.
- Known concern (accepted): the DM cannot order captures it did not observe, so
  divergence reports a mismatch only; the pilot decides via the pre-archive confirmation.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "4.1"] },
    { "id": 1, "tasks": ["1.2", "2.1", "3.1", "4.2", "4.3", "4.4", "4.5", "4.7", "7.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "2.4", "3.2", "4.6", "4.8", "6.1", "6.2", "6.3", "7.2", "7.3"] },
    { "id": 3, "tasks": ["6.4", "7.4", "8.1", "10.1", "10.3"] },
    { "id": 4, "tasks": ["10.2", "10.4"] },
    { "id": 5, "tasks": ["10.5", "12.1"] },
    { "id": 6, "tasks": ["12.2"] },
    { "id": 7, "tasks": ["14.1"] },
    { "id": 8, "tasks": ["16.1"] },
    { "id": 9, "tasks": ["17.1"] },
    { "id": 10, "tasks": ["17.2"] }
  ]
}
```
