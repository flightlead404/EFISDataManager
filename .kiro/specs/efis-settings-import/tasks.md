# Implementation Plan: EFIS Settings Import

## Overview

Build the read → select → map → preview → apply pipeline for importing GRT HXr
EFIS settings backups into the dashboard's alert thresholds. Work is ordered
bottom-up: pure data/parse primitives first (`Settings_Parser`, checksum,
`Settings_Selector`), then the value translation (`Settings_Mapper` + declarative
SID table), then identity/hash helpers, then the orchestration
(`Import_Workflow`) that ties detection/preview/apply together. Only after the
pure core is testable do we extend the two existing I/O-touching modules
(`archiver.py` `.dat` copying, `analysis.py` new thresholds + alert paths), then
the Flask routes and Settings-page UI, and finally config migration + a
requirements.txt bump and a full-suite run.

All new pure logic lives in a single new module
`src/efis_data_manager/efis_settings.py`. Property-based tests use **Hypothesis**
(min 100 iterations each, tagged `# Feature: efis-settings-import, Property N`),
and each design property (Properties 1–32, with Property 20 intentionally
absent) is implemented as exactly one property-based test. Example/integration tests cover the concrete SID table,
archiver `.dat` copying, Vne mutual exclusivity, source-keyed detection, and the
Flask routes via a temp SQLite `fdl_data` DB.

The import is read-only on source files, applied atomically via `save_config`,
and gated behind a double-confirmation.

## Tasks

- [x] 1. Create `efis_settings.py` with the parser and data models
  - [x] 1.1 Implement `Settings_Parser` and `ParsedBackup`
    - Create `src/efis_data_manager/efis_settings.py`.
    - Add the `ParsedBackup` dataclass (`path`, `sids: dict[str,str]`,
      `update_value`, `checksize`, `checksum`, `line_count`, `valid_pairs`) and
      a `SettingsParseError` exception.
    - Implement `Settings_Parser.parse(path)`: open read-only ("r"), decode as
      ASCII with `errors="replace"`; split each line on the FIRST "=" only;
      retain unknown SIDs; recognize `UPDATE=`/`CHECKSIZE=`/`CHECKSUM=`; skip
      malformed lines; default missing special lines to `None`; raise
      `SettingsParseError` on OSError or zero valid pairs. Never write/truncate/
      rename/delete the source.
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 10.1, 10.2_

  - [x]* 1.2 Write property test for parse round-trip
    - **Property 1: Parse round-trip preserves SID map**
    - **Validates: Requirements 1.1, 1.3**

  - [x]* 1.3 Write property test for garbage tolerance
    - **Property 2: Parser tolerates garbage and never crashes**
    - **Validates: Requirements 1.2, 10.2**

  - [x]* 1.4 Write property test for read-only source guarantee
    - **Property 3: Parser is read-only on the source** (SHA-256 of source
      identical before/after `parse()`, including failure paths).
    - **Validates: Requirements 1.6, 10.1**

  - [x]* 1.5 Write property test for empty-of-valid-pairs failure
    - **Property 4: Empty-of-valid-pairs input fails cleanly** (`parse()` raises
      `SettingsParseError`, source unmodified).
    - **Validates: Requirements 1.6**

  - [x]* 1.6 Write property test for UPDATE/checksum extraction
    - **Property 5: UPDATE / checksum extraction**
    - **Validates: Requirements 1.4, 1.5**

- [x] 2. Implement best-effort checksum + `Settings_Selector`
  - [x] 2.1 Add pluggable checksum verifier
    - In `efis_settings.py`, add `ChecksumStatus` enum
      (`VERIFIED`/`FAILED`/`UNVERIFIABLE`) and `verify_checksum(parsed)`
      defaulting to `UNVERIFIABLE`. Do NOT invent GRT's algorithm; keep the
      boundary honest and pluggable.
    - _Requirements: 2.3, 10.1_

  - [x] 2.2 Implement `Settings_Selector`
    - Add `SelectionResult` dataclass (`current`, `reason`, `checksum_note`) and
      `Settings_Selector.select(candidates)`: restrict to `VERIFIED` when some
      verify and some fail; return `current=None` on all-`FAILED`; treat
      `UNVERIFIABLE`-only as no integrity signal and fall back to highest
      `update_value`; single candidate wins; deterministic tiebreak (prefer
      `.dat`, then path) with a note when all `update_value` are `None`.
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

  - [x]* 2.3 Write property test for UPDATE ordering
    - **Property 6: Selector picks the higher UPDATE_Value** (and single
      candidate is chosen).
    - **Validates: Requirements 2.1, 2.2**

  - [x]* 2.4 Write property test for verification handling
    - **Property 7: Selector honors integrity verification when available**
      (inject stubbed `verify_checksum` returning VERIFIED/FAILED/UNVERIFIABLE).
    - **Validates: Requirements 2.3, 2.4, 2.5**

