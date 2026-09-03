# Implementation Plan

## Overview

Rework the drive updater into per-family sync jobs (scanned charts, approach
plates, nav DB) with commit-marker atomicity, count+size verification,
idempotent/resumable behavior, and interruption handling (eject, sleep, dock
removal). The routine currency check stays cheap because a family's marker only
appears once its payload is fully verified. Tasks are ordered to build the
data/state primitives first, then the job engine, then app-level orchestration.

## Tasks

- [x] 1. Add sync-state helper and data models
  - Add `JobResult` and `SyncJob` dataclasses to `drive_updater.py`.
  - Add sync-state functions (`read_sync_state`, `begin_family`,
    `complete_family`, `pending_families`) backed by
    `~/EFIS/DataManagerLogs/.sync_state.json`; migrate a legacy
    `.sync_in_progress` file into it on first read.
  - Unit tests: begin/complete/pending transitions; empty-file cleanup; legacy
    migration.
  - _Requirements: 7.1, 7.7_

- [x] 2. Build per-family job definitions
  - Add a `build_jobs(mount_point, families)` that returns SyncJob objects for
    scanned (ChartData minus Plates minus marker), plates (Plates minus marker),
    and nav (NAV.DB, NAV-proc.DB), with correct excludes and marker paths.
  - Unit test: job specs contain the right roots/excludes/markers per family.
  - _Requirements: 1.1, 1.5_

- [x] 3. Implement quick currency check (per-family, marker-based)
  - Rewrite `check_drive_currency` to test scanned + plates by marker
    mtime+size and structural non-emptiness, and nav by checksum.
  - Force a family not-current if `pending_families` names it.
  - Keep backward-compatible `is_current`/`stale_items`; add `families` detail.
  - Unit tests: scanned-current-while-plates-stale; nav checksum mismatch;
    interrupted-marker forces not-current.
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [x] 4. Implement efficient payload sync (tree job)
  - Implement the tree-job rsync: `-r --delete --size-only --modify-window=2`
    plus existing excludes and the marker exclude; remove the `-c` flag and the
    fixed 1-hour timeout.
  - Log rsync failures at ERROR; capture stderr into errors.
  - Unit test: rsync converges a temp source->dest; second run is a no-op
    (idempotency).
  - _Requirements: 4.1, 4.2, 8.1_

- [x] 5. Implement payload verification (count + size)
  - Add `verify_family(job, mount_point, deep=False)` building {relpath:size}
    maps for local and drive (excluding marker) and returning discrepancies.
  - Unit tests: detect missing, extra, and size-mismatch; clean tree returns no
    discrepancies.
  - _Requirements: 5.1, 5.2, 5.4, 6.1_

- [x] 6. Implement commit-marker write + job driver
  - Implement `run_sync_job`: preflight -> begin_family -> payload sync ->
    verify -> write marker (copy + fsync) only if verified and no errors ->
    complete_family. Nav variant uses checksum copy+verify.
  - Ensure marker is NOT written on any error/abort.
  - Unit tests (Property 1, 2, 4): abort-before-marker leaves marker absent and
    family stale; plates failure leaves scanned marker valid.
  - _Requirements: 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4, 5.3_

- [x] 7. Implement mount-presence watchdog and abort path
  - Add a watchdog that polls `os.path.ismount(mount_point)` while a job runs
    and an `is_aborted()` predicate that terminates the rsync subprocess.
  - On abort: JobResult "aborted", leave interrupted marker, log ERROR, set
    status.
  - Unit test: mock ismount -> False mid-job; assert abort, marker unwritten,
    interrupted state retained, ERROR logged.
  - _Requirements: 7.2, 7.3, 8.1_

- [x] 8. Implement exhaustive verify + repair
  - Add `verify_drive(mount_point, families=None, deep=False)` aggregating
    `verify_family`; add repair = re-run the job(s) then re-verify.
  - On success clear the family from sync-state.
  - Unit tests: introduce discrepancies, repair, assert clean and state
    cleared.
  - _Requirements: 6.1, 6.2, 6.4, 6.5_

- [x] 9. Rework update_drive aggregation
  - Rewrite `update_drive(mount_point, families=None, progress_callback=None)`
    to run only requested/stale families via `run_sync_job` and aggregate into
    `{"jobs": {...}, "errors": [...], "aborted": bool}`.
  - Update `prepare_drive` to use it and report per-family results.
  - Unit test: mixed result (one updated, one failed) aggregates correctly.
  - _Requirements: 1.1, 8.2, 9.3, 9.4_

