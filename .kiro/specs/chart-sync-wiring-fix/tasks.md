# Implementation Plan: chart-sync-wiring-fix

## Overview

Finish wiring the SHA-256 manifest engine into the live sync, remove the
false-aborting stall detector, gate recognition on both provisioning files,
give Adopt a device-selection step and the A/B/C/D content matrix (with a scoped
`ChartData/`-only delete and a build-manifest-from-card path), update the
identity schema, and make the post-sync relabel nav-aware. Work proceeds
bottom-up: pure helpers first (labels, delete, from-card manifest), then the
`build_jobs` diff wiring, then the recognition gate and prepare/adopt
provisioning, then the app-side threading, and finally the version bump and a
full-suite checkpoint.

Implementation language is Python (the design uses concrete Python against the
existing `efis_data_manager` package). Property tests use Hypothesis (already a
project dependency) and run a minimum of 100 iterations. Run tests with
`./venv/bin/python -m pytest` using the `./temp/cmd_output.txt` output channel
per the workspace rules.

## Tasks

- [x] 1. Remove the stall detector (R1)
  - [x] 1.1 Delete stall machinery from `drive_updater.py`
    - Remove `STALL_TIMEOUT_SECONDS`, `_proc_cpu_seconds`, `_parse_ps_time`, the
      `stall_hook` parameter from `sync_payload`, and the poll-loop stall
      branch; reduce the poll loop to the `is_aborted()` check plus temp-file
      `proc.wait()` draining (design Section A).
    - Keep `_is_progress2_noise`, the `datetime`/`timezone`/`time` imports, and
      the `MountWatchdog` abort wiring untouched.
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_
  - [x] 1.2 Sever the `stall_hook` wiring in `repro_harness.py`
    - Remove `make_stall_hook`, the `_sync_payload_with_hook` wrapper, and the
      `drive_updater.sync_payload` monkeypatch; call `update_drive` /
      `verify_drive` directly; make `_detect_stall` return
      `(False, "stall detector removed")`.
    - _Requirements: 1.6_
  - [x] 1.3 Update stall-detector tests
    - In `tests/test_run_sync_job.py` and `tests/test_sync_payload.py`, remove
      the tests that monkeypatch `_proc_cpu_seconds` / `STALL_TIMEOUT_SECONDS`
      and the stall branch; drop `stall_hook` assertions; keep abort-path and
      file-count assertions.
    - _Requirements: 1.1, 1.2, 1.3_
  - [x] 1.4 Assert `repro_harness` imports and self-tests clean
    - Add/keep a test that imports `repro_harness` and runs `--self-test` with
      no `stall_hook` reference.
    - _Requirements: 1.6_

- [x] 2. Add the nav-only label builder (R9)
  - [x] 2.1 Implement `labels.build_nav_volume_label`
    - Build `<PREFIX><MMDD>` reusing `_sanitize_label`, `_LABEL_MAX_LEN`,
      `_LABEL_ALLOWED_RE`; parse `MM/DD/YYYY` for MMDD (empty when unparseable);
      fall back to `EFISNAV`, reduced to `NAV` when length requires; no year;
      `[A-Z0-9]` only; <=11 chars (design Section F).
    - _Requirements: 9.4, 9.5, 9.6_
  - [x] 2.2 Write property test for the nav-only label
    - **Feature: chart-sync-wiring-fix, Property 6: nav-only label is well-formed**
    - **Validates: Requirements 9.4, 9.5, 9.6**

- [x] 3. Add the scoped ChartData delete helper (R7)
  - [x] 3.1 Implement `drive_updater._delete_chart_data_tree`
    - `rmtree` only `<mount>/ChartData` (scanned payload, `Plates/` payload, and
      the `ScannedCharts.sqlite` / `Plates.sqlite` markers within it); guard on
      `isdir`; leave nav DBs, identity, manifest, `GRTCHARTS/`, and all
      root-level settings/flight-data/backup files untouched (design Section D.4).
    - _Requirements: 7.1, 7.2, 7.3, 10.6_
  - [x] 3.2 Write property test for the delete scope
    - **Feature: chart-sync-wiring-fix, Property 9: adopt ChartData delete scope**
    - **Validates: Requirements 7.1, 7.2, 7.3, 10.6**

