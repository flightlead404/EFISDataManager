# Requirements Document

## Introduction

Settings Management gives the EFIS Data Manager (DM) a per-device, dated history
of the GRT settings families (Settings, WP, Plan) it sees on returning drives,
a read-only way to explore and compare those archived versions in the dashboard,
and a DM-side writer that can place a chosen archived version — or the latest
archived version — back onto a USB drive bound to a device.

The DM is the ARCHIVE. The pilot, not the DM, decides what to save: "Backup
Settings to USB" is a MANUAL action performed on the EFIS in the aircraft. A
settings file that comes back on a returning drive was therefore a deliberate
pilot save that was at-least-flight-test-acceptable at the time it was written.
The DM TRUSTS such a returning file as a legitimate new version of that device's
settings and records it; it does NOT second-guess the pilot with an
inadvertent-bad-change guard or a round-trip "were these loaded?" prompt.

Restoration in this spec means the DM WRITING a chosen archived version back onto
a USB drive so the pilot can then load it on the EFIS via the EFIS's own native
"Restore All Settings" action. This is distinct from — and does not implement —
the EFIS-side undo, which is a pilot action on the display and needs no DM
feature. Settings are flight-critical (Vne, redlines, engine limits, cylinder
count), so the writer is correctness-first: it never fabricates or recomputes the
GRT UPDATE/CHECKSIZE/CHECKSUM bytes, it reuses an archived file's own original
bytes verbatim, and it makes the chosen version the sole settings slot on the
drive so the EFIS loads it by default.

This spec builds on the shipped multi-device identity foundation
(`multi-device-settings`): device configuration, Mode_S aircraft verification,
and drive-to-device resolution/binding already exist and are reused, not
reinvented. It also reuses the shipped settings parser, Source_Key extraction,
Content_Hash, and the content-aware archiver from `efis-settings-import`.

The component split from the multi-device decision record holds: the DM owns
archive-on-insert, the restoration/population WRITE, and the divergence warning;
the dashboard owns read-only exploration, comparison, and selecting a version for
restoration. Restoration is the one cross-cutting flow — the dashboard SELECTS
the version and the DM WRITES it.

Staging "latest EFIS/AHRS software" onto drives is explicitly OUT OF SCOPE for
this spec. Enforcing the analysis-data-source gate (FDL/logbook import from one
device) is also out of scope; only its declaration already exists from the
multi-device foundation.

## Glossary

- **DM**: The EFIS Data Manager menu-bar application (`app.py`). It owns drive
  mount, archive, sync/push, restoration write, and device management.
- **Dashboard**: The separate web UI. In this spec it owns the read-only settings
  explorer, the compare view, and the version-selection handoff for restoration.
- **Device**: One GRT display in the managed aircraft, identified by its Link_ID
  (position 1-3). Reused from `multi-device-settings`.
- **Link_ID**: The GRT inter-display link position (SID 387), value 1/2/3. The
  stable key for a configured device. Reused from `multi-device-settings`.
- **Managed aircraft**: The single aircraft the DM manages, keyed by Mode_S.
  Reused from `multi-device-settings`.
- **Resolution**: The existing drive-to-device classification from
  `device_resolution.resolve_drive` producing a `ResolutionOutcome` (status +
  resolved device/Link_ID). Reused to decide which device an inserted drive
  belongs to before archiving.
- **Settings family / Archived_Settings_Family**: A GRT settings-format family the
  DM keeps a change-history for: Settings, WP (`CHECKTYPE=UWP`), and Plan
  (`CHECKTYPE=PLAN`). The State family is explicitly NOT archived. Reused from
  `efis-settings-import` (`_ARCHIVED_SETTINGS_FAMILIES`).
- **Slot**: A family's `.bak` or `.dat` file on a drive. The current slot is the
  higher-UPDATE_Value slot when both are present, else the single present slot.
  Reused from `efis-settings-import` selection.
- **Content_Hash**: The hash over a settings file's parsed SID/value content,
  EXCLUDING the volatile `UPDATE=`, `CHECKSIZE=`, and `CHECKSUM=` keys. Reused
  from `efis_settings.content_hash`.
