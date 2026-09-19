# Bugfix Requirements Document

## Introduction

Follow-up bug in the shipped v1.5.0 `chart-sync-stall-fix`. The mount-swap
mitigation (unmount the FSKit `/Volumes/<label>` mount and remount via
`/sbin/mount_msdos` for the heavy write window) was wired into the managed-drive
auto-sync path (`app._run_drive_update`) only. The explicit **Prepare Drive**
flows — **Start clean** (reformat + populate) and **Adopt** (non-destructive
adopt + populate) — were never routed through the swap.

As a result, running **Prepare Drive → Start clean** on a fresh USB fails to
populate. Confirmed from live logs (not hypothesis) against drive `EFIS_4`, two
distinct defects combine:

- **Defect 1 — populate runs on the stalling FSKit path.**
  `drive_updater.prepare_drive()` and `drive_updater.adopt_drive()` call
  `update_drive(mount_point, ...)` directly against the raw FSKit
  `/Volumes/<label>` mount, so the from-scratch populate runs on exactly the
  path the stall fix was built to avoid. Observed:
  `rsync scanned failed (exit 11): rsync: /Volumes/EFIS_4/ChartData/: mkpath:
  Permission denied` on the freshly-formatted FSKit mount.

- **Defect 2 — Prepare Drive races the mount-detect auto-sync.** When
  `prepare_drive` reformats and the volume remounts as a MANAGED drive (identity
  file is written before populate), the USB poller (`usb_monitor` →
  `app._on_efis_drive_mounted`) fires on a separate thread → `_run_archive` →
  `_run_drive_update`, which DOES perform the swap. Two operations then run on
  the same drive concurrently: prepare's FSKit populate (defect 1) AND the
  auto-sync's `mount_msdos` swap. The swap unmounts `/Volumes/EFIS_4` out from
  under prepare's later per-family jobs, so those jobs fail with
  `plates/nav: drive mount /Volumes/EFIS_4 is not present`.

Log evidence sequence (`EFIS_4`): Formatting → "Wrote identity" → "Syncing
scanned..." (FSKit) → `mkpath: Permission denied` → "USB monitor paused
(mount-swap window)" + "swapped /Volumes/EFIS_4 to private mount" (the auto-sync
thread) → "plates: drive mount not present" → "nav: drive mount not present".

Impact: a fresh drive cannot be prepared by Prepare Drive at all. `EFIS_4` is
currently being salvaged only because the auto-sync fallback happens to complete
the populate; the fix must make Prepare Drive work correctly on its own for the
two remaining fresh test drives.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN Prepare Drive → Start clean (or Adopt) populates a drive THEN
`prepare_drive`/`adopt_drive` call `update_drive` against the raw FSKit
`/Volumes/<label>` mount, so the heavy write runs on the stalling FSKit path and
fails (observed `rsync ... mkpath: Permission denied`, exit 11) instead of using
the in-kernel `mount_msdos` path.

1.2 WHEN `prepare_drive` writes the identity file before populate and the
freshly-formatted volume remounts THEN the mount-detect poller
(`_on_efis_drive_mounted`) treats it as a managed drive and unconditionally
launches `_run_archive` → `_run_drive_update` on a separate thread, so a second
update runs concurrently with the in-progress prepare on the same drive.

1.3 WHEN the concurrent auto-sync (from 1.2) enters its mount swap THEN it
unmounts `/Volumes/<label>` out from under prepare's remaining per-family jobs,
so those jobs fail with `drive mount /Volumes/<label> is not present`.

### Expected Behavior (Correct)

2.1 WHEN Prepare Drive → Start clean (or Adopt) populates a drive THEN the
system SHALL run the populate write window through the mount swap (the
`update_drive` populate call SHALL execute inside an `msdos_mount(mount_point,
...)` context) so the heavy write uses the non-stalling in-kernel `msdosfs` path,
exactly as `_run_drive_update` does.

2.2 WHEN a Prepare Drive / Adopt operation is in progress for a drive THEN the
system SHALL NOT concurrently launch archive + auto-sync (`_run_archive` /
`_run_drive_update`) for that same drive from the mount-detect handler, so only
one operation writes to the drive at a time. This guard SHALL cover the entire
prepare span (format → identity → populate), because prepare's own
reformat+remount trips the poller.

2.3 WHEN the mount swap cannot be established during a prepare/adopt populate
(`MountSwapError`) THEN the system SHALL report the prepare/adopt as failed and
leave the drive in a safe, usable state (FSKit mount restored, no silent
unmount), rather than proceeding with an unsafe write.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a managed EFIS drive is mounted normally (no prepare/adopt in progress)
THEN the system SHALL CONTINUE TO run `_on_efis_drive_mounted` → `_run_archive`
→ `_run_drive_update` with its existing mount swap and auto-sync behavior
unchanged.

3.2 WHEN `prepare_drive`/`adopt_drive` run THEN the system SHALL CONTINUE TO
write the identity file before populate (so the drive resolves its id), and
CONTINUE TO apply the existing verify / commit-marker / sync-state semantics via
`update_drive` unchanged.

3.3 WHEN a prepare/adopt operation completes (success or failure) THEN the
system SHALL CONTINUE TO leave the drive on its restored FSKit `/Volumes/` mount
so Finder/eject and subsequent normal auto-sync behave as before, and SHALL
resume the mount-detect handler for that drive.

3.4 WHEN `update_drive`, `msdos_mount`, `usb_monitor.pause()/resume()`, and the
existing `_run_drive_update` swap window are exercised THEN the system SHALL
CONTINUE TO behave exactly as in v1.5.0 for all inputs that do not involve the
prepare/adopt populate path (their signatures and behavior are preserved).
