# Implementation Plan

## Overview

Fix the FSKit-driven 120s write stall by slotting a thin **mount-swap layer**
underneath the existing, unchanged sync engine: for the write window only, the
managed drive is unmounted from its FSKit `/Volumes/` mount and remounted via
`/sbin/mount_msdos` (the confirmed non-stalling in-kernel msdosfs path) at a
private app-owned mountpoint, then robustly restored to FSKit afterward. All
privileged calls funnel through a single `run_privileged` shim so the privilege
backend is swappable with no engine change.

Delivery is in **two phases** (user-approved committed decision):

- **Phase 1** ships a fully working fix on the interim **Option C** narrow
  `sudoers.d` backend — proves the entire mount-swap workflow end-to-end on real
  drives with no code signing and no paid Apple Developer ID.
- **Phase 2** migrates to the **Option A** signed `SMAppService` privileged
  helper. It is **gated on Phase-1 success** (the point at which the Developer ID
  is acquired) and swaps ONLY the `run_privileged` backend + install path — the
  sync engine and mount-swap logic do not change.

The existing `drive-sync-integrity` engine internals (`build_jobs`,
`sync_payload`, `verify_family`, commit markers, id-keyed sync-state) are reused
**as-is** and are not modified — they run unchanged against whatever
`mount_point` they are handed.

## Tasks

### Phase 1 — Working fix on interim sudoers backend

- [x] 1. Add mount-swap module skeleton, pure helpers, and privileged shim (sudoers backend)
  - [x] 1.1 Create `mount_swap.py` with pure helpers and transient swap state
    - Add `src/efis_data_manager/mount_swap.py` with `private_mountpoint(label)`
      returning an app-owned path outside `/Volumes/` (e.g.
      `/private/tmp/efis-datamanager/<label>`), created `0700`, owned by the user.
    - Add `resolve_device_node(mount_point)` that parses `diskutil info -plist`
      with `plistlib`, reads the BSD name / `DeviceNode` key, and returns `None`
      on non-zero exit / unparseable plist / missing key (None-safe).
    - Define the transient per-swap state fields (`device_node`, `label`,
      `work_mount`, `swap_state` in `entered`/`mounted`/`restored`).
    - _Requirements: 3.1, 6.4, 8.1_

  - [x]* 1.2 Write unit tests for `resolve_device_node` and `private_mountpoint`
    - Given canned `diskutil info -plist` output, returns the BSD node; returns
      `None` on non-zero exit, unparseable plist, and missing key.
    - `private_mountpoint` derives a distinct `0700` path per volume label.
    - _Requirements: 3.1, 6.4_

  - [x] 1.3 Implement the single `run_privileged` shim with sudoers backend + argument validation
    - Add `run_privileged(argv, timeout) -> subprocess.CompletedProcess` that
      executes **argv arrays only** (never a shell string) via the interim
      sudoers backend (`sudo <exact binary> ...`).
    - Validate before every call: the target device is a removable FAT32 slice
      and the mountpoint is under the app's private dir; refuse anything else.
    - Confine the injection surface to this one audited function.
    - _Requirements: 3.1, 8.1_

  - [x]* 1.4 Write unit tests for `run_privileged` argument validation
    - Commands are built as argv arrays (no shell string); arguments outside the
      allowed removable-FAT32-device / private-mountpoint set are rejected.
    - _Requirements: 8.1_

- [x] 2. Install the interim narrow `sudoers.d` grant (Phase 1 privilege backend)
  - Update `install.sh` to drop `/etc/sudoers.d/efis-data-manager` granting the
    install user `NOPASSWD` for ONLY the exact commands: `/sbin/mount_msdos`,
    `/usr/sbin/diskutil unmount`, `/usr/sbin/diskutil mount`, with pinned binary
    paths and no wildcards where avoidable, behind the admin password the
    installer already collects.
  - Add a matching uninstall/removal path for the sudoers file.
  - Document the accepted interim risk (broad `mount_msdos` grant) inline.
  - _Requirements: 3.3, 8.1_