- **Archive_Date**: The calendar date encoded in an archived file's name
  (`<Family>-YYYY-MM-DD[-N].<ext>`), stamped by the DM at archive time. The only
  reliable cross-capture ordering signal (UPDATE_Value is not globally monotonic;
  EFIS FAT mtimes are unreliable). Reused from `efis-settings-import`.
- **Archive root**: The local archive directory, config key `archive_path`
  (default `~/Documents/EFIS_Archive`). Reused.
- **Per-device archive location**: The per-device, per-family archive subtree the
  DM writes under the archive root, partitioned by Link_ID so one device's history
  never mixes with another's (e.g. `<archive_root>/Settings/link1/`).
- **Unresolved-device archive location**: A designated per-family location under
  the archive root for snapshots whose owning device Link_ID cannot be derived
  from their content (e.g. `<archive_root>/Settings/unresolved/`), used so such
  snapshots are preserved rather than discarded or misassigned.
- **Device settings history**: For a given device and family, the time-ordered
  list of that family's archived snapshots, ordered by Archive_Date (and, within a
  date, by the non-destructive `-N` sequence).
- **Latest archived version**: For a given device and family, the most recent
  snapshot in the device settings history by Archive_Date (and within that date,
  the latest `-N`).
- **Returning drive**: A populated USB drive inserted into the DM that Resolution
  classifies as belonging to a configured device of the managed aircraft.
- **Checksum wall**: The cockpit-confirmed GRT behavior that a settings file whose
  embedded CHECKSUM does not match its contents is SILENTLY IGNORED by the EFIS
  (the EFIS reports success but keeps old values). The DM therefore never
  fabricates, bumps, or recomputes UPDATE/CHECKSIZE/CHECKSUM; it only ever writes
  an archived file's own original, EFIS-written bytes.
- **Restoration write**: The DM operation that writes a chosen archived settings
  version onto a drive bound to a device, as the sole settings slot for that
  family, reusing the archived file's original bytes.
- **Settings population**: The DM operation that writes a device's latest archived
  version onto a prepared/bound drive when that content is not already present.
  Mechanically the restoration write sourcing "latest archived version" instead of
  a user-chosen one.
- **Divergence**: The condition where a returning drive's current settings content
  for a device is OLDER than that device's latest archived version (the drive is
  behind the DM's known-good history).

## Requirements

### Requirement 1: Per-device settings archiving on a returning drive

**User Story:** As a multi-display aircraft owner, I want the DM to archive the
settings that come back on each device's drive separately per device, so that one
display's settings history never mixes with another's.

#### Acceptance Criteria

1. WHEN a returning drive is resolved to a configured device of the managed
   aircraft, THEN for each Archived_Settings_Family present on the drive the DM
   SHALL archive that family's current slot into the per-device archive location
   for the resolved device's Link_ID.
2. THE DM SHALL select the current slot of each family (higher-UPDATE_Value slot
   when both `.bak` and `.dat` are present, otherwise the single present slot)
   using the existing selection from `efis-settings-import`.
3. THE DM SHALL treat a returning settings file as a trusted, legitimate version
   and SHALL NOT prompt the user to confirm it is a good change and SHALL NOT
   require a round-trip "were these loaded" confirmation before archiving it.
4. WHEN the current slot of a family has a Content_Hash that differs from the
   Content_Hash of that device-and-family's latest archived version, OR WHEN that
   device-and-family has no existing archive, THEN THE DM SHALL write the current
   slot as a new snapshot date-stamped with the current Archive_Date in the
   per-device archive location.
5. WHEN the current slot of a family has a Content_Hash equal to the Content_Hash
   of that device-and-family's latest archived version, THEN THE DM SHALL skip
   archiving that family and SHALL NOT write a new snapshot.
6. WHERE the DM would write a new snapshot but a snapshot with the current
   Archive_Date already exists for that device and family, THE DM SHALL write to a
   non-destructive sequence-suffixed name (`<Family>-YYYY-MM-DD-N.<ext>`, N
   starting at 2) and SHALL NOT overwrite or discard the existing same-date
   snapshot.