- [x] 3. Implement `Settings_Mapper` with the declarative SID table
  - [x] 3.1 Add the mapping table and `Settings_Mapper.map`
    - Add `MappedValue` and `MappedSettings` dataclasses and the declarative SID
      table (SID → threshold key(s), value kind, disabled sentinel, tier flag)
      covering SIDs 120/118/121/151/144/147/1463/1464/353/354/123/150/22/889/
      331/329/332/330/345/1055/1054/58.
    - Implement units/rounding by kind: temp/speed → nearest int; C→F via
      `v*9/5+32` when SID 345==1; cooling rate (SID 150) uses delta form
      `v*9/5` (no offset); g/voltage → 1 decimal; pressure/rpm/count → int.
    - Skip Disabled_Value SIDs (omit key from output). Emit CHT/EGT/Oil Temp
      (SIDs 151/144/121) as `tier_choices`; map G-meter both tiers directly.
    - Preserve negative-G sign (SIDs 332/330 stored ≤ 0).
    - Vne mutual exclusivity via SID 889: set one of `vne_tas_redline`/
      `vne_ias_redline` and clear the other to sentinel `9999`; if SID 22 is a
      Disabled_Value, set neither.
    - Cylinder count precedence: SID 1055 → 1054 → 58, else leave unchanged.
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 4.10, 5.1, 5.3, 5.4, 5.7, 6.1, 6.2, 6.3, 6.4, 7.1, 7.2, 7.3, 7.4, 7.5, 12.1, 13.1, 13.2, 13.4_

  - [x]* 3.2 Write example tests for the SID→threshold-key table
    - One assertion per table entry (Req 4.1–4.9, 5.1, 6.1, 6.2) plus
      exactly-at-Disabled_Value boundaries; assert unknown SIDs never map.
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9_

  - [x]* 3.3 Write property test for unrecognized SID rejection
    - **Property 8: Mapper ignores unrecognized SIDs**
    - **Validates: Requirements 1.3, 4.10**

  - [x]* 3.4 Write property test for disabled-value skipping
    - **Property 9: Disabled values are skipped**
    - **Validates: Requirements 4.10, 5.7, 6.4**

  - [x]* 3.5 Write property test for temperature unit conversion
    - **Property 10: Temperature unit conversion** (incl. cooling-rate delta form).
    - **Validates: Requirements 7.3, 7.4, 7.5, 12.1**

  - [x]* 3.6 Write property test for one-decimal rounding
    - **Property 11: One-decimal rounding for G and voltage**
    - **Validates: Requirements 7.1, 7.2**

  - [x]* 3.7 Write property test for negative-G sign
    - **Property 12: Negative-G sign preserved**
    - **Validates: Requirements 6.3**

  - [x]* 3.8 Write property test for Vne mutual exclusivity
    - **Property 13: Vne TAS/IAS mutual exclusivity**
    - **Validates: Requirements 5.1, 5.3, 5.4**

  - [x]* 3.9 Write property test for cylinder-count precedence
    - **Property 15: Cylinder-count precedence**
    - **Validates: Requirements 13.1, 13.2, 13.4**

- [x] 4. Implement Source_Key and Content_Hash helpers
  - [x] 4.1 Add `SourceIdentity`, `extract_source_key`, `content_hash`
    - In `efis_settings.py`, add SID constants (`SID_MODE_S="1063"`,
      `SID_LINK_ID="387"`, `SID_FLIGHT_ID="1062"`), the `SourceIdentity`
      dataclass, `extract_source_key(parsed)` (key `"<mode_s>:<link_id>"` with
      graceful fallback, `None` when neither identity SID readable), and
      `content_hash(parsed)` (SHA-256 over sorted `SID=value` pairs excluding
      `UPDATE=`/`CHECKSIZE=`/`CHECKSUM=`).
    - _Requirements: 14.1, 14.8_

  - [x]* 4.2 Write example tests for identity fallback and hash stability
    - Both SIDs present → `"A60670:1"`; only Mode_S; only Link_ID; neither
      (`None`). Content_Hash order-independent and unchanged when only `UPDATE=`
      differs.
    - _Requirements: 14.1, 14.8_

