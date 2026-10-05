# Requirements Document

## Introduction

The EFIS Data Manager menu-bar tool writes GRT chart data (scanned/en-route
charts, approach plates) and the nav database (NAV.DB + NAV-proc.DB) to a
physical FAT32 USB drive that is carried to a GRT EFIS. Today that write runs
rsync delta-copy into a mount-swapped `/sbin/mount_msdos` mount
(`chart-sync-stall-fix`). Three problems are rooted in mounting the FAT32 volume
through macOS: the FSKit write stall, the AppleDouble sidecar tax (the mount
layer writes a `._*` companion for most files — measured ~+86% files — which
`COPYFILE_DISABLE=1` and rsync `--exclude` do not suppress), and screen-lock
teardown fragility that can drop the drive off the bus mid-write.

This feature replaces **how** charts and nav get written. Instead of writing
through a mount, the tool composes the entire drive filesystem as a FAT32 image
on the local SSD, writes that image to the drive's raw partition slice
(`/dev/rdiskNsN`) in one sequential `dd`, and confirms completion by raw
readback (hash the written device region against the image hash) with no mount
of the drive at any point. The approach was validated end to end on 2026-10-03:
a GRT EFIS read a `dd`'d drive normally, and the Mac-side compose + dd-to-slice +
raw-readback-hash matched exactly. This design is authoritative; the open
questions it raised are resolved and reflected here.

The composition is decoupled from drive presence: it is (re)built when the data
manager's existing periodic checks report new charts or nav downloaded, so the
slow per-file work happens once on local SSD rather than per-drive over USB.

This effort is a change to the menu-bar tool only. It does not change what chart
or nav data is sourced or downloaded, and it does not alter the currency check
or the archive/FDL-import path, which are preserved as-is.

## Glossary

- **System**: The EFIS Data Manager menu-bar tool.
- **Composer**: The component that maintains the local FAT32 image and
  (re)builds its contents from current authoritative sources. Touches no drive.
- **Writer**: The component that `dd`-writes the composed image to a drive's raw
  partition slice after the device guard passes.
- **Verifier**: The component that confirms a write by raw readback hash of the
  device region against the composed image hash. Read-only; no mount.
- **Image**: The local FAT32 filesystem image (`EFIS_USB.img`) on the SSD, sized
  to the target partition slice.
- **Target slice**: The drive's FAT32 partition slice, addressed as the raw node
  `/dev/rdiskNsN` for the `dd` write and raw readback (NOT the whole-disk node
  `/dev/rdiskN`, which would discard the partition scheme).
- **Raw readback**: Reading the device region directly from `/dev/rdiskNsN`
  without mounting, for hash comparison against the image.
- **Device guard**: The destructive-write safety check that must pass before any
  `dd` is issued to a device node.
- **AppleDouble sidecar**: A `._*` companion file the macOS mount layer
  synthesizes for files on filesystems without native resource-fork support.
- **Sidecar-free populate**: Populating the image so the finished image contains
  no `._*` or `.DS_Store` entries.
- **Completion marker**: The chart commit marker / sync sentinel baked into the
  image and written last in the composition, standing in for "this image is
  complete and current"; confirmed on the drive by raw readback, not by a mount.
- **Current authoritative source**: The data manager's own current local copy of
  a component (ChartData under the configured USB image path; NAV.DB /
  NAV-proc.DB downloaded by the nav check) — never an old drive's on-drive files.
- **Data-manager check**: The existing periodic chart check (every 12h) and
  nav-DB check (every 24h) the app already runs.
- **Interim sudoers backend**: The existing `/etc/sudoers.d/efis-data-manager`
  NOPASSWD allow-list routed through `mount_swap.run_privileged`, extended by
  this feature for the specific `dd` write and raw read. (The signed-helper
  replacement is out of scope — roadmap item "sudo retirement".)
- **Mount-swap fallback**: The existing `msdos_mount` write path, retained behind
  a config flag so a write can fall back to it if image-write is disabled or
  fails during rollout.
