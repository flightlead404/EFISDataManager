# Changelog

All notable changes to EFIS Data Manager are documented here. The project uses
a single release version (git tag + `__version__` + `pyproject.toml`) with
independent display labels for the menu-bar tool (`MENUBAR_VERSION`) and the
dashboard (`DASHBOARD_VERSION`).

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
