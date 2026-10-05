# Implementation Plan

## Overview

Replace the chart/nav write mechanism with: compose a FAT32 image on the local
SSD, `dd` it to the drive's raw partition slice (`/dev/rdiskNsN`), and verify by
raw readback (no mount). This is implemented in Python (the existing project
language: pytest + Hypothesis), bottom-up and test-driven. Pure, headless-
testable pieces land first — the composer image build, the destructive-write
device guard, and the raw-readback verifier — each with Hypothesis property
tests mirroring the existing `test_mount_swap_privileged` style. Privileged
wiring (extended `run_privileged` allow-list + `sudoers.d` grant) and app
integration (event-driven refresh, persistent notification + badge, write-on-sync
with the `msdos_mount` fallback) follow once the pure cores are proven.

Everything that performs a real `dd`, a real raw readback over USB, or a real
EFIS read is a **MANUAL** harness step (like the existing `repro_harness`), not
CI. A **safety checkpoint** gates every task that can write a raw device.

The existing engine stays intact: `mount_swap.run_privileged` /
`validate_device_node` / `msdos_mount` are reused and extended (not rewritten),
the `drive_updater` identity helpers (`read_identity` / `write_identity` /
`_ensure_identity`, `EFIS_DRIVE_ID.json`) are reused, and `currency` + the
archive/FDL path are untouched.

**DECISION TO LOCK IN AT TASK 1 — sidecar-free populate method:** primary is
`rsync --no-xattrs` with `._*` / `.DS_Store` excluded (the source tree on SSD
carries no resource forks, and `--no-xattrs` stops rsync synthesizing
AppleDouble), followed by a mandatory post-populate strip of any residual
`._*` / `.DS_Store` and a hard assert that the finished image contains none.
`COPYFILE_DISABLE` is NOT relied on (it did not suppress the mount-layer sidecars
in the current path). Task 1.1 validates and locks this method before the rest of
the composer is built.

## Tasks

- [ ] 1. Composer core — local FAT32 image build (headless)
  - [ ] 1.1 Lock in and implement the sidecar-free populate method
    - Add an `image_composer` module with a populate step that copies a source
      tree into the mounted local image via `rsync --no-xattrs` excluding `._*`
      and `.DS_Store`, then strips any residual `._*` / `.DS_Store`, then asserts
      the finished image contains none.
    - Decide and document the method here (primary `rsync --no-xattrs` + strip +
      assert); record why `COPYFILE_DISABLE` is insufficient.
    - _Requirements: 3.1, 3.2, 3.3_

  - [ ] 1.2 Implement image creation sized to the target slice
    - Create the FAT32 image file at a configured path sized to a given slice
      byte length via `/sbin/newfs_msdos`; attach locally with `/usr/bin/hdiutil`
      for population; detach when done. Return `ComposeResult` (image_path,
      image_sha256, byte_size, families_included, composed_at).
    - _Requirements: 1.1, 2.1, 2.2_

  - [ ] 1.3 Implement current-source population + baked completion marker
    - Populate ChartData from the configured USB image path, NAV.DB / NAV-proc.DB
      from the current nav source, and `EFIS_DRIVE_ID.json` (drive's own identity
      via `drive_updater.read_identity`, or newly minted) — never from an old
      drive. Write the completion marker LAST.
    - _Requirements: 1.2, 1.3, 1.4, 1.5, 4.1_

  - [ ]* 1.4 Property test: composer contents, current-source, drive-independence
    - **Property family A — Composition is correct and drive-independent (design Property 5)**
    - For any fixture source tree, the finished image contains exactly the
      sourced charts + nav + identity + marker; compose touches no `/dev/disk`
      and no `/Volumes` USB mount (device access mocked to raise) and still
      succeeds with no drive attached.
    - Hypothesis, min 100 iterations; tag: "Feature: image-based-drive-write,
      Property 5: composition is drive-independent".
    - **Validates: Requirements 1.2, 1.3, 1.5, 1.6, 4.1**

  - [ ]* 1.5 Property test: image is sidecar-free
    - **Property family B — Sidecar-free populate**
    - For any source tree (including entries salted with `._*` / `.DS_Store`),
      the finished image contains zero `._*` and zero `.DS_Store` entries.
    - Hypothesis, min 100 iterations; tag: "Feature: image-based-drive-write,
      Property: sidecar-free image".
    - **Validates: Requirements 3.1, 3.2, 3.3**

  - [ ]* 1.6 Property test: image byte size equals target slice size
    - **Property family C — Image sized to slice**
    - For any valid target slice byte size, the composed image `byte_size`
      equals the slice size.
    - Hypothesis, min 100 iterations; tag: "Feature: image-based-drive-write,
      Property: image sized to slice".
    - **Validates: Requirements 2.1**

