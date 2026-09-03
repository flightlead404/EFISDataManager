# Requirements Document

## Introduction

The EFIS Data Manager syncs a local chart/nav image to a physical EFIS USB
drive. Today the sync is a single monolithic job that rsyncs the entire
`ChartData` tree with checksums (`rsync -rc`), copies standalone files, and
uses the modification time of one SQLite file (`ScannedCharts.sqlite`) as the
sole currency proxy.

This has produced real problems:

- The `-c` (checksum) rsync over ~9 GB / ~100k files on USB media routinely
  runs for an hour and hits the 1-hour timeout, which is then reported to the
  user as "Update errors" even though nothing is actually wrong.
- Several error branches in the updater append to an errors list without
  logging, so the menu-bar status ("Update errors") contradicts the "Recent
  Errors" panel (empty).
- The currency check inspects only `ScannedCharts.sqlite` and is blind to the
  approach-plates product family, which has its own separate manifest
  (`Plates/Plates.sqlite`). Stale or partial plates can be reported "current".
- There is no handling for the drive being ejected, the machine sleeping, or a
  dock/hub being removed mid-sync. An interrupted sync can leave the drive in a
  partial state that the quick currency check cannot detect.

The chart data on the drive is flight-relevant. The trust model is explicit:
**a quick currency check is only safe if the update mechanism is atomic,
exhaustive, and resumable.** This feature makes the updater trustworthy and
then lets the routine currency check stay cheap, backed by an on-demand
exhaustive verification.

## Product families and their markers

The local image contains three independently-sourced products, each with its
own currency marker that MUST be written last in its job:

1. **Scanned/en-route charts** — sectionals (SEC/), IFR low (LO/), IFR high
   (HI/), etc. Commit marker: `ChartData/ScannedCharts.sqlite`.
2. **Approach plates & airport diagrams** — `ChartData/Plates/` tree. Commit
   marker: `ChartData/Plates/Plates.sqlite`.
3. **Navigation database** — `NAV.DB` (and `NAV-proc.DB`). Single standalone
   file(s) from a different source; currency and verification is a per-file
   checksum.

## Glossary

- **Product family**: One of the three independently-sourced data sets synced
  to the drive — scanned/en-route charts, approach plates, or the nav database.
- **Commit marker**: The per-family SQLite file (or, for nav, the file itself)
  whose presence and freshness on the drive stands in for "this whole family is
  complete and current". Written last so it only appears once the payload is in.
- **Payload**: All files in a family except its commit marker.
- **Quick currency check**: A fast comparison (marker mtime/size, nav checksum)
  used routinely to decide whether a sync is needed.
- **Exhaustive verification**: A full walk of a family comparing file count and
  per-file size between the local image and the drive.
- **Idempotent / resumable**: Re-running the sync after an interruption
  converges the drive to the same correct final state without duplicating work
  or corrupting files.
- **Interrupted-sync marker**: A durable record that a sync was in progress for
  a given drive/family, used to force exhaustive verify+repair before trusting
  the quick check again.

## Requirements

### Requirement 1: Separate sync jobs per product family

**User Story:** As a pilot, I want the scanned-chart set and the approach-plate
set synced as independent jobs, so that one can complete and be marked current
even if the other is still in progress or fails.

#### Acceptance Criteria
1. WHEN a drive update runs THEN the system SHALL execute the scanned-charts
   sync and the plates sync as two separate jobs, each with its own success/
   error result.
2. WHEN one job fails or is interrupted THEN the other job's currency state
   SHALL be unaffected (its marker remains valid if it completed).
3. WHEN a job's payload sync completes without error THEN the system SHALL write
   that job's commit marker (its SQLite file) as the FINAL step of the job.
4. IF any payload step in a job errors THEN the system SHALL NOT write that
   job's commit marker.
5. WHEN the NAV database is synced THEN it SHALL be handled as a standalone
   file job, verified by checksum rather than by a directory marker.

### Requirement 2: Commit-marker atomicity

**User Story:** As a pilot, I want a "current" marker on the drive to reliably
mean the entire product set is complete, so that I can trust the quick check.

#### Acceptance Criteria
1. WHEN syncing a product family THEN the system SHALL copy all payload files
   BEFORE copying that family's commit marker, and SHALL exclude the marker
   from the bulk payload copy.
2. WHEN the payload copy is verified complete (count + size match, see Req 5)
   THEN and only then SHALL the marker be copied and flushed to the drive.
3. IF the process is interrupted after payload copy but before the marker is
   written THEN the drive marker SHALL remain older/absent, so the next quick
   check detects the family as stale.
4. WHEN the marker file is written THEN the system SHALL flush it to persistent
   storage before declaring the job complete.

### Requirement 3: Quick currency check (routine)

**User Story:** As a pilot, I want the routine currency check to be fast, so
inserting the drive doesn't trigger a long operation every time.

