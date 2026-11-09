# Changelog

All notable changes to EFIS Data Manager are documented here. The project uses
a single release version (git tag + `__version__` + `pyproject.toml`) with
independent display labels for the menu-bar tool (`MENUBAR_VERSION`) and the
dashboard (`DASHBOARD_VERSION`).

## v1.7.1

Release version 1.7.1 · menu bar 0.15.0 · dashboard 1.3.1

### Fixes

- **Alert chips on the flight page showed "psi" for every non-temperature
  parameter.** RPM, Vne and G-load exceedances were labelled psi. Each episode
  now carries its own unit from the analysis (RPM, kt, g, °F, psi, °/min), and
  the page uses it instead of guessing from the parameter name.

### Documentation

- Logged a known limitation: GRT's temperature-units setting (SID 345) has five
  values and only Fahrenheit and Celsius are handled by settings import and
  percent power. Rare; recorded in the settings-import design and the backlog.
- Backlog: enforce the analysis data source for FDL/logbook import, and skip the
  repeat archive pass after a relabel remount.

## v1.7.0

Release version 1.7.0 · menu bar 0.15.0 · dashboard 1.3.0

Settings management: each display now has its own dated settings history, a
returning card is checked against it before archiving, and the dashboard can
browse, compare, and restore archived settings.

### Per-device settings archive

- **Settings, WP, and Plan are archived per display.** A resolved drive archives
  into `Settings/link{N}/`; an unresolved drive into `Settings/unresolved/`.
  Archiving stays content-aware (no new snapshot when content is unchanged) and
  same-day changes are sequenced, never overwritten.
- **Existing archives are adopted automatically.** On first launch a one-time
  migration moves flat `Settings/` snapshots into the per-device layout, routing
  each by the Link_ID in its own content. Legacy Plan/WP files carry no identity
  and land in `unresolved/` (documented in the design).

### Confirm before archiving a differing card

- **The menu-bar tool asks before archiving settings that differ from the latest
  known-good version.** The prompt names the display and the latest archive
  date, and says whether the card matches an older archived version (out of
  sequence, or changed and changed back) or is new content. "Skip settings"
  leaves the settings archive and card unchanged; flight data, logbook, and the
  rest still archive. No prompt when the card matches, or there is no history.

### Writing settings to a card

- **Populate Settings (menu bar)** writes the display's latest archived Settings
  to the card, or reports that the card is already current.
- **Restore (dashboard)** writes any archived version from the display's history
  to the card bound to that display.
- Both copy the archived file's bytes unchanged (checksum lines stay valid),
  verify the write by SHA-256, and only then remove the other `.bak`/`.dat` slot,
  so the card is left with exactly one Settings slot.

### Dashboard settings explorer

- **Browse, view, and compare archived settings per display**, including
  comparing a version against the card in the reader.
- **Settings are labeled from GRT's own reference.** GRT's `Settings.h` and
  settings-format document ship with the app; every SID in a Settings file now
  has a label, about 60 show what their value means (e.g. Aux1 Function
  `3 — Amp`), and GRT's internal name is shown on hover.

### Fixes

- **Settings import and percent-power find backups again after migration.** Both
  now read the per-device folders.
- **Import considers only the primary display (Link_ID 1).** A newer backup from
  another display no longer hides the primary's latest backup.
- **Property tests no longer flake on macOS.** Generated file names are limited to
  single-case FAT32-safe characters, matching the EFIS cards, so case-insensitive
  collisions no longer break them.

## v1.6.7

Release version 1.6.7 · menu bar 0.14.5 · dashboard 1.2.3

Foundation for managing a 1-3 display GRT aircraft: the EFIS Data Manager now
learns which aircraft it manages, knows each display by position, and routes an
inserted drive to the right device automatically. This is the groundwork that
later per-device archiving, chart/settings push, and restore build on; those
consumer features are not in this release.

### Device identity and aircraft learning

- **The manager now has a per-aircraft, per-device model.** Configuration gained
  an aircraft-keyed record: the managed aircraft is identified by its Mode_S, and
  each display is a device keyed by its position (Link_ID 1-3) with a
  user-declared friendly name and a chart-capable flag. A one-time, crash-safe
  migration adds the new structure to existing configs without touching any other
  setting.