- [ ] 2. Destructive-write device guard (headless)
  - [ ] 2.1 Implement the guard extending `validate_device_node`
    - Add a `device_guard` function that reuses `mount_swap.validate_device_node`
      (`/dev/diskNsN` + removable + FAT32 via diskutil) and additionally requires:
      target slice size == expected image size, volume label / identity ==
      expected, and an explicit confirmation flag. Any mismatch or unverifiable
      check raises (like `PrivilegeError`). Produce `DeviceGuardInfo`.
    - _Requirements: 2.4, 7.1, 7.2, 7.3, 7.4, 7.5, 7.6_

  - [ ]* 2.2 Property test: guard refuses any single mismatch
    - **Property family D — Destructive-write safety (design Property 4)**
    - For guard-info with exactly one attribute wrong (non-removable /
      non-FAT32 / wrong size / wrong label / unconfirmed), the guard refuses;
      a fully-matching, confirmed node is permitted. Mock diskutil, mirroring
      `test_mount_swap_privileged`.
    - Hypothesis, min 100 iterations; tag: "Feature: image-based-drive-write,
      Property 4: destructive-write safety".
    - **Validates: Requirements 7.1, 7.2, 7.3, 7.4, 7.5, 7.6**

- [ ] 3. Raw-readback verifier (headless)
  - [ ] 3.1 Implement the verifier
    - Add a `verify(image_path, readback_source) -> bool` that hashes the raw
      readback region and compares to the composed image hash; the readback
      source is injectable (a byte buffer in tests, `/dev/rdiskNsN` in
      production) so the hash logic is testable without a device. Match ->
      complete; mismatch/short-read -> incomplete.
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 4.3_

  - [ ]* 3.2 Property test: verified completion iff hash match
    - **Property family E — Verified completion (design Property 3)**
    - For any image buffer, a matching readback reports complete; mutating any
      byte or truncating the readback reports incomplete.
    - Hypothesis, min 100 iterations; tag: "Feature: image-based-drive-write,
      Property 3: verified completion".
    - **Validates: Requirements 6.2, 6.3, 6.4**

  - [ ]* 3.3 Property test: interrupted write leaves local image/state intact
    - **Property family F — Marker atomicity / no local corruption (design Property 2)**
    - For any interruption offset (mock `dd` aborts), the local image file and
      composer state are byte-identical before and after, and the write status
      is incomplete (a partial write never presents a complete marker on readback).
    - Hypothesis, min 100 iterations; tag: "Feature: image-based-drive-write,
      Property 2: marker atomicity".
    - **Validates: Requirements 4.2, 5.4**

- [ ] 4. Checkpoint — Ensure all headless composer/guard/verifier tests pass
  - Ensure all Task 1-3 unit and property tests pass under the venv; ask the
    user if questions arise. (No raw device has been touched yet.)