- **Currency check**: The existing per-family currency decision that determines
  whether a write is needed. Preserved unchanged.

## Requirements

### Requirement 1: Drive-independent image composition from current sources

**User Story:** As a pilot, I want the local USB image built from the current
chart and nav data the tool maintains, so that a prepared drive is never stale.

#### Acceptance Criteria

1. THE Composer SHALL maintain a local FAT32 image at a configured path on the local SSD.
2. WHEN composing the image, THE Composer SHALL populate ChartData from the current chart source under the configured USB image path.
3. WHEN composing the image, THE Composer SHALL populate NAV.DB and NAV-proc.DB from the current nav source the data manager downloads, not from any drive's on-drive files.
4. WHEN composing the image, THE Composer SHALL include the drive identity file `EFIS_DRIVE_ID.json`, using the target drive's own identity when known or a newly minted identity when preparing a fresh drive.
5. THE Composer SHALL source every included component from its current authoritative source and SHALL NOT read any component from an old drive's files.
6. THE Composer SHALL perform composition without accessing any `/dev/disk` node and without mounting any USB drive, so that composition can run with no drive attached.

### Requirement 2: Image sized to the target partition slice

**User Story:** As a developer, I want the image sized to the drive's partition
slice, so that a `dd` write preserves the partition scheme and the EFIS reads it.

#### Acceptance Criteria

1. THE Composer SHALL size the image to the byte length of the target FAT32 partition slice.
2. THE Composer SHALL create the image filesystem with `/sbin/newfs_msdos` as FAT32.
3. THE Writer SHALL write the image to the raw partition-slice node `/dev/rdiskNsN` and SHALL NOT write to the whole-disk node.
4. IF the composed image byte size does not equal the target slice size at write time, THEN THE Writer SHALL refuse the write.

### Requirement 3: Sidecar-free image population

**User Story:** As a pilot, I want the image free of AppleDouble sidecars, so
that the drive carries only real chart/nav files and avoids the ~+86% file tax.

#### Acceptance Criteria

1. WHEN populating the image, THE Composer SHALL use a populate method that does not direct the macOS mount layer to synthesize AppleDouble companions, specifically `rsync --no-xattrs` with `._*` and `.DS_Store` excluded.
2. WHEN population completes, THE Composer SHALL remove any residual `._*` and `.DS_Store` entries from the image.
3. WHEN composition completes, THE Composer SHALL assert the finished image contains no `._*` and no `.DS_Store` entries, and IF any are present THEN THE Composer SHALL report the composition as failed and SHALL NOT mark the image ready.

### Requirement 4: Completion marker baked into the image

**User Story:** As a pilot, I want the completion marker to appear on the drive
only when the whole image was written, so that a partial write never looks done.

#### Acceptance Criteria

1. WHEN composing the image, THE Composer SHALL write the completion marker into the image as the last populated content.
2. THE Writer SHALL write the completion marker to the drive only as part of writing the full image, and SHALL NOT write the completion marker to the drive through any mount.
3. WHEN verifying a write, THE Verifier SHALL confirm the completion marker on the drive by raw readback rather than by mounting the drive.

### Requirement 5: `dd` write to the raw slice

**User Story:** As a pilot, I want the composed image written to the drive in one
sequential transfer, so that the write is fast and avoids the FSKit stall.

#### Acceptance Criteria

1. WHEN a write is requested and the device guard passes, THE Writer SHALL write the composed image to the drive's raw partition slice with `/bin/dd`.
2. THE Writer SHALL perform the write without mounting the drive and without routing the write through FSKit or `mount_msdos`.
3. WHEN the write completes, THE Writer SHALL return the number of bytes written and the resulting write status.
4. IF the `dd` write is interrupted before completion, THEN THE Writer SHALL report the write as incomplete and SHALL leave the local image and composer state unchanged.

### Requirement 6: Raw-readback verification

**User Story:** As a pilot, I want the write verified by reading the drive back,
so that a drive is reported complete only when its bytes match the image.