- [x] 3. Implement mount/unmount lifecycle and robust teardown
  - [x] 3.1 Implement `msdos_mount` context manager and FSKit unmount / `mount_msdos` remount
    - Add `@contextmanager msdos_mount(fskit_mount_point) -> work_mount` that
      resolves the BSD node BEFORE unmount, quiesces the poller, `diskutil
      unmount`s the FSKit mount, creates the private mountpoint, and calls
      `run_privileged(['/sbin/mount_msdos', device_node, work_mount], ...)`.
    - Detect and unmount any stray `/Volumes/<label>` reappearance before
      proceeding.
    - _Requirements: 3.1, 3.3, 5.1, 5.2_

  - [x] 3.2 Implement `robust_unmount` and `restore_fskit_mount`
    - `robust_unmount(work_mount, device_node)`: settle (`os.sync()` + short
      wait), prefer `diskutil unmount` over raw `umount`, use `lsof +D` to find
      holders, retry with bounded backoff, escalate to `diskutil unmount force`
      only after bounded retries (logged loudly). Returns True once unmounted.
    - `restore_fskit_mount(device_node)`: `diskutil mount <device_node>` so the
      volume reappears under `/Volumes/` and Finder/eject behave normally.
    - _Requirements: 3.3, 5.1, 7.5_

  - [x]* 1.5 Write unit tests for the rollback state machine and `robust_unmount`
    - Rollback: for each `swap_state` at which a mocked `run_privileged` raises,
      teardown reaches a terminal state that is "restored to FSKit" OR
      "explicitly reported needs-reinsert" — never "left unmounted silently" and
      never "reported current".
    - `robust_unmount`: simulate "Resource busy" then success; assert it prefers
      `diskutil unmount`, retries with backoff, and escalates to `force` only
      after bounded retries.
    - _Requirements: 3.3, 7.5, 6.4_

- [x] 4. Add poller quiesce to `usb_monitor`
  - [x] 4.1 Implement `pause()`/`resume()` quiesce in `usb_monitor.py`
    - Add a quiesce flag consulted at the top of `_poll_loop` and by the
      mount/eject callbacks so the 2s `/Volumes/` poller does not react to the
      transient disappearance/reappearance of the volume during the swap.
    - On `resume()`, re-read `/Volumes/` and rebaseline the restored mount as
      pre-existing (no spurious auto-sync / no redundant mount event).
    - _Requirements: 6.4, 8.1_

  - [x]* 4.2 Write unit tests for poller quiesce
    - With the monitor paused, injected `/Volumes/` churn fires NO mount/unmount
      callbacks; on resume the restored mount is rebaselined (no spurious
      auto-sync).
    - _Requirements: 6.4_

- [x] 5. Wire the mount-swap context into `app._run_drive_update`
  - Wrap BOTH write windows (the verify+repair pending-families branch AND the
    `update_drive` branch) in `with msdos_mount(mount_point) as work_mount:` so
    the existing engine runs `build_jobs(work_mount, families)` unchanged against
    the private work mount.
  - Keep identity/currency reads either on the FSKit mount before the swap or on
    `work_mount` inside it, per the design.
  - On swap failure (typed error from `msdos_mount`): restore the FSKit mount,
    report the sync incomplete, name the affected family, leave interrupted
    sync-state intact, and surface safe-next-step guidance; restore FSKit on all
    paths.
  - Do NOT modify engine internals (`build_jobs`/`sync_payload`/`verify_family`/
    commit markers/sync-state).
  - _Requirements: 5.1, 5.2, 5.3, 6.4, 7.1, 7.2, 7.4, 7.5_

- [x] 6. Checkpoint - Ensure all unit tests pass
  - Ensure all Phase-1 mock-based unit tests pass, ask the user if questions arise.

- [x] 7. Implement the three Correctness Properties as Hypothesis property tests
  - [x]* 7.1 Property test: swap never ends unmounted-and-unreported
    - **Property 1: The swap never ends with the drive unmounted-and-unreported**
    - For any sequence of injected failures at any lifecycle point (mocked
      privileged calls), the teardown terminal state is "restored to FSKit" OR
      "failure explicitly reported with reinsert instruction" — never "unmounted
      / on private mountpoint with success reported".
    - Hypothesis, min 100 iterations; tag: "Feature: chart-sync-stall-fix,
      Property 1: swap never ends unmounted-and-unreported".
    - **Validates: Requirements 7.1, 7.2, 7.5**

  - [x]* 7.2 Property test: commit marker only on clean verify
    - **Property 2: A commit marker is written only on a clean verify**
    - For any family run against the swapped mountpoint, the commit marker is
      present iff count+size verification passed with no errors and no abort; any
      abort/failure/mismatch leaves interrupted state and no marker.
    - Hypothesis, min 100 iterations; tag: "Feature: chart-sync-stall-fix,
      Property 2: commit marker only on clean verify".
    - **Validates: Requirements 6.1, 6.2, 6.4**

  - [x]* 7.3 Property test: sync-state attributed by drive id, invariant to mount path
    - **Property 3: Sync-state is attributed by drive id, invariant to mount path**
    - For any mountpoint the write window runs against (FSKit `/Volumes/` path or
      any private `mount_msdos` path), `begin_family`/`complete_family`/
      `pending_families` resolve to the same durable `drive_id`.
    - Hypothesis, min 100 iterations; tag: "Feature: chart-sync-stall-fix,
      Property 3: sync-state attributed by drive_id invariant to mount path".
    - **Validates: Requirements 5.3, 6.4**

