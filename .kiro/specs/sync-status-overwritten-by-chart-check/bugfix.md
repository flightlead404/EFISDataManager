# Bugfix Requirements Document

## Introduction

When a managed drive is inserted, the EFIS Data Manager menu bar can show the idle
"connected" state (the filled ● glyph) and an "Idle" status line while a chart/nav sync
(rsync) is actively running. The status is misleading: the user believes the drive is
done and safe to remove when in fact a write is in progress, which risks a mid-write
removal.

The root cause is a status race between independent background flows that all write a
single shared status line via `_set_status`. On insert the app launches the drive
update/sync flow (`_run_drive_update` / the family sync, which sets "Updating drive...")
AND, concurrently on a separate thread, a chart-currency CHECK flow (`_run_chart_check*`
against Seattle Avionics). The chart check is usually fast and, when nothing new is
available, ends by calling `self._set_status("Idle")`. Because there is no coordination
between the flows, the fast chart check's "Idle" write lands AFTER the slow sync's
"Updating drive..." write and clobbers it. The menu-bar title logic then renders the
"connected, idle" ● even though rsync is still transferring.

Observed on 2026-10-31 with drive `C2609P2610` (chart-capable, HXr Link 1): the
notification history shows "Charts Current" and "Updating Drive (3 families...)" within
the same second, after which the status read "Idle" / ● while `rsync ... ->
/Volumes/C2609P2610/ChartData/` was confirmed still running ~8 minutes later.

The fix must ensure the status line (and the menu-bar glyph derived from it) reflects an
in-progress sync for its full duration: a concurrent, lower-priority flow (the chart
check) must not be able to reset the status to "Idle" while a drive sync/update is
active.

### Bug Condition

A drive sync/update is "active" from the moment the update flow sets a working status
(e.g. "Updating drive...") until it reaches a terminal status (e.g. "Drive current",
"Update failed"). "Idle"/connected-idle is a terminal display state that must not be
shown while a sync is active.

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type AppStatusState
  OUTPUT: boolean

  // A drive sync/update is running, yet the displayed status line is a
  // terminal idle state (and the menu-bar glyph shows connected-idle ●).
  RETURN X.sync_in_progress = TRUE
         AND X.displayed_status IN { "Idle", "" }
END FUNCTION
```

### Property Specification (Fix Checking)

```pascal
// Property: Fix Checking - a running sync is never displayed as idle
FOR ALL X WHERE isBugCondition(X) DO
  result <- F'(X)
  ASSERT result.displayed_status <> "Idle"
         AND result.displayed_status reflects the active sync
END FOR
```

Where **F** is the status handling before the fix and **F'** is the handling after the
fix. Concretely, `F'` prevents a concurrent lower-priority flow from writing a terminal
"Idle" status while a sync/update is active (for example via an active-operation guard, a
status owner/priority, or by not having the chart CHECK flow write "Idle" at all when it
did not itself start a user-facing operation).

### Preservation Goal (Preservation Checking)

```pascal
// Property: Preservation Checking - normal status transitions unchanged
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
END FOR
```

Counterexample demonstrating the bug: on insert, the sync flow sets "Updating drive..."
and starts rsync; the concurrent chart check finds nothing new and calls
`_set_status("Idle")`; the menu bar shows ● connected-idle while rsync is still writing
`ChartData/`.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a managed drive is inserted THEN the system starts a drive update/sync flow and a chart-currency CHECK flow concurrently on separate threads, both of which write the single shared status line via `_set_status`
1.2 WHEN the chart CHECK flow finds no new chart updates WHILE a drive sync is in progress THEN the system calls `_set_status("Idle")`, overwriting the sync flow's "Updating drive..." status
1.3 WHEN the status line is set to "Idle" WHILE a sync is in progress THEN the menu-bar title logic renders the connected-idle ● glyph, indicating (incorrectly) that no work is running
1.4 WHEN the status is reset to idle during an active sync THEN the user receives no visual indication that a write is in progress, creating a risk of removing the drive mid-write

### Expected Behavior (Correct)

2.1 WHEN a drive sync/update is in progress THEN the system SHALL display a status that reflects the active sync (e.g. "Updating drive...") for the full duration of the sync
2.2 WHEN a concurrent chart CHECK flow completes (current or not) WHILE a drive sync is in progress THEN the system SHALL NOT reset the displayed status to "Idle" / connected-idle
2.3 WHEN a drive sync/update reaches its own terminal state THEN the system SHALL set the terminal status ("Drive current", "Update failed", etc.) — the sync flow, not a concurrent flow, owns the transition out of the active state
2.4 WHEN the menu-bar glyph is derived from status WHILE a sync is in progress THEN the glyph SHALL indicate activity (not the connected-idle ● state)

### Unchanged Behavior (Regression Prevention)

3.1 WHEN no drive sync is in progress and a chart CHECK finds nothing new THEN the system SHALL CONTINUE TO set "Idle" as it does today
3.2 WHEN a drive sync completes cleanly THEN the system SHALL CONTINUE TO show the terminal "Drive current" status and settle to connected-idle afterward
3.3 WHEN the chart check finds new downloadable updates THEN the system SHALL CONTINUE TO surface the "N chart update(s) available" status/notification and the Download menu item
3.4 WHEN other independent status-setting flows run in isolation (archive, verify, eject, nav check) with no concurrent sync THEN the system SHALL CONTINUE TO display their statuses unchanged
3.5 WHEN a sync fails or is interrupted THEN the system SHALL CONTINUE TO surface the failure/terminal status and the interrupted-sync handling on the next insert

## Notes (non-authoritative — for design phase)

- Primary suspects (verify in design): `_set_status` is a single shared sink; the chart
  CHECK flows (`_run_chart_check_manual` and siblings around app.py lines ~1398-1505)
  call `_set_status("Idle")` in their "nothing new" branches; the drive update flow
  (around app.py lines ~1082-1266) sets "Updating drive..." then a terminal status.
- The sync itself is correct and unaffected — this is purely a status/indicator defect.
  The underlying rsync/verify/commit logic must not be touched by the fix.
- Candidate fix directions to weigh in design: an explicit "active operation" guard that
  blocks lower-priority terminal writes; a status owner/priority token; or decoupling the
  chart CHECK flow so it does not assert "Idle" on a status it did not set. Choose the
  least invasive option that preserves 3.1-3.5.
- Backlog item: captured 2026-10-31 during chart-capable adopt testing. Not yet scheduled.
