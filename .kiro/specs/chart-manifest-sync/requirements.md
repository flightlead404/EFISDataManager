# Requirements Document

## Introduction

The Chart Manifest Sync feature replaces the EFIS chart-drive sync engine's
correctness mechanism and its write mechanism. These two concerns are addressed
together because they are entangled in the code being retired.

The correctness problem is the core driver. Today the chart tree is synced with
`rsync --size-only`, which has a genuine correctness gap: a chart file revised
to the same byte size but different content is compared equal and skipped, so
the card silently keeps a stale file. The field investigation
(`INVESTIGATION-2026-10-24.md`) also established that file timestamps are not a
reliable correctness signal on these charts, so neither size nor time can be
trusted. The only correct mechanism is content hashing. This feature introduces
a uniform SHA-256 content manifest of the chart tree, computed at download time
on the fast SSD, and diffs it against a manifest stored on the card to drive the
sync. Correctness is never traded away for performance.

The write problem is the second concern. Benchmarking established that a plain
`rsync --delete` to the ordinary FSKit `/Volumes` mount completes a full
worst-case rewrite cleanly with no hang. This feature therefore moves the write
path to the plain FSKit mount and retires both the mount-swap and the image-dd
write paths. This feature supersedes `chart-sync-stall-fix`.

The engine gains three independent data streams with different freshness rules,
each gated cheaply before any expensive work: the scanned chart family (SEC + LO
+ optional HI) on the 56-day charts cycle; the plates family on the 28-day
plates cycle; and nav (NAV.DB, NAV-proc.DB) which has no cycle gate and is always
evaluated by its existing per-file checksum. nav is nominally on a 28-day cycle,
but there is no cycle number available to gate on — none appears on the GRT
download page or in the filename (the only external clue is a human-readable
date), and GRT posting is best-effort. Because no reliable cycle number exists
and the payload is small, nav is evaluated by content checksum on every insert,
which never skips a newer nav database. The layering per chart family,
cheapest first, is cycle-number check, then (only when the cycle rolled) a
manifest diff, then a write of only the delta.

## Glossary

- **Sync_Engine**: The EFIS chart-drive sync subsystem under this feature,
  spanning the download-time manifest build (`currency.py`), the sync-engine
  wiring (`drive_updater.py`), and the drive-update orchestration
  (`app._run_drive_update`).
- **EFIS_Card**: The per-device FAT32 USB card the GRT HXr EFIS reads charts
  and navigation data from, mounted by the OS at a plain FSKit `/Volumes` mount
  point.
- **Chart_File**: A single file within a chart family tree on the local SSD
  chart store and, after sync, on the EFIS_Card.
- **Chart_Family**: One of the sync families whose content is governed by a
  content manifest: the Scanned_Family or the Plates_Family.
- **Scanned_Family**: The chart family comprising the enabled subset of
  sectional (SEC), IFR-low (LO), and IFR-high (HI) charts plus the
  `ScannedCharts.sqlite` catalog marker, governed by the Charts_Cycle on the
  56-day cycle.
- **Plates_Family**: The chart family comprising approach plates, airport
  diagrams, and procedures, governed by the Plates_Cycle on the 28-day cycle.
- **Nav_Stream**: The navigation-data stream comprising `NAV.DB` and
  `NAV-proc.DB`, evaluated by per-file SHA-256 checksum with no cycle gate and
  no manifest. nav is nominally a 28-day product, but no gateable cycle number
  is exposed (not on the download page, not in the filename), and GRT posting is
  best-effort, so nav is checksum-evaluated every insert rather than
  cycle-gated.
- **Data_Stream**: One of the three independent sync streams: the
  Scanned_Family, the Plates_Family, or the Nav_Stream.
- **Charts_Cycle**: The 56-day cycle number that governs the Scanned_Family.
- **Plates_Cycle**: The 28-day cycle number that governs the Plates_Family.
- **Family_Manifest**: A record for one Chart_Family that lists, for every
  Chart_File in that family, its family-root-relative POSIX relative path, its
  lowercase hexadecimal SHA-256 content digest, and its size in bytes, together
  with the family name, the governing cycle, a build timestamp, and a schema
  version.
- **Local_Manifest**: A Family_Manifest stored on the SSD under the application
  support directory, representing the content of a Chart_Family in the local
  chart store for the newly downloaded cycle.