- **The aircraft is learned on first insert, only with your confirmation.** When
  no aircraft is yet established, inserting a drive whose settings carry a Mode_S
  prompts to adopt that aircraft; declining leaves the drive mounted and changes
  nothing. A drive from a different Mode_S is reported as foreign and left alone.

### Automatic drive-to-device resolution on insert

- **An inserted drive is matched to the correct display automatically.** A new
  resolution step reads the drive's settings and recorded binding and classifies
  it: resolved (proceed as before), needs-learn, unrecognized aircraft,
  undetermined aircraft, unconfigured device, or undetermined device. Only a
  confidently resolved drive proceeds to the existing archive/sync behavior; every
  other case notifies the specific reason and leaves the drive mounted with no
  action.
- **The settings-file position wins.** When a drive carries a settings Link_ID it
  is authoritative for which device the drive belongs to; a differing recorded
  binding is reported as a mismatch but resolution still follows the settings
  file. When no settings Link_ID is present, the recorded binding is used as the
  fallback.

### Settings UI and blank-drive binding

- **Device setup in Settings.** The Settings window gained a device-count control
  (1-3), a per-device row (friendly name, chart-capable, Link_ID), and a
  single-select analysis-data-source. Invalid edits (a count outside 1-3, a
  duplicate Link_ID) are rejected and the previous valid values retained.
- **Blank drives are bound at Prepare time.** Preparing a blank drive records
  which device it is for: a single-device install binds without asking; a
  multi-device install prompts for the device. The binding is written into the
  drive identity file, whose schema advanced to v2 while remaining readable for
  older v1 drives.

### Tests

- Added `device_resolution.py` with ten Hypothesis properties (migration
  idempotency and key preservation, label composition, authoritative settings
  Link_ID, recorded-binding fallback, aircraft-gated resolution, unconfigured
  Link_ID, determinism, validation reject-and-retain, learn-only-on-confirmation)
  plus the full eight-row resolution matrix, identity-file v1 compatibility, the
  migration chain, and Prepare-binding integration tests.

## v1.6.6

Release version 1.6.6 · menu bar 0.14.4 · dashboard 1.2.3

The legacy `rsync --size-only --delete` whole-tree chart-sync mechanism is
retired. Every chart-family transfer now runs the SHA-256 manifest
content-hash diff, closing a silent wrong-tile flight-safety gap.

### Chart sync is now manifest-only on every path

- **Normal insert, adopt populate, and interrupted-sync repair all use the
  content-hash manifest diff.** Previously an interrupted-sync repair forced a
  whole-tree `rsync --size-only --delete` job, and a corrupt or missing manifest
  fell back to the same size-only transfer. Now `_chart_family_job` always
  drives transfers from a `--files-from` list derived from the SHA-256 manifest
  diff, and a missing or corrupt local manifest is rebuilt (hashed from the SSD
  family tree, with the scanned `Plates/` prune applied) rather than triggering
  a size-only fallback.

- **Interrupted-sync repair diffs against a fresh rescan of the card.** Repair
  now builds the local manifest against a fresh content-hash rescan of the card
  (not the possibly-stale on-disk card manifest), so the transferred set equals
  the true local-vs-card difference.

- **A same-byte-size content change is now detected and transferred.** Because
  `--size-only` compared file sizes only, a tile edited to an identical byte
  size could be left stale on the card — the wrong chart tile with no signal.
  The content-hash diff catches it and transfers it.

- **Build/rescan failures fail safe.** If the local manifest build or the card
  rescan fails, the family is left pending (interrupted marker intact) and an
  error is surfaced; there is no size-only fallback. A clean, already-current
  insert still performs no card rescan.

### Tests

- Added `tests/test_manifest_only_chart_sync.py` (exploration test pinning the
  old size-only repair defect and the same-size change case),
  `tests/test_build_local_manifest.py`, `tests/test_manifest_only_preservation.py`,
  and `tests/test_manifest_only_error_paths.py`; updated the existing sync,
  repair, and invariant suites to match the manifest-only behavior.

