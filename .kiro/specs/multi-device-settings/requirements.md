# Requirements Document

## Introduction

This specification covers the **first increment** of multi-device handling for an
aircraft with one to three GRT displays: **device identity + DM setup
(foundation)**. It establishes an aircraft-keyed configuration model, resolves an
inserted drive to a configured device by its Link_ID, verifies that a drive
belongs to the managed aircraft by its Mode_S, learns the managed aircraft's
Mode_S (with user confirmation) on first use, binds blank drives to a device at
Prepare time, and provides the setup UI inside the Data Manager (DM) menu-bar
app. It preserves single-device behavior for the common case.

The configuration data model is **aircraft-keyed from the start**: devices,
device count, and the analysis-data-source selection are per-aircraft attributes
stored under an aircraft record keyed by Mode_S
(`aircraft[Mode_S] -> { devices..., device_count, analysis_data_source }`). This
increment MANAGES EXACTLY ONE aircraft, but the data shape represents a map of
aircraft so that adding multi-aircraft support later is a non-breaking extension.
There is no aircraft-switcher UI and no multi-aircraft management in this
increment: only the forward-compatible shape plus the single managed aircraft.

This increment is the FOUNDATION that later increments consume. It does NOT cover
per-device archiving, outbound sync/push, settings exploration/compare/restore,
FDL/logbook enforcement to the analysis-source device, or the divergence warning.
Those are named in "Out of Scope / Deferred" with pointers to BACKLOG roadmap
items #1 and #2.

The design reuses shipped mechanisms rather than inventing new ones. Identity is
extracted with the existing `extract_source_key` (producing a `Source_Key` of
`Mode_S:Link_ID`) from `efis_settings.py`. The per-drive identity file
(`EFIS_DRIVE_ID.json`, read/written by `read_identity` / `write_identity` /
`_new_identity` in `drive_updater.py`) is extended with a device binding.
Per-device configuration lives in `DEFAULT_CONFIG` / `load_config` / `save_config`
in `config.py`, alongside the existing `settings_import` map. Setup UI lives in the
DM menu-bar app (`EFISDataManagerApp` in `app.py`, surfaced through
`settings_window.show_settings`).

This is a flight-critical domain. Settings identity determines which device a
settings file belongs to and whether a drive belongs to the managed aircraft, so
correctness and safety are emphasized wherever identity is resolved. Wherever a
device's Link_ID is resolved, the settings-file Link_ID is authoritative over any
stale recorded binding.

## Glossary

- **DM**: The EFIS Data Manager menu-bar application (`EFISDataManagerApp` in
  `app.py`). It owns drive mount, archive, sync, and device management. The
  dashboard defers all drive/device management to the DM.
- **Dashboard**: The separate web UI. In this increment it has no responsibilities;
  it is named only to mark the component split for later increments.
- **Device**: One GRT display in the aircraft (e.g. an HXr or a Mini A/P),
  identified by its position (Link_ID). An aircraft has 1 to 3 devices.
- **Link_ID**: The GRT inter-display link position, SID 387 (`SID_LINK_ID = "387"`),
  value 1/2/3 for primary/secondary/tertiary. Link_ID is POSITION ONLY; it does
  NOT imply device type or capability. It is the stable key for a configured
  device.
- **Mode_S**: The aircraft's Mode S / ICAO 24-bit address, SID 1063
  (`SID_MODE_S = "1063"`), e.g. `A60670`. Constant across all displays in one
  aircraft; used as the aircraft identity key.
- **Aircraft**: One airframe, identified by its Mode_S. In the configuration data
  model an aircraft record is keyed by Mode_S and owns that aircraft's device
  count, Device_Config records, and analysis-data-source selection
  (`aircraft[Mode_S] -> { devices..., device_count, analysis_data_source }`). The
  model represents a map of aircraft so multi-aircraft is a future non-breaking
  extension.
- **Managed aircraft**: The single aircraft this increment manages. Its Mode_S is
  learned (with user confirmation) on first use and thereafter is the Mode_S a
  drive must match to be processed. This increment has exactly one managed
  aircraft at a time.