#### Acceptance Criteria
1. WHEN a routine currency check runs THEN the system SHALL compare, per family,
   the local commit marker against the drive marker using modification time
   and size, plus existence of the family's top-level structure.
2. WHEN the NAV database is checked THEN the system SHALL compare a checksum of
   the single local file against the drive file.
3. WHEN a marker indicates the drive family is older/missing/size-mismatched
   THEN the system SHALL report that family as stale.
4. WHEN all families' markers match AND no prior interrupted sync is recorded
   THEN the system SHALL report the drive current.
5. WHEN a prior interrupted sync is recorded for the drive THEN the routine
   check SHALL NOT report current until an exhaustive verification has cleared
   it (see Req 6).

### Requirement 4: Efficient payload sync (no full-tree checksums)

**User Story:** As a user, I want routine syncs to finish quickly, so the drive
is ready without an hour-long wait.

#### Acceptance Criteria
1. WHEN syncing a chart family's payload THEN the system SHALL use a delta
   strategy based on size and modification time (with a modify-window tolerance
   suitable for FAT/exFAT timestamp granularity), NOT a full-content checksum
   of every file.
2. WHEN the sync runs THEN the system SHALL NOT impose a fixed wall-clock
   timeout that fails an otherwise-healthy large transfer; progress and
   liveness SHALL be tracked instead (see Req 7).
3. WHEN a full-content verification is explicitly requested THEN checksum-based
   comparison MAY be used as an opt-in deep verify, separate from routine sync.

### Requirement 5: Payload completeness verification (count + size)

**User Story:** As a pilot, I want the updater to confirm the drive actually
received all files before marking a set current.

#### Acceptance Criteria
1. WHEN a family's payload sync finishes THEN the system SHALL verify the drive
   copy against the local source by comparing file count and per-file size.
2. IF the count or any size differs THEN the system SHALL treat the job as
   failed, log the discrepancy, and NOT write the commit marker.
3. WHEN verification passes THEN the system SHALL proceed to write the marker.
4. The exhaustive verification SHALL use count + size comparison (not content
   hashing) as the default depth.

### Requirement 6: Exhaustive verification and repair

**User Story:** As a pilot, I want a way to fully verify the drive and repair
any gaps, especially after an interruption.

#### Acceptance Criteria
1. WHEN an exhaustive verification runs THEN the system SHALL walk each family's
   tree on both local image and drive and compare file count and per-file size.
2. WHEN discrepancies are found THEN the system SHALL feed them into the update
   mechanism to repair (re-copy missing/changed files), then re-verify.
3. WHEN a prior interrupted sync is recorded THEN the system SHALL run an
   exhaustive verification (and repair if needed) before clearing the
   interrupted state and declaring the drive current.
4. WHEN the user explicitly requests "Verify Drive" THEN the system SHALL run an
   exhaustive verification on demand and report per-family results.
5. WHEN exhaustive verification and any repair complete with no remaining
   discrepancy THEN the system SHALL clear the interrupted-sync record.

### Requirement 7: Interruption handling (eject / sleep / dock removal)

**User Story:** As a pilot, I want the app to handle the drive being removed or
the laptop sleeping mid-sync without corrupting the drive or lying about
currency.

#### Acceptance Criteria
1. WHEN a sync starts THEN the system SHALL record a durable interrupted-sync
   marker identifying the drive and the family/families being written, BEFORE
   copying payload.
2. WHILE a sync is running THEN the system SHALL periodically confirm the drive
   mount is still present, and SHALL abort promptly if the mount disappears.
3. WHEN the mount disappears mid-sync THEN the system SHALL stop copying, leave
   the interrupted-sync marker in place, log a clear error, and set an
   informative status.
4. WHEN the machine signals it is about to sleep THEN the system SHALL treat an
   in-progress sync as at-risk: either pause/stop safely and leave the
   interrupted marker, so it can resume on wake or next mount.
5. WHEN the drive is next mounted (or on next launch) AND an interrupted marker
   exists for it THEN the system SHALL run exhaustive verify+repair before
   declaring current (see Req 6.3).
6. The sync operation SHALL be idempotent and resumable: running it again after
   an interruption SHALL converge the drive to the correct final state without
   duplicating work or corrupting files, without needing to know where it
   stopped.
7. WHEN a sync completes fully (all attempted families verified and markers
   written) THEN the system SHALL remove the interrupted-sync marker.

### Requirement 8: Accurate, consistent error reporting

**User Story:** As a user, I want the status line and the Recent Errors panel to
agree, so I can trust what the app tells me.

#### Acceptance Criteria
1. WHEN any sync/verify error is recorded THEN the system SHALL log it at
   WARNING or ERROR level so it appears in the Recent Errors panel.
2. WHEN a job completes with no real error THEN the system SHALL NOT report an
   error status (e.g. a large-but-successful transfer SHALL NOT be reported as
   an error).
3. WHEN the status line shows an error state THEN there SHALL be a corresponding
   logged entry explaining it.