## v1.6.5

Release version 1.6.5 · menu bar 0.14.3 · dashboard 1.2.3

Currency-check hardening and a sync-state key fix, found during chart-capable
drive testing.

### Quick currency check no longer trusts the commit marker alone

- **A short-filled chart family is now caught.** The quick currency check added
  a cheap file-count cross-check against the local (SSD) manifest for the
  scanned and plates families, applying the same exclude rules the sync uses
  (commit marker + macOS metadata) so the comparison is exact. If the drive's
  count does not match the expected set, the family is marked not-current and
  re-synced — previously a matching commit marker alone could report a drive
  "current" while it was missing thousands of tiles. The check is a fast
  negative only: a count match does not by itself declare currency (a same-size
  content change still requires the content-hash check), and a missing local
  manifest fails safe to not-current.

### Interrupted-sync key canonicalized

- **A completed repair now reliably clears its interrupted-sync record.** When a
  drive has no stable identity and the sync-state key falls back to the mount
  path, that path is now canonicalized, so the record written at the start of a
  sync and cleared at the end always use the same key even when the mount is
  reached via a symlinked path.

### Tests

- Added autouse isolation of the local manifest store so unit tests never read
  a real on-disk manifest.

## v1.6.4

Release version 1.6.4 · menu bar 0.14.2 · dashboard 1.2.3

Three drive-handling / analysis fixes found during chart-capable adopt testing.

### Interrupted-sync state is keyed by a stable drive id

- **Fixed a drive wedged in a permanent "resuming interrupted sync" loop.**
  Interrupted-sync state was keyed through `resolve_drive_id`, which returned no
  id (falling back to the mount path) whenever the card manifest had not yet
  been written. Because the drive label — and the mount path derived from it —
  changes with every chart/plate cycle, and the card manifest does not exist
  until the first successful sync, the same physical drive recorded and cleared
  its sync state under different keys, so a completed family was never cleared.
  A new `resolve_sync_state_key` keys sync state on the static identity `id`
  from `EFIS_DRIVE_ID.json` (written once at prepare/adopt, independent of the
  label), so a completed sync is reliably marked complete.

### Fuel used no longer counts a refuel as burn

- **Fixed grossly overstated fuel burn after a refuel.** The EFIS fuel-total
  gauge counts down as fuel burns but steps up when reset after a refuel (the
  normal procedure after engine start). Fuel used was computed as
  first-minus-last of the gauge, which counted the reset step as burned fuel (a
  ~15-minute flight once reported 20+ gallons). It is now the sum of the
  gauge's decreases, which ignores refuel resets while matching the old value
  exactly for flights with no reset. A one-time migration recomputes fuel used
  for already-imported flights.

### Status no longer falsely reads idle mid-sync

- **Fixed the menu bar showing idle while a sync was still running.** A
  concurrent chart/software/nav check could reset the status line to idle while
  a drive sync was in progress, so the menu bar looked done when it was still
  writing. The status line now ignores an idle reset from a background check
  while a sync is active.

## v1.6.3

Release version 1.6.3 · menu bar 0.14.1 · dashboard 1.2.3

Reworked how charts and the nav database are written, plus two drive-handling
fixes.

### Write mechanism

- **Removed the image-compose + raw `dd`-to-slice write path and the mount-swap
  layer.** The "FSKit write stall" premise that motivated them was never
  confirmed. Writing now goes directly to the normal mounted volume.
- **Removed the false-aborting write-stall detector.** A long, silent
  receiver-side scan over a large existing chart tree on a slow USB card is
  healthy, not wedged; the detector was cancelling good syncs. Drive-removal and
  sleep aborts remain handled by the mount watchdog.
- **Wired the SHA-256 chart manifest into the live sync.** After the first sync
  builds the manifest, subsequent syncs transfer only the changed files
  (detected by content hash, so a same-size edit is no longer missed), with the
  legacy whole-tree reconcile kept as a self-healing fallback.

### Drive handling

- **Stale "Not connected" after an adopt relabel.** Adopting a drive relabels
  its volume, which the monitor sees as an eject of the old name; the eject
  handler now reconciles against what is actually mounted instead of blanking
  the status while a managed drive is still present.