- [x] 4. Add the build-manifest-from-card helper (R6)
  - [x] 4.1 Implement `drive_updater._card_manifest_from_card`
    - Run `Manifest_Builder.build("scanned", <ChartData/>, cycle=None)` and prune
      the `Plates/` subtree (same prune as `currency._build_family_manifest`);
      `Manifest_Builder.build("plates", <ChartData/Plates/>, cycle=None)`;
      assemble a `CardManifest(charts_cycle=None, plates_cycle=None, families={…})`;
      surface hashing progress via `progress_callback` (design Section D.3).
    - _Requirements: 6.3, 6.5, 6.11_
  - [x] 4.2 Write property test for from-card manifest build correctness
    - **Feature: chart-sync-wiring-fix, Property 8: adopt manifest-from-card build correctness**
    - **Validates: Requirements 6.3, 6.11**

- [x] 5. Change the identity schema (R8)
  - [x] 5.1 Drop `label`, add `chart_capable` + `nav_label_prefix`
    - Remove the `label` field/param from `_new_identity`; thread
      `chart_capable` and `nav_label_prefix` through `_ensure_identity` /
      `_new_identity`; keep the uuid4 `id`; store no cycle numbers; default
      `chart_capable` to `True` on read (design Section E, Data Models).
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.7_
  - [x] 5.2 Write property test for identity schema invariants
    - **Feature: chart-sync-wiring-fix, Property 7: identity schema invariants**
    - **Validates: Requirements 8.1, 8.3, 8.4**
  - [x] 5.3 Update tests asserting `identity["label"]`
    - Change them to assert `label` is absent and `chart_capable` is present.
    - _Requirements: 8.1, 8.5_

- [x] 6. Wire the manifest diff into `build_jobs` (R2)
  - [x] 6.1 Implement `_chart_family_job` + `build_jobs` diff wiring
    - Load the card once; per chart family: `card is None` -> legacy job;
      `load_local` None -> legacy job; `decide_streams` `skip-cycle` -> omit the
      job; otherwise set `files_from` / `removed` from
      `diff_family(local, card_entries)`; keep nav as a `kind=="files"` job with
      `files_from=None` (design Section B).
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_
  - [x] 6.2 Write property test for diff wiring
    - **Feature: chart-sync-wiring-fix, Property 1: build_jobs populates the manifest diff**
    - **Validates: Requirements 2.1, 2.6**
  - [x] 6.3 Write property test for skip-cycle omission
    - **Feature: chart-sync-wiring-fix, Property 2: skip-cycle omits the transfer job**
    - **Validates: Requirements 2.2**
  - [x] 6.4 Write property test for absent-manifest legacy job
    - **Feature: chart-sync-wiring-fix, Property 3: absent card manifest yields a legacy job**
    - **Validates: Requirements 2.3**
  - [x] 6.5 Write property test for relpath/payload-root alignment
    - **Feature: chart-sync-wiring-fix, Property 4: diff relpaths align with family payload roots**
    - **Validates: Requirements 2.4**
  - [x] 6.6 Write property test for root flight-data preservation under prune
    - **Feature: chart-sync-wiring-fix, Property 5: chart sync never deletes root-level flight data**
    - **Validates: Requirements 10.5**
  - [x] 6.7 Update `tests/test_chart_family_integration.py`
    - Assert behavior through the real `build_jobs` (drop the `_patched_build_jobs`
      shim / `_scanned_files_from_job` helper where the real path now matches);
      keep the end-to-end rsync assertions.
    - _Requirements: 2.1, 2.4_