- [x] 5. Implement `Import_Workflow` (detect / preview / apply)
  - [x] 5.1 Implement source-keyed `detect_new_backup`
    - Add `NewBackupInfo` dataclass and
      `Import_Workflow.detect_new_backup(config, archive_root)`: select the
      newest archived backup, compute its Source_Key, and decide `available`
      per the per-Source_Key `settings_import` marker map — first-for-source,
      strictly-newer-for-source, stale `n-1` suppression, different-source
      flagging, and the undetermined-source Content_Hash+UPDATE fallback.
      Tolerate a legacy single-object marker via the undetermined path.
    - Apply two gates BEFORE the Requirement 14 logic (R15.8): (0a) PRIMARY
      GATE — `available=False`, reason "not the primary display" when SID 387
      (Link_ID) != `1`; this is device-agnostic, checking only Link_ID and
      never the display model/class. (0b) BOUND-SOURCE GATE — when a
      `bound_import_source` is set and the candidate `Source_Key` != the bound
      value, `available=False` with `is_bound_source_mismatch=True`; when no
      candidate anywhere reports Primary, offer nothing and never select a
      non-primary source.
    - Add the new `NewBackupInfo` fields: `is_primary` (SID 387 == 1),
      `link_id` (SID 387 value), `bound_import_source` (currently bound
      Source_Key, `None` if unbound), and `is_bound_source_mismatch`.
    - _Requirements: 9.1, 14.1, 14.3, 14.4, 14.5, 14.6, 14.7, 14.8, 15.1, 15.2, 15.4, 15.6, 15.7, 15.8_

  - [x] 5.2 Implement `build_preview` with tier prompt + caution<redline guard
    - Add `ThresholdChange`/`ImportPreview` dataclasses and
      `build_preview(parsed, config, tier_selections)`: run `Settings_Mapper`,
      join proposed values against current config, apply CHT/EGT/Oil tier
      selections, run the strict caution<redline guard (`can_apply=False` +
      warning on conflict), include `num_cylinders`, and set
      `is_different_source`/`source_note` when the Source_Key differs from the
      most-recent prior import.
    - Compose an additional rebind note atop `is_different_source` when the
      candidate `Source_Key` differs from a set `bound_import_source` (informs
      the user an explicit rebind is required; does not relax the double-confirm).
    - _Requirements: 8.1, 8.2, 8.3, 9.2, 9.3, 13.3, 14.6, 14.7, 15.5_

  - [x] 5.3 Implement `apply` with double-confirm, atomicity, per-source marker
    - Add `ApplyResult` and `apply(preview, confirm1, confirm2, config)`:
      no-op (byte-identical config + reason) unless
      `confirm1 AND confirm2 AND preview.can_apply`; on success assemble the
      full next config in memory and persist with a single `save_config`
      (`analysis_thresholds` additions + `num_cylinders`), and refresh ONLY the
      candidate Source_Key's Import_Marker (`last_update_value`, `content_hash`,
      `backup_name`, `imported_at`), preserving other sources' markers.
    - On the FIRST successful import (none bound yet), also bind
      `bound_import_source` to this primary's `Source_Key` (never overwriting an
      existing bound value). For a bound-source mismatch, `apply` is a no-op
      until the user explicitly rebinds; an explicit rebind still requires
      confirm1 AND confirm2.
    - _Requirements: 9.4, 9.5, 9.6, 10.3, 14.2, 14.9, 15.3, 15.4, 15.5_

  - [x]* 5.4 Write property test for active-airspeed alert gating (mapper side)
    - **Property 14: Active airspeed limit alerts iff its airspeed reaches it**
      — the mapper-produced active/inactive Vne keys feed this; the alerting
      half is verified in task 7. Assert exactly one Vne key is active.
    - **Validates: Requirements 5.5, 5.6**

  - [x]* 5.5 Write property test for preview completeness
    - **Property 16: Preview completeness** (one `ThresholdChange` per mapped
      key incl. `num_cylinders`, each with current + proposed).
    - **Validates: Requirements 9.2, 13.3**

  - [x]* 5.6 Write property test for caution-below-redline guard
    - **Property 17: Caution-below-redline guard**
    - **Validates: Requirements 8.2**

  - [x]* 5.7 Write property test for double-confirm gating
    - **Property 18: Apply is gated on double-confirmation and no-op otherwise**
    - **Validates: Requirements 9.4, 9.6, 10.3**

  - [x]* 5.8 Write property test for apply frame property
    - **Property 19: Apply updates only mapped keys (frame property)**
    - **Validates: Requirements 9.5**

  - [x]* 5.9 Write property test for source-keyed offer decision
    - **Property 23: Source-keyed detection — offered iff first-for-source or
      strictly newer**
    - **Validates: Requirements 14.1, 14.3, 14.4**

  - [x]* 5.10 Write property test for regression-proof suppression (stale n-1)
    - **Property 24: Regression-proof — not offered when not newer for its source**
    - **Validates: Requirements 14.5**

  - [x]* 5.11 Write property test for different-source flagging + no silent overwrite
    - **Property 25: Different source is flagged and never silently overwrites**
    - **Validates: Requirements 14.6, 14.7, 14.9**

  - [x]* 5.12 Write property test for undetermined-source fallback
    - **Property 26: Undetermined source falls back to content-hash + update and
      never regresses**
    - **Validates: Requirements 14.8**

  - [x]* 5.13 Write property test for candidate-only marker refresh
    - **Property 27: Successful apply refreshes only the candidate source's marker**
    - **Validates: Requirements 14.2**

  - [x]* 5.14 Write property test for the primary gate
    - **Property 28: Primary gate — import offered only for the Primary_Display**
    - **Validates: Requirements 15.1, 15.2**

  - [x]* 5.15 Write property test for device-agnostic primary gate
    - **Property 29: Primary gate is device-agnostic**
    - **Validates: Requirements 15.1**

  - [x]* 5.16 Write property test for bound-source binding and exclusivity
    - **Property 30: Bound-source binding and exclusivity**
    - **Validates: Requirements 15.3, 15.4, 15.5**

  - [x]* 5.17 Write property test for no-primary-present suppression
    - **Property 31: No primary present ⇒ no offer and no non-primary source**
    - **Validates: Requirements 15.6, 15.7**

  - [x]* 5.18 Write property test for primary gate composing with Requirement 14
    - **Property 32: Primary gate composes with Requirement 14 detection**
    - **Validates: Requirements 15.8**

