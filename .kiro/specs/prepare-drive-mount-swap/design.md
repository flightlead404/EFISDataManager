# Prepare Drive Mount-Swap Bugfix Design

## Overview

The v1.5.0 `chart-sync-stall-fix` moved the heavy chart-write window off the
stalling FSKit `/Volumes/<label>` mount and onto an in-kernel `msdosfs` mount
established by `mount_swap.msdos_mount(...)`. That wiring landed in exactly one
place: `app._run_drive_update` (the managed-drive auto-sync path). The explicit
**Prepare Drive** flows never got it.

This bugfix closes two gaps that together break **Prepare Drive → Start clean**
on a fresh USB:

1. **Route the prepare/adopt populate through the mount swap** so the heavy
   from-scratch write uses the non-stalling in-kernel `msdosfs` path instead of
   the raw FSKit mount. The chosen approach wraps the `update_drive` populate
   call *inside* `drive_updater.prepare_drive`/`adopt_drive` in an
   `msdos_mount(mount_point, poller=poller)` context, taking an optional
   `poller` passthrough the app supplies. This keeps the invariant "populate
   always goes through the swap" in `drive_updater` and requires no app import
   in `drive_updater`.

2. **Serialize prepare/adopt against the mount-detect auto-sync** so the two
   never write the same drive concurrently. An app-level "operation in progress"
   guard set (keyed by drive/mount) causes `_on_efis_drive_mounted` to skip
   launching archive + auto-sync while a prepare/adopt is active for that drive.
   The guard covers the whole format → identity → populate span, because
   prepare's own reformat+remount trips the poller before populate even starts.

The fix is deliberately minimal and additive: it reuses `msdos_mount` and
`update_drive` unchanged, and it does not touch the sync engine, verify, marker,
or sync-state semantics. Per the versioning policy this is a menu-bar-tool
bugfix: on release bump `MENUBAR_VERSION` and `__version__` patch (v1.5.1) and
keep `pyproject.toml` in sync; leave `DASHBOARD_VERSION` unchanged.

## Glossary

- **Bug_Condition (C)**: The condition that triggers the bug — a prepare/adopt
  populate that (a) writes through the raw FSKit mount instead of the swap,
  and/or (b) runs concurrently with the mount-detect auto-sync for the same
  drive.
- **Property (P)**: The desired behavior — the populate write runs inside an
  `msdos_mount` context, and while a prepare/adopt is in progress for a drive
  the mount-detect handler does not launch a second update on that drive.
- **Preservation**: Managed-drive auto-sync behavior, identity-before-populate,
  and the existing verify/marker/sync-state semantics must remain unchanged for
  inputs that do not involve the prepare/adopt populate path.
- **F**: The original (unfixed) functions — `prepare_drive`/`adopt_drive` call
  `update_drive(mount_point, ...)` on the raw FSKit mount;
  `_on_efis_drive_mounted` unconditionally launches archive + auto-sync.
- **F'**: The fixed functions — `prepare_drive`/`adopt_drive` wrap the populate
  in `msdos_mount(mount_point, poller=poller)`; `_on_efis_drive_mounted`
  consults an operation-in-progress guard before launching auto-sync.
- **`prepare_drive`**: `drive_updater.prepare_drive(volume_path, label,
  progress_callback)` — formats the disk, `_ensure_identity`, then
  `update_drive` to populate.
- **`adopt_drive`**: `drive_updater.adopt_drive(mount_point, progress_callback)`
  — writes identity (no format), then `update_drive` to populate.
- **`update_drive`**: `drive_updater.update_drive(mount_point, families=None,
  progress_callback=None, is_aborted=None)` — the unchanged sync engine
  entry point.
- **`msdos_mount`**: `mount_swap.msdos_mount(fskit_mount_point, poller=None)` —
  context manager yielding the private in-kernel `msdosfs` work mount, restoring
  FSKit on exit; raises `MountSwapError` on failure.