#### Acceptance Criteria

1. WHEN a write completes, THE Verifier SHALL read the written device region directly from `/dev/rdiskNsN` and SHALL NOT mount the drive to verify.
2. THE Verifier SHALL compute a hash of the raw-readback region and compare it to the composed image hash.
3. WHEN the raw-readback hash matches the composed image hash, THE Verifier SHALL report the write complete.
4. IF the raw-readback hash does not match the composed image hash, THEN THE Verifier SHALL report the write incomplete and SHALL surface the next step.

### Requirement 7: Destructive-write safety device guard

**User Story:** As a pilot, I want a hard guard before any raw write, so that a
`dd` can never target the wrong device and destroy data.

#### Acceptance Criteria

1. THE Writer SHALL issue a `dd` write only to a device node that passes the full device guard.
2. THE device guard SHALL confirm the node is a `/dev/diskNsN` partition-slice node, is removable, and is FAT32 via diskutil, reusing the existing `mount_swap.validate_device_node` checks.
3. THE device guard SHALL confirm the target slice size matches the expected image size.
4. THE device guard SHALL confirm the target drive's volume label or identity matches the expected drive.
5. THE device guard SHALL require explicit user confirmation of the specific target drive before the write.
6. IF any device-guard check fails or cannot be verified, THEN THE Writer SHALL refuse the write by raising, consistent with the existing `PrivilegeError` contract, and SHALL NOT write.

### Requirement 8: Interim privileged backend for raw write and read

**User Story:** As a maintainer, I want the raw write and read scoped as tightly
as possible through the existing privilege shim, so that the interim grant stays
narrow and auditable.

#### Acceptance Criteria

1. THE System SHALL route the privileged `dd` write and the privileged raw read through the existing `mount_swap.run_privileged` allow-list shim.
2. THE System SHALL extend the `run_privileged` allowed-binary set to include only the specific binaries needed for the raw write and raw read.
3. THE System SHALL extend the interim `/etc/sudoers.d/efis-data-manager` grant to allow only the specific privileged write and read commands, scoped to the specific binary paths and the `/dev/rdiskNsN` device-node pattern.
4. WHEN a privileged write or read command does not match the allowed binary and device-node pattern, THE System SHALL refuse the command before execution.
5. WHEN a privileged call fails, THE System SHALL surface safe-next-step guidance consistent with the existing mount-swap error contract.

### Requirement 9: Event-driven composer auto-refresh

**User Story:** As a pilot, I want the image rebuilt automatically when new
charts or nav land, so that a fresh image is ready without my intervention.

#### Acceptance Criteria

1. WHEN the data-manager chart check completes and reports newly downloaded chart data, THE Composer SHALL rebuild the image.
2. WHEN the data-manager nav-DB check completes and reports a newly downloaded nav database, THE Composer SHALL rebuild the image.
3. THE Composer SHALL trigger refresh from the existing data-manager checks and SHALL NOT run a separate polling timer for refresh.
4. IF a composition fails, THEN THE Composer SHALL keep the previous good image and SHALL NOT replace it with a bad image.

### Requirement 10: Persistent, actionable "new image ready" notification

**User Story:** As a pilot, I want a visible, persistent indication when a fresh
image is ready, so that I don't miss it and know what changed.

#### Acceptance Criteria

1. THE System SHALL present a persistent menu item reflecting image state that reads as current when no new image is pending.
2. WHEN a fresh image is composed, THE System SHALL update the persistent menu item to indicate a new image is ready and SHALL name what is new, including the nav cycle and the chart cycle.
3. WHEN a fresh image is ready, THE System SHALL badge the menu-bar title glyph to indicate the pending image.
4. THE "new image ready" menu item SHALL be actionable to begin preparing a drive.
5. WHEN the user acts on the new image, THE System SHALL return the persistent menu item and the menu-bar badge to the current state.

### Requirement 11: Mount-swap retirement with rollout fallback