- **Card_Manifest**: The single `EFIS_MANIFEST.json` file stored at the
  EFIS_Card volume root, recording the authoritative Charts_Cycle and
  Plates_Cycle written to that card and the per-family manifest of what content
  was last written to that card.
- **Manifest_Builder**: The component that walks a Chart_Family tree at download
  time on the SSD and computes its Family_Manifest, excluding macOS metadata
  files and the commit marker.
- **Manifest_Store**: The component that reads and writes the Local_Manifest on
  the SSD and the Card_Manifest on the EFIS_Card.
- **Manifest_Diff**: The pure comparison that diffs a Local_Manifest against the
  corresponding family entries of a Card_Manifest and produces the Change_Set.
- **Change_Set**: The result of a Manifest_Diff for one Chart_Family: the added
  relative paths (present locally, absent on card), the changed relative paths
  (present on both with differing SHA-256), the removed relative paths (present
  on card, absent locally), and the count of unchanged files.
- **Cycle_Gate**: The per-stream freshness check that compares a stream's local
  cycle number to the EFIS_Card's stored cycle number and decides whether to
  skip the family, diff the family, or evaluate it.
- **Volume_Label**: The human-facing EFIS_Card volume name in the form
  `C<charts>P<plates>` that mirrors the Charts_Cycle and Plates_Cycle for
  at-a-glance reading, maintained after each sync but not trusted for the
  Cycle_Gate decision.
- **Commit_Marker**: The per-family durable record that a family's sync fully
  converged, written only after a verified sync.
- **Chart_Capability_Gate**: The existing multi-device-settings check
  (`_device_chart_capable` / `CHART_FAMILIES`) that determines whether a device
  receives chart and plate payloads or nav only.
- **HI_Destination**: The dedicated on-card subdirectory under `ChartData/` into
  which IFR-high (HI) files are placed so that the GRT EFIS reads them from the
  location it expects.

## Requirements

### Requirement 1: Content-change correctness

**User Story:** As a pilot relying on current charts, I want a chart whose
content changes to always be rewritten to the card even when its byte size does
not change, so that the card never silently keeps a stale chart.

#### Acceptance Criteria

1. WHEN a Chart_File's content differs between the Local_Manifest and the
   Card_Manifest, THE Sync_Engine SHALL include that Chart_File in the Change_Set
   as changed and SHALL write it to the EFIS_Card.
2. WHEN a Chart_File's content changes while its byte size is unchanged, THE
   Sync_Engine SHALL detect the change by comparing SHA-256 content digests and
   SHALL write the new content to the EFIS_Card.
3. THE Sync_Engine SHALL use the SHA-256 content digest as the authoritative
   signal for whether a Chart_File differs, and SHALL NOT treat equal byte size
   or file timestamp as sufficient evidence that two Chart_Files are identical.
4. THE Sync_Engine SHALL NOT skip or defer a detected content change for the
   sake of reducing sync time.

### Requirement 2: Three independent data streams

**User Story:** As a pilot, I want charts, plates, and navigation data evaluated
as independent streams with their own freshness rules, so that each is brought
current according to its own cycle without one stream affecting another.

#### Acceptance Criteria

1. THE Sync_Engine SHALL evaluate three independent Data_Streams: the
   Scanned_Family on the 56-day Charts_Cycle, the Plates_Family on the 28-day
   Plates_Cycle, and the Nav_Stream with no cycle gate.
2. THE Sync_Engine SHALL decide and perform each Data_Stream's write
   independently, so that the decision for one Data_Stream does not alter the
   decision or outcome for another Data_Stream.
3. THE Sync_Engine SHALL always evaluate the Nav_Stream regardless of the
   Charts_Cycle and Plates_Cycle values.
4. WHERE IFR-high (HI) charts are enabled by configuration, THE Sync_Engine
   SHALL treat the HI charts as part of the Scanned_Family on the Charts_Cycle
   and SHALL NOT treat them as a separate Data_Stream.
5. WHERE IFR-high (HI) charts are disabled by configuration, THE Sync_Engine
   SHALL exclude HI charts from the Scanned_Family.

### Requirement 3: Per-stream cycle gate

**User Story:** As a pilot, I want a stream whose cycle has not rolled to be
skipped with a single cheap check, so that an unchanged chart family is not
re-hashed or rewritten.

