# Requirements Document

## Introduction

The EFIS Data Manager menu-bar tool syncs GRT chart data (scanned/en-route
charts, approach plates, and the nav database) from a local image to a physical
FAT32 USB drive. During large or heavy write sessions — a from-scratch populate
of the full ~101,563-file / 9.1 GB image, or a substantial incremental top-up —
write progress stops for 120 seconds, the existing no-progress watchdog aborts
the sync, and the drive is left partially synced with the affected family's
sync-state marked "interrupted". Because the interrupted state persists, every
subsequent reinsert re-attempts the same heavy write and re-stalls, so the
primary chart drive can never complete a sync.

The completed `drive-sync-integrity` feature already provides the correctness
machinery this effort depends on: AppleDouble/sidecar exclusion
(`._*`/`.DS_Store`, `--delete-excluded`, `COPYFILE_DISABLE=1`), rsync stdout
draining to avoid pipe-buffer deadlock, a 120-second no-progress watchdog, and
resumable/idempotent per-family sync-state (families: scanned, plates, nav) with
commit markers written last. That machinery survives and resumes a stall but
does not prevent it. This effort targets the stall itself.

**Working hypothesis, not established fact:** Kernel-log correlation points to
Apple's FSKit driver (`com.apple.fskit.msdos` / `lifs`) as the root cause —
kernel logs show `com.apple.fskit.msdos` client init and `clientDied` churn plus
"Denying dirty-tracking opt-in" messages bracketing the 120-second freezes. This
correlation is suggestive but unproven. The environment is macOS 15.7.9 (build
24G830); the 15.7.9 update did not resolve the stall. Evidence also indicates
the stall is a latent, pre-existing driver behavior rather than a regression
this project introduced: before 2026-09-03 nearly every mount reported "up to
date, no sync needed" (no heavy writes occurred), the 120-second stall-detection
code was itself only added 2026-09-03, and the first heavy populate that ran
(2026-09-03) already exhibited the interrupt/resume thrashing. Consequently this
effort must not assume a code regression and must not rely on reverting to a
prior version. Confirming or refuting the FSKit root cause with instrumentation
is an explicit requirement before any mitigation is committed.

The preferred mitigation to evaluate first is mounting the drive via
`/sbin/mount_msdos` instead of the default FSKit mount, **if** it actually
bypasses the FSKit `lifs` path. Its viability is uncertain: on macOS 15
`msdos.fs` is itself an FSKit module and the only loaded kext is
`com.apple.filesystems.lifs`, so `mount_msdos` may route through the same
stalling path. A viability spike must therefore precede adoption, with fallback
mitigations if it does not help.

This feature does not patch Apple's driver and remains a change to the menu-bar
tool only. The scope is limited to WHAT the fix must achieve and WHY, not the
implementation.

## Glossary

- **Stall**: A period during a sync in which write progress to the drive makes
  no measurable advance (no increase in bytes/files written) for a sustained
  interval.
- **Stall watchdog**: The existing mechanism that aborts a sync after 120
  seconds of no write progress.
- **FSKit driver**: Apple's user-space file-system framework components
  implicated by kernel-log correlation, specifically `com.apple.fskit.msdos` and
  the `com.apple.filesystems.lifs` kext, hypothesized to cause the stall.
- **Root-cause confirmation**: Instrumented evidence that correlates an
  app-side stall with concurrent FSKit/kernel events strongly enough to confirm
  or refute the FSKit hypothesis.
- **Product family**: One of the three independently-synced data sets — scanned
  charts, approach plates, or nav database — each with its own commit marker.
- **Commit marker**: The per-family file written last, whose presence and
  freshness stands in for "this family is complete and current".
- **Full populate**: A from-scratch sync of the entire local image
  (~101,563 files / ~9.1 GB) to a freshly formatted drive.
- **Incremental top-up**: A sync that copies only the files missing or changed
  on a partially-populated drive.
- **Reproduction gate**: A repeatable, measurable procedure that reliably
  triggers the stall and serves as the acceptance test for any mitigation.