**User Story:** As a maintainer, I want the mount-swap removed from the normal
write path but available as a fallback, so that I can roll out image-write safely.

#### Acceptance Criteria

1. WHEN image-write is enabled, THE System SHALL use the image-write path for the chart/nav write and SHALL NOT use the `msdos_mount` swap for that write.
2. WHERE a configuration flag selects the fallback, THE System SHALL perform the chart/nav write via the existing `msdos_mount` swap path.
3. IF an image-write attempt fails and the fallback is enabled, THEN THE System SHALL fall back to the `msdos_mount` swap write path.
4. THE System SHALL retain the `msdos_mount` implementation available for the fallback path.

### Requirement 12: Preserve the currency and archive/FDL-import path

**User Story:** As a pilot, I want the existing currency decision and
archive-on-insert behavior unchanged, so that only the write mechanism changes.

#### Acceptance Criteria

1. THE System SHALL use the existing currency check to decide whether a write is needed.
2. WHEN a drive is inserted, THE System SHALL continue to archive settings and FDL as it does today.
3. WHEN the currency check reports the drive current, THE System SHALL perform no write.
4. WHEN a write is needed, THE System SHALL replace only the mechanism by which charts and nav are written, and SHALL NOT change what chart or nav data is sourced or downloaded.

### Requirement 13: Unit of work and interrupted-write handling

**User Story:** As a pilot, I want an interrupted write handled cleanly, so that
a drive is never silently left half-written and looking current.

#### Acceptance Criteria

1. THE System SHALL treat the whole image as the unit of work for the chart/nav write — verified or not — rather than tracking per-family write state for this path.
2. WHEN a write is interrupted or fails verification, THE System SHALL handle recovery by re-writing and re-verifying the whole image.
3. WHILE an image write is unverified, THE System SHALL report the drive as not current.

### Requirement 14: Scope, constraints, and release versioning

**User Story:** As a maintainer, I want scope and versioning constraints
enforced, so that the change stays in the menu-bar tool and releases are labeled
correctly.

#### Acceptance Criteria

1. THE System SHALL implement image-based drive write as a change to the menu-bar tool and the shared data path, using only system-native tools (`/sbin/newfs_msdos`, `/bin/dd`, `/usr/bin/hdiutil`) without adding new third-party dependencies.
2. WHEN this feature is released, THE System SHALL bump `MENUBAR_VERSION` per the versioning policy.
3. WHERE the dashboard is unchanged by this feature, THE System SHALL leave `DASHBOARD_VERSION` unchanged.

## Correctness Properties Mapping

The design's five Correctness Properties map to these requirements:

- **Property 1 — No mount of the target drive.** Validates: Requirements 5.2,
  6.1, 4.3.
- **Property 2 — Marker atomicity.** Validates: Requirements 4.1, 4.2, 5.4.
- **Property 3 — Verified completion.** Validates: Requirements 6.2, 6.3, 6.4.
- **Property 4 — Destructive-write safety.** Validates: Requirements 7.1, 7.2,
  7.3, 7.4, 7.5, 7.6.
- **Property 5 — Composition is drive-independent.** Validates: Requirements
  1.6, 2.1.

## Non-Goals (deferred to future increments / roadmap)

- **Per-device settings staging into the image.** This increment carries
  charts + nav + identity only. Settings continue to be archived FROM drives,
  not placed onto them. Depends on roadmap settings-management and multi-device
  work.
- **EFIS/AHRS software staging into the image.** Deferred backlog item; the data
  manager cannot auto-download GRT software (bot protection blocks headless
  download), so software currency stays the user's manual responsibility.
- **Drive-to-device binding / `device_link_id`.** Roadmap multi-device item;
  the identity file is carried as-is this increment.
- **Signed privileged helper.** The interim sudoers backend is used; replacing
  it with a signed `SMAppService` helper is the roadmap "sudo retirement" item.
- **Changing what chart or nav data is sourced or downloaded**, and
  **redesigning the currency check or the archive/FDL-import path**, which are
  preserved as-is.