- **Source_Key**: The identity string produced by `extract_source_key`,
  `"<mode_s>:<link_id>"` (e.g. `A60670:1`), with documented fallbacks: Mode_S only
  (`A60670`); `:<link_id>` when Mode_S is absent; `None` when neither is present
  (undetermined).
- **extract_source_key**: The existing function in `efis_settings.py` that parses
  a settings file into a `SourceIdentity(source_key, mode_s, link_id, flight_id)`.
  This increment reuses it for identity resolution and verification; it is not
  reinvented.
- **Chart-capable**: A user-declared boolean per device (yes/no) stating whether
  that device can use charts/plates. User-declared, NOT inferred from a device
  model or Link_ID, and NOT validated against any GRT device catalog.
- **Analysis-data-source device**: Exactly one user-selected device, designated
  as a per-aircraft attribute of the managed aircraft, intended to be the single
  authoritative source of analysis data in a later increment. In THIS increment
  only the DECLARATION is in scope; enforcement (FDL/logbook gating) is deferred.
- **Unrecognized-aircraft drive**: An inserted drive whose derived Mode_S is NOT
  the managed aircraft's Mode_S (a different aircraft, the ">1 aircraft" case), OR
  whose Mode_S is undetermined (`extract_source_key` returns no Mode_S). The DM
  shows a WARNING and a NOTIFICATION that the drive is from an unrecognized
  aircraft and is not being processed, takes NO action (no archive, no sync, no
  settings-push, no eject), and LEAVES THE DRIVE MOUNTED for the user to handle
  manually. This is not a hard reject or eject. It is forward-compatible with
  managing multiple aircraft later: once multi-aircraft exists, such a drive would
  instead route to its own aircraft record.
- **Binding**: The association of a physical drive to a configured device. Resolved
  from a populated drive's settings file (by Link_ID), or set explicitly at Prepare
  time for a blank drive and persisted as `device_link_id` in `EFIS_DRIVE_ID.json`.
- **Identity file**: `EFIS_DRIVE_ID.json` at a drive's volume root (constant
  `IDENTITY_FILENAME`), read/written by `read_identity` / `write_identity`, created
  by `_new_identity` in `drive_updater.py`.
- **Device_Config**: The persisted per-device record in `config.py` config:
  friendly name, chart-capable boolean, and Link_ID. Device_Config records live
  under an aircraft record keyed by Mode_S, alongside that aircraft's device count
  and analysis-data-source selection.
- **Display label**: How a device is shown in any UI (Settings surface, device
  pickers, notifications). The default label is `Link N`, where N is the device's
  Link_ID. When the user sets a friendly name, the label is
  `{friendly name} (Link N)` (e.g. `Backup Mini (Link 2)`) so the Link_ID stays
  visible as informational data.
- **Settings file**: A GRT `Settings.bak` / `Settings.dat` backup file written by a
  display to its drive, parsed read-only by `Settings_Parser`.

## Requirements

### Requirement 1: Device configuration model

**User Story:** As a multi-display aircraft owner, I want to declare how many GRT
displays I have and describe each one under my aircraft, so that the DM can tell
the devices apart and manage their drives correctly.

#### Acceptance Criteria

1. THE DM SHALL persist the configuration as an aircraft-keyed map in the
   `config.py` configuration, where each aircraft record is keyed by Mode_S and
   contains that aircraft's device count, Device_Config records, and
   analysis-data-source selection.
2. THE DM SHALL manage exactly one aircraft record in this increment while
   persisting it within the aircraft-keyed map shape so that adding further
   aircraft later requires no change to the stored structure.
3. THE DM SHALL persist, as a per-aircraft attribute of the managed aircraft, a
   device count between 1 and 3 inclusive.
4. WHEN the user sets the managed aircraft's device count outside the range 1 to 3,
   THE DM SHALL reject the value and retain the previous valid device count.
5. THE DM SHALL persist, per configured device of the managed aircraft, a
   Device_Config record containing a friendly name, a chart-capable boolean, and a
   Link_ID.
