# Changelog

All notable changes to EFIS Data Manager are documented here. The project uses
a single release version (git tag + `__version__` + `pyproject.toml`) with
independent display labels for the menu-bar tool (`MENUBAR_VERSION`) and the
dashboard (`DASHBOARD_VERSION`).

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