- **Mitigation**: A change to how the sync mounts, writes, or recovers that
  prevents the stall or lets a heavy sync complete despite driver behavior.
- **mount_msdos spike**: A bounded investigation of whether `/sbin/mount_msdos`
  bypasses the FSKit path and whether a heavy write still stalls under it.
- **Test drive EFIS_3**: A disposable FAT32 test drive (the user retains two
  other flight-ready drives) used for reproduction and acceptance testing.
- **System**: The EFIS Data Manager menu-bar tool.

## Reproduction environment (authoritative for acceptance testing)

- Local chart source `~/EFIS/USB/ChartData`: 101,563 files / 9.1 GB, both
  commit markers present and healthy.
- Test drive EFIS_3: currently holds 94,893 of those files, is missing the
  plates marker and ~6,670 files (mostly plates) — a reliable incremental
  top-up reproduction.
- A from-scratch full populate reproduction is available by reformatting EFIS_3.

## Requirements

### Requirement 1: Repeatable, measurable stall reproduction

**User Story:** As a developer, I want a repeatable and measurable reproduction
of the stall, so that I have an objective acceptance gate for any mitigation.

#### Acceptance Criteria

1. THE System SHALL provide a documented reproduction procedure that performs an incremental top-up against a partially-populated drive missing at least the plates marker and approximately 6,670 plates files.
2. THE System SHALL provide a documented reproduction procedure that performs a from-scratch full populate of approximately 101,563 files and 9.1 GB to a freshly formatted drive.
3. WHEN a reproduction procedure runs against an unmitigated sync, THE System SHALL record whether a 120-second no-progress stall occurred.
4. THE System SHALL record measured write throughput and cumulative write-progress over time for each reproduction run.
5. THE System SHALL express the acceptance gate as measurable pass/fail criteria based on stall occurrence and sync completion.

### Requirement 2: Confirm or refute the FSKit root-cause hypothesis

**User Story:** As a developer, I want the FSKit root-cause hypothesis confirmed
or refuted with instrumentation, so that mitigation effort targets the real
cause rather than an assumption.

#### Acceptance Criteria

1. WHEN a stall occurs during an instrumented reproduction run, THE System SHALL record the app-side stall onset time and the write-progress state at that time.
2. WHEN a stall occurs during an instrumented reproduction run, THE System SHALL capture concurrent FSKit and kernel events, including `com.apple.fskit.msdos` client lifecycle events and dirty-tracking opt-in denials.
3. THE System SHALL correlate app-side stall intervals with captured FSKit/kernel events by timestamp.
4. THE System SHALL produce a written determination that either confirms or refutes the FSKit driver as the stall root cause, supported by the recorded correlation.
5. IF the recorded evidence is insufficient to confirm or refute the FSKit hypothesis, THEN THE System SHALL document the evidence gap and the additional instrumentation required.

### Requirement 3: Evaluate mount_msdos viability before adoption

**User Story:** As a developer, I want the `mount_msdos` mitigation validated by
a spike before adoption, so that the effort does not commit to a mount path that
routes through the same stalling driver.

#### Acceptance Criteria

1. THE System SHALL determine whether mounting the drive via `/sbin/mount_msdos` bypasses the FSKit `lifs` path on macOS 15.7.9.
2. WHEN the drive is mounted via `/sbin/mount_msdos`, THE System SHALL run a heavy-write reproduction and record whether a 120-second stall occurs.
3. IF `/sbin/mount_msdos` bypasses the FSKit path and a heavy write completes without a stall, THEN THE System SHALL identify `mount_msdos` as the adopted mitigation.
4. IF `/sbin/mount_msdos` routes through the FSKit path or a heavy write still stalls, THEN THE System SHALL record `mount_msdos` as non-viable and select a fallback mitigation.
5. THE System SHALL document the spike outcome, including the observed mount path and stall result.

### Requirement 4: Fallback mitigation strategy

**User Story:** As a developer, I want defined fallback mitigations, so that a
heavy sync can complete even when `mount_msdos` does not bypass the stall.