- **Adopt now records the device binding.** A drive adopted for a nav-only
  device now stores its device link id, so it is recognized on re-insert instead
  of reporting the device as undetermined.

## v1.6.0

Release version 1.6.0 · menu bar 0.14.0 · dashboard 1.2.3

Image-based drive write (later superseded).

- **Image-compose + raw `dd` drive write.** Composed the entire FAT32 drive
  filesystem locally and wrote it to the drive's raw partition in one pass, with
  raw-readback hash verification and no mounted write. This replaced the
  mount-swap write path. NOTE: this subsystem and the mount-swap layer were both
  removed in v1.6.3 after the write mechanism was reworked around a direct rsync
  to the mounted volume; the stall premise that motivated them was never
  confirmed.

## v1.5.6

Release version 1.5.6 · menu bar 0.13.0 · dashboard 1.2.0

- **FDL operation segmentation fix.** The importer treated one FDL CSV as
  exactly one operation (one engine start/stop cycle). Files containing multiple
  start/stop cycles were mis-segmented; imports are now split into distinct
  operations by the gaps between recording sessions, so each engine run becomes
  its own operation.

## v1.5.1

Release version 1.5.1 · menu bar 0.12.1 · dashboard 1.2.0

- **Mount-swap also applied to Prepare Drive (later superseded).** The v1.5.0
  write-stall mitigation was wired only into the managed-drive auto-sync path;
  the explicit Prepare Drive flow still wrote through the stalling mount. This
  extended the mitigation to Prepare. NOTE: the mount-swap layer was removed in
  v1.6.3.

## v1.5.0

Release version 1.5.0 · menu bar 0.12.0 · dashboard 1.2.0

- **Chart-sync write-stall mitigation (later superseded).** Large or heavy write
  sessions to the FAT32 USB drive could stall. This release unmounted the FSKit
  volume and remounted it via `/sbin/mount_msdos` for the write window. NOTE:
  this mount-swap approach was removed in v1.6.3 in favor of a direct rsync to
  the mounted volume plus removal of the false-aborting stall detector.

## v1.4.0

Release version 1.4.0 · menu bar 0.11.0 · dashboard 1.2.0

Dashboard and menu-bar feature work.

- **Notification history.** Menu-bar notifications auto-dismissed before they
  could be read, with no way to review them. Added a persisted, reviewable
  notification history.
- **Oil-age warning.** Surfaces engine oil hours (from the EFIS `STID_OIL_HOURS`
  state value) so oil age no longer has to be tracked by hand.
- **Percent power.** Computes percent power — absent from the GRT HXr FDL feed —
  from the aircraft's engine power map so it is available for leaning and cruise
  analysis.
- **EFIS settings import (begun).** Reads the GRT HXr settings backup
  (Settings.bak / Settings.dat) to populate the dashboard's alert thresholds
  from the values configured in the aircraft instead of hand-entering them.
- **Multi-device settings foundation (begun).** Aircraft-keyed configuration and
  per-device drive resolution by Link_ID, developed incrementally through the
  v1.6 series.

> Note: entries for v1.5.2–v1.5.5, v1.6.1, and v1.6.2 are not reconstructed here
> — no spec maps confidently to them, and they are omitted rather than guessed.

## v1.3.0

Release version 1.3.0 · menu bar 0.9.0 · dashboard 1.1.0

Analysis dashboard: airspeed and G-load limit alerting, plus a GAMI plot zoom
fix.

### Alerts (analysis dashboard)

- **Vne (never-exceed) alerting.** Flags any recorded exceedance of the
  never-exceed speed. Checked against TRUE airspeed, because Vne is a
  flutter/TAS limit on many aircraft (incl. the RV-8) — an indicated-airspeed
  check would be non-conservative at altitude. Warning-only (no caution band).
  Default 200 KTAS (RV-8 230 mph TAS); configurable in Settings, or set to 9999
  to disable. The EFIS handles the in-cockpit indicated-Vne display; this is a
  post-flight exceedance check on the recorded data.
