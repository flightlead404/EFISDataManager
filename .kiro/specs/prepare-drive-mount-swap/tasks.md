# Implementation Plan

- [ ] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Prepare/Adopt Populate Routes Through The Mount Swap And Does Not Race
  - **IMPORTANT**: Write this property-based test BEFORE implementing the fix.
  - **CRITICAL**: This test MUST FAIL on the current (unfixed) code — failure confirms the bug exists. DO NOT attempt to fix the test or the code when it fails.
  - **NOTE**: This test encodes the expected behavior — it will validate the fix when it passes after implementation.
  - **GOAL**: Surface counterexamples that demonstrate the bug (populate runs on the raw FSKit mount; a concurrent auto-sync launches for the same drive).
  - **Scoped PBT Approach**: The defect is deterministic on current code, so scope the property to concrete orchestration scenarios (prepare, adopt) rather than a broad input domain. Mock `msdos_mount`, `update_drive`, and the poller so no real device is touched (see `tests/test_prop_mount_swap_never_orphaned.py` for the mocking style).
  - Create `tests/test_prop_prepare_adopt_uses_swap.py`.
  - **Assertion (a) — populate goes through the swap**: patch `drive_updater.msdos_mount` with a context manager that records entry/exit and yields a sentinel work-mount path; patch `drive_updater.update_drive` to record its `mount_point` argument. Assert that for `prepare_drive` and `adopt_drive`, `msdos_mount` is ENTERED before `update_drive` is called AND `update_drive` receives the yielded work-mount, NOT the raw FSKit `/Volumes/<label>` path (from Bug Condition / Property 1 in design). Stub the format/identity/diskutil steps so only orchestration is exercised.
  - **Assertion (b) — no concurrent auto-sync during prepare**: construct the app object (or a minimal stand-in exposing the guard + `_on_efis_drive_mounted`), mark a drive as prepare-in-progress, then invoke `_on_efis_drive_mounted(mount_point)` for that same drive with `_run_archive`/`_run_drive_update` (and `threading.Thread`) patched. Assert NO archive/auto-sync is launched for the in-progress drive.
  - Run the test on UNFIXED code.
  - **EXPECTED OUTCOME**: Test FAILS — (a) `update_drive` is called with the raw FSKit path and `msdos_mount` is never entered; (b) `_on_efis_drive_mounted` launches auto-sync because no guard exists. This proves the bug.
  - Document the counterexamples found (e.g. "prepare_drive called update_drive('/Volumes/EFIS_4', ...) without entering msdos_mount"; "auto-sync launched for a prepare-in-progress drive").
  - Mark this task complete when the test is written, run, and its failure is documented.
  - _Requirements: 1.1, 1.2, 1.3_