- [ ] 5. Interim privileged backend for raw write + read
  - [ ] 5.1 Extend `run_privileged` allow-list and add the write/read commands
    - Add the raw-write (`/bin/dd` to `/dev/rdiskNsN`) and raw-read binaries to
      `mount_swap._ALLOWED_BINS`; extend `_validate_argv` so these commands are
      accepted only against a `/dev/rdiskNsN` device token and refused otherwise.
      Add the `ImageWriter.write` and verifier raw-read calls routing through
      `run_privileged`.
    - _Requirements: 5.1, 5.2, 5.3, 8.1, 8.2, 8.4, 8.5_

  - [ ]* 5.2 Property test: privileged argv validation refuses bad binary/device
    - **Property family G — Privileged allow-list refusal**
    - For argv with a disallowed binary or a device token not matching
      `/dev/rdiskNsN`, validation refuses; the allowed write/read argv passes.
    - Hypothesis, min 100 iterations; tag: "Feature: image-based-drive-write,
      Property: privileged allow-list refusal".
    - **Validates: Requirements 8.2, 8.4**

  - [ ] 5.3 Install the extended narrow `sudoers.d` grant
    - Update `install.sh` to extend `/etc/sudoers.d/efis-data-manager` with
      NOPASSWD for ONLY the specific raw-write and raw-read commands, pinned to
      the exact binary paths and the `/dev/rdisk*s*` device pattern; add matching
      uninstall/removal. Document the accepted interim risk inline. (Signed
      helper is out of scope — roadmap "sudo retirement".)
    - _Requirements: 8.3_

- [ ] 6. SAFETY CHECKPOINT — before any task that writes a raw device
  - Confirm with the user before running any step that issues a real `dd` to a
    device: the device guard (Task 2) and verifier (Task 3) pass their tests, the
    target is a disposable scratch drive (e.g. `EFIS_3`), and the extended
    sudoers grant is scoped to `/dev/rdisk*s*`. No real `dd` runs until this
    checkpoint is explicitly cleared.

- [ ] 7. Wire the writer end to end (guard -> dd -> verify)
  - Add `ImageWriter.write(image_path, device_node)` that runs the device guard,
    performs the `dd` to the raw slice via `run_privileged`, then calls the
    verifier via raw readback; return `WriteResult`
    (device_node, bytes_written, verified, status, error). On guard refusal or
    verify failure, report incomplete and leave the local image/state untouched.
    Treat the whole image as the unit of work; recovery is re-write + re-verify.
  - _Requirements: 2.3, 4.2, 5.1, 5.3, 6.1, 7.1, 13.1, 13.2, 13.3_

- [ ] 8. App integration — event-driven refresh
  - Hook the composer refresh onto the EXISTING data-manager checks: when the
    chart check (`@rumps.timer(43200)`) or nav check (`@rumps.timer(86400)`)
    reports newly downloaded data, trigger a compose; do NOT add a separate
    polling timer. On compose failure, keep the previous good image.
  - _Requirements: 9.1, 9.2, 9.3, 9.4_

  - [ ]* 8.1 Unit tests for the refresh hook
    - Check reports new data -> composer invoked; reports no new data -> not
      invoked; compose raises -> previous image retained; assert no new timer is
      registered.
    - _Requirements: 9.1, 9.2, 9.3, 9.4_

- [ ] 9. App integration — persistent notification + menu-bar badge
  - Add a persistent menu item reflecting image state ("USB image current" ->
    "New image ready (nav <date> + plates <cycle>)") mirroring the existing
    "Download Charts" dynamic menu item; make it actionable to begin preparing a
    drive; badge `self.title` when a fresh image is ready; reset both when the
    user acts.
  - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5_

  - [ ]* 9.1 Unit tests for notification state transitions
    - Driving fresh-image state sets the menu item text (naming nav + chart
      cycle) and the title badge; acting on it resets both to current.
    - _Requirements: 10.1, 10.2, 10.3, 10.5_

- [ ] 10. App integration — write-on-sync with mount-swap fallback, preserving currency/archive
  - Route the chart/nav write through the image-write path when enabled; keep
    `msdos_mount` available and fall back to it when a config flag selects it or
    when image-write fails. Preserve the existing currency decision (current ->
    no write) and the archive/FDL-on-insert path unchanged; image-write replaces
    only HOW charts/nav are written when a write is needed.
  - _Requirements: 11.1, 11.2, 11.3, 11.4, 12.1, 12.2, 12.3, 12.4_

  - [ ]* 10.1 Unit tests for write-path routing and preservation
    - Enabled -> image path (no `msdos_mount`); fallback flag -> `msdos_mount`;
      image-write failure + fallback -> `msdos_mount`; currency current -> no
      write; archive-on-insert still runs.
    - _Requirements: 11.1, 11.2, 11.3, 12.1, 12.2, 12.3_

