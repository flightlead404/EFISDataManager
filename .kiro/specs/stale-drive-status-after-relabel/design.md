# Stale Drive Status After Relabel Bugfix Design

## Overview

Adopting a non-chart-capable device relabels its mounted volume (for example
`EFIS_MINI` → `MINIAP1029`). The USB monitor's `_poll_loop` tracks mounted
volumes by their mount **path** under `/Volumes/`, so a single relabel is
observed on the next 2-second poll as two independent events: a NEW mount at the
new name and an EJECTION of the old name. The eject handler
`_on_efis_drive_ejected` reacts unconditionally — it blanks the connected
status to "Not connected", forces the overall status to "Idle", and fires the
"Drive removed safely" notification — clobbering the correct "Connected" status
the adopt worker set moments earlier via `_reconcile_drive_status()`.

The fix makes the eject handler reflect ground truth instead of assuming the
eject means "no drive is present". On any eject event the handler reconciles the
connected status against `find_mounted_managed_drive()`. It only shows
"Not connected", forces "Idle", and emits the removal notification when NO
managed drive remains mounted. When a managed drive is still mounted (the
relabel case), the handler reconciles to "Connected" and stays silent — no
notification, no status downgrade.

The change is confined to a single method, `_on_efis_drive_ejected` in
`src/efis_data_manager/app.py`. It reuses the existing `find_mounted_managed_drive()`
ground-truth scan and `_reconcile_drive_status()` / `_set_drive_status()`
helpers, so the UI-mutation marshalling and Eject/Verify enablement logic are
untouched.

## Glossary

- **Bug_Condition (C)**: A managed drive is mounted at a volume root
  (`find_mounted_managed_drive()` returns a path) yet the menu-bar connected
  status reads "Not connected".
- **Property (P)**: After the eject handler runs, the connected status reflects
  ground truth — "Connected: <mount>" whenever a managed drive is still mounted.
- **Preservation**: Genuine removals (no managed drive left) still blank the
  status, set "Idle", and notify; new mounts still connect; the adopt reconcile
  path and Eject/Verify enablement are unchanged.
- **Managed drive**: A volume carrying a valid `EFIS_DRIVE_ID.json` at its root
  whose `kind` is `efis-chart-drive`, as determined by `is_managed_drive` /
  `find_mounted_managed_drive` in `usb_monitor.py`.
- **Relabel**: An adopt operation renames the volume. Observed by the
  path-keyed poll loop as an eject of the old name plus a mount of the new name.