- **`_run_drive_update`**: `app._run_drive_update(mount_point)` — the
  managed-drive auto-sync path that ALREADY wraps its write window in
  `msdos_mount(mount_point, poller=self._usb_monitor)`; the reference pattern.
- **`_on_efis_drive_mounted`**: `app._on_efis_drive_mounted(mount_point)` — the
  mount-detect handler that launches `_run_archive` → `_run_drive_update`.
- **operation-in-progress guard**: an app-level set of drive keys currently
  being prepared/adopted; consulted by `_on_efis_drive_mounted` to suppress the
  concurrent auto-sync.

## Bug Details

### Bug Condition

The bug manifests whenever a Prepare Drive (Start clean) or Adopt operation
populates a drive. Two independent defects hold:

- **Defect 1 (no swap):** `prepare_drive`/`adopt_drive` invoke
  `update_drive(mount_point, ...)` directly against the raw FSKit
  `/Volumes/<label>` mount. The populate write therefore runs on the stalling /
  permission-failing FSKit path (`mkpath: Permission denied`, rsync exit 11) —
  it is NOT wrapped in an `msdos_mount` context the way `_run_drive_update` is.

- **Defect 2 (race):** because `prepare_drive` writes the identity file before
  populate, the remounted volume is a MANAGED drive; the 2s USB poller fires
  `_on_efis_drive_mounted`, which unconditionally launches `_run_archive` →
  `_run_drive_update` on a separate thread. That auto-sync performs its own
  mount swap, unmounting `/Volumes/<label>` out from under prepare's remaining
  per-family jobs (`drive mount /Volumes/<label> is not present`).

**Formal Specification:**
```
FUNCTION isBugCondition(op)
  INPUT: op describing a prepare/adopt populate on a managed EFIS drive
  OUTPUT: boolean

  RETURN op.kind IN {"prepare", "adopt"}
         AND (
           // Defect 1: populate not wrapped in the mount swap
           NOT op.populateRanInsideMsdosMount
           OR
           // Defect 2: mount-detect auto-sync launched concurrently for the
           // same drive while the prepare/adopt was in progress
           op.concurrentAutoSyncLaunchedForSameDrive
         )
END FUNCTION
```

`isBugCondition` is true on the CURRENT (unfixed) code for every prepare/adopt:
the populate always runs on the raw FSKit mount, and the guard that would
suppress the concurrent auto-sync does not exist.

### Examples

- **Fresh USB, Start clean (observed on `EFIS_4`):** format → "Wrote identity"
  → `update_drive` runs `rsync` on `/Volumes/EFIS_4/ChartData/` (FSKit) →
  `mkpath: Permission denied` (exit 11). Expected: `update_drive` runs on the
  private `mount_msdos` work mount and populates cleanly.