6. WHERE the user has not entered a friendly name for a device, THE DM SHALL
   default that device's display label to `Link N`, where N is the device's
   Link_ID.
7. WHERE the user has entered a friendly name for a device, THE DM SHALL compose
   that device's display label as `{friendly name} (Link N)`, where N is the
   device's Link_ID.
8. THE DM SHALL store each device's chart-capable value as a user-declared boolean
   without consulting any device-model template or capability catalog.
9. THE DM SHALL assign each configured device a Link_ID in the range 1 to 3 that is
   unique among the managed aircraft's configured devices.
10. IF the user assigns a Link_ID that is already assigned to another configured
    device of the managed aircraft, THEN THE DM SHALL reject the assignment and
    retain the previous Link_ID assignments.
11. THE DM SHALL persist, as a per-aircraft attribute of the managed aircraft,
    exactly one analysis-data-source selection identifying one configured device by
    its Link_ID.
12. THE DM SHALL allow the user to select any configured device as the
    analysis-data-source device without gating the selection on the chart-capable
    value or any other capability.
13. WHEN the user saves the device configuration, THE DM SHALL write the managed
    aircraft record, including its device count, each Device_Config record, and the
    analysis-data-source selection, using `save_config`.

### Requirement 2: Identity resolution on drive insert

**User Story:** As a user, I want the DM to recognize which display a populated
drive came from when I insert it, so that I do not have to identify the drive
manually.

#### Acceptance Criteria

1. WHEN a populated drive is inserted and it contains a settings file, THE DM SHALL
   parse that settings file with `Settings_Parser` and derive its identity with
   `extract_source_key`.
2. WHEN a populated drive contains a readable settings file, THE DM SHALL treat the
   settings-file Link_ID derived by `extract_source_key` as authoritative over any
   `device_link_id` recorded in `EFIS_DRIVE_ID.json`.
3. WHEN `extract_source_key` returns a Link_ID that matches a configured device's
   Link_ID, THE DM SHALL map the inserted drive to that configured device.
4. IF `extract_source_key` returns a Link_ID that does not match any configured
   device's Link_ID, THEN THE DM SHALL report that the drive's display position is
   not configured, SHALL take no archive action and no sync action, and SHALL leave
   the drive mounted.
5. IF the inserted drive contains no readable settings file, THEN THE DM SHALL fall
   back to the drive binding recorded in `EFIS_DRIVE_ID.json` to determine the
   device.
6. IF neither a readable settings file nor a recorded binding identifies a device,
   THEN THE DM SHALL report that the device is undetermined, SHALL take no archive
   action and no sync action, and SHALL leave the drive mounted.

### Requirement 3: Mode_S aircraft verification and learning

**User Story:** As a pilot, I want the DM to learn my aircraft from the first drive
and ignore drives from a different aircraft, so that my aircraft is established
deliberately and another aircraft's settings are never mistaken for mine.

#### Acceptance Criteria

1. WHEN a populated drive is inserted, THE DM SHALL derive the drive's Mode_S with
   `extract_source_key`.
2. WHERE no managed aircraft has been established yet AND the inserted drive's
   derived Mode_S is present, THE DM SHALL prompt the user to confirm adopting that
   Mode_S as the managed aircraft, and SHALL bind it as the managed aircraft ONLY
   upon user confirmation.
3. WHILE no managed aircraft has been established, IF the user declines to confirm
   the prompted Mode_S, THEN THE DM SHALL NOT bind any aircraft, SHALL take no
   archive action and no sync action, and SHALL leave the drive mounted.
4. WHEN the derived Mode_S matches the managed aircraft's Mode_S, THE DM SHALL treat
   the drive as belonging to the managed aircraft and SHALL proceed to device
   resolution by Link_ID.
5. IF the derived Mode_S is present but does NOT match the managed aircraft's Mode_S
   (a different aircraft), THEN THE DM SHALL classify the drive as from an
   unrecognized aircraft, SHALL display a warning and a notification that the drive
   is from an unrecognized aircraft and is not being processed, SHALL take no
   archive action, no sync action, no settings-push action, and no eject action,
   and SHALL leave the drive mounted.