- **G-load alerting, positive and negative.** Each direction has a caution and
  a limit (a caution flags an early excursion; reaching the limit is a warning).
  Defaults +3.0/+3.8 g and -1.5/-3.0 g; set to your aircraft's weight/category
  in Settings (e.g. RV-8 +6/-3 g at aerobatic gross).
- Airspeed and G exceedances appear both as timestamped per-flight episodes
  (with jump-to markers) and in the cross-flight Alerts summary.

### GAMI lean test plot

- **X-only zoom.** Zooming/panning the EGT-vs-fuel-flow plot now affects only
  the fuel-flow axis; EGT auto-fits to the visible window, matching the flight
  plots. Zooming in on closely-grouped peak EGTs spreads them out instead of
  running the data off the top of the chart.

## v1.2.1

Release version 1.2.1 · menu bar 0.9.0 · dashboard 1.0.0

Follow-up to 1.2.0 after a full end-to-end drive populate validated against the
official Seattle Avionics Windows tool (which reported the synced drive fully
current).

- **Removed the volume-root metadata sweep.** macOS writes a hidden AppleDouble
  companion file (`._<name>`) next to each file on FAT32/exFAT drives, because
  those filesystems can't store the `com.apple.provenance` attribute macOS
  stamps on every file. This can't be prevented on the copy or the source (the
  attribute is not removable on current macOS), and sweeping the files after a
  sync only adds time — they reappear the moment a file is rewritten. The sweep
  is gone; the sidecars are left in place (harmless to the EFIS, which ignores
  them).
- **Docs:** README and Getting Started now explain the `._*` companion files and
  why a full first-time populate is slow (roughly one extra file written per
  chart). Routine incremental updates only rewrite changed files, so they stay
  fast.

## v1.2.0

Release version 1.2.0 · menu bar 0.9.0 · dashboard 1.0.0

Hardening release driven by real-world Prepare Drive + full-sync testing on
hardware. Fixes the root cause of chart-sync stalls, adds automatic recovery
for a wedged drive, makes Prepare Drive clearer and safer, and stops a
misleading nav-database "parse error" false alarm. Adds a short Getting Started
guide.

### Sync reliability (menu-bar tool)

- **Fixed the chart-sync stall (root cause).** rsync's per-file output was
  written to OS pipes that were only drained after the process exited. On a
  large chart set that pipe filled and rsync blocked mid-transfer — appearing
  as a hung sync (drive still mounted, 0 bytes/s, no error). rsync output now
  streams to temp files, so the transfer runs to completion regardless of how
  many files it reports. This was misdiagnosed in the field as a failing USB
  drive, a dock, or a VM USB conflict; it was none of those.
- **Stall detection for a wedged-but-mounted drive.** A drive that stops
  accepting writes while staying mounted (flaky USB link, controller hang,
  hypervisor USB contention) is not caught by the mount-removal watchdog. Each
  family sync now aborts with a clear "sync stalled" error if it makes no
  progress for a set window, instead of hanging indefinitely.

### Prepare Drive (menu-bar tool)

- **Correct whole-disk format.** `diskutil eraseDisk` is now always given the
  whole-disk identifier (resolved via `ParentWholeDisk`), never a partition
  slice, and format failures are logged instead of failing silently.
- **Format progress feedback.** The (blocking) format step now shows an
  elapsed-time heartbeat so a slow format no longer looks like a hang, with a
  generous safety timeout.
- **Clearer prompts.** The first prompt is now "Select Drive to Prepare"
  (choosing which drive), clearly distinct from the later "Choose Label" step,
  which is pre-filled with the drive's current label suffix.