- [x] 7. Force a legacy full reconcile on verify+repair (R2)
  - [x] 7.1 Make `verify_drive`'s repair branch run a legacy full job
    - For a discrepant family, re-run via a legacy job (`files_from=None`) so
      `--size-only --delete` re-converges the whole family tree, then let
      `_write_card_manifest_for_family` rewrite that family's manifest (design
      Section B.4).
    - _Requirements: 2.3, 10.2_
  - [x] 7.2 Write unit test for repair legacy-full behavior
    - Confirm repair transfers the whole family, not a stale diff.
    - _Requirements: 2.3_

- [x] 8. Gate recognition on both files (R3)
  - [x] 8.1 Add the card-manifest AND check to `resolve_drive_id`
    - After the identity parse + VolumeUUID cross-check and before returning the
      id, return `None` when `Manifest_Store.load_card(mount_point)` is `None`
      (design Section C).
    - _Requirements: 3.1, 3.2, 3.3, 3.4_
  - [x] 8.2 Write unit tests for the both-files gate
    - Id returned only when identity AND a valid manifest are present; `None`
      when the manifest is absent/corrupt even with valid identity.
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [x] 9. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Prepare writes an empty initial Card_Manifest (R4)
  - [x] 10.1 Write `CardManifest()` before prepare's first populate
    - In `prepare_drive`, after `_ensure_identity`/provenance and before the
      populate `update_drive`, `save_card` an empty `CardManifest()`; keep it
      best-effort (WARN, do not fail prepare) (design Section D.1).
    - _Requirements: 4.1, 4.2, 4.3, 4.4_
  - [x] 10.2 Write unit test for prepare recognition-immediately
    - After prepare, both files present and `resolve_drive_id` returns the id;
      diff-against-empty yields `files_from=all`, `removed=[]`.
    - _Requirements: 4.1, 4.3, 4.4_

- [x] 11. Nav-aware post-sync relabel + chart_capable refresh (R8, R9)
  - [x] 11.1 Make `_relabel_from_card_manifest` nav-aware
    - Read `chart_capable` from the identity (fail-safe `True`): chart-capable
      keeps `C<charts>P<plates>`; non-chart-capable builds `<PREFIX><MMDD>` from
      the stored `nav_label_prefix` + `_current_data_cycle()` GRT date, never
      reading the aircraft config (design Section F).
    - _Requirements: 9.1, 9.2, 9.3, 9.7_
  - [x] 11.2 Refresh `chart_capable` in identity at end of a managed sync
    - Thread the resolved capability into `update_drive` and merge via
      `_safe_update_provenance` before relabel; never raise into the sync path
      (design Section E).
    - _Requirements: 8.6_
  - [x] 11.3 Write unit tests for the relabel branches
    - Chart-capable identity -> `C<charts>P<plates>`; non-chart-capable identity
      -> `<PREFIX><MMDD>` without reading the aircraft config; patch
      `_rename_volume` to capture the label.
    - _Requirements: 9.2, 9.3, 9.7_

- [x] 12. Adopt content-handling in `adopt_drive` (R5, R6, R7)
  - [x] 12.1 Extend `adopt_drive` with the content-action signature + branches
    - Add keyword args `chart_capable`, `nav_label_prefix`, `content_action`;
      write identity with `chart_capable` + `nav_label_prefix`; implement
      `manifest_from_card` (A1: build-from-card `save_card` then diff sync),
      `delete_then_fresh` (A2: `_delete_chart_data_tree` then fresh),
      `fresh` (B: empty manifest then copy-all), `nav_only_delete` (C-confirmed:
      delete, empty manifest, nav-only populate+label), and `nav_only` (D: empty
      manifest, nav-only populate+label) (design Section D.3).
    - _Requirements: 5.5, 6.1, 6.3, 6.4, 6.6, 6.9, 6.10, 6.11, 7.4_
  - [x] 12.2 Write unit tests for the four content cases
    - One example per quadrant (A with the A1/A2 choice, B, C-confirm, D);
      after each non-aborted case both files present and `resolve_drive_id`
      returns the id; A1 builds the from-card manifest then diff-syncs.
    - _Requirements: 6.1, 6.3, 6.4, 6.6, 6.9, 6.10, 7.4_
  - [x] 12.3 Write unit test for Case C decline-aborts
    - Declining the Case C destructive confirm stops the adopt entirely: no
      identity write, no manifest write, `ChartData/` left intact.
    - _Requirements: 6.7, 6.8_

