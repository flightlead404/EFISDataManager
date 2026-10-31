# Implementation Plan

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Eject reconciles to ground truth (relabel case)
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior - it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate the bug exists on the unfixed `_on_efis_drive_ejected`
  - **Scoped PBT Approach**: For this deterministic bug, scope the property to the concrete relabel case: `find_mounted_managed_drive()` returns a mounted managed path (e.g. `/Volumes/MINIAP1029`) while `_on_efis_drive_ejected("/Volumes/EFIS_MINI")` runs
  - Build an app instance (or lightweight harness) with `find_mounted_managed_drive`, `_set_drive_status`, `_set_status`, and `_notify` stubbed/observed rather than driving a live AppKit UI (per design Testing Strategy > Validation Approach)
  - Bug condition from design `isBugCondition(X)`: `find_mounted_managed_drive(X.volumes_dir) <> NULL AND X.menubar_drive_status = "Not connected"`
  - Assert the expected (post-fix) behavior so the test fails on unfixed code:
    - Drive status ends "Connected" (unfixed sets "Not connected" — counterexample 1)
    - `_set_status("Idle")` is NOT called (unfixed calls it — counterexample 2)
    - `_notify(...)` with "Drive removed safely" is NOT called (unfixed fires it — counterexample 3)
  - Run test on UNFIXED code via `./venv/bin/python -m pytest`
  - **EXPECTED OUTCOME**: Test FAILS (this is correct - it proves the bug exists)
  - Document counterexamples found (e.g. "relabel eject with /Volumes/MINIAP1029 still mounted → status 'Not connected', 'Idle', removal notification fired")
  - Mark task complete when test is written, run, and failure is documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Genuine removal and normal flows unchanged
  - **IMPORTANT**: Follow observation-first methodology — run the UNFIXED handler first, record outputs, then assert them
  - Observe on UNFIXED code for inputs where `isBugCondition` is false (no managed drive remains): `find_mounted_managed_drive()` returns `None` → handler calls `_set_drive_status("Not connected")`, `_set_status("Idle")`, and `_notify("EFIS Drive Ejected", "Drive removed safely.")`
  - Write property-based tests (Hypothesis, already used in `.hypothesis`) capturing the Preservation Requirements from design:
    - Generate random `/Volumes/` configurations (no managed drive, one managed drive, multiple, varied names) and a random ejected path; stub `find_mounted_managed_drive` to reflect each config
    - Property: the removal notification fires iff no managed drive remains mounted; `_set_status("Idle")` is called iff no managed drive remains
    - Property: `_drive_connected` ends false in the genuine-removal branch and true in the relabel branch (drives Eject/Verify enablement per 3.5)
  - Preserve new-mount path (3.3): assert `_on_efis_drive_mounted` behavior is unchanged (not touched by the fix)
  - Preserve adopt reconcile path (3.4): assert `_reconcile_drive_status()` behavior is unchanged
  - Run tests on UNFIXED code via `./venv/bin/python -m pytest`
  - **EXPECTED OUTCOME**: Tests PASS for the genuine-removal/new-mount/adopt cases (confirms baseline behavior to preserve). The relabel-branch assertions belong to Property 1, not here.
  - Mark task complete when tests are written, run, and passing on unfixed code for the non-bug-condition inputs
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [x] 3. Fix stale drive status after relabel in the eject handler

  - [x] 3.1 Rewrite `_on_efis_drive_ejected` to reconcile against ground truth
    - File: `src/efis_data_manager/app.py`, method `_on_efis_drive_ejected(self, mount_point)` (~line 752)
    - Log the eject event for `mount_point`
    - Call `find_mounted_managed_drive()` once to get the ground-truth remaining managed drive (the single oracle distinguishing relabel from genuine removal)
    - Relabel branch (a managed drive remains, scan returns non-None): call `self._reconcile_drive_status()` to assert "Connected: <mount>", log that the eject was a relabel/stale-path event and the drive remains connected, then `return` — do NOT call `_set_status("Idle")` and do NOT emit the "Drive removed safely" notification
    - Genuine-removal branch (no managed drive remains, scan returns None): run the original behavior verbatim — `self._set_drive_status("Not connected")`, `self._set_status("Idle")`, `self._notify("EFIS Drive Ejected", "Drive removed safely.")`
    - Reuse existing helpers unchanged: `find_mounted_managed_drive` (usb_monitor.py), `_reconcile_drive_status`, `_set_drive_status` (app.py); add no new locks (UI mutation already marshalled onto the main queue by those helpers)
    - _Bug_Condition: isBugCondition(X) = find_mounted_managed_drive(X.volumes_dir) <> NULL AND X.menubar_drive_status = "Not connected"_
    - _Expected_Behavior: on eject, status reflects ground truth — "Connected: <mount>" whenever a managed drive is still mounted; "Not connected"/"Idle"/notification only when none remains_
    - _Preservation: genuine removals still blank status + set Idle + notify; new-mount, adopt-reconcile, and Eject/Verify enablement via _drive_connected unchanged_
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4, 3.1, 3.2, 3.3, 3.4, 3.5_

  - [x] 3.2 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Eject reconciles to ground truth (relabel case)
    - **IMPORTANT**: Re-run the SAME test from task 1 - do NOT write a new test
    - The test from task 1 encodes the expected behavior; when it passes, it confirms the relabel eject reconciles to "Connected" with no Idle and no removal notification
    - Run the bug condition exploration test from step 1 via `./venv/bin/python -m pytest`
    - **EXPECTED OUTCOME**: Test PASSES (confirms bug is fixed)
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [x] 3.3 Verify preservation tests still pass
    - **Property 2: Preservation** - Genuine removal and normal flows unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - Run the preservation property tests from step 2 via `./venv/bin/python -m pytest`
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions — genuine removal still notifies/idles, new-mount/adopt/enablement unchanged)
    - Confirm all tests still pass after the fix (no regressions)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [x] 4. Add integration tests for the adopt-relabel flow
  - Simulate the full adopt-relabel flow: adopt sets "Connected" via `_reconcile_drive_status()`, then a poll delivers the stale-name eject; assert the menu bar still reads "Connected" and Eject/Verify stay enabled
  - Simulate a physical removal after adopt: assert the menu bar reads "Not connected", overall status is "Idle", and the removal notification fires once
  - Simulate a relabel followed by an actual removal: assert the first eject is silent and the second (no drive remaining) notifies
  - Stub/observe `find_mounted_managed_drive`, `_set_drive_status`, `_set_status`, and `_notify` rather than driving a live AppKit UI
  - Run via `./venv/bin/python -m pytest`
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 3.1, 3.2, 3.3, 3.4, 3.5_

- [x] 5. Checkpoint - Ensure all tests pass
  - Run the full suite via `./venv/bin/python -m pytest`
  - Confirm the Property 1 exploration test passes (bug fixed) and all Property 2 preservation + integration tests pass (no regressions)
  - Ensure all tests pass; ask the user if questions arise