- **Same run, race (observed on `EFIS_4`):** the poller sees the managed remount
  and launches auto-sync; "swapped /Volumes/EFIS_4 to private mount" (auto-sync
  thread) unmounts the FSKit volume; prepare's later jobs report `plates: drive
  mount not present`, `nav: drive mount not present`. Expected: no concurrent
  auto-sync launches while prepare is in progress for that drive.
- **Adopt of an existing GRT/Windows drive:** identity written, then
  `update_drive` runs on the raw FSKit mount (same defect 1). Expected: the
  incremental populate runs inside the swap.
- **Edge — swap cannot be established:** `msdos_mount` raises `MountSwapError`
  (device node unresolvable, unmount/`mount_msdos` failed). Expected:
  prepare/adopt reports failure, the drive is left on its restored FSKit mount
  and remains usable — the populate is NOT attempted on the raw FSKit mount as a
  silent fallback.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Managed-drive auto-sync on a normal mount (no prepare/adopt in progress):
  `_on_efis_drive_mounted` → `_run_archive` → `_run_drive_update` with its
  existing mount swap and poller quiescing, unchanged.
- Identity written before populate in `prepare_drive`/`adopt_drive` (so the
  drive resolves its durable id during `update_drive`).
- The sync engine: `build_jobs` → `sync_payload` → `verify_family` → commit
  marker → sync-state, all keyed by durable `drive_id`, unchanged.
- The `msdos_mount` context manager's own lifecycle/teardown ("never left
  orphaned") and `usb_monitor.pause()/resume()` semantics, unchanged.
- On completion (success or failure), the drive ends on its restored FSKit
  `/Volumes/` mount so Finder/eject and future auto-sync behave normally, and
  the mount-detect handler resumes for that drive.

**Scope:**
All inputs that do NOT involve the prepare/adopt populate path should be
completely unaffected by this fix. This includes:
- Managed-drive auto-sync triggered by a normal (non-prepare) mount event.
- The `update_drive` / `msdos_mount` / `usb_monitor` public behavior and
  signatures (the fix adds an optional `poller` passthrough to
  `prepare_drive`/`adopt_drive`; it does not change `update_drive` or
  `msdos_mount`).
- Verify-only re-checks, eject, and every non-populate drive operation.

## Hypothesized Root Cause

This root cause is **CONFIRMED from live logs**, not hypothesized, but the
contributing factors are:

1. **Incomplete wiring of the v1.5.0 mount swap.** The swap was integrated only
   into `app._run_drive_update`. `drive_updater.prepare_drive`/`adopt_drive`
   were left calling `update_drive(mount_point, ...)` on the raw FSKit mount, so
   the from-scratch populate — the single heaviest write in the app — runs on
   the exact path the fix was meant to avoid.

2. **Identity-before-populate turns the drive "managed" mid-prepare.**
   `_ensure_identity` writes the identity file before populate (correct, and
   must be preserved), which makes the remounted volume a MANAGED drive that the
   poller auto-syncs. There is no guard telling `_on_efis_drive_mounted` that a
   prepare/adopt already owns this drive.

3. **Two writers, one drive.** The auto-sync's swap unmounts `/Volumes/<label>`
   while prepare is still iterating families against that same FSKit path, so
   prepare's later jobs lose their mount.

4. **No serialization primitive exists** between the explicit provisioning flows
   and the mount-detect flow; they run on independent threads with no shared
   in-progress state.

## Correctness Properties

Property 1: Bug Condition - Prepare/Adopt Populate Routes Through The Mount Swap And Does Not Race

_For any_ prepare or adopt operation where the bug condition holds
(`isBugCondition` returns true), the fixed code SHALL (a) invoke the
`update_drive` populate inside an `msdos_mount(mount_point, poller=...)` context
(the populate runs against the private `mount_msdos` work mount, never the raw
FSKit `/Volumes/` mount), and (b) while that prepare/adopt operation is in
progress for the drive, cause the mount-detect handler
(`_on_efis_drive_mounted`) to NOT launch a second, concurrent archive+auto-sync
(`_run_archive`/`_run_drive_update`) for the same drive.

**Validates: Requirements 2.1, 2.2, 2.3**

Property 2: Preservation - Managed Auto-Sync And Sync Semantics Unchanged

_For any_ input where the bug condition does NOT hold (`isBugCondition` returns
false) — notably a managed-drive mount event with no prepare/adopt in progress —
the fixed code SHALL produce the same result as the original function,
preserving `_on_efis_drive_mounted` → `_run_archive` → `_run_drive_update` with
its existing swap, the identity-before-populate ordering, and the
verify/marker/sync-state semantics of `update_drive`.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**

## Fix Implementation

Assuming the (confirmed) root-cause analysis:

### Change 1 — route prepare/adopt populate through the swap

**File**: `src/efis_data_manager/drive_updater.py`

**Functions**: `prepare_drive`, `adopt_drive`

**Specific Changes**:
1. **Add an optional `poller` parameter** to `prepare_drive(volume_path, label,
   progress_callback, poller=None)` and `adopt_drive(mount_point,
   progress_callback, poller=None)`. `poller` is an opaque object exposing
   `pause()`/`resume()`; it is only ever passed through to `msdos_mount`. No
   import of `app`/`usb_monitor` is added to `drive_updater`.
2. **Wrap the populate `update_drive` call** in `msdos_mount`:
   ```
   from efis_data_manager.mount_swap import msdos_mount, MountSwapError
   ...
   with msdos_mount(mount_point, poller=poller) as work_mount:
       update_results = update_drive(work_mount, progress_callback=...)
   ```
   The identity write (`_ensure_identity`) and provenance stamp stay BEFORE the
   swap so the identity file is present at the volume root (it travels with the
   volume regardless of mount path, and `resolve_drive_id` reads it on
   `work_mount`).
3. **Handle `MountSwapError`**: catch it around the populate, report the
   prepare/adopt failed with a clear message, and return the failure dict. The
   `msdos_mount` context guarantees the FSKit mount is restored (or flagged
   needs-reinsert) on exit, so no extra teardown is needed here — do NOT fall
   back to populating the raw FSKit mount.

### Change 2 — serialize against the mount-detect auto-sync

**File**: `src/efis_data_manager/app.py`

**Functions**: `_do_prepare_drive`, `_do_adopt_drive`, `_on_efis_drive_mounted`,
plus a small guard primitive on the app object.

**Specific Changes**:
1. **Add an operation-in-progress guard**: a set (e.g.
   `self._provisioning_drives`) protected by a lock, holding a stable key for
   the drive being prepared/adopted. Because the mount path can change across a
   reformat+remount, key on something stable enough to cover the span — e.g. the
   whole-disk / device identifier resolved up front, and/or the label; the
   design accepts covering the label + the discovered mount path. Add
   register/unregister helpers.
2. **Register the guard for the WHOLE prepare/adopt span** in `_do_prepare_drive`
   / `_do_adopt_drive`: mark the drive in-progress before format/identity, and
   unregister in a `finally` after populate completes (success or failure). This
   covers format → identity → populate, so the remount-triggered poller event is
   suppressed for that drive.
3. **Consult the guard in `_on_efis_drive_mounted`**: if the mounted drive
   matches an in-progress prepare/adopt key, log and return WITHOUT launching
   `_run_archive`/`_run_drive_update`.
4. **Pass the poller through**: `_do_prepare_drive`/`_do_adopt_drive` pass
   `poller=self._usb_monitor` into `prepare_drive`/`adopt_drive` so the swap
   quiesces the poller across its inner window too (belt-and-suspenders with the
   guard).

Design note (weighed options): the operation-in-progress guard is preferred over
"just keep `usb_monitor` paused for the whole prepare/adopt" because a
`prepare_drive` reformat legitimately needs the poller to observe the remount to
resolve the new mount path, and a blanket pause across a multi-minute format is
brittle if prepare crashes. The guard is explicit, easy to reason about, and
fails safe (a stale key at worst suppresses one auto-sync, which the user can
re-trigger by reinserting). The inner-window poller pause via `msdos_mount`
remains as the second layer.

### Versioning (release step, not part of the exploration test)

- Bump `__version__` and `MENUBAR_VERSION` to the v1.5.1 patch; keep
  `DASHBOARD_VERSION` unchanged; keep `pyproject.toml` `version` in sync with
  `__version__`.

## Testing Strategy

### Validation Approach

Two-phase: first surface counterexamples that demonstrate the bug on the UNFIXED
code (exploration), then verify the fix works and preserves existing behavior.
Because the real defect is concurrency + subprocess/mount behavior, the tests
target the ORCHESTRATION invariant with mocks (mock `msdos_mount`,
`update_drive`, and the poller) rather than touching a real device.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples proving that (a) prepare/adopt populate is
invoked against the raw FSKit mount, not inside an `msdos_mount` context, and
(b) while a prepare is in progress the mount-detect handler launches a second
update on the same drive. Confirm the root cause; if refuted, re-hypothesize.

**Test Plan**: Drive `prepare_drive`/`adopt_drive` with `update_drive` and
`msdos_mount` mocked so the test can observe call ordering. Assert that
`update_drive` is called *within* the `msdos_mount` context (e.g. `msdos_mount`
is entered before `update_drive` and the mount point handed to `update_drive` is
the yielded work mount, not the FSKit path). Separately, simulate a prepare in
progress and fire `_on_efis_drive_mounted` for the same drive; assert no
concurrent `_run_archive`/`_run_drive_update` is launched. Run on UNFIXED code —
these assertions FAIL, confirming the bug.

**Test Cases**:
1. **Prepare populate not swapped**: `prepare_drive` calls `update_drive` with
   the raw FSKit mount and never enters `msdos_mount` (fails on unfixed code).
2. **Adopt populate not swapped**: same for `adopt_drive` (fails on unfixed
   code).
3. **Concurrent auto-sync race**: with a prepare marked in progress,
   `_on_efis_drive_mounted` still launches auto-sync for the same drive (fails
   on unfixed code — no guard exists).
4. **Edge — swap failure surfaces cleanly**: when `msdos_mount` raises
   `MountSwapError`, prepare/adopt returns failure and does not call
   `update_drive` on the FSKit mount (defines post-fix behavior).

**Expected Counterexamples**:
- `update_drive` invoked with `/Volumes/<label>` and `msdos_mount` never
  entered during prepare/adopt.
- `_run_archive`/`_run_drive_update` launched while a prepare is in progress for
  the same drive.

### Fix Checking

**Goal**: For all inputs where the bug condition holds, the fixed code produces
the expected behavior.

**Pseudocode:**
```
FOR ALL op WHERE isBugCondition(op) DO
  result := runPrepareOrAdopt_fixed(op)
  ASSERT updateDriveWasCalledInsideMsdosMount(result)
  ASSERT NOT concurrentAutoSyncLaunchedForSameDrive(result)