- [ ] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Managed Auto-Sync And Sync Semantics Unchanged
  - **IMPORTANT**: Follow the observation-first methodology — observe the UNFIXED behavior, then assert it.
  - **GOAL**: Capture the baseline behavior for inputs where the bug condition does NOT hold (a normal managed mount with no prepare/adopt in progress), so the fix can be proven not to regress it.
  - Create `tests/test_prop_prepare_adopt_preserves_autosync.py`.
  - Observe on UNFIXED code: for a managed drive mount event with NO prepare/adopt in progress, `_on_efis_drive_mounted` launches `_run_archive` → `_run_drive_update` (patch `_run_archive`/`threading.Thread` and assert it is launched once for the drive).
  - Observe on UNFIXED code: `prepare_drive`/`adopt_drive` write the identity (`_ensure_identity`) BEFORE `update_drive` runs (record call order with the format/diskutil steps stubbed).
  - Observe on UNFIXED code: `update_drive` is invoked with the expected families/progress-callback shape (verify/marker/sync-state semantics are delegated unchanged).
  - Write property-based tests over generated non-prepare mount scenarios asserting the auto-sync launch behavior and the identity-before-populate ordering (from Preservation Requirements in design).
  - Run the tests on UNFIXED code.
  - **EXPECTED OUTCOME**: Tests PASS on unfixed code (this confirms the baseline behavior to preserve).
  - Mark this task complete when the tests are written, run, and passing on unfixed code.
  - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [ ] 3. Fix: route prepare/adopt populate through the mount swap and serialize against auto-sync

  - [ ] 3.1 Route prepare/adopt populate through `msdos_mount` in `drive_updater`
    - In `src/efis_data_manager/drive_updater.py`, add an optional `poller=None` parameter to `prepare_drive(volume_path, label, progress_callback, poller=None)` and `adopt_drive(mount_point, progress_callback, poller=None)`.
    - Keep `_ensure_identity` + provenance stamp BEFORE the swap (identity must be at the volume root so `resolve_drive_id` reads it on the work mount).
    - Wrap the populate call: `with msdos_mount(mount_point, poller=poller) as work_mount: update_results = update_drive(work_mount, progress_callback=...)` (import `msdos_mount`, `MountSwapError` from `efis_data_manager.mount_swap`).
    - Catch `MountSwapError` around the populate: return a failure dict with a clear message; do NOT fall back to populating the raw FSKit mount. `msdos_mount` guarantees FSKit restore / needs-reinsert on exit.
    - Do NOT change `update_drive`, `msdos_mount`, or the sync engine. No `app`/`usb_monitor` import in `drive_updater` (poller is an opaque passthrough).
    - _Bug_Condition: isBugCondition(op) — populate not inside msdos_mount (Defect 1)_
    - _Expected_Behavior: Property 1 — update_drive runs inside msdos_mount on the work mount_
    - _Preservation: identity-before-populate + update_drive semantics unchanged (Property 2)_
    - _Requirements: 2.1, 2.3, 3.2_

  - [ ] 3.2 Add the operation-in-progress guard and consult it in the mount-detect handler
    - In `src/efis_data_manager/app.py`, add a lock-protected in-progress set (e.g. `self._provisioning_drives`) with register/unregister helpers keyed on a value stable across a reformat+remount (e.g. whole-disk/device id and/or label + discovered mount path).
    - In `_do_prepare_drive`/`_do_adopt_drive`: register the drive BEFORE format/identity and unregister in a `finally` AFTER populate (covers the whole format → identity → populate span). Pass `poller=self._usb_monitor` into `prepare_drive`/`adopt_drive`.
    - In `_on_efis_drive_mounted`: if the mounted drive matches an in-progress key, log and RETURN without launching `_run_archive`/`_run_drive_update`.
    - _Bug_Condition: isBugCondition(op) — concurrent auto-sync for the same drive (Defect 2)_
    - _Expected_Behavior: Property 1 — no concurrent auto-sync while prepare/adopt is in progress_
    - _Preservation: normal managed-mount auto-sync unchanged when no prepare in progress (Property 2)_
    - _Requirements: 2.2, 3.1, 3.3_

  - [ ] 3.3 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Prepare/Adopt Populate Routes Through The Mount Swap And Does Not Race
    - **IMPORTANT**: Re-run the SAME test from task 1 — do NOT write a new test.
    - Run `tests/test_prop_prepare_adopt_uses_swap.py`.
    - **EXPECTED OUTCOME**: Test PASSES (populate runs inside `msdos_mount` on the work mount; no concurrent auto-sync launches for an in-progress drive) — confirms the bug is fixed.
    - _Requirements: 2.1, 2.2, 2.3_

  - [ ] 3.4 Verify preservation tests still pass
    - **Property 2: Preservation** - Managed Auto-Sync And Sync Semantics Unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 — do NOT write new tests.
    - Run `tests/test_prop_prepare_adopt_preserves_autosync.py`.
    - **EXPECTED OUTCOME**: Tests PASS (normal managed-mount auto-sync, identity-before-populate, and update_drive semantics unchanged) — confirms no regressions.
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [ ] 4. Checkpoint - Ensure all tests pass
  - Run the full test suite (`./venv/bin/python -m pytest`); ensure all tests pass, including the existing `chart-sync-stall-fix` mount-swap/usb-monitor tests (no regressions).
  - Manually validate Prepare Drive → Start clean on one of the two remaining fresh test drives (EFIS_4 is being salvaged by the auto-sync fallback and is not a clean test).
  - Release step (per versioning policy): bump `__version__` and `MENUBAR_VERSION` to the v1.5.1 patch, keep `pyproject.toml` `version` in sync with `__version__`, and leave `DASHBOARD_VERSION` unchanged. Tag `v1.5.1`.
  - Ask the user if any questions arise.