4. WHEN a job succeeds THEN the status SHALL return to an idle/current state
   rather than remaining on a stale error string.

### Requirement 9: Backward compatibility and safety

**User Story:** As an existing user, I want the new sync to work with my current
drive and image layout without re-downloading everything.

#### Acceptance Criteria
1. WHEN the new updater runs against an existing populated drive THEN it SHALL
   NOT force a full re-copy if the payload already matches (size + mtime).
2. WHEN markers already exist on the drive from the old scheme THEN the system
   SHALL interpret them under the new per-family model without data loss.
3. The updater SHALL continue to leave drive files outside the known product
   families untouched (e.g. .bak, System Volume Information).
4. The `Prepare Drive` (format + populate) flow SHALL use the new per-family
   sync and report per-family results.

### Requirement 10: Stable per-drive identity for multi-drive rotation

**User Story:** As a pilot who rotates an arbitrary number of EFIS USB drives
(e.g. one current/in-use, one n-1 kept in the airplane, one at the Mac for
archiving/updating), I want the app to track each physical drive's sync state
independently, so that swapping drives never causes one drive's interrupted-sync
state to be mis-attributed to another.

**Context / problem being fixed:** The interrupted-sync record and the currency
gating key on the drive's *mount path* (`/Volumes/EFIS_1`). macOS derives the
mount path from the volume label and disambiguates duplicate labels with a
timing/history-dependent numeric suffix, so the same physical drive can mount at
`/Volumes/EFIS` one time and `/Volumes/EFIS_1` the next, and two differently-
labeled-identically drives collide on the same paths in an unpredictable order.
Mount path is therefore not a stable per-drive identity. Observed on the
maintainer's own machine: a single drive mounted at both `/Volumes/EFIS` and
`/Volumes/EFIS_1` across sessions.

#### Acceptance Criteria
1. WHEN the app manages an EFIS chart drive THEN it SHALL maintain a durable,
   visible identity file at the volume root (`EFIS_DRIVE_ID.json`) containing at
   least: an app-generated drive `id` (UUID), a `kind` discriminator
   (`"efis-chart-drive"`), and a `schema_version`.
2. WHEN determining a drive's identity THEN the system SHALL key sync-state on
   the app-generated `id` from the identity file (the "UUID plus something
   else" = UUID + `kind`), NOT on the mount path or volume label.
3. WHEN the `Prepare Drive` flow formats/populates a drive THEN it SHALL write a
   fresh identity file as part of preparation.
4. WHEN a recognized EFIS drive (by existing detection heuristics) mounts
   WITHOUT an identity file THEN the system SHALL adopt it by writing an
   identity file (lazy adoption), so pre-existing drives gain identity without
   reformatting.
5. WHEN the durable sync-state records interrupted families THEN it SHALL do so
   in a map keyed by drive `id`, supporting an arbitrary number of drives
   (N-drive), so that `begin_family` / `complete_family` / `pending_families`
   for one drive never modify another drive's entry.
6. WHEN a drive is swapped mid-rotation THEN interrupted-sync state SHALL follow
   the physical drive by `id`, regardless of which mount path each drive
   receives.
7. WHEN the drive identity cannot be resolved (identity file missing/unreadable
   and adoption fails, or a `diskutil`/read error) THEN the system SHALL fail
   safe: it SHALL NOT apply any other drive's interrupted-sync state to this
   drive, SHALL still perform a normal marker-based currency check and update,
   and SHALL log a WARNING. Currency decisions SHALL never depend on the
   identity file's contents (the on-drive commit markers + payload remain the
   sole source of truth for "is this drive current").
8. WHEN the identity file is written or updated THEN the system SHALL record
   provenance/telemetry fields: `created`, `prepared_by` (app version + host),
   `last_sync_started`, `last_sync_completed`, `last_sync_result`
   (`clean|aborted|failed`) with the families covered, `sync_count`, and
   `data_cycle` (the chart/nav cycle the drive was last brought to). These
   fields SHALL be written atomically (temp + rename) and updated around the
   commit-marker step so a partial sync does not advertise false provenance.
9. WHEN the identity file also captures the OS `VolumeUUID` and volume `label`
   at write time THEN a later mismatch (e.g. a cloned drive or a copied identity
   file) SHALL be logged as a WARNING; the app-generated `id` remains the key.
10. WHEN any recognized EFIS drive mounts (including the n-1 emergency drive)
    THEN the system SHALL archive and refresh it to current; there are no
    per-drive "roles" and no drive is exempted from update.
11. The requirement to uniquely label drives SHALL be documented as a
    recommended user practice (defense in depth), but correctness SHALL NOT
    depend on unique labels once identity files exist.
12. Having two EFIS drives mounted simultaneously is OUT OF SCOPE and SHALL be
    documented as a known limitation; the identity keying SHALL avoid
    cross-drive state corruption but concurrent-drive behavior is neither
    guaranteed nor tested.