6. IF the derived Mode_S is undetermined (`extract_source_key` returns no Mode_S),
   THEN THE DM SHALL display a message and a notification that the drive's aircraft
   cannot be determined and is not being processed, SHALL take no archive action,
   no sync action, no settings-push action, and no eject action, and SHALL leave
   the drive mounted.

### Requirement 4: Drive-to-device binding

**User Story:** As a user, I want the DM to remember which device each drive is
for, including blank drives I prepare, so that rotation between cards works without
re-identifying drives each time.

#### Acceptance Criteria

1. WHEN a populated drive's settings file resolves to a configured device by
   Link_ID, THE DM SHALL treat that device as the drive's bound device for the
   current operation.
2. WHERE more than one device is configured AND a blank drive is prepared via the
   Prepare Drive flow, THE DM SHALL prompt the user to choose which configured
   device the drive is for.
3. WHEN the user chooses a device for a blank drive during Prepare, THE DM SHALL
   record that device's Link_ID as `device_link_id` in the drive's
   `EFIS_DRIVE_ID.json` using `write_identity`.
4. WHERE exactly one device is configured AND a blank drive is prepared, THE DM
   SHALL bind the drive to that single device without prompting and SHALL record its
   Link_ID as `device_link_id` in `EFIS_DRIVE_ID.json`.
5. WHEN a drive with a recorded `device_link_id` is inserted and its settings file
   is absent or unreadable, THE DM SHALL resolve the drive's device from the
   recorded `device_link_id`.
6. IF a populated drive's settings-file Link_ID conflicts with the drive's recorded
   `device_link_id`, THEN THE DM SHALL resolve the drive to the authoritative
   settings-file Link_ID and SHALL display a message reporting the binding
   mismatch.

### Requirement 5: Setup UI location and behavior

**User Story:** As a user, I want to configure my devices in the DM and keep the
shove-in-and-walk-away workflow, so that managing multiple displays does not add
friction to normal drive insertion.

#### Acceptance Criteria

1. THE DM SHALL present the device configuration controls within the DM menu-bar
   Settings surface (`settings_window.show_settings`).
2. THE DM SHALL allow the user to edit each device's friendly name within the
   Settings surface.
3. WHERE a device has a friendly name, THE DM SHALL display that device as
   `{friendly name} (Link N)` in the Settings surface, in any device picker, and in
   notifications, where N is the device's Link_ID.
4. WHERE a device has no friendly name, THE DM SHALL display that device as
   `Link N` in the Settings surface, in any device picker, and in notifications,
   where N is the device's Link_ID.
5. WHEN a managed drive is mounted during normal operation, THE DM SHALL resolve its
   device automatically without presenting a blocking prompt.
6. WHEN a drive is inserted, THE DM SHALL auto-detect the drive's device using
   identity resolution (Requirement 2) and binding (Requirement 4) without
   requiring the user to open the Settings surface.
7. WHEN the user saves device configuration changes in the Settings surface, THE DM
   SHALL persist the changes with `save_config` and SHALL apply them to subsequent
   drive-insert resolutions.

### Requirement 6: Backward compatibility with single-device installs

**User Story:** As a single-display owner, I want the DM to keep working exactly as
before, so that the multi-device feature does not disrupt the common case.

#### Acceptance Criteria

1. WHERE no multi-device configuration is present, THE DM SHALL operate as a
   single-device install using existing drive-insert behavior.
2. WHEN `load_config` reads a configuration that lacks any device configuration
   keys, THE DM SHALL supply single-device defaults without raising an error.
3. WHEN `load_config` reads an existing flat or implicit single-aircraft
   configuration that predates the aircraft-keyed shape, THE DM SHALL present it as
   a single managed-aircraft record keyed by Mode_S without raising an error, using
   defaults where per-aircraft attributes are absent and binding the Mode_S when it
   becomes known per Requirement 3.