7. THE DM SHALL determine a device-and-family's latest archived version by ordering
   that device's archived snapshots for the family by Archive_Date, and within a
   single Archive_Date by the latest sequence suffix, and SHALL NOT use
   UPDATE_Value or EFIS-written file modification times for recency.
8. THE DM SHALL NOT delete or modify any settings file on the returning drive.
9. THE DM SHALL NOT archive the State family.
10. WHERE the inserted drive does not resolve to a configured device of the managed
    aircraft, THE DM SHALL take no settings-archiving action for that drive
    (consistent with the multi-device resolution outcomes).

### Requirement 2: Reading a device's settings history

**User Story:** As a pilot, I want to see the dated history of settings for each
of my displays, so that I can tell what was configured and when.

#### Acceptance Criteria

1. THE DM SHALL expose, per configured device and per Archived_Settings_Family,
   the device settings history as a list of archived snapshots ordered from newest
   to oldest by Archive_Date (and within a date by sequence suffix).
2. THE DM SHALL present each snapshot in the history with its Archive_Date, family,
   sequence suffix when present, and the device's display label.
3. WHERE a device and family have no archived snapshots, THE DM SHALL report an
   empty history for that device and family without error.
4. THE DM SHALL read archived snapshots as read-only and SHALL NOT modify or delete
   any archived file while reading the history.

### Requirement 3: Exploring an archived settings version (read-only)

**User Story:** As a pilot, I want to open an archived settings version and see
its values with human-readable labels, so that I can understand what a given
snapshot contains.

#### Acceptance Criteria

1. WHEN the user opens an archived settings snapshot in the dashboard, THE
   Dashboard SHALL parse it read-only into SID/value pairs and SHALL present each
   recognized SID with its human-readable label and value.
2. THE Dashboard SHALL present an archived snapshot's values using the existing
   settings SID-to-label mapping rather than a new mapping.
3. WHERE a snapshot contains a SID the mapping does not recognize, THE Dashboard
   SHALL still display that SID and its raw value and SHALL mark it as unlabeled.
4. THE Dashboard SHALL treat every archived snapshot as read-only and SHALL NOT
   provide any control that edits, re-saves, or deletes a snapshot's contents.
5. IF an archived snapshot cannot be parsed, THEN THE Dashboard SHALL report the
   failure and SHALL leave the snapshot file unchanged.

### Requirement 4: Comparing two settings versions

**User Story:** As a pilot, I want to compare two settings versions and see what
changed, so that I can understand the difference between a returning card and my
known-good history, or between any two dated snapshots.

#### Acceptance Criteria

1. WHEN the user selects two settings versions to compare, THE Dashboard SHALL
   present a labeled, per-SID difference showing, for each differing SID, the
   label, the value in the first version, and the value in the second version.
2. THE Dashboard SHALL base the comparison on parsed SID/value content and SHALL
   exclude the volatile `UPDATE=`, `CHECKSIZE=`, and `CHECKSUM=` keys from the
   reported differences.
3. THE Dashboard SHALL identify SIDs present in one version but absent in the other
   as added or removed in the difference.
4. WHERE the two versions have identical Content_Hash, THE Dashboard SHALL report
   that the two versions are equivalent in configuration content.
5. THE Dashboard SHALL allow comparing two archived snapshots of the same device
   and family, and SHALL allow comparing an archived snapshot against the current
   slot of a returning drive for the same device and family.

### Requirement 5: Selecting a version and restoring it to a drive

**User Story:** As a pilot, I want to pick an archived settings version and have
the DM write it to my USB drive, so that I can then load it on the EFIS to recover
a known-good configuration.

#### Acceptance Criteria

1. WHEN the user selects an archived settings version for a device in the dashboard
   and requests restoration, THE Dashboard SHALL hand the selected version and the
   target device to the DM, and THE DM SHALL perform the restoration write.