- [x] 10. Wire app orchestration and status/error consistency
  - Rewrite `_run_drive_update`: quick check -> sync stale families ->
    per-family status -> aggregate terminal status; ensure no sticky error
    strings and every error is logged >= WARNING.
  - Update `_on_efis_drive_mounted` to run verify+repair when
    `pending_families` names the mounted drive before declaring current.
  - _Requirements: 3.5, 6.3, 8.2, 8.3, 8.4_

- [x] 11. Add sleep/wake handling
  - Register NSWorkspace `WillSleep`/`DidWake` observers; on will-sleep mark
    at-risk and stop the current job safely (leave interrupted marker); on wake
    trigger verify+repair if a drive is mounted with pending families.
  - _Requirements: 7.4, 7.5, 7.6_

- [x] 12. Add "Verify Drive" menu action
  - Add a menu item that runs `verify_drive(deep=False)` on the connected drive
    and reports per-family results via notification/status.
  - _Requirements: 6.4_

- [x] 13. Test scaffolding and regression pass
  - Add a local `tests/` tree and a `.gitattributes` `export-ignore` for
    `tests/` so tests stay out of release ZIPs.
  - Ensure all property/unit tests from the design run green under the venv.
  - _Requirements: 2.1, 2.2, 3.5, 5.1, 6.1, 7.6, 8.1_

- [x] 14. Add drive-identity file + resolver (multi-drive)
  - Add `EFIS_DRIVE_ID.json` read/write helpers (atomic temp+rename) and
    `resolve_drive_id(mount_point)` that reads the identity file, adopts a
    recognized-but-unmarked drive by generating a UUID + writing the file, and
    returns None (fail-safe) when the drive is not ours/unreadable.
  - Include `schema_version`, `id`, `kind`, `volume_uuid`, `label`, `created`,
    `prepared_by`; capture VolumeUUID via `diskutil info -plist` and WARN on
    mismatch. Call `wait_for_mount_ready` first.
  - Unit tests: read existing id; adopt when missing; None when kind wrong /
    unreadable; VolumeUUID-mismatch warning; atomic write.
  - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.7, 10.9_

- [x] 15. Re-key sync-state by drive id (N-drive)
  - Convert `.sync_state.json` to the v2 id-keyed map; change `begin_family`,
    `complete_family`, `pending_families` to take a `drive_id`. Discard legacy
    v1/`.sync_in_progress` formats on first read.
  - Unit tests: two drive ids tracked independently; swap does not mis-attribute
    (Property 6); begin/complete/pending per id; legacy discard.
  - _Requirements: 10.5, 10.6, 10.7_

- [x] 16. Wire identity into currency/update/app + provenance
  - Resolve the drive id once per operation in `check_drive_currency`,
    `run_sync_job`/`update_drive`, and the app mount/verify paths; pass id to
    the sync-state helpers. On unresolved id, fail safe (normal marker-based
    check/update, no cross-drive state, WARN).
  - Update identity provenance atomically around the commit-marker step
    (`last_sync_*`, `sync_count`, `data_cycle`); derive `data_cycle` from the
    source markers.
  - _Requirements: 10.7, 10.8, 10.10_

- [x] 17. Prepare-drive writes identity + docs
  - `prepare_drive` writes a fresh identity file as part of format/populate.
  - Document: recommend unique drive labels (defense in depth); note that two
    EFIS drives mounted at once is an unsupported/known limitation.
  - Unit test: prepare writes a valid identity file; smoke that a prepared drive
    resolves to a stable id.
  - _Requirements: 10.3, 10.11, 10.12_

- [x] 18. Identity-only detection (drop label matching)
  - Rename `is_efis_drive` -> `is_managed_drive` and make it check ONLY for a
    valid identity file (`EFIS_DRIVE_ID.json`, `kind == "efis-chart-drive"`).
    Remove the `EFIS`/`EFIS_N` volume-label regex entirely. Update all callers
    (usb_monitor, app.py mount/eject/verify handlers, resolve_drive_id) so the
    volume label is used nowhere for detection.
  - Remove lazy adoption from `resolve_drive_id`: it returns the id if the
    identity file is present, else None (no writing on mount).
  - Keep a helper to detect an *adoption candidate* (has `GRTCHARTS/` or
    `ChartData/` but no identity) for Prepare Drive's use only.
  - Unit tests: managed only when identity present; label never matches;
    GRTCHARTS-without-identity is NOT auto-detected but IS an adoption candidate.
  - _Requirements: 10.1, 10.2, 10.3_