- **`_on_efis_drive_ejected(self, mount_point)`**: The eject-event handler in
  `app.py` (~line 752), invoked from the USBMonitor poll thread. The function
  being fixed (F → F').
- **`_reconcile_drive_status(self)`**: Sets the drive-connected UI to match
  `find_mounted_managed_drive()` — "Connected: <mount>" when a managed drive is
  mounted, else "Not connected". Already thread-safe (marshals UI onto the main
  queue). Reused unchanged.
- **`_set_drive_status(self, drive_text)`**: Sets the `_drive_connected` flag
  ("Not connected" not in `drive_text`), updates the menu title, and toggles
  Eject/Verify enablement. Reused unchanged.
- **`find_mounted_managed_drive(volumes_dir="/Volumes")`**: Pure, side-effect-free
  scan returning a mounted managed-drive path or None. The ground-truth oracle.

## Bug Details

### Bug Condition

The bug manifests when the poll loop reports an ejection of a stale mount path
(the old name of a just-relabelled volume) while the relabelled volume is still
mounted and managed. `_on_efis_drive_ejected` is either not checking whether any
managed drive remains mounted, blindly setting "Not connected", or firing the
removal notification for an event that was not an actual removal.

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X of type DriveState
  OUTPUT: boolean

  // A managed drive is mounted at a volume root, yet the menu-bar
  // connected status shows "Not connected".
  RETURN find_mounted_managed_drive(X.volumes_dir) <> NULL
         AND X.menubar_drive_status = "Not connected"
END FUNCTION
```

### Examples

- **Relabel (the reported defect)**: Adopt relabels `EFIS_MINI` → `MINIAP1029`.
  Next poll reports an eject of `/Volumes/EFIS_MINI`. Expected: status stays
  "Connected: /Volumes/MINIAP1029" (the relabelled drive is still mounted).
  Actual (unfixed): status becomes "Drive: Not connected", Eject/Verify greyed
  out, "Drive removed safely" notification fires.
- **Idle status override**: On the same stale-name eject, expected status stays
  reflecting the connected drive. Actual (unfixed): overall status forced to
  "Idle", clobbering the "Connected" state the adopt worker set.
- **Spurious notification**: Expected on a relabel: no "Drive removed safely"
  notification. Actual (unfixed): the notification fires even though nothing was
  removed.
- **Genuine removal (edge case — must still work)**: User physically unplugs the
  only managed drive. Poll reports an eject and no managed drive remains.
  Expected: status "Not connected", overall "Idle", "Drive removed safely"
  notification fires. This is NOT the bug condition and must be preserved.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Genuine removal (no managed drive remains after the eject) must still set the
  menu-bar status to "Drive: Not connected" and the overall status to "Idle".
- Genuine removal must still emit the "EFIS Drive Ejected — Drive removed
  safely." notification.
- A newly mounted managed drive (no relabel involved) must still be detected and
  shown as "Connected" via the existing `_on_efis_drive_mounted` / reconcile path.
- The adopt worker must still call `_reconcile_drive_status()` and reflect
  "Connected" — this path is not touched by the fix.
- Eject/Verify menu actions must still be enabled whenever a managed drive is
  mounted and disabled when none is mounted, driven by `_drive_connected` set
  inside `_set_drive_status`.

**Scope:**
All inputs where the bug condition does NOT hold must behave exactly as before.
This includes:
- Genuine physical removals where no managed drive remains mounted.
- New mounts handled by `_on_efis_drive_mounted`.
- Adopt/prepare completion calling `_reconcile_drive_status()`.
- Manual eject via the Eject Drive menu action and its `_run_eject` worker.

## Hypothesized Root Cause

Based on the bug description and the code in `app.py` (~line 752), the root cause
is well isolated:

1. **Unconditional status blanking**: `_on_efis_drive_ejected` calls
   `self._set_drive_status("Not connected")` on every eject event, with no check
   for whether another managed drive is still mounted. A relabel's stale-name
   eject therefore blanks a status that should remain "Connected".

2. **Unconditional status override**: The handler calls `self._set_status("Idle")`
   on every eject, overriding the "Connected"-aligned state the adopt worker set
   via `_reconcile_drive_status()`.

3. **Unconditional removal notification**: The handler calls
   `self._notify("EFIS Drive Ejected", "Drive removed safely.")` on every eject,
   announcing a removal that did not happen in the relabel case.

4. **Path-keyed tracking surfaces relabels as eject+mount**: The USB monitor
   `_poll_loop` tracks volumes by mount path, so a relabel is inherently reported
   as an eject of the old path plus a mount of the new path. The eject handler is
   the correct place to disambiguate, because `find_mounted_managed_drive()`
   gives authoritative ground truth regardless of which path the event names.

The fix targets causes 1–3 by consulting ground truth in the handler; cause 4 is
inherent to path-keyed polling and is intentionally left as-is (changing the
monitor's identity tracking would be a larger, riskier change outside this fix).

## Correctness Properties

Property 1: Bug Condition - Eject reconciles to ground truth

_For any_ input where the bug condition holds (a managed drive is still mounted
when `_on_efis_drive_ejected` runs — the relabel case), the fixed handler SHALL
reconcile the connected status against `find_mounted_managed_drive()` and show
"Connected: <mount>", SHALL NOT force the overall status to "Idle", and SHALL NOT
emit the "Drive removed safely" notification.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4**

Property 2: Preservation - Genuine removal and normal flows unchanged

_For any_ input where the bug condition does NOT hold (no managed drive remains
mounted after the eject, or the event is a new mount / adopt reconcile), the
fixed code SHALL produce exactly the same result as the original: genuine
removals still set "Not connected" + "Idle" and emit the removal notification;
new mounts still connect; the adopt reconcile path and Eject/Verify enablement
are unchanged.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**

## Fix Implementation

### Changes Required

**File**: `src/efis_data_manager/app.py`

**Function**: `_on_efis_drive_ejected(self, mount_point: str)` (~line 752)

The handler is rewritten to consult ground truth once and branch on whether any
managed drive remains mounted.

**Specific Changes**:

1. **Query ground truth first**: Call `find_mounted_managed_drive()` to determine
   whether a managed drive is still mounted after this eject event. This single
   call is the oracle that distinguishes a relabel (drive still present) from a
   genuine removal (none present).

2. **Relabel branch (a managed drive remains)**: Call `_reconcile_drive_status()`
   so the UI is asserted to "Connected: <mount>" against the mounted volume. Do
   NOT call `_set_status("Idle")`. Do NOT emit the "Drive removed safely"
   notification. Log at info/debug that the eject was a relabel/stale-path event
   and the drive remains connected.

3. **Genuine-removal branch (no managed drive remains)**: Preserve the original
   behavior exactly — `self._set_drive_status("Not connected")`,
   `self._set_status("Idle")`, and
   `self._notify("EFIS Drive Ejected", "Drive removed safely.")`.

4. **Reuse, don't reimplement**: `_reconcile_drive_status()` already wraps
   `find_mounted_managed_drive()` and sets "Connected: <mount>" vs "Not
   connected"; the removal branch can call `_set_drive_status("Not connected")`
   directly (matching the pre-fix call) or `_reconcile_drive_status()` (which
   resolves to the same "Not connected" when the scan returns None). The design
   uses a single explicit `find_mounted_managed_drive()` check in the handler so
   the notification/Idle gating and the status reconcile derive from one
   consistent observation.

**Reference shape (pseudocode, not final code):**
```
FUNCTION _on_efis_drive_ejected(self, mount_point)
  log("EFIS drive eject event: " + mount_point)
  remaining <- find_mounted_managed_drive()
  IF remaining <> NULL THEN
    // Relabel / stale-path eject: a managed drive is still mounted.
    log("managed drive still mounted at " + remaining +
        "; treating as relabel, status preserved")
    self._reconcile_drive_status()        // asserts "Connected: <remaining>"
    RETURN                                // no Idle, no removal notification
  END IF
  // Genuine removal: no managed drive remains — original behavior.
  self._set_drive_status("Not connected")
  self._set_status("Idle")
  self._notify("EFIS Drive Ejected", "Drive removed safely.")
END FUNCTION
```

### Distinguishing relabel from genuine removal

The notification/Idle suppression keys off exactly one signal: whether
`find_mounted_managed_drive()` returns a path **after** the eject event is
observed. A relabel leaves the relabelled volume mounted, so the scan finds it
and the handler takes the silent reconcile branch. A genuine removal leaves no
managed drive, so the scan returns None and the handler takes the
removal-notification branch. There is no need to compare the ejected
`mount_point` against anything — ground truth after the event is sufficient and
is robust to which name (old or new) the poll happened to report.

### Thread-safety considerations

`_on_efis_drive_ejected` runs on the USBMonitor poll thread, not the main
thread. The fix keeps all UI mutation marshalled onto the main queue by reusing
the existing helpers:

- `find_mounted_managed_drive()` is a pure, side-effect-free `os.listdir` scan —
  safe to call from any thread.
- `_reconcile_drive_status()` and `_set_drive_status()` already dispatch the
  actual menu mutation via `NSOperationQueue.mainQueue().addOperationWithBlock_`,
  so they are documented safe to call from a worker thread. The fix adds no new
  direct AppKit access from the poll thread.
- `_set_status()` and `_notify()` are already called from this handler today
  (from the poll thread), so the genuine-removal branch introduces no new
  cross-thread concern.
- There is an inherent ordering consideration between the relabel's mount event
  and eject event within a single poll pass, but because the handler reads
  ground truth (`/Volumes/`) rather than cached state, it is correct regardless
  of the order in which the two events are dispatched: as long as the relabelled
  volume is physically mounted, the scan finds it. No new lock is required.

### Satisfying the fix-checking property

For every input where `isBugCondition(X)` holds, a managed drive is mounted, so
`find_mounted_managed_drive()` returns non-None and the handler takes the
reconcile branch, which calls `_reconcile_drive_status()` → `_set_drive_status("Connected: <mount>")`.
That sets `menubar_drive_status` to "Connected", satisfying
`ASSERT result.menubar_drive_status = "Connected"`. The handler also skips
`_set_status("Idle")` and the removal notification, satisfying Requirements 2.3
and 2.4.

### Preserving section-3 behaviors

- **3.1 / 3.2 genuine removal**: When no managed drive remains,
  `find_mounted_managed_drive()` returns None and the handler runs the original
  three calls verbatim — status "Not connected", overall "Idle", and the removal
  notification. Byte-for-byte the pre-fix behavior.
- **3.3 new mounts**: Untouched — handled by `_on_efis_drive_mounted`, which the
  fix does not modify.
- **3.4 adopt reconcile**: Untouched — the adopt worker still calls
  `_reconcile_drive_status()`; the fix only ensures a subsequent stale-name eject
  no longer undoes it.
- **3.5 Eject/Verify enablement**: Driven entirely by `_drive_connected` inside
  `_set_drive_status`, which the fix reuses unchanged. In the relabel branch the
  flag stays true (so actions stay enabled); in the removal branch it goes false
  (so actions disable) exactly as before.

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples
that demonstrate the bug on the unfixed handler, then verify the fix reconciles
to ground truth and preserves genuine-removal and normal-flow behavior. Because
the handler reacts to `/Volumes/` state and mutates rumps menu items off the main
thread, tests exercise the handler with `find_mounted_managed_drive`,
`_set_drive_status`, `_set_status`, and `_notify` stubbed/observed, asserting on
the resulting calls rather than on a live AppKit UI.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing
the fix. Confirm the root cause: the unconditional eject handler blanks status
and notifies even when a managed drive is still mounted.

**Test Plan**: Construct an app instance with the UI helpers observable, simulate
a relabel by making `find_mounted_managed_drive()` return a mounted path, invoke
`_on_efis_drive_ejected("/Volumes/EFIS_MINI")`, and assert on the resulting
status/notification. Run against the UNFIXED handler to observe failures.

**Test Cases**:
1. **Relabel still mounted**: `find_mounted_managed_drive()` returns
   `/Volumes/MINIAP1029`; eject the old name. Assert drive status ends
   "Connected" (will fail on unfixed code — it sets "Not connected").
2. **No Idle override on relabel**: Same setup; assert `_set_status("Idle")` is
   NOT called (will fail on unfixed code).
3. **No notification on relabel**: Same setup; assert `_notify` with "Drive
   removed safely" is NOT called (will fail on unfixed code).
4. **Edge — genuine removal**: `find_mounted_managed_drive()` returns None;
   assert status "Not connected", "Idle", and the removal notification fire
   (passes on unfixed code — defines the preserved baseline).

**Expected Counterexamples**:
- On a relabel, drive status becomes "Not connected", overall status becomes
  "Idle", and the removal notification fires, despite a managed drive still being
  mounted.
- Cause: unconditional `_set_drive_status("Not connected")`, `_set_status("Idle")`,
  and `_notify(...)` in `_on_efis_drive_ejected`.

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed
handler produces "Connected".

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := _on_efis_drive_ejected_fixed(input)
  ASSERT result.menubar_drive_status = "Connected"
  ASSERT NOT setStatusIdleCalled
  ASSERT NOT removedSafelyNotified
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the
fixed handler produces the same result as the original.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT _on_efis_drive_ejected_original(input)
       = _on_efis_drive_ejected_fixed(input)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation
checking because:
- It generates many `/Volumes/` configurations automatically across the input
  domain (no managed drive, one managed drive, multiple, varied names).
- It catches edge cases manual unit tests might miss (e.g. the ejected path equal
  to vs different from the remaining managed path).
- It gives strong confidence that genuine-removal behavior is unchanged for all
  non-buggy inputs.

**Test Plan**: Observe behavior on the UNFIXED handler for genuine removals and
normal flows, then write property-based tests capturing that behavior and assert
the fixed handler matches when no managed drive remains.

**Test Cases**:
1. **Genuine-removal preservation**: For states where `find_mounted_managed_drive()`
   returns None, assert the fixed handler calls `_set_drive_status("Not connected")`,
   `_set_status("Idle")`, and emits the removal notification — identical to
   original.
2. **Enablement preservation**: Assert `_drive_connected` ends true in the
   relabel branch and false in the removal branch, so Eject/Verify enablement
   matches the mounted state.
3. **New-mount preservation**: Assert `_on_efis_drive_mounted` behavior is
   unchanged (not touched by the fix) — a newly mounted managed drive still
   connects.
4. **Adopt-reconcile preservation**: Assert `_reconcile_drive_status()` behavior
   is unchanged and that a stale-name eject after adopt no longer reverts it.

### Unit Tests

- Relabel case: managed drive still mounted → "Connected", no Idle, no
  notification.
- Genuine removal: no managed drive mounted → "Not connected", "Idle", removal
  notification.
- Ejected path equals remaining managed path vs differs from it → both reconcile
  to "Connected".
- `_drive_connected` flag and Eject/Verify enablement in each branch.

### Property-Based Tests

- Generate random sets of mounted volumes (managed/unmanaged, varied names) and a
  random ejected path; assert: status is "Connected" iff a managed drive remains,
  and the removal notification fires iff no managed drive remains.
- Generate states with no managed drive remaining and assert fixed == original
  (preservation of the removal path).

### Integration Tests

- Simulate the full adopt-relabel flow: adopt sets "Connected" via
  `_reconcile_drive_status()`, then a poll delivers the stale-name eject; assert
  the menu bar still reads "Connected" and Eject/Verify stay enabled.
- Simulate a physical removal after adopt; assert the menu bar reads "Not
  connected", status is "Idle", and the removal notification fires once.
- Simulate a relabel followed by an actual removal; assert the first eject is
  silent and the second (no drive remaining) notifies.