2. THE DM SHALL perform the restoration write only to a drive bound to the target
   device, resolving the binding via the existing drive-to-device resolution.
3. THE DM SHALL write the selected archived version's original file bytes verbatim
   and SHALL NOT fabricate, recompute, or modify the `UPDATE=`, `CHECKSIZE=`, or
   `CHECKSUM=` values.
4. THE DM SHALL write the restored version as the sole slot of that family on the
   drive, removing any other `.bak`/`.dat` slot of the same family so the EFIS's
   native "Restore All Settings" loads the restored version by default.
5. THE DM SHALL confirm the restoration write succeeded by verifying the written
   file's bytes match the selected archived version before reporting success.
6. IF the restoration write cannot be completed or verified, THEN THE DM SHALL
   report the failure and SHALL leave the drive's settings slots in a state that
   does not silently present a partially written version as current.
7. THE DM SHALL require explicit user action to initiate a restoration write and
   SHALL NOT perform a restoration write automatically on drive insert.
8. THE DM SHALL perform the restoration write as a WRITE-NOW one-shot against the
   currently inserted/bound drive and SHALL NOT defer or queue it.
9. THE DM SHALL restore only the Settings family and SHALL NOT restore the WP or
   Plan families.

### Requirement 6: Populating a device's latest settings onto a drive

**User Story:** As a pilot, I want every prepared card for a device to carry that
device's latest known-good settings, so that my rotating cards converge toward the
same baseline and a fresh card is never missing settings.

#### Acceptance Criteria

1. WHEN the DM prepares or syncs a drive bound to a configured device AND that
   device has a latest archived version for the Settings family, THE DM SHALL
   write that latest archived version onto the drive if the drive does not already
   carry settings content equal to it.
2. THE DM SHALL determine "already carries equal content" by comparing the
   Content_Hash of the drive's current Settings slot against the Content_Hash of
   the device's latest archived version, and SHALL skip the population write when
   they are equal.
3. THE DM SHALL perform the population write using the same mechanism as the
   restoration write, sourcing the device's latest archived version instead of a
   user-chosen version, including writing the archived file's original bytes
   verbatim and making it the sole slot of that family.
4. WHERE a device has no latest archived version for a family, THE DM SHALL skip
   population for that family without error.
5. THE DM SHALL scope population per device so that a drive bound to one device is
   only ever populated with that device's archived settings.
6. THE DM SHALL populate only the Settings family and SHALL NOT populate the WP or
   Plan families.

### Requirement 7: Divergence confirmation before archiving a returning drive

**User Story:** As a pilot who rotates two cards per display, I want to be asked
before a returning card's settings become the new latest archived version when
they do not match my latest known-good version, so that I can skip archiving a
card inserted out of sequence and notice drift before it bites me in the cockpit.

#### Acceptance Criteria

1. WHEN a returning drive is resolved to a configured device AND that device has a
   latest archived version for the Settings family AND the drive has a current
   Settings slot, THE DM SHALL, BEFORE archiving any settings family, compare the
   Content_Hash of the drive's current Settings slot against the Content_Hash of
   the device's latest archived version.
2. WHERE the Content_Hashes differ, THE DM SHALL ask the pilot to confirm before
   archiving, identifying the device by its display label and naming the
   Archive_Date of the latest archived version, and SHALL offer two choices:
   archive the settings, or skip archiving the settings.
3. WHERE the drive's current Settings Content_Hash equals the Content_Hash of an
   OLDER archived Settings version for that device (not the latest), THE DM SHALL
   additionally state in the confirmation the Archive_Date of the most recent
   archived version it matches, so the pilot can judge whether the card is out of
   sequence. WHERE it matches no archived version, THE DM SHALL state that the
   content is new.
4. WHEN the pilot chooses to skip, THE DM SHALL NOT archive the Settings, WP, or
   Plan families for that insert, and SHALL leave the card and the archive
   unchanged for those families. All other archiving (flight data, logbook, demo
   and snapshot files, cleanup) SHALL proceed as normal.