#### Acceptance Criteria

1. WHERE `mount_msdos` is non-viable, THE System SHALL evaluate throttled or chunked writes with inter-batch fsync and brief drain pauses as a fallback mitigation.
2. WHERE `mount_msdos` is non-viable, THE System SHALL evaluate a clean unmount-and-remount cycle followed by a resume as a fallback mitigation.
3. WHERE `mount_msdos` is non-viable, THE System SHALL evaluate changes to write granularity and cadence as a fallback mitigation.
4. WHEN a fallback mitigation is selected, THE System SHALL validate it against the reproduction gate.
5. WHEN a fallback mitigation reacts to a detected stall, THE System SHALL resume the interrupted sync using the existing resumable sync-state rather than only aborting.

### Requirement 5: Heavy syncs complete without a stall

**User Story:** As a pilot, I want a full populate and an incremental top-up to
finish, so that my chart drive is complete and current before a flight.

#### Acceptance Criteria

1. WHEN a full populate of approximately 101,563 files and 9.1 GB runs under the adopted mitigation, THE System SHALL complete the sync without a 120-second stall.
2. WHEN an incremental top-up of the remaining missing files runs under the adopted mitigation, THE System SHALL complete the sync without a 120-second stall.
3. WHEN a heavy sync completes under the adopted mitigation, THE System SHALL clear the interrupted sync-state for each completed family.
4. WHEN a drive that previously held the interrupted state is reinserted after a completed sync, THE System SHALL report the drive as current without re-attempting the stalled write.

### Requirement 6: Preserve existing correctness guarantees

**User Story:** As a pilot, I want the existing correctness guarantees kept
intact, so that the drive is never misreported as current.

#### Acceptance Criteria

1. WHEN a family finishes syncing under the adopted mitigation, THE System SHALL verify the file count and total size of that family against the local image before writing its commit marker.
2. THE System SHALL write a family commit marker only after that family passes count-and-size verification.
3. WHEN a completed sync is followed by a second sync run against the same drive, THE System SHALL perform no write work and report the drive as current.
4. WHEN the drive is ejected or the machine sleeps mid-sync, THE System SHALL leave the affected family marked interrupted and resumable rather than current.
5. THE System SHALL exclude AppleDouble sidecars and `.DS_Store` files from the synced payload under the adopted mitigation.

### Requirement 7: Safe fallback user experience when the driver still wedges

**User Story:** As a pilot, I want clear guidance and a safe state if the driver
still wedges despite mitigation, so that I never rely on a drive that is
silently incomplete.

#### Acceptance Criteria

1. IF a stall persists despite the adopted mitigation, THEN THE System SHALL report the sync as incomplete and identify the affected family.
2. IF a stall persists despite the adopted mitigation, THEN THE System SHALL present user guidance describing the safe next step.
3. WHEN a wedged sync is retried after a stall, THE System SHALL resume from the existing sync-state without duplicating completed work.
4. IF a sync is incomplete, THEN THE System SHALL report the affected family as not current.
5. THE System SHALL avoid reporting a drive as current while any family remains in the interrupted state.

### Requirement 8: Scope, constraints, and release versioning

**User Story:** As a maintainer, I want scope and versioning constraints
enforced, so that the fix stays within the menu-bar tool and releases are
labeled correctly.

#### Acceptance Criteria

1. THE System SHALL implement the mitigation as a change to the menu-bar tool without modifying Apple's file-system driver.
2. WHEN this feature is released, THE System SHALL bump `MENUBAR_VERSION` per the versioning policy.
3. WHERE the dashboard is unchanged by this feature, THE System SHALL leave `DASHBOARD_VERSION` unchanged.

## Non-Goals

- Patching, replacing, or reverse-engineering Apple's FSKit / `lifs` driver.
- Reverting to a prior application version as the fix (the stall is a latent
  driver behavior, not a regression this project introduced).
- Any change to the web dashboard component.
- Redesigning the existing `drive-sync-integrity` resumable sync-state,
  verification, or commit-marker machinery, which is reused as-is.
