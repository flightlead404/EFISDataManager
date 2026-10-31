# Requirements Document

## Introduction

This spec finishes and fixes the `chart-manifest-sync` feature. That feature built a
SHA-256 manifest engine (`manifest.py`: `Manifest_Builder`, `Manifest_Store`,
`diff_family`, `ManifestDiff`, `decide_streams`) and added `SyncJob.files_from` /
`SyncJob.removed`, but the engine was never wired into the live sync. Two concrete
defects result, both verified by investigation:

- **Dead manifest engine.** `build_jobs()` never populates `SyncJob.files_from`, and
  `decide_streams` / `diff_family` have zero production callers. Every sync — including
  verify+repair — therefore still runs the legacy `rsync --size-only --delete`
  whole-tree job over roughly 102,000 files on a slow USB FAT card, and a same-byte-size
  content change can be silently skipped.
- **False-aborting stall detector.** `sync_payload` carries a stall detector
  (`STALL_TIMEOUT_SECONDS = 120`) that treats the sync as wedged when both of its
  liveness signals — rsync stdout growth (`--out-format=%n`, one line per COMPLETED
  file) and rsync CPU advance (`ps -o time=`) — stay flat. During the long silent
  receiver-side file-list / `--delete` scan over 102k files on slow USB, nothing has
  transferred yet (no `%n` lines) and the scan is I/O-bound (negligible CPU), so both
  signals go flat and a healthy sync is false-aborted at 120s. This was empirically
  reproduced on a known-good device. The "FSKit stall" premise that justified the
  detector (and the retired mount-swap / image-dd detours) was never verified.

Drive removal and system sleep are already handled by a separate, reliable mechanism:
`MountWatchdog` (`os.path.ismount` polling) feeds an `is_aborted` predicate into
`sync_payload`, and the sync runs on a daemon background thread, so a truly-wedged
rsync cannot freeze the UI and is recoverable by removing the drive.

This work is a feature spec but fundamentally a correctness fix, so acceptance criteria
that are amenable to property-based testing (diff/round-trip/invariant behavior) are
called out explicitly.

### Out of scope (known follow-up)

The "HI files appearing despite `ifr_high=false`" bug is a **separate investigation the
user will do next**. It is NOT designed or implemented here. (R2's absent-manifest
`--delete` self-heal will naturally clean orphan HI tiles as a side effect, but the root
cause is out of scope.)

### Versioning note

Several of these changes affect the menu bar tool's live sync behavior. Per the project
versioning policy, `MENUBAR_VERSION` MUST be bumped when this work is released. The git
tag, `__version__`, and `pyproject.toml` version are handled by the release step, not by
this feature.

## Glossary

- **Sync_Engine**: The `drive_updater` module functions that build and run sync jobs
  (`build_jobs`, `sync_payload`, `run_sync_job`, `update_drive`, `verify_drive`).
- **Manifest_Engine**: The `manifest` module (`Manifest_Builder`, `Manifest_Store`,
  `diff_family`, `ManifestDiff`) plus `decide_streams` in `drive_updater`.
- **Card_Manifest**: The `EFIS_MANIFEST.json` file at a drive's volume root, holding
  `charts_cycle`, `plates_cycle`, and per-family SHA-256 entry maps (the authoritative
  cycle record).
- **Identity_File**: The `EFIS_DRIVE_ID.json` file at a drive's volume root, holding the
  drive's durable uuid4 `id` and provenance.
- **Chart_Family**: One of the chart streams `scanned` (payload `ChartData/` minus
  `Plates/`) or `plates` (payload `ChartData/Plates/`).
- **Nav_Family**: The standalone `nav` stream (`NAV.DB`, `NAV-proc.DB`), verified per
  file by size + SHA-256, with no manifest and no cycle gate.
- **Files_From**: A `SyncJob.files_from` list of family-root-relative relpaths driving
  `rsync --files-from=-` (the added + changed set from a `ManifestDiff`).
- **Removed_Set**: A `SyncJob.removed` list of family-root-relative relpaths pruned
  explicitly after a clean `--files-from` transfer.
- **Legacy_Job**: A `SyncJob` with `files_from = None`, run via the legacy
  `rsync --size-only --delete --delete-excluded --modify-window=2` whole-tree path.
- **Mount_Watchdog**: The `MountWatchdog` mechanism that polls `os.path.ismount` and
  feeds `is_aborted` into `sync_payload` for drive-removal / sleep abort.