- [x] 6. Checkpoint - core pipeline
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Extend `archiver.py` to copy `.dat` settings files
  - [x] 7.1 Add `.dat` siblings to the archive loop
    - In `archive_efis_drive`, extend the copy loop to include `Settings.dat`,
      `State.dat`, `WP.dat`, `Plan.dat` alongside the existing `.bak` files,
      reusing `_copy_with_datestamp` (copy-only, size-based skip).
    - _Requirements: 3.1, 3.2, 3.3_

  - [x]* 7.2 Write archiver integration test
    - Temp mount dir with `.dat`/`.bak` files → run `archive_efis_drive` →
      assert dated `.dat` copies appear, source hash unchanged (read-only), and
      a pre-existing same-size dated copy is skipped.
    - _Requirements: 3.1, 3.2, 3.3_

- [x] 8. Extend `analysis.py` with new thresholds and alert paths
  - [x] 8.1 Add new `DEFAULT_THRESHOLDS` entries
    - Add `rpm_redline`, `cht_cooling_rate_max` (°/min), and `vne_ias_redline`
      (default `9999` disabled, mutually exclusive with `vne_tas_redline`).
    - _Requirements: 5.2_

  - [x] 8.2 Add RPM redline + IAS-Vne alert paths
    - In `detect_episodes()`, add a warning episode on `rpm1` at/above
      `rpm_redline` (gated on being set), and add an `indicated_airspeed` high
      episode when `vne_ias_redline < 9999` mirroring the existing
      `true_airspeed`/`vne_tas_redline` path.
    - In `_check_alerts()`, add flight-summary lines for RPM redline
      (`stats.max_rpm >= rpm_redline`) and IAS-Vne
      (`stats.max_indicated_airspeed >= vne_ias_redline`).
    - _Requirements: 5.5, 5.6, 11.1, 11.2, 11.3_

  - [x] 8.3 Add CHT shock-cooling rate detection
    - Build the hottest-cylinder CHT series (reusing `cyl_series`), smooth over
      a documented W-second window, compute cooling rate in °/min, and feed a
      high-direction episode against `cht_cooling_rate_max` via
      `_detect_episodes_series`; add a flight-summary line when the max windowed
      cooling rate exceeds the threshold. Skip entirely when unset.
    - _Requirements: 12.2, 12.3, 12.4_

  - [x]* 8.4 Write property test for RPM redline alert
    - **Property 21: RPM redline alert** — synthetic `rpm1` rows in a temp
      SQLite `fdl_data` DB; run `detect_episodes`/`get_flight_stats`.
    - **Validates: Requirements 11.1, 11.2, 11.3**

  - [x]* 8.5 Write property test for CHT shock-cooling alert
    - **Property 22: CHT shock-cooling rate alert** — controlled-slope CHT
      series (incl. noisy-but-flat for noise immunity) in temp SQLite DB.
    - **Validates: Requirements 12.2, 12.3, 12.4**

  - [x]* 8.6 Write property test for active-airspeed alert (analysis side)
    - **Property 14: Active airspeed limit alerts iff its airspeed reaches it**
      — TAS and IAS series in temp SQLite DB; assert the alert fires iff
      `max(active airspeed) >= active Vne`.
    - **Validates: Requirements 5.5, 5.6**