- [ ] 11. Checkpoint — Ensure all unit + property tests pass
  - Ensure all headless and app-integration tests pass under the venv; ask the
    user if questions arise.

- [ ] 12. MANUAL hardware harness (not CI) — real dd + raw readback + EFIS read
  - Extend the existing `repro_harness` style with a MANUAL compose -> `dd` to
    `/dev/rdiskNsN` on scratch drive `EFIS_3` -> raw-readback-hash verify ->
    read in the real GRT EFIS. Record timings vs the current rsync+swap path.
    Gated by the Task 6 safety checkpoint. This step is MANUAL and excluded from
    CI (like the existing physical-drive harness).
  - _Requirements: 5.1, 6.1, 6.3, 2.3_

- [ ] 13. Release / versioning
  - Per the versioning policy (menu-bar / data-path change): tag the repo, bump
    `__version__` in `src/efis_data_manager/__init__.py`, keep
    `pyproject.toml` `version` in sync, bump `MENUBAR_VERSION`, and leave
    `DASHBOARD_VERSION` unchanged. Confirm no new third-party dependency was
    added (native tools only: `newfs_msdos`, `dd`, `hdiutil`).
  - _Requirements: 14.1, 14.2, 14.3_

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "3.1", "5.3"] },
    { "id": 1, "tasks": ["1.2", "2.2", "3.2", "3.3", "5.1"] },
    { "id": 2, "tasks": ["1.3", "5.2"] },
    { "id": 3, "tasks": ["1.4", "1.5", "1.6"] },
    { "id": 4, "tasks": ["7", "8", "9", "10"] },
    { "id": 5, "tasks": ["8.1", "9.1", "10.1"] },
    { "id": 6, "tasks": ["12"] },
    { "id": 7, "tasks": ["13"] }
  ]
}
```

Critical path: 1.1 -> 1.2 -> 1.3 -> (composer property tests) -> 7 (writer wiring)
-> safety checkpoint -> 12 (manual hardware) -> 13 (release). The guard (2) and
verifier (3) are independent of the composer build and run in parallel in early
waves.

## Notes

- **Sidecar-free populate decision (locked at Task 1.1):** `rsync --no-xattrs`
  with `._*` / `.DS_Store` excluded, then strip residual `._*` / `.DS_Store`,
  then hard-assert the finished image contains none. `COPYFILE_DISABLE` is NOT
  relied on — it did not suppress the mount-layer sidecars in the current path.
- **Safety first:** Task 6 is a mandatory safety checkpoint before ANY real `dd`.
  The device guard (Task 2) is the highest-priority first-class requirement and
  must pass its property tests before Task 7/12.
- **Manual vs CI:** real `dd`, real raw readback over USB, and real EFIS reads
  are MANUAL harness steps (Task 12), not CI — mirroring the existing
  `repro_harness` / physical-drive pattern. All other tests are headless and run
  in CI.
- **Design Correctness Properties -> tasks:** Property 1 (no mount) is enforced by
  construction across Tasks 7/3/12 (write + verify never mount) and asserted in
  1.4's drive-independence check; Property 2 -> Task 3.3; Property 3 -> Task 3.2;
  Property 4 -> Task 2.2; Property 5 -> Task 1.4. Each property test runs at min
  100 Hypothesis iterations, tagged "Feature: image-based-drive-write, ...".
- **Reuse, don't rewrite:** `mount_swap.run_privileged` /
  `validate_device_node` / `msdos_mount` are extended; `drive_updater` identity
  helpers and `currency` / archive path are untouched.
- **Versioning:** menu-bar / data-path change -> on release bump
  `MENUBAR_VERSION` (and `__version__` + `pyproject.toml`); leave
  `DASHBOARD_VERSION` unchanged (Task 13).
- Tasks marked with `*` are optional test sub-tasks and may be skipped for a
  faster MVP; core implementation tasks are never optional.
- Out-of-scope deferrals (settings staging, software staging, device binding,
  signed helper) are NOT in this task list — see requirements Non-Goals.