- **Chart_Capable_Snapshot**: The `chart_capable` boolean recorded in the Identity_File
  for labeling only; it does NOT gate which families sync.
- **Sync_Gate**: `app._device_chart_capable`, derived from the aircraft config and the
  drive's Link_ID (fail-safe to chart-capable), which decides which families are written.
  Config is authoritative.
- **Nav_Valid_Date**: The GRT nav DB validity-start date (`nav_db_valid_date` in the
  currency GRT metadata), the same source `_current_data_cycle` reads.
- **MMDD**: The two-digit month followed by two-digit day of the Nav_Valid_Date.
- **Adopt_Device**: The configured device (from multi-device-settings) that an
  adopted drive is bound to, resolved from the drive's own `Settings.bak` via
  `device_resolution.resolve_drive` (Link_ID SID 387 -> configured device), or
  chosen by the user when resolution is ambiguous. It yields the `chart_capable`
  boolean and the friendly name used for `nav_label_prefix`.
- **Existing_Chart_Data**: A non-empty `ChartData/` tree physically present on the
  card at adopt time (the scanned payload, the `ChartData/Plates/` plates payload,
  and the `ScannedCharts.sqlite` / `Plates.sqlite` markers within `ChartData/`).
- **Adopt_Content_Case**: One of four adopt content-handling cases determined by the
  Adopt_Device's `chart_capable` flag and whether Existing_Chart_Data is present:
  Case A (chart-capable + existing data), Case B (chart-capable + no data),
  Case C (non-chart-capable + existing data), Case D (non-chart-capable + no data).
- **Card_Manifest_From_Card**: A Card_Manifest built at adopt time by running
  `Manifest_Builder.build` over the card's own `ChartData/` tree (SHA-256 hashing the
  existing files), with the `scanned` family manifest pruning the `Plates/` subtree
  exactly as `currency._build_family_manifest` does, and both per-family cycles set
  to null because the card carries no trustworthy cycle stamp.
- **ChartData_Delete**: A targeted removal of only the `ChartData/` tree (scanned
  payload, `ChartData/Plates/` plates payload, and the `ScannedCharts.sqlite` /
  `Plates.sqlite` markers within it), leaving `NAV.DB`, `NAV-proc.DB`, the
  Identity_File, the Card_Manifest, `GRTCHARTS/`, and all root-level settings /
  flight-data / backup files untouched. It is NOT a format and NOT a root-level wipe.

## Requirements

### Requirement 1: Remove the stall detector

**User Story:** As a user syncing a large chart set to a slow USB card, I want the sync
to never false-abort on a healthy drive, so that a normal large transfer completes
instead of failing at 120 seconds during the silent file-list scan.

#### Acceptance Criteria

1. THE Sync_Engine SHALL NOT define `STALL_TIMEOUT_SECONDS`, `_proc_cpu_seconds`, or
   `_parse_ps_time`.
2. THE `sync_payload` function SHALL NOT accept a `stall_hook` parameter.
3. WHILE `sync_payload` polls a running rsync process, THE Sync_Engine SHALL perform
   ONLY (a) the `is_aborted()` check and (b) temp-file stdout/stderr draining with
   `proc.wait()`, and SHALL NOT evaluate any no-progress / CPU-advance stall condition.
4. IF a running rsync makes no measurable progress for any duration, THEN THE
   Sync_Engine SHALL allow it to continue until completion or until `is_aborted()`
   returns true, and SHALL NOT terminate it on a progress-timeout basis.
5. WHEN `is_aborted()` returns true during a sync, THE Sync_Engine SHALL terminate the
   rsync process and report the result as aborted (drive removed or system sleeping).
6. THE `repro_harness.py` module SHALL import successfully and pass its self-test with
   no reference to a `stall_hook` parameter.

### Requirement 2: Wire the manifest diff into the live sync

**User Story:** As a user with a provisioned chart drive, I want each sync to transfer
only the files the SHA-256 manifest shows changed, so that an update over roughly
102,000 files on slow USB copies the delta instead of rescanning the whole tree.

#### Acceptance Criteria

1. WHEN `build_jobs` builds a Chart_Family job AND a valid Card_Manifest is present at
   the drive volume root, THE Sync_Engine SHALL populate that job's Files_From with the
   added-plus-changed relpaths and its Removed_Set with the removed relpaths, computed by
   `decide_streams` and `diff_family` for that family.