- [x] 9. Checkpoint - alerting integration
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Add Flask routes in `dashboard/app.py`
  - [x] 10.1 Implement the three settings-import routes
    - `GET /api/settings-import/status` → `detect_new_backup` JSON
      (`available`, `source_key`, `is_primary`, `bound_import_source`,
      `is_first_for_source`, `is_different_source`, `candidate_update_value`,
      `source_last_imported_update`, `reason`). The `reason` conveys the
      suppressed states, including the non-primary ("not the primary display")
      and bound-source-mismatch cases.
    - `GET /api/settings-import/preview?tier_cht=&tier_egt=&tier_oil=` →
      `build_preview` JSON (diff table, tier prompts, warnings, checksum note,
      `can_apply`).
    - `POST /api/settings-import/apply` with `{confirm1, confirm2, tier
      selections}` → `apply`, reusing `save_config`; returns updated thresholds
      or error reason. All thin wrappers over `Import_Workflow`.
    - _Requirements: 8.1, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 10.3, 15.2, 15.5_

  - [x]* 10.2 Write route integration tests
    - Flask test client + temp archive/config: status offers on first-for-source
      and suppresses stale `n-1`; preview returns the diff table + guard
      warnings; apply is a no-op without both confirms and writes on double
      confirm.
    - _Requirements: 9.1, 9.2, 9.4, 9.5, 9.6, 14.4, 14.5_

- [x] 11. Add the Settings-page import UI in `settings.html`
  - [x] 11.1 Build the import card, preview table, and confirm flow
    - Add an "Import limits from EFIS backup" card with an availability badge
      (from `/status`), a "Review import…" button that renders the preview
      diff table (Threshold / Current / Imported), Caution/Redline tier
      selectors for CHT/EGT/Oil (re-fetch preview on change so the guard
      re-runs), a different-source note, an explicit overwrite warning, and a
      first "Apply import" button plus a second confirmation dialog before the
      POST. On success re-fetch `/api/thresholds` + `/api/config`.
    - Extend `thresholdLabels` with labels/tips for `rpm_redline`,
      `cht_cooling_rate_max`, `vne_ias_redline`, and `num_cylinders`.
    - Show a "not the primary display" / non-primary hidden state when the
      candidate is not a Primary_Display, and a bound-source rebind affordance
      when the candidate is a primary from a different source than the bound
      import source.
    - _Requirements: 8.1, 8.2, 9.2, 9.3, 9.4, 13.3, 14.7, 15.5_

- [x] 12. Wire-up, config migration, and dependency bump
  - [x] 12.1 Add legacy `settings_import` marker migration
    - In `config.py` (load path or a migration helper), convert a legacy
      single-object `settings_import` into the per-Source_Key map shape (routed
      through the undetermined-source path until the first source-keyed import
      writes a proper key). Ensure `num_cylinders` remains a top-level key.
    - An absent `bound_import_source` means "not yet bound" — the first eligible
      Primary import binds it — so no explicit migration step is needed, but the
      load path MUST tolerate its absence.
    - _Requirements: 14.2, 14.8, 15.3_

  - [x] 12.2 Add Hypothesis to requirements.txt
    - Add a pinned `hypothesis` entry to `requirements.txt`.
    - _Requirements: 1.1_

  - [x]* 12.3 Write config migration test
    - Legacy single-object marker migrates without crashing and detection routes
      it through the undetermined-source path (no regression).
    - _Requirements: 14.2, 14.8_

- [x] 13. Final checkpoint - full suite
  - Ensure all tests pass, ask the user if questions arise.

## Enhancement batch: cross-date ordering (Req 17) + per-threshold selection (Req 16)

The base EFIS Settings Import feature above is fully implemented and its tasks
are complete. The tasks below are ADDITIVE for two folded-in enhancements. They
are ordered bottom-up and Requirement 17 (the validated cross-date ordering bug
fix) comes first, because correct newest-backup selection is the foundation the
per-threshold preview/apply of Requirement 16 builds on. All new logic stays in
`efis_settings.py` and the dashboard (`dashboard/app.py`, `settings.html`); the
menu-bar tool is untouched.