#### Acceptance Criteria

1. WHEN a Chart_Family's local cycle number equals the EFIS_Card's stored cycle
   number for that family, THE Cycle_Gate SHALL skip that Chart_Family without
   hashing or writing any file in it.
2. WHEN a Chart_Family's local cycle number differs from the EFIS_Card's stored
   cycle number for that family, THE Cycle_Gate SHALL select that Chart_Family
   for a Manifest_Diff.
3. WHEN a Chart_Family is skipped because its cycle number is equal, THE
   Sync_Engine SHALL leave that family's content on the EFIS_Card byte-identical.
4. THE Sync_Engine SHALL record a Chart_Family's cycle number in the
   Card_Manifest only after that family's sync for that cycle has fully converged
   and been verified, so that an equal stored cycle number implies equal content.
5. THE Cycle_Gate SHALL use the Card_Manifest's `charts_cycle` and
   `plates_cycle` values as the authoritative cycle numbers for the skip
   decision.
6. THE Sync_Engine SHALL cross-check the Volume_Label against the Card_Manifest
   cycle numbers, and IF the Volume_Label disagrees with the Card_Manifest, THEN
   THE Sync_Engine SHALL log the disagreement at WARNING level and SHALL use the
   Card_Manifest values for the Cycle_Gate decision.

### Requirement 4: Manifest diff and delta write

**User Story:** As a pilot, I want only the files that actually changed to be
written to the card, so that a rolled chart family converges to the current
content without rewriting unchanged files.

#### Acceptance Criteria

1. WHEN a Chart_Family is selected for a Manifest_Diff, THE Manifest_Diff SHALL
   compare the Local_Manifest against the corresponding family entries of the
   Card_Manifest by relative path and SHA-256 digest and SHALL produce a
   Change_Set of added, changed, and removed relative paths.
2. THE Manifest_Diff SHALL NOT re-hash the EFIS_Card, and SHALL treat the
   Card_Manifest as the faithful record of what was last written to the card.
3. WHEN a Change_Set is produced for a Chart_Family, THE Sync_Engine SHALL write
   the added and changed relative paths to the EFIS_Card and SHALL remove the
   removed relative paths from the EFIS_Card.
4. WHEN a Chart_Family's sync completes successfully, THE Sync_Engine SHALL leave
   that family's tree on the EFIS_Card equal to the Local_Manifest content and
   SHALL update the Card_Manifest for that family to equal the Local_Manifest.
5. THE Sync_Engine SHALL write the delta to the plain FSKit `/Volumes` mount and
   SHALL NOT use a mount-swap write path or an image-based raw write path.

### Requirement 5: Manifest schema and storage

**User Story:** As a maintainer, I want manifests stored in a stable,
round-trippable schema both locally and on the card, so that the cycle gate and
diff can rely on reading back exactly what was written.

#### Acceptance Criteria

1. THE Manifest_Builder SHALL compute a Family_Manifest for a Chart_Family by
   walking the family tree on the SSD and recording, for each Chart_File, its
   family-root-relative POSIX relative path, its lowercase hexadecimal SHA-256
   digest, and its size in bytes.
2. THE Manifest_Builder SHALL exclude macOS metadata files (`.DS_Store`, `._*`)
   and the Commit_Marker from the Family_Manifest so that the manifest matches
   exactly what the Sync_Engine transfers.
3. THE Manifest_Store SHALL serialize and deserialize a Family_Manifest such
   that loading a previously saved Family_Manifest yields an equal
   Family_Manifest.
4. THE Manifest_Store SHALL store one Local_Manifest per Chart_Family on the SSD
   and SHALL store a single Card_Manifest at the EFIS_Card volume root.
5. WHEN writing the Card_Manifest, THE Manifest_Store SHALL write it atomically
   so that a read during a write never observes a partially written
   Card_Manifest.

### Requirement 6: First-sync and recovery fallback

**User Story:** As a pilot using a fresh or repaired card, I want a missing or
unreadable card manifest to trigger a full, self-healing write, so that the card
is always brought to a known-good state.

#### Acceptance Criteria

1. IF the EFIS_Card has no Card_Manifest, THEN THE Sync_Engine SHALL diff each
   Chart_Family against an empty Card_Manifest, resulting in a full write of each
   enabled Chart_Family, and SHALL evaluate the Nav_Stream normally.