- [x] 13. Adopt device selection on the main thread (R5)
  - [x] 13.1 Add `_resolve_adopt_device_link_id` and thread the plan into the worker
    - On the main-thread adopt handler, resolve the Adopt_Device via
      `_find_drive_settings` + `read_identity` + `device_resolution.resolve_drive`
      (single RESOLVED device -> use it, no prompt); otherwise delegate to
      `_choose_prepare_device_link_id`; `None` cancels/aborts the adopt before
      the worker starts; derive `chart_capable` + `nav_label_prefix` +
      `content_action` and pass them into the `_do_adopt_drive` worker (design
      Section D.2).
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_
  - [x] 13.2 Write unit tests for resolve-then-prompt and cancel-aborts
    - Single RESOLVED device -> no prompt; unresolved/ambiguous -> prompt;
      prompt `None` -> adopt aborted, drive unmodified.
    - _Requirements: 5.1, 5.2, 5.3, 5.4_
  - [x] 13.3 Expand `tests/test_adopt_drive.py` end-to-end
    - Exercise the new `adopt_drive` signature across the four cases plus device
      selection and cancel/decline aborts (design Testing Strategy).
    - _Requirements: 5.4, 6.1, 6.8, 7.4_

- [x] 14. Verify preserved invariants (R10)
  - [x] 14.1 Write/keep tests for the non-regressions
    - Nav job unchanged (`files_from`/`removed` None, `files` NAV DBs); abort
      path terminates rsync and returns aborted with the stall machinery gone;
      label-disagreement WARN with the manifest winning; adopt delete scoped to
      `ChartData/` distinct from the sync `--delete`.
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6_

- [x] 15. Bump `MENUBAR_VERSION` (versioning policy)
  - [x] 15.1 Bump `MENUBAR_VERSION` in `src/efis_data_manager/__init__.py`
    - This work changes live menu-bar sync behavior; bump `MENUBAR_VERSION` and
      leave `DASHBOARD_VERSION` unchanged. The git tag, `__version__`, and
      `pyproject.toml` version are handled by the release step, not here (design
      Versioning note).
    - _Requirements: (versioning policy; see Introduction "Versioning note")_

- [x] 16. Final checkpoint — Ensure the full suite passes
  - Run `./venv/bin/python -m pytest` over the whole suite via the
    `./temp/cmd_output.txt` output channel; ensure all tests pass, ask the user
    if questions arise.

## Notes

- Tasks marked with `*` are optional (tests) and can be skipped for a faster
  MVP; the core implementation tasks are never optional.
- Each task cites the specific requirement sub-clauses it implements.
- Property tests are tagged `Feature: chart-sync-wiring-fix, Property N: <text>`
  and run a minimum of 100 iterations (Hypothesis).
- Checkpoints (tasks 9 and 16) provide incremental validation.
- Pure helpers (labels, delete, from-card manifest, identity schema) land before
  the wiring that consumes them; the app-side device-selection threading (task
  13) lands after `adopt_drive`'s content branches (task 12) exist to receive it.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "3.1", "4.1", "5.1"] },
    { "id": 1, "tasks": ["1.2", "2.2", "3.2", "4.2", "5.2", "5.3", "6.1"] },
    { "id": 2, "tasks": ["1.3", "1.4", "6.2", "6.3", "6.4", "6.5", "6.6", "7.1", "8.1"] },
    { "id": 3, "tasks": ["6.7", "7.2", "8.2", "10.1", "11.1", "11.2", "12.1"] },
    { "id": 4, "tasks": ["10.2", "11.3", "12.2", "12.3", "13.1"] },
    { "id": 5, "tasks": ["13.2", "13.3", "14.1"] },
    { "id": 6, "tasks": ["15.1"] }
  ]
}
```