- [x] 14. Add the Archive_Date cross-date ordering layer (Requirement 17)
  - [x] 14.1 Add `parse_archive_date`, `ArchivedBackup`, and `select_current_backup`
    - In `src/efis_data_manager/efis_settings.py`, add
      `parse_archive_date(path) -> Optional[date]` that extracts the
      `Settings-YYYY-MM-DD` datestamp the Archiver stamps into the archived
      filename; return `None` when no parseable datestamp exists (that candidate
      sorts last / is ignored for cross-date ordering).
    - Add the frozen `ArchivedBackup` dataclass (`path`, `archive_date: date`,
      `parsed: ParsedBackup`).
    - Add `select_current_backup(archived: list[ArchivedBackup]) -> SelectionResult`:
      group by `archive_date`, take the group with the LATEST `archive_date` as
      the current capture (Req 17.1) — never an older date regardless of any
      `UPDATE=` an older-dated file carries (Req 17.2) — then delegate the
      same-date `.bak`/`.dat` pair to the existing `Settings_Selector.select()`
      (UPDATE= only within the date, Req 17.3). A single-backup Source_Key
      selects that backup unchanged (Req 17.6). Do NOT use file mtimes (Req 17.4).
    - _Requirements: 17.1, 17.2, 17.3, 17.4, 17.6_

  - [x]* 14.2 Write property test for latest-Archive_Date selection
    - **Property 38: Cross-date selection uses the latest Archive_Date** —
      generate archived backups for one Source_Key across multiple
      Archive_Dates with arbitrary `UPDATE_Value`s (including all-equal, and an
      older date carrying a higher/equal `UPDATE=`); assert `select_current_backup`
      returns a backup from the latest date and never an earlier one.
    - **Validates: Requirements 17.1, 17.2**

- [x] 15. Route file selection through the Archive_Date layer + content-hash offer
  - [x] 15.1 Revise `load_current_settings_sids` and `detect_new_backup` selection
    - In `efis_settings.py`, revise both `load_current_settings_sids()` and
      `Import_Workflow.detect_new_backup()` so file selection goes through
      `select_current_backup`: glob candidate Settings backups, parse each,
      wrap every `ParsedBackup` in an `ArchivedBackup` with its
      `parse_archive_date`, group by Archive_Date, take the latest date, and
      pick the same-date `.bak`/`.dat` slot via `Settings_Selector.select()`.
      Keep the public return shapes unchanged (`load_current_settings_sids` still
      returns `(sids, temp_units_celsius)`; `detect_new_backup` still returns a
      `NewBackupInfo`).
    - _Requirements: 17.1, 17.2, 17.3, 17.4, 17.6_

  - [x] 15.2 Refine the `detect_new_backup` offer gate to Content_Hash
    - Refine the cross-date offer decision in `detect_new_backup` to compare the
      candidate `content_hash` against the per-Source_Key marker `content_hash`
      (offer iff `candidate.content_hash != marker.content_hash`) instead of
      `UPDATE=` magnitude; identical Content_Hash means "nothing new to import"
      (no offer) regardless of Archive_Date or `UPDATE=`. Keep `UPDATE=` only for
      same-date pairing, and keep `last_update_value` in the marker for display.
      This refines Req 14.4/14.5 so regression-proof detection rests on
      `(Source_Key, Archive_Date, Content_Hash)`.
    - _Requirements: 17.5_

  - [x]* 15.3 Write property test for content-hash offer decision
    - **Property 39: Content-hash decides whether an import is offered** — for a
      latest-dated candidate and any prior marker, assert `detect_new_backup`
      offers iff `content_hash` differs from the marker, and treats identical
      Content_Hash as no offer, independent of Archive_Date and `UPDATE_Value`.
    - **Validates: Requirements 17.5**

  - [x]* 15.4 Revise existing Properties 23/24/26 tests to content-hash detection
    - REVISE (do not duplicate) the existing property tests from tasks 5.9
      (Property 23), 5.10 (Property 24), and 5.13 (Property 26), which were
      written around `UPDATE=` magnitude, so they describe the refined
      Content_Hash-based offer/regression detection (design refined Req 14.4/14.5
      to `content_hash`). Update generators/assertions to drive the decision by
      Content_Hash + Archive_Date rather than `UPDATE=` magnitude.
    - _Requirements: 17.5, 14.4, 14.5_

- [x] 16. Checkpoint - cross-date ordering
  - Ensure all tests pass, ask the user if questions arise.