2. IF the EFIS_Card's Card_Manifest is corrupt or unparseable, THEN THE
   Sync_Engine SHALL treat it as absent, SHALL perform a full write as for a
   missing Card_Manifest, and SHALL log the condition at WARNING level.
3. IF a Local_Manifest for a Chart_Family is absent when the EFIS_Card is
   inserted, THEN THE Sync_Engine SHALL perform a full write of that
   Chart_Family and SHALL log the condition at WARNING level.
4. WHEN a full-write fallback completes successfully, THE Sync_Engine SHALL write
   the Card_Manifest and update the Volume_Label to reflect the written cycles.

### Requirement 7: Durable commit and flush

**User Story:** As a pilot, I want a sync reported as done to mean the bytes are
durably on the card, so that pulling the card after a completed sync never yields
a half-written or stale result.

#### Acceptance Criteria

1. WHEN a sync completes successfully, THE Sync_Engine SHALL flush the EFIS_Card
   by performing `os.sync` and unmounting the card before reporting the sync as
   done, so that the written bytes and the Card_Manifest are durable on the card.
2. THE Sync_Engine SHALL write a Chart_Family's Commit_Marker and the
   Card_Manifest only after that family's sync has been verified.
3. IF a sync is interrupted before a Chart_Family is verified, THEN THE
   Sync_Engine SHALL leave that family's Commit_Marker unwritten and SHALL leave
   the prior Card_Manifest intact, so that the next insert re-diffs and converges
   and no stale completion is recorded.
4. IF the EFIS_Card is removed during a write, THEN THE Sync_Engine SHALL abort
   the running transfer and SHALL leave the affected family's Commit_Marker and
   the Card_Manifest unwritten.
5. IF the data for a Chart_Family is written but the Card_Manifest write then
   fails, THEN THE Sync_Engine SHALL treat that Chart_Family as not completed,
   SHALL NOT update the Volume_Label for it, and SHALL log the condition at ERROR
   level, so that the next insert re-converges the family.

### Requirement 8: Navigation stream checksum sync

**User Story:** As a pilot, I want navigation data verified and rewritten by
content checksum on every insert, so that `NAV.DB` and `NAV-proc.DB` are always
correct regardless of cycle. (nav is nominally on a 28-day cycle, but no gateable
cycle number is exposed on the page or in the filename and GRT posting is
best-effort — checksum-every-insert is the correct, gate-free mechanism.)

#### Acceptance Criteria

1. THE Sync_Engine SHALL evaluate the Nav_Stream on every insert by comparing
   each nav file's size and SHA-256 checksum against the EFIS_Card copy, with no
   cycle gate and no manifest.
2. WHEN a nav file's content differs from the EFIS_Card copy, THE Sync_Engine
   SHALL rewrite that nav file to the EFIS_Card.
3. THE Sync_Engine SHALL verify the Nav_Stream by deep checksum before completing
   the Nav_Stream.

### Requirement 9: Chart-capability gate

**User Story:** As a pilot with a mix of devices, I want a device that is not
chart-capable to receive navigation data only, so that chart payloads are never
written to a device that cannot use them, while any uncertainty fails safe.

#### Acceptance Criteria

1. WHEN a device is positively resolved as not chart-capable, THE
   Chart_Capability_Gate SHALL cause the Sync_Engine to write only the Nav_Stream
   and SHALL NOT write any Scanned_Family or Plates_Family payload.
2. IF the chart-capability resolution for a device fails or is indeterminate,
   THEN THE Chart_Capability_Gate SHALL treat the device as chart-capable.

### Requirement 10: IFR-high extraction placement

**User Story:** As an IFR pilot who may enable high-altitude enroute charts, I
want HI files placed in their correct dedicated on-card subdirectory, so that the
GRT EFIS reads them instead of leaving them loose where the EFIS cannot find
them.

#### Acceptance Criteria

1. WHERE IFR-high (HI) charts are enabled, WHEN the Sync_Engine extracts a file
   named in the `<cycle>/W###/<tile>.HI.<n>.png` form, THE Sync_Engine SHALL
   place that file in the HI_Destination under `ChartData/` rather than at the
   flat `ChartData/` root.
2. THE Sync_Engine SHALL place HI files in the HI_Destination that matches what
   the GRT EFIS expects, parallel to the SEC and LO destination routing.
