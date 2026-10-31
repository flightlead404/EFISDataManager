# Bugfix Requirements Document

## Introduction

After a drive is adopted for a non-chart-capable device, the EFIS Data Manager menu bar
displays "Drive: Not connected" even though the drive is mounted, valid, and correctly
detected as managed. The adopt operation relabels the volume (for example `EFIS_MINI` ->
`MINIAP1029`). Because the USB monitor tracks volumes by mount path, a single relabel is
observed as two separate events on the next poll: a NEW mount at the new name and an
EJECTION of the OLD name. The ejection handler unconditionally blanks the connected
status, clobbering the correct "Connected" status that the adopt worker set moments
earlier.

The user impact is a stale, misleading UI: the menu bar claims no drive is connected,
and the Eject/Verify menu actions are disabled, even though a managed drive is physically
mounted and ready. The drive's identity and manifest JSON are correct, and live detection
(`find_mounted_managed_drive()`) confirms the drive is present and managed — only the
displayed status is wrong.

The fix makes the eject handler reflect ground truth: it must only show "Not connected"
when NO managed drive remains mounted, and it must only announce an actual removal (not a
relabel) via the "Drive removed safely" notification.

### Bug Condition

A managed drive is one carrying a valid `EFIS_DRIVE_ID.json` at its volume root whose
`kind` is `efis-chart-drive` (as determined by `is_managed_drive` /
`find_mounted_managed_drive` in `usb_monitor.py`).

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type DriveState
  OUTPUT: boolean

  // A managed drive is mounted at a volume root, yet the menu-bar
  // connected status shows "Not connected".
  RETURN find_mounted_managed_drive(X.volumes_dir) <> NULL
         AND X.menubar_drive_status = "Not connected"
END FUNCTION
```

### Property Specification (Fix Checking)

```pascal
// Property: Fix Checking - connected status reflects ground truth
FOR ALL X WHERE isBugCondition(X) DO
  result <- F'(X)
  ASSERT result.menubar_drive_status = "Connected"
END FOR
```

Where **F** is the eject-event handling before the fix and **F'** is the handling after
the fix. Concretely, `F'` reconciles the connected status against
`find_mounted_managed_drive()` on an eject event instead of unconditionally setting
"Not connected".

### Preservation Goal (Preservation Checking)

```pascal
// Property: Preservation Checking - genuine removals and normal flows unchanged
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
END FOR
```

Counterexample demonstrating the bug: adopting a non-chart-capable device relabels
`EFIS_MINI` to `MINIAP1029`; on the next 2s poll the monitor reports an eject of
`/Volumes/EFIS_MINI`, the eject handler sets "Not connected", and the menu bar shows
"Drive: Not connected" while `/Volumes/MINIAP1029` is still mounted and managed.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN an adopt operation relabels a mounted managed drive and the USB monitor poll reports an ejection of the old (stale) mount path THEN the system unconditionally sets the menu-bar status to "Drive: Not connected"
1.2 WHEN the eject handler fires for the stale mount path WHILE another managed drive (the relabelled volume) is still mounted THEN the system reports "Not connected" without checking whether any managed drive remains mounted
1.3 WHEN the stale-name ejection is processed THEN the system sets the overall status to "Idle", overriding the "Connected" state the adopt worker set via `_reconcile_drive_status()`
1.4 WHEN the stale-name ejection is processed for a relabel (not a physical removal) THEN the system emits the "EFIS Drive Ejected — Drive removed safely." notification even though no drive was actually removed

### Expected Behavior (Correct)

2.1 WHEN an adopt operation relabels a mounted managed drive and the USB monitor poll reports an ejection of the old mount path THEN the system SHALL reconcile the connected status against ground truth and SHALL show "Connected" because a managed drive is still mounted
2.2 WHEN the eject handler fires WHILE a managed drive is still mounted THEN the system SHALL set "Not connected" ONLY IF `find_mounted_managed_drive()` returns None
2.3 WHEN a managed drive remains mounted after the stale-name ejection THEN the system SHALL NOT override the connected state (SHALL NOT force status to "Idle" in a way that blanks the connected UI)
2.4 WHEN an ejection event corresponds to a relabel rather than an actual removal (a managed drive is still mounted) THEN the system SHALL NOT emit the "Drive removed safely" notification

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a managed drive is physically removed and NO managed drive remains mounted THEN the system SHALL CONTINUE TO set the menu-bar status to "Drive: Not connected" and set the overall status to "Idle"
3.2 WHEN a managed drive is physically removed and NO managed drive remains mounted THEN the system SHALL CONTINUE TO emit the "EFIS Drive Ejected — Drive removed safely." notification
3.3 WHEN a managed drive is newly mounted (no relabel involved) THEN the system SHALL CONTINUE TO detect it and show "Connected" via the existing mount/reconcile path
3.4 WHEN the adopt worker completes successfully THEN the system SHALL CONTINUE TO call `_reconcile_drive_status()` and reflect "Connected"
3.5 WHEN the Eject/Verify menu actions depend on `_drive_connected` THEN the system SHALL CONTINUE TO enable them whenever a managed drive is mounted and disable them when none is mounted