- [x] 17. Add per-threshold selection to `build_preview` and `apply` (Requirement 16)
  - [x] 17.1 Extend `ThresholdChange` and `build_preview` with selection
    - In `efis_settings.py`, add `selected: bool = True` and
      `selection_key: str` to `ThresholdChange`. In `build_preview`, set
      `selection_key = key` for ordinary rows (including `num_cylinders`) and
      `"vne"` for BOTH `vne_*` rows so they form one atomic Vne unit
      (Req 16.3, 16.4).
    - Add a `selected_keys: Optional[set[str]] = None` parameter to
      `build_preview` (None => every row selected — the fresh, all-checked
      initial state, Req 16.1/16.2/16.8; backward-compatible with pre-Req-16
      callers), and run the strict caution<redline guard over the SELECTED
      subset only (Req 16.6): compare each selected caution proposal against the
      paired redline's selected-or-current value and set `can_apply=False` +
      warning on conflict.
    - _Requirements: 16.1, 16.2, 16.3, 16.4, 16.6, 16.8_

  - [x] 17.2 Extend `apply` to write only the selected subset
    - Add `selected_keys: Optional[set[str]] = None` to `apply(...)`: write only
      changes whose `selection_key` is in the set, leaving unselected thresholds
      at their current dashboard value; the `"vne"` token expands to BOTH
      `vne_*` keys (impossible to write exactly one, Req 16.3/16.4). None => all
      changes written (backward compat). An empty set => no-op that changes no
      threshold and reports "nothing selected to import" (Req 16.7). The
      double-confirm of Requirement 9 still gates every apply (Req 16.5, 16.9).
    - _Requirements: 16.5, 16.7, 16.9_

  - [x]* 17.3 Write property test for fresh all-selected initialization
    - **Property 33: Preview initializes fresh with every row selected** —
      `build_preview(selected_keys=None)` marks every `ThresholdChange`
      `selected == True` (including `num_cylinders` and the single Vne row), and
      rebuilding again yields all-selected (nothing carried over).
    - **Validates: Requirements 16.1, 16.2, 16.8**

  - [x]* 17.4 Write property test for the apply frame property
    - **Property 34: Apply writes exactly the selected subset (frame property)**
      — for any subset `S`, after a double-confirmed `apply(selected_keys=S)`
      every threshold whose `selection_key` is in `S` equals its proposed value
      (`"vne"` expanding to both `vne_*`), and every unselected threshold is
      byte-identical to its current config value.
    - **Validates: Requirements 16.5**

  - [x]* 17.5 Write property test for Vne unit atomicity
    - **Property 35: Vne unit is atomic** — selecting `"vne"` applies BOTH
      `vne_*` keys preserving SID-889 exclusivity, and no `selected_keys` set
      causes `apply` to write exactly one of the two `vne_*` keys.
    - **Validates: Requirements 16.3, 16.4**

  - [x]* 17.6 Write property test for the selected-subset guard
    - **Property 36: Guard evaluates over the selected subset** —
      `build_preview(selected_keys=S)` sets `can_apply == True` iff no selected
      caution proposal is `>=` its paired redline (paired-tier selected proposal
      when also selected, else current); a selected conflict yields a warning and
      `can_apply == False`.
    - **Validates: Requirements 16.6**

  - [x]* 17.7 Write property test for deselect-all no-op
    - **Property 37: Deselect-all is a no-op** — a double-confirmed `apply` with
      an empty `selected_keys` set leaves the config byte-identical and reports a
      "nothing selected to import" reason.
    - **Validates: Requirements 16.7**

- [x] 18. Wire per-threshold selection through the dashboard routes
  - [x] 18.1 Thread `selected_keys` through preview/apply routes
    - In `dashboard/app.py`, add `selected_keys` to
      `GET /api/settings-import/preview` (parse comma-separated / repeated query
      params into a set) and to the `POST /api/settings-import/apply` request
      body; pass them through to `build_preview` / `apply`. Serialize each
      change's `selected` and `selection_key` in the preview JSON so the client
      can render checkboxes and the atomic Vne row.
    - _Requirements: 16.1, 16.2, 16.5, 16.6, 16.7_

  - [x]* 18.2 Write route integration test for selection pass-through
    - Flask test client + temp archive/config: preview echoes `selected`/
      `selection_key` and re-runs the guard when `selected_keys` is supplied;
      apply with a subset writes only that subset; apply with an empty
      `selected_keys` is a no-op ("nothing selected to import"); apply with
      `selected_keys` omitted still applies all (backward compat).
    - _Requirements: 16.5, 16.6, 16.7_