4. WHERE exactly one device is configured, THE DM SHALL resolve every drive
   belonging to the managed aircraft to that single device without prompting for a
   device selection.
5. WHEN a drive is prepared on a single-device install, THE DM SHALL NOT prompt the
   user to choose a device.
6. THE DM SHALL preserve the existing `EFIS_DRIVE_ID.json` schema fields written by
   `_new_identity` when adding the `device_link_id` field, so that drives prepared
   before this feature remain readable.

## Out of Scope / Deferred

The following are explicitly NOT part of this first increment. They consume this
foundation and are tracked in `BACKLOG.md` roadmap items #1 (Multi-device handling)
and #2 (Settings management):

- **Per-device inbound archiving** — archiving returning settings/WP/Plan per
  device, keyed by Link_ID into per-device subdirectories, reusing the v1.5.4
  content-aware archiver (BACKLOG #1 INBOUND).
- **Outbound sync/push** — pushing charts/plates to chart-capable devices,
  NAV.DB/NAV-proc.DB to all devices, and most-recent archived settings to every
  prepped drive; and the DERIVED write-mechanism selection (image+dd for
  chart-capable vs small-file sync otherwise) (BACKLOG #1 OUTBOUND).
- **FDL/logbook enforcement to the analysis-source device** — restricting analysis
  DB ingestion to the one designated analysis-source device. Declaring the source
  is in scope here; enforcing it is deferred (BACKLOG #1 INBOUND).
- **Settings explorer / compare / version-restore** — the dashboard read-only
  explorer, labeled SID diffing, and DM-writes-selected-version restoration
  (BACKLOG #2 capabilities B and C).
- **Divergence warning** — flagging a returning card whose settings are older than
  the DM's latest known-good for that device (BACKLOG #2).
- **Forget / clear the managed aircraft** — a DM action to discard the learned
  managed-aircraft Mode_S and re-enter the learn-on-insert flow (for a sold
  aircraft, a panel swap, or a mislearned Mode_S). This increment only LEARNS
  the managed aircraft; clearing it is aircraft-lifecycle management, adjacent
  to future multi-aircraft keying (BACKLOG roadmap item #1, SOMEDAY).

## Assumptions and Constraints

- **Single managed aircraft, aircraft-keyed shape.** Exactly one aircraft is
  managed in this increment, but the configuration is stored as an aircraft-keyed
  map (`aircraft[Mode_S] -> { devices..., device_count, analysis_data_source }`) so
  multi-aircraft is a non-breaking future extension. There is no aircraft-switcher
  UI and no multi-aircraft management here. Multi-aircraft keying/management is a
  separate deferred item.
- **Maximum of 3 devices per aircraft**, Link_ID values 1/2/3. Device count (1-3)
  and the analysis-data-source selection are per-aircraft attributes.
- **Flight-critical domain.** Correctness and safety are emphasized wherever
  settings identity determines device or aircraft membership.
- **Reuse shipped pieces.** The requirements reference existing mechanisms:
  `extract_source_key` / `Source_Key` / `SourceIdentity` and the SID constants
  (`SID_MODE_S`, `SID_LINK_ID`, `SID_FLIGHT_ID`) in `efis_settings.py`; the
  `EFIS_DRIVE_ID.json` identity file (`read_identity` / `write_identity` /
  `_new_identity`) and `resolve_drive_id` in `drive_updater.py`; `DEFAULT_CONFIG` /
  `load_config` / `save_config` and the existing `settings_import` map in
  `config.py`; and the DM menu-bar app with its Settings and Prepare Drive flows in
  `app.py`.
- **Factual carry-forwards from the parked draft:** SID 387 = Link_ID, SID 1063 =
  Mode_S, SID 1062 = Flight_ID/N-number; both the primary HXr and the Mini A/P write
  settings backups under the SAME filenames (`Settings.bak` / `Settings.dat`), so
  their backups collide on a shared card — the reason device resolution keys on the
  in-file Link_ID rather than the filename. The parked draft's R1/R2 user-prefix and
  redistribute-to-every-drive design is SUPERSEDED by the refined model and is not
  carried forward.