2. WHEN `decide_streams` returns `skip-cycle` for a Chart_Family (local cycle equals the
   Card_Manifest recorded cycle), THE Sync_Engine SHALL NOT run a transfer job for that
   family.
3. WHEN `build_jobs` builds a Chart_Family job AND the Card_Manifest is absent or
   corrupt, THE Sync_Engine SHALL produce a Legacy_Job for that family with
   `files_from = None` so the legacy `--size-only --delete` whole-tree reconcile runs.
4. FOR ANY Chart_Family diff, every relpath in Files_From and Removed_Set SHALL be
   interpreted relative to that family's root (scanned = `ChartData/` minus `Plates/`;
   plates = `ChartData/Plates/`), matching the `--files-from` payload roots in
   `sync_payload`.
5. THE Nav_Family job SHALL remain a `kind == "files"` job verified by size and SHA-256
   per file, with no manifest and no cycle gate.
6. THE `decide_streams` and `diff_family` functions SHALL each have at least one
   production caller reached through `build_jobs`.

### Requirement 3: Recognition requires both files

**User Story:** As a user, I want a drive to be recognized as managed only when it is
fully provisioned, so that a partially set up or foreign drive is routed to the explicit
Prepare / Adopt flow instead of being auto-synced.

#### Acceptance Criteria

1. WHEN both the Identity_File and the Card_Manifest are present and valid at the volume
   root, THE Sync_Engine SHALL return the Identity_File `id` from `resolve_drive_id`.
2. IF the Identity_File is absent or corrupt, THEN THE Sync_Engine SHALL return `None`
   from `resolve_drive_id`.
3. IF the Card_Manifest is absent or corrupt, THEN THE Sync_Engine SHALL return `None`
   from `resolve_drive_id`.
4. WHEN `resolve_drive_id` returns `None`, THE Sync_Engine SHALL treat the drive as not
   managed so the application routes it to the explicit Prepare / Adopt flow.

### Requirement 4: Prepare writes an initial Card_Manifest

**User Story:** As a user preparing a drive, I want the manifest written up front, so
that a freshly provisioned blank drive satisfies the both-files recognition rule
immediately instead of being unrecognizable until its first sync completes.

#### Acceptance Criteria

1. WHEN `prepare_drive` provisions a blank drive, THE Sync_Engine SHALL write a valid
   initial empty Card_Manifest at the volume root before starting the first populate
   sync.
2. THE initial Card_Manifest written by `prepare_drive` SHALL record `charts_cycle` and
   `plates_cycle` as null and each Chart_Family entry map as empty.
3. WHEN `prepare_drive` has written both the Identity_File and the initial Card_Manifest,
   THE Sync_Engine SHALL return the drive's `id` from `resolve_drive_id`.
4. WHEN `prepare_drive` populates the blank drive, THE Sync_Engine SHALL diff each
   Chart_Family against the empty manifest so that Files_From is every local file and
   Removed_Set is empty.

### Requirement 5: Adopt device selection

**User Story:** As a user adopting a previously-used drive in a multi-device setup, I
want the adopt bound to the correct configured device, so that the adopted drive is
labeled and sized for the display it serves and never ends up bound to the wrong device.

#### Acceptance Criteria

1. WHEN `adopt_drive` begins, THE Sync_Engine SHALL determine the Adopt_Device by
   resolving the drive's own `Settings.bak` through `device_resolution.resolve_drive`
   (Link_ID SID 387 -> configured device), the same path `app._device_chart_capable`
   uses for auto-sync.
2. WHEN resolution yields exactly one configured device, THE Sync_Engine SHALL use that
   device as the Adopt_Device without prompting.
3. IF resolution yields no configured device or is ambiguous, THEN THE Sync_Engine SHALL
   prompt the user to choose the Adopt_Device by reusing the Prepare device-selection
   prompt `_choose_prepare_device_link_id`.
4. WHEN the user cancels the Adopt_Device prompt, THE Sync_Engine SHALL abort the adopt
   without modifying the drive, matching the Prepare cancel-aborts behavior.
5. WHEN the Adopt_Device is determined, THE Sync_Engine SHALL write that device's
   `chart_capable` boolean and `nav_label_prefix` (from the device friendly name) into
   the Identity_File.