5. WHEN the pilot chooses to archive, THE DM SHALL archive per Requirement 1.
6. WHERE the drive's current Settings Content_Hash equals the latest archived
   version's, OR the device has no archived Settings version, OR the drive has no
   current Settings slot, THE DM SHALL NOT prompt and SHALL archive per
   Requirement 1.
7. THE DM SHALL base the divergence decision and the history match solely on
   Content_Hash equality and SHALL NOT attempt to decide which of two captures is
   newer using UPDATE_Value or EFIS-written file modification times (see the
   known concern in Assumptions and Constraints: the DM cannot reliably order two
   captures it did not observe in sequence). The confirmation is shown even for
   a legitimately newer card; the choice is left to the pilot.

### Requirement 8: Preserve the checksum wall on every write

**User Story:** As a pilot relying on flight-critical limits, I want to be certain
the DM never hands the EFIS a settings file the EFIS will silently ignore, so that
a "restored" configuration is actually loaded.

#### Acceptance Criteria

1. THE DM SHALL, on every settings write to a drive (restoration or population),
   write only the original EFIS-written bytes of an archived file and SHALL NOT
   alter the `UPDATE=`, `CHECKSIZE=`, or `CHECKSUM=` values.
2. THE DM SHALL NOT provide any capability to edit a settings file's values and
   write the result to a drive.
3. THE DM SHALL preserve the archived file byte-for-byte from archive capture
   through restoration/population write so that the bytes the EFIS reads are the
   bytes a GRT display originally wrote.

### Requirement 9: Read-only and fault-tolerant behavior

**User Story:** As a pilot, I want settings management to be safe and robust, so
that a malformed file or a missing archive never crashes the DM or corrupts my
settings.

#### Acceptance Criteria

1. THE DM and Dashboard SHALL treat every returning-drive settings file and every
   archived snapshot as read-only for the purposes of archiving, exploration, and
   comparison.
2. WHEN a settings file is malformed, missing an UPDATE_Value, or contains unknown
   SIDs, THE DM and Dashboard SHALL handle the condition and SHALL continue without
   terminating abnormally.
3. IF an archiving, exploration, comparison, restoration, or population operation
   cannot be completed, THEN the responsible component SHALL report the reason and
   SHALL leave archived snapshots and the source drive files unchanged, except for
   the single intended slot change of a confirmed restoration/population write.

### Requirement 10: Component split and backward compatibility

**User Story:** As the maintainer, I want settings management to fit the existing
DM/dashboard split and existing single-device installs, so that it integrates
cleanly and does not disturb current users.

#### Acceptance Criteria

1. THE DM SHALL own archive-on-insert, the restoration write, the population write,
   and the divergence warning.
2. THE Dashboard SHALL own the read-only settings explorer, the comparison view,
   and the selection of a version for restoration, and SHALL delegate the actual
   drive write to the DM.
3. WHERE the managed aircraft is configured with a single device, THE DM SHALL
   archive, restore, and populate that device using the same per-device archive
   location mechanism without requiring any multi-device prompt.
4. THE DM SHALL reuse the existing device configuration, drive-to-device
   resolution, settings parser, Source_Key extraction, Content_Hash, and
   content-aware archiver, and SHALL NOT reinvent them.

### Requirement 11: Adopt existing archives into the per-device structure

**User Story:** As an existing user with settings already archived under the old
flat layout, I want my prior snapshots adopted into the new per-device structure,
so that my history is continuous and nothing is orphaned.

#### Acceptance Criteria

1. WHERE settings snapshots exist under the pre-existing flat per-family archive
   layout (not under a per-device Link_ID location), THE DM SHALL adopt them into
   the per-device archive location as a one-time migration.
2. THE DM SHALL determine the owning device for an existing flat snapshot by the
   Link_ID derived from that snapshot's own parsed content via the existing
   Source_Key extraction, and SHALL place it under that Link_ID's per-device
   archive location.
3. WHERE an existing flat snapshot's Link_ID cannot be derived from its content,
   THE DM SHALL adopt it into a designated unresolved-device location rather than
   discard it, and SHALL NOT assign it to an arbitrary configured device.