- **Label fix.** Entering a value that already starts with `EFIS_` (e.g. the
  drive's full current label) no longer double-prefixes to `EFIS_EFIS_1`.
- **Data-cycle provenance.** The drive's data cycle is stamped into the identity
  file at prepare/adopt time, so provenance is present even if the first
  populate is interrupted.
- **Volume-root metadata cleanup.** macOS AppleDouble `._*` / `.DS_Store`
  sidecars are now swept from the volume root after provisioning (the per-family
  purge only covered the chart tree).

### Currency checks (menu-bar tool)

- **No more nav-DB "parse error" false alarm.** A transient incomplete page load
  or bot-protection challenge on the GRT nav-database page is now treated as a
  soft "couldn't check, will retry" instead of a hard error implying the page
  layout changed. The page fetch also waits for the data to actually render
  (network-idle + element wait) rather than a fixed sleep, so the check is far
  less likely to catch a half-loaded page.
- **Charts-current notification on startup.** The startup check now reports
  "Charts Current" alongside the nav DB and software checks; the routine daily
  chart check stays silent when nothing changed.

## v1.1.0

Release version 1.1.0 · menu bar 0.8.0 · dashboard 1.0.0

This release makes the chart-drive sync trustworthy and fixes a class of
real-world failures found in the field. The drive updater was reworked from a
single monolithic checksum rsync into atomic, verifiable, resumable per-family
sync jobs, and drives are now tracked by a durable identity so a multi-drive
rotation can never confuse one drive's state with another.

### Drive sync integrity (menu-bar tool)

- **Per-family sync jobs.** Scanned/en-route charts, approach plates, and the
  nav database are now synced as independent jobs, each with its own commit
  marker written last. One family can be current while another is mid-sync or
  failed; a failure in one never invalidates another.
- **Commit-marker atomicity.** A family's currency marker is written only after
  its payload is fully copied and verified (count + size), so a "current"
  marker reliably means the whole product set is complete.
- **Fast routine sync.** Replaced the full-content checksum rsync (which
  routinely ran for an hour and hit a timeout reported as a false error) with a
  size + modification-time delta and a FAT/exFAT modify-window. No fixed
  wall-clock timeout; a large-but-healthy transfer is never failed as an error.
- **Exhaustive verify + repair.** New on-demand "Verify Drive" menu action does
  a count + size walk and reports per-family results. After an interruption the
  app runs verify + repair before trusting the drive again.
- **Interruption handling.** A mount-presence watchdog aborts a sync safely if
  the drive is removed mid-copy, leaving a durable interrupted-sync marker so
  the next mount forces verify + repair. Sleep/wake is handled as a
  stop-then-resume special case (NSWorkspace observers).
- **macOS metadata purge.** AppleDouble `._*` sidecar files that had silently
  accumulated on drives (tens of thousands) are now actively purged, while the
  sibling family's subtree, the legacy directory, and the commit marker are
  protected from deletion.
- **Consistent status/errors.** Every surfaced error is logged so the status
  line and the Recent Errors panel agree; successful terminal states clear back
  to "Drive current" with no sticky error strings.

### Multi-drive rotation (menu-bar tool)

- **Durable per-drive identity.** Each managed drive carries a visible
  `EFIS_DRIVE_ID.json` at its root with an app-generated id, so an arbitrary
  number of drives (current, an n-1 spare in the airplane, and one at the Mac)
  are tracked independently. Interrupted-sync state follows the physical drive
  by id, never by mount path — fixing mis-attribution when macOS reassigns
  `/Volumes/EFIS`, `/Volumes/EFIS_1`, etc.
- **Lazy adoption.** An existing EFIS drive without an identity file is adopted
  (an id is written) on first sight; `Prepare Drive` writes a fresh identity as
  part of provisioning.
- **Provenance/telemetry.** The identity file records last-sync timestamps,
  result, a per-drive sync count, and the data cycle the drive was brought to.
  Provenance never affects currency decisions — the on-drive markers and
  payload remain the sole source of truth.
- **Fail-safe.** If a drive's identity cannot be resolved, the app never applies
  another drive's state to it; it falls back to the marker-based currency check
  and logs a warning.
- **Docs.** README now covers using multiple drives, the recommendation to give
  each a unique label, and the known limitation that connecting two EFIS drives
  at once is unsupported.

### Fixes

- Fixed a syntax error that prevented the app from launching from the
  Applications icon after an interrupted edit.
- Fixed a verify bug that counted macOS `._*` metadata as spurious drive
  discrepancies (wildcard exclude patterns are now honored).
- Fixed a mount-readiness race and a sticky mount-watchdog latch that could
  cause a false "drive removed" abort immediately on re-mount.