6. WHEN device selection requires a prompt, THE Sync_Engine SHALL raise that prompt on
   the main thread before the background `_do_adopt_drive` worker starts, matching the
   Prepare device-selection placement.

### Requirement 6: Adopt content-handling matrix

**User Story:** As a user adopting a drive, I want the existing chart content handled
correctly for the chosen device's capability, so that a chart-capable drive keeps or
rebuilds its charts truthfully and a nav-only drive never carries charts.

#### Acceptance Criteria

1. WHEN the Adopt_Device is chart-capable AND Existing_Chart_Data is present (Case A),
   THE Sync_Engine SHALL prompt the user to choose between (A1) building the
   Card_Manifest_From_Card then running the normal diff sync, and (A2) deleting the
   existing `ChartData/` tree then populating fresh.
2. WHILE the Case A prompt is shown, THE Sync_Engine SHALL present both options as a
   two-way choice where each option proceeds with the adopt and neither option aborts.
3. WHEN the user selects Case A option A1, THE Sync_Engine SHALL build the
   Card_Manifest_From_Card over the card's `ChartData/`, save it with `save_card`, and
   then run the normal diff sync so the current cycle is transferred and orphans are
   pruned via the Files_From and Removed_Set path.
4. WHEN the user selects Case A option A2, THE Sync_Engine SHALL perform a
   ChartData_Delete and then proceed as Case B.
5. WHEN building the Card_Manifest_From_Card, THE Sync_Engine SHALL report hashing
   progress, because hashing roughly 100,000 small files on a slow USB card is a
   multi-minute operation.
6. WHEN the Adopt_Device is chart-capable AND Existing_Chart_Data is absent (Case B),
   THE Sync_Engine SHALL write an empty Card_Manifest, save it with `save_card`, and run
   a populate that diffs against empty so Files_From is every local file and Removed_Set
   is empty.
7. WHEN the Adopt_Device is non-chart-capable AND Existing_Chart_Data is present
   (Case C), THE Sync_Engine SHALL show a blocking destructive confirmation defaulting to
   No that the existing chart data will be deleted.
8. IF the user declines the Case C confirmation, THEN THE Sync_Engine SHALL stop the
   adopt entirely and leave the drive unmodified so no nav-identity-with-charts drive
   results.
9. WHEN the user confirms the Case C confirmation, THE Sync_Engine SHALL perform a
   ChartData_Delete, write an empty Card_Manifest, save it with `save_card`, and run a
   nav-only populate with a nav-only label.
10. WHEN the Adopt_Device is non-chart-capable AND Existing_Chart_Data is absent
    (Case D), THE Sync_Engine SHALL write an empty Card_Manifest, save it with
    `save_card`, and run a nav-only populate with a nav-only label without prompting.
11. WHEN building the Card_Manifest_From_Card for Case A option A1, THE Sync_Engine SHALL
    set `charts_cycle` and `plates_cycle` to null, and SHALL build the `scanned` family
    manifest with the `Plates/` subtree pruned and the `plates` family manifest rooted at
    `ChartData/Plates/`, matching `currency._build_family_manifest`.

### Requirement 7: Adopt delete scope and post-adopt recognition

**User Story:** As a user adopting a drive, I want the destructive delete scoped to
exactly the chart data, so that my nav database, identity, settings, and flight-data
backups are never touched, and the drive is recognized as managed right after adopt.

#### Acceptance Criteria

1. WHEN a ChartData_Delete runs (Case A2 or Case C confirmed), THE Sync_Engine SHALL
   remove only the `ChartData/` tree, including the scanned payload, the
   `ChartData/Plates/` plates payload, and the `ScannedCharts.sqlite` / `Plates.sqlite`
   markers within `ChartData/`.
2. WHEN a ChartData_Delete runs, THE Sync_Engine SHALL leave `NAV.DB`, `NAV-proc.DB`, the
   Identity_File, the Card_Manifest, `GRTCHARTS/`, and all root-level settings,
   flight-data, and backup files (for example `Settings.dat`, `Plan.dat`, `State.dat`,
   `WP.dat`, and `*.bak`) untouched.
3. THE ChartData_Delete SHALL be a targeted `ChartData/` removal and SHALL NOT format the
   volume or wipe the volume root.