- [x] 19. Identity-gated mount auto-action
  - Update the USB monitor / app mount handler so archive + sync run ONLY for
    managed drives (identity present). An unmanaged drive mount triggers no
    automatic action (log an informational note; optionally surface a
    "found an unmanaged drive — use Prepare Drive to adopt" hint).
  - Ensure eject/verify handlers no longer rely on label detection.
  - Requirements: 10.4, 10.5, 10.8
  - _Requirements: 10.4, 10.5, 10.8_

- [x] 20. Prepare Drive: adopt-vs-clean + flight-data safety
  - Rework the Prepare Drive flow: inspect the selected volume; branch on
    (blank | GRTCHARTS-without-identity | already-identified).
  - Add "Adopt & update" (non-destructive: write identity + incremental
    `update_drive`) alongside "Start clean" (reformat + populate).
  - Before either path, if the drive holds unarchived flight data/logbooks,
    prompt Import/Archive vs Erase and act accordingly (archive uses the
    existing `archive_efis_drive`).
  - Add an `adopt_drive(mount_point, progress_callback=None)` helper in
    drive_updater (write identity + update_drive, no format) for the adopt path.
  - Unit tests (headless): adopt_drive writes identity + runs update without
    formatting; adoption-candidate detection; flight-data presence check. The
    interactive prompts live in app.py (not unit-tested); extract any pure
    branch/decision helper and test that.
  - Update README: detection by identity (not label), first-contact requires
    Prepare Drive, adoption path for pre-existing/Windows-tool drives, labels
    are cosmetic, migration note.
  - _Requirements: 10.9, 10.10, 10.11, 10.14, 10.16_

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1"] },
    { "wave": 2, "tasks": ["2"] },
    { "wave": 3, "tasks": ["3", "4", "5"] },
    { "wave": 4, "tasks": ["6"] },
    { "wave": 5, "tasks": ["7", "8"] },
    { "wave": 6, "tasks": ["9"] },
    { "wave": 7, "tasks": ["10"] },
    { "wave": 8, "tasks": ["11", "12"] },
    { "wave": 9, "tasks": ["13"] },
    { "wave": 10, "tasks": ["14"] },
    { "wave": 11, "tasks": ["15"] },
    { "wave": 12, "tasks": ["16"] },
    { "wave": 13, "tasks": ["17"] },
    { "wave": 14, "tasks": ["18"] },
    { "wave": 15, "tasks": ["19"] },
    { "wave": 16, "tasks": ["20"] }
  ],
  "dependencies": {
    "1": [],
    "2": ["1"],
    "3": ["1", "2"],
    "4": ["2"],
    "5": ["2"],
    "6": ["4", "5"],
    "7": ["6"],
    "8": ["5", "6"],
    "9": ["6", "7", "8"],
    "10": ["3", "9"],
    "11": ["7", "8", "10"],
    "12": ["8", "10"],
    "13": ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"],
    "14": ["1"],
    "15": ["1", "14"],
    "16": ["3", "9", "10", "15"],
    "17": ["9", "16"],
    "18": ["14", "15", "16"],
    "19": ["18"],
    "20": ["18", "19"]
  }
}
```

Critical path: 1 -> 2 -> {4,5} -> 6 -> {7,8} -> 9 -> 10 -> {11,12} -> 13. Drive-identity (multi-drive, Req 10): 14 -> 15 -> 16 -> 17, depending on the completed sync-state (1) and orchestration (3,9,10) tasks.

## Notes

- Tests live in a local `tests/` tree and are excluded from release ZIPs via
  `.gitattributes export-ignore` (task 13).
- Correctness Properties 1-5 in design.md map to tasks 6 (P1/P4), 3+7 (P2),
  4+8 (P3), and 10 (P5).
- No physical USB is required for tests; "local image" and "drive" are temp
  dirs and mount/sleep are mocked.
- This is a menu-bar-tool change; on release, bump MENUBAR_VERSION per the
  versioning policy.