4. THE DM SHALL preserve each adopted snapshot's Archive_Date and sequence suffix
   and SHALL preserve its file bytes unchanged during adoption.
5. WHERE adopting a snapshot into the per-device location would collide with an
   existing snapshot of the same device, family, Archive_Date, and sequence, THE
   DM SHALL adopt it to a non-destructive sequence-suffixed name and SHALL NOT
   overwrite or discard either snapshot.
6. THE DM SHALL perform the adoption idempotently so that running it again after a
   completed migration makes no further changes.

7. THE existing consumers of the archived Settings backups (the EFIS settings
   import detection and the percent-power settings lookup) SHALL continue to find
   the current archived backup after migration, by considering archived Settings
   snapshots in the per-device `link{N}/` and `unresolved/` locations as well as
   any legacy files directly under `Settings/`, and SHALL select the current
   backup exactly as they did before (latest Archive_Date, then the same-date
   `.bak`/`.dat` pair).

8. THE EFIS settings import detection SHALL consider only archived Settings
   backups written by the primary display (content Link_ID, SID 387, equal to
   `1`), wherever they are archived (`link{N}/`, `unresolved/`, or legacy flat),
   so a newer backup from a non-primary display never hides the primary's
   latest backup. The percent-power settings lookup is unaffected by this
   criterion.

## Out of Scope / Deferred

- **Staging latest EFIS/AHRS software onto drives** — version sourcing and staging
  of the GRT display software uploaders is a separate concern, deferred.
- **EFIS-side "Restore All Settings"** — the pilot's native undo on the display is
  not a DM feature; this spec only writes a loadable version to the drive.
- **Analysis-data-source enforcement** — gating FDL/logbook import to the single
  designated device is deferred; only its declaration exists (multi-device
  foundation).
- **Editing settings values** — blocked by the checksum wall; the DM never edits
  and re-checksums a settings file.
- **Multi-aircraft** — the single managed-aircraft assumption from the multi-device
  foundation holds.
- **Reconciling two independent on-card save histories the DM never observed** —
  inherent to the two-card rotation; mitigated by population (Requirement 6) and
  the divergence warning (Requirement 7), not fully solved.

## Assumptions and Constraints

- A single-slot settings file written by the DM loads cleanly on the EFIS via its
  native "Restore All Settings" action (cockpit-confirmed).
- "Backup Settings to USB" is a manual pilot action on the EFIS; the DM never
  writes settings to a drive except via an explicit restoration or a population
  step, and never writes to the aircraft/EFIS directly.
- The GRT CHECKSUM algorithm is undocumented and a mismatched checksum is silently
  ignored by the EFIS; the DM therefore only ever moves original EFIS-written
  bytes and never regenerates checksum/update fields.
- Archive_Date (observation order) is the only reliable cross-capture ordering
  signal; UPDATE_Value is not globally monotonic across displays and EFIS FAT
  mtimes are unreliable.
- This spec builds on and reuses the shipped `multi-device-settings` and
  `efis-settings-import` components.

### Known concern: cross-capture ordering of settings is not decidable

The timestamps on EFIS-generated settings files on a USB drive have been shown to
be unreliable (FAT modification times do not track real write order), and
UPDATE_Value is not monotonic across captures or displays. The DM therefore
cannot reliably decide which of two settings captures it did not observe in
sequence is "newer". As a consequence:

- The only ordering the DM trusts is Archive_Date — its OWN observation order at
  archive time — which orders snapshots the DM captured but says nothing about a
  returning card whose content the DM has never archived.
- The divergence warning (Requirement 7) therefore reports only that a returning
  card does NOT match the latest archived version, never a definitive
  "older/newer" verdict.
- The two-card drift problem is mitigated (population in Requirement 6 converges
  cards toward the latest; the divergence warning flags a mismatch) but not fully
  solved, because the DM cannot reconstruct an on-card history it never observed.

This is accepted as a known limitation of the GRT firmware and USB/FAT behavior,
not a defect to be engineered around.