- [x] 19. Add the per-threshold selection UI in `settings.html`
  - [x] 19.1 Add checkbox column, select-all/none, and atomic Vne row
    - In `dashboard/templates/settings.html`, add a checkbox column to the
      preview diff table (default every row checked) and a select-all/none header
      control. Render the two `vne_*` rows as a SINGLE "Vne" checkbox row keyed
      `"vne"`. Re-fetch the preview on any checkbox toggle (so the guard re-runs
      server-side with the current `selected_keys`), include `selected_keys` in
      the apply POST body, and disable/block "Apply import" with a "nothing
      selected to import" message when all rows are unticked. Selection is
      one-time (fresh all-checked each time the card opens; never persisted).
    - _Requirements: 16.1, 16.2, 16.3, 16.4, 16.6, 16.7, 16.8, 16.9_

- [x] 20. Final checkpoint - enhancement batch full suite
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test tasks and can be skipped for a faster
  MVP; core implementation tasks are never optional.
- Each task references specific requirement sub-clauses and, for test tasks, the
  exact design property number it implements.
- All design properties (Properties 1–32, with Property 20 intentionally absent)
  map to exactly one property-based test:
  P1–P5 → 1.2–1.6; P6–P7 → 2.3–2.4; P8–P13,P15 → 3.3–3.9; P14 → 5.4 (mapper) and
  8.6 (analysis); P16–P19 → 5.5–5.8; P21 → 8.4; P22 → 8.5; P23–P27 → 5.9–5.13;
  P28–P32 → 5.14–5.18. (Property 20 is intentionally absent from the design.)
- Enhancement-batch property → test mapping (Req 16, 17): P38 → 14.2; P39 → 15.3;
  P33 → 17.3; P34 → 17.4; P35 → 17.5; P36 → 17.6; P37 → 17.7. Each is exactly one
  Hypothesis property test carrying a `# Feature: efis-settings-import, Property N`
  tag, consistent with the base batch.
- Task 15.4 REVISES the existing Properties 23/24/26 tests (from tasks 5.9/5.10/
  5.13) in place — it does NOT add duplicate tests. The design refined Req
  14.4/14.5 so regression-proof detection rests on `(Source_Key, Archive_Date,
  Content_Hash)` instead of `UPDATE_Value` magnitude; those tests are updated to
  describe that real behavior.
- The Req 16 + Req 17 enhancement batch touches only `efis_settings.py` (analysis
  layer) and the dashboard (`dashboard/app.py`, `settings.html`); it does NOT
  touch the menu-bar tool. Per the versioning policy, this batch bumps
  `DASHBOARD_VERSION` (not `MENUBAR_VERSION`).
- Property-based tests use Hypothesis (`max_examples>=100`) and carry a
  `# Feature: efis-settings-import, Property N: …` tag.
- Alerting properties (14, 21, 22) run against a temp SQLite `fdl_data` DB using
  the real `detect_episodes`/`get_flight_stats`, not mocks.
- Checksum verification stays best-effort (`UNVERIFIABLE` by default); GRT's
  algorithm is not invented.
- The import is read-only on source files and applied atomically via
  `save_config` behind a double-confirm.
- This change spans both the menu-bar tool (`archiver.py`) and the dashboard;
  on release, bump versions per the versioning policy.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "7.1", "8.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "1.4", "1.5", "1.6", "2.1", "4.1", "7.2", "8.2", "8.3", "12.2"] },
    { "id": 2, "tasks": ["2.2", "3.1", "4.2", "8.4", "8.5", "8.6"] },
    { "id": 3, "tasks": ["2.3", "2.4", "3.2", "3.3", "3.4", "3.5", "3.6", "3.7", "3.8", "3.9"] },
    { "id": 4, "tasks": ["5.1", "5.2", "5.3"] },
    { "id": 5, "tasks": ["5.4", "5.5", "5.6", "5.7", "5.8", "5.9", "5.10", "5.11", "5.12", "5.13", "5.14", "5.15", "5.16", "5.17", "5.18", "12.1"] },
    { "id": 6, "tasks": ["10.1", "12.3"] },
    { "id": 7, "tasks": ["10.2", "11.1"] },
    { "id": 8, "tasks": ["14.1"] },
    { "id": 9, "tasks": ["14.2", "15.1"] },
    { "id": 10, "tasks": ["15.2"] },
    { "id": 11, "tasks": ["15.3", "15.4", "17.1"] },
    { "id": 12, "tasks": ["17.2"] },
    { "id": 13, "tasks": ["17.3", "17.4", "17.5", "17.6", "17.7", "18.1"] },
    { "id": 14, "tasks": ["18.2", "19.1"] }
  ]
}
```