END FOR
```

### Preservation Checking

**Goal**: For all inputs where the bug condition does NOT hold, the fixed code
produces the same result as the original.

**Pseudocode:**
```
FOR ALL op WHERE NOT isBugCondition(op) DO
  ASSERT behavior_original(op) = behavior_fixed(op)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation
because it generates many non-prepare mount scenarios automatically and catches
edge cases. Observe behavior on UNFIXED code first, then write property-based
tests capturing it.

**Test Plan**: Observe on UNFIXED code that a normal managed mount (no
prepare/adopt in progress) launches `_run_archive` → `_run_drive_update`, and
that `update_drive`/`msdos_mount`/`usb_monitor` behave as in v1.5.0. Capture
those as property tests and confirm they still pass after the fix.

**Test Cases**:
1. **Managed auto-sync preserved**: for any managed mount with no prepare in
   progress, `_on_efis_drive_mounted` launches auto-sync exactly as before.
2. **Identity-before-populate preserved**: `prepare_drive`/`adopt_drive` still
   write the identity before `update_drive` runs.
3. **update_drive semantics preserved**: verify/marker/sync-state behavior of
   `update_drive` is unchanged (call shape and results).

### Unit Tests

- `prepare_drive`/`adopt_drive` enter `msdos_mount` and call `update_drive` on
  the work mount; `MountSwapError` yields a failure result and no FSKit-mount
  populate.
- App guard: register/unregister around prepare/adopt; `_on_efis_drive_mounted`
  suppresses auto-sync for an in-progress drive and allows it otherwise.
- `poller` passthrough reaches `msdos_mount`.

### Property-Based Tests

- Property 1 (exploration): over generated prepare/adopt scenarios, populate is
  always inside the swap and no concurrent auto-sync launches for the same
  drive.
- Property 2 (preservation): over generated non-prepare mount scenarios, the
  auto-sync launch behavior matches the unfixed baseline.

### Integration Tests

- Full Prepare Drive → Start clean flow (with `msdos_mount`/`update_drive`
  mocked at the subprocess boundary) completes populate through the swap with no
  concurrent auto-sync, and the guard is cleared on completion.
- Adopt flow analogous.
- Managed-drive normal mount still triggers a single auto-sync.