- [x] 8. Build instrumentation and root-cause reproducibility harness
  - Add instrumentation that records app-side stall onset time + write-progress
    state at that moment (from the watchdog's log-size tracking).
  - Capture concurrent FSKit/kernel events via `log show`/`log stream` filtered
    to `com.apple.fskit.msdos` lifecycle (init/`clientDied`) and "Denying
    dirty-tracking opt-in" messages, and correlate with app stall intervals by
    timestamp.
  - Record observed mount flags (`fskit` vs `noowners`) and loaded kexts
    (`kmutil showloaded` / msdosfs-kext-load evidence).
  - Emit a written determination artifact recording confirmation, or the
    evidence gap + additional instrumentation needed if inconclusive.
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 3.5_

- [x] 9. Build the reproduction / acceptance harness
  - Implement an incremental top-up reproduction against test drive `EFIS_3`
    (partially populated, missing the plates marker and ~6,670 plates files).
  - Implement a from-scratch full-populate reproduction (~101,563 files / 9.1 GB
    to a freshly formatted `EFIS_3`).
  - Each run records whether a 120s no-progress stall occurred and measured write
    throughput + cumulative write-progress over time.
  - Express the acceptance gate as pass/fail: (1) no 120s stall, (2) every family
    verifies clean on count+size, (3) idempotent no-op re-run reports current
    without re-attempting a stalled write.
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 5.4_

- [x]* 10. Physical-drive integration tests (adopted mitigation, EFIS_3)
  - Incremental top-up and from-scratch full populate complete with no 120s
    stall, verified count+size, idempotent no-op re-run.
  - A reinsert of a formerly-interrupted drive (after a completed sync) reports
    current without re-attempting the stalled write.
  - Eject/sleep mid-sync leaves the family interrupted+resumable and the drive
    returns to a normal FSKit mount.
  - Induced verify mismatch leaves no commit marker and reports not current.
  - _Requirements: 5.1, 5.2, 5.4, 6.1, 6.2, 6.4, 7.3_

- [x] 11. Test scaffolding and `.gitattributes` exclusion
  - Add the mount-swap tests under the existing local `tests/` tree; ensure the
    `.gitattributes` `export-ignore` for `tests/` keeps them out of release ZIPs.
  - Ensure all property/unit tests run green under the venv.
  - _Requirements: 8.1_

- [x] 12. Phase-1 release / versioning
  - Per the versioning policy (menu-bar change): on release, tag the repo, bump
    `__version__` in `src/efis_data_manager/__init__.py` and keep
    `pyproject.toml` `version` in sync; bump `MENUBAR_VERSION`; leave
    `DASHBOARD_VERSION` unchanged.
  - _Requirements: 8.2, 8.3_

- [x] 13. Phase-1 acceptance checkpoint (Phase-2 gate)
  - Ensure the full mount-swap workflow (unmount -> `mount_msdos` -> sync ->
    verify -> marker -> restore) passes the acceptance gate on real drives and
    all tests pass. This success is the gate that authorizes Phase 2 and the
    Apple Developer ID acquisition. Ask the user if questions arise.

### Phase 2 — Migrate to signed SMAppService helper (gated on Phase 1)

> Gated on Phase-1 acceptance (task 13). This is the point at which the paid
> Apple Developer ID is acquired. Tasks 14-17 swap ONLY the `run_privileged`
> backend + install path; the sync engine and mount-swap logic do not change.

- [ ] 14. Adopt code signing + notarization (Developer ID)
  - Introduce Developer ID code signing + notarization for the `.app` bundle in
    the build/`install.sh` flow, with matching designated requirements
    (`SMAuthorizedClients` / `SMPrivilegedExecutables`) for the helper.
  - _Requirements: 8.1_

- [ ] 15. Build the signed `SMAppService`/launchd privileged helper
  - Implement a tiny single-purpose root helper registered via `SMAppService`
    (launchd daemon) exposing ONLY: unmount device X, `mount_msdos` device X at a
    mountpoint under the app private dir, restore-FSKit-mount device X.
  - Implement local XPC/socket IPC between the unprivileged app and the helper.
  - Validate arguments in the helper (target is a removable FAT32 device;
    mountpoint under the app private dir); refuse anything else.
  - _Requirements: 8.1_

  - [ ]* 15.1 Write unit tests for helper argument validation and IPC contract
    - Helper rejects non-removable / non-FAT32 devices and mountpoints outside
      the app private dir; accepts only the whitelisted operations.
    - _Requirements: 8.1_

- [ ] 16. Swap the `run_privileged` backend from sudoers to the signed helper
  - Reimplement ONLY the `run_privileged` backend in `mount_swap.py` to route
    calls through the helper IPC instead of `sudo`. No change to `msdos_mount`,
    `robust_unmount`, `restore_fskit_mount`, `usb_monitor` quiesce, or the engine.
  - _Requirements: 8.1_

  - [ ]* 16.1 Update unit tests for the helper-backed `run_privileged`
    - Argv-array contract and argument validation still hold against the helper
      backend; injection surface remains confined to the shim.
    - _Requirements: 8.1_

- [ ] 17. Update installer and retire the interim sudoers grant
  - Update `install.sh` to register/bless the signed helper (replacing the
    "copy files + LaunchAgent" step with helper registration).
  - Remove/retire the interim `/etc/sudoers.d/efis-data-manager` grant from
    fresh installs and add an upgrade step that removes it from existing installs.
  - _Requirements: 3.3, 8.1_

- [ ] 18. Phase-2 release / versioning
  - Per the versioning policy (menu-bar change): on release, tag the repo, bump
    `__version__` and keep `pyproject.toml` `version` in sync; bump
    `MENUBAR_VERSION`; leave `DASHBOARD_VERSION` unchanged.
  - _Requirements: 8.2, 8.3_

- [ ] 19. Final checkpoint - Ensure all tests pass
  - Ensure all unit + property tests pass and the acceptance harness passes under
    the signed-helper backend; ask the user if questions arise.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.3", "2", "8"] },
    { "id": 1, "tasks": ["1.2", "1.4", "3.1", "4.1"] },
    { "id": 2, "tasks": ["3.2", "4.2", "9"] },
    { "id": 3, "tasks": ["1.5", "5"] },
    { "id": 4, "tasks": ["7.1", "7.2", "7.3", "10", "11"] },
    { "id": 5, "tasks": ["12"] },
    { "id": 6, "tasks": ["14"] },
    { "id": 7, "tasks": ["15"] },
    { "id": 8, "tasks": ["15.1", "16"] },
    { "id": 9, "tasks": ["16.1", "17"] },
    { "id": 10, "tasks": ["18"] }
  ]
}
```

Critical path (Phase 1): 1.1/1.3 -> 3.1 -> 3.2 -> 5 -> property/integration tests
-> 12 -> (Phase-1 acceptance gate) -> 14 -> 15 -> 16 -> 17 -> 18.

## Notes

- **Two-phase gating:** Phase 1 (tasks 1-13) ships a working fix on the interim
  `sudoers.d` backend with no signing. Phase 2 (tasks 14-19) is gated on Phase-1
  acceptance (task 13) and is the point at which the Apple Developer ID is
  acquired.
- **Single seam:** every privileged call funnels through `run_privileged`, so
  Phase 2 swaps only that backend + the install path — the sync engine and
  mount-swap logic are untouched across the migration.
- **Engine untouched:** `build_jobs`/`sync_payload`/`verify_family`/commit
  markers/sync-state run unchanged against whatever `mount_point` they are handed.
- **Interim risk accepted:** the broad `NOPASSWD mount_msdos` sudoers grant is a
  documented, user-accepted risk for Phase 1, retired in Phase 2 (task 17).
- **Tests** live in the local `tests/` tree and are excluded from release ZIPs
  via `.gitattributes export-ignore`.
- **Correctness Properties 1-3** (design.md) map to tasks 7.1, 7.2, 7.3, each a
  Hypothesis property test at min 100 iterations, tagged
  "Feature: chart-sync-stall-fix, Property N: ...".
- **PBT scope:** only the three pure invariants are property-tested; the
  side-effecting mount/unmount orchestration is covered by mock-based unit tests
  and physical-drive integration tests.
- **Versioning:** this is a menu-bar-tool change — on each release bump
  `MENUBAR_VERSION` (and `__version__` + `pyproject.toml`); leave
  `DASHBOARD_VERSION` unchanged.
- Tasks marked with `*` are optional test sub-tasks and may be skipped for a
  faster MVP; core implementation tasks are never optional.