4. WHEN `adopt_drive` completes successfully in any of Cases A, B, C-confirmed, or D,
   THEN with both the Identity_File and a valid Card_Manifest present at the volume root,
   `resolve_drive_id` SHALL return the drive's `id`.

### Requirement 8: Identity_File schema changes

**User Story:** As a maintainer, I want the Identity_File to carry only durable identity
plus a labeling hint, so that the Card_Manifest stays the single authoritative cycle
record and no stale volume label is duplicated into identity.

#### Acceptance Criteria

1. THE `_new_identity` function SHALL produce an identity dict that does not contain a
   `label` field.
2. THE Sync_Engine SHALL NOT read an `identity.label` field for any cross-check or
   consumer.
3. THE identity dict SHALL retain a uuid4 string as its `id`.
4. THE identity dict SHALL NOT store chart or plate cycle numbers.
5. WHEN `prepare_drive` or `adopt_drive` provisions a drive, THE Sync_Engine SHALL write
   a boolean `chart_capable` field into the Identity_File.
6. WHEN a managed drive completes a sync, THE Sync_Engine SHALL refresh the
   `chart_capable` field in the Identity_File.
7. THE `chart_capable` field SHALL be used only for labeling and SHALL NOT determine
   which families are synced.

### Requirement 9: Capability authority and post-sync relabeling

**User Story:** As a user, I want the drive's volume label to mirror its current data
after every sync, so that I can read the drive's contents in Finder while the cycle gate
stays driven by config and the manifest.

#### Acceptance Criteria

1. THE Sync_Gate `app._device_chart_capable` SHALL continue to derive which families are
   written from the aircraft config and the drive's Link_ID, fail-safe to chart-capable,
   with config authoritative and unchanged by this feature.
2. WHEN a sync completes successfully on a chart-capable drive whose Card_Manifest has at
   least one chart cycle, THE Sync_Engine SHALL relabel the volume to `C<charts>P<plates>`,
   degrading to the single present component (for example `C2611` or `P2611`) when only
   one Chart_Family synced.
3. WHEN a sync completes successfully on a non-chart-capable drive (per the Identity_File
   `chart_capable` snapshot, with no chart cycles), THE Sync_Engine SHALL relabel the
   volume to `<PREFIX><MMDD>`, where PREFIX is the device friendly name sanitized to
   FAT32 rules (uppercase A-Z / 0-9) and MMDD is the month and day of the Nav_Valid_Date.
4. IF the device friendly name is blank or sanitizes to empty, THEN THE Sync_Engine SHALL
   use the fallback PREFIX `EFISNAV`, further reduced to `NAV` when needed to satisfy the
   length limit.
5. THE non-chart-capable volume label `<PREFIX><MMDD>` SHALL contain the month and day
   only, with no year component.
6. THE volume label produced by the relabel step SHALL be at most 11 characters and
   contain only uppercase A-Z and 0-9.
7. WHEN the relabel step determines whether a drive is chart-capable, THE Sync_Engine
   SHALL read the `chart_capable` field from the Identity_File and SHALL NOT read the
   aircraft config at relabel time.

### Requirement 10: Preserved invariants and non-regressions

**User Story:** As a user, I want the behaviors that already work correctly to keep
working, so that wiring the manifest and removing the stall detector does not silently
break nav sync, cycle authority, abort handling, or root-level flight data.

#### Acceptance Criteria

1. THE Nav_Family SHALL be synced by size and SHA-256 per file with deep verification,
   no cycle gate, and no manifest, unchanged by this feature.
2. THE Card_Manifest SHALL remain authoritative for the cycle gate.
3. WHEN a volume label disagrees with the Card_Manifest, THE Sync_Engine SHALL log a
   WARNING and use the Card_Manifest value.
4. WHEN a drive is removed or the system sleeps during a sync, THE Sync_Engine SHALL
   abort the sync via the Mount_Watchdog `is_aborted` path with the stall detector
   removed.
5. THE chart sync SHALL NOT delete root-level flight data files outside `ChartData/`
   (for example `Settings.dat`, `Plan.dat`, `State.dat`, `WP.dat`, and `*.bak`), because
   family deletion is scoped to `ChartData/` and `ChartData/Plates/`.
6. THE adopt ChartData_Delete (Case A2 or Case C confirmed) SHALL be an explicit,
   user-confirmed action scoped to the `ChartData/` tree only, distinct from the sync's
   family `--delete`, and SHALL NOT delete root-level flight data.
