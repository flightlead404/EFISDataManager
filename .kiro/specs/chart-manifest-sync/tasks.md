# Implementation Plan: Chart Manifest Sync

## Overview

This plan implements the design in the exact staged order of the design's
"Retirement plan", so the shipped sync is never broken mid-refactor: new code is
**added alongside** the live `--size-only` transfer first, the transfer is then
switched to manifest-driven `rsync --files-from` with the per-stream cycle gate,
the write dispatch is collapsed to the single FSKit `/Volumes` mount, and only
then are the retired paths (mount-swap, image-dd) deleted. The post-sync relabel
reuses `build_volume_label` + `_cycles_from_metadata`, which are **relocated**
(not deleted) before the image-dd module is removed.

Implementation language is **Python** (the existing codebase). Tests run via
`./venv/bin/python -m pytest` and use Hypothesis for the 8 correctness
properties (min 100 iterations each, tagged
`Feature: chart-manifest-sync, Property N: <text>`).

There is **no Integrity-Verify feature** in this spec — the diagnostic verify
was cut. Do not add any verify module, task, or test.

Manual acceptance (NOT a task): real-hardware full-cycle and alternating-cycle
writes (~2.5 h, longer with HI enabled) are validated by hand on a physical card
between stages per the Retirement plan. These are never automated.

## Tasks

- [x] 1. Stage 1 — Add the manifest module and download-time build alongside the live `--size-only` sync
  - [x] 1.1 Create `src/efis_data_manager/manifest.py` with the manifest data model and builder
    - Define `ManifestEntry` (frozen: `relpath`, `sha256`, `size`), `FamilyManifest` (`family`, `cycle`, `entries` dict, `built`, `schema_version`), and `CardManifest` (`schema_version`, `kind`, `charts_cycle`, `plates_cycle`, `written`, `families` dict), per the design Components and Data Models sections.
    - Implement `Manifest_Builder.build(family, family_root, cycle)`: walk `family_root`, SHA-256 each file (reuse the chunking/chunk size of `drive_updater._file_checksum`), key entries by `os.path.relpath(path, family_root)` in POSIX form; exclude `.DS_Store`, `._*`, and the per-family commit marker (`drive_updater.SCANNED_MARKER` / `PLATES_MARKER` basenames) so the manifest matches exactly what rsync transfers.
    - _Requirements: 5.1, 5.2_

  - [x] 1.2 Write property test for the manifest builder content signal
    - **Property 1: Content-change detection (closes the `--size-only` gap)**
    - Generate a file tree plus a mutation set that **includes same-byte-size content changes**; build before/after `FamilyManifest`s with `Manifest_Builder.build`; assert every mutated relpath has a differing `sha256` (and would appear in `changed`).
    - Tag: `Feature: chart-manifest-sync, Property 1: Content-change detection`; min 100 iterations.
    - _Requirements: 1.1, 1.2, 1.3, 1.4_

  - [x] 1.3 Implement `Manifest_Store` (local + card) with atomic card writes
    - Add `LOCAL_MANIFEST_DIR` under the app-support dir (`scanned.json`, `plates.json`) and `CARD_MANIFEST_NAME = "EFIS_MANIFEST.json"`.
    - Implement `load_local(family)` / `save_local(manifest)`; implement `load_card(mount_point)` returning `None` on missing/corrupt/unparseable, and `save_card(mount_point, card)` using temp + `fsync` + `os.replace` + `os.sync` (mirror `drive_updater.write_identity`).
    - _Requirements: 5.3, 5.4, 5.5_

  - [x] 1.4 Write property test for manifest round-trip
    - **Property 5: Manifest round-trip**
    - Generate arbitrary `FamilyManifest` values; assert `load_local(save_local(m))` yields an equal manifest (serialize -> parse is identity).
    - Tag: `Feature: chart-manifest-sync, Property 5: Manifest round-trip`; min 100 iterations.
    - _Requirements: 5.3_

  - [x] 1.5 Implement `Manifest_Diff` pure diff
    - Add `ManifestDiff` (`added`, `changed`, `removed`, `unchanged`, `files_from` property = added + changed) and `diff_family(local, card_family)` comparing by `relpath -> sha256`; size is a first discriminator but SHA-256 is authoritative; do NOT re-hash the card.
    - _Requirements: 4.1, 4.2_

  - [x] 1.6 Write property test for manifest/card convergence
    - **Property 2: Manifest/card convergence**
    - Generate a local manifest `L` and a card family dict `C`; apply `diff_family` to a fake card tree (add/overwrite `added`+`changed`, delete `removed`); assert the resulting card tree equals `L` and the updated card family entries equal `L.entries`.
    - Tag: `Feature: chart-manifest-sync, Property 2: Manifest/card convergence`; min 100 iterations.
    - _Requirements: 4.1, 4.3, 4.4_

  - [x] 1.7 Wire the download-time manifest build into `currency.py`
    - After a successful `extract_chart`, build the affected family's manifest via `Manifest_Builder.build` and persist it with `Manifest_Store.save_local`, alongside `chart_cycles.json`. Harvest the cycle number from the SA download-URL key of `load_cycle_metadata()` and from `valid_dates` (reuse the `extract_cycle` logic being relocated in task 5.1). A manifest build failure is logged at WARNING and does NOT fail download/extract (first-sync/absent-manifest fallback covers it later).
    - KEEP the existing `--size-only` `sync_payload` as the live transfer in this stage — manifests are computed and stored but do NOT yet drive transfer.
    - _Requirements: 5.1, 6.3_

  - [x] 1.8 Write unit test for cycle-number extraction
    - Assert the cycle harvest yields `"2611"` from a representative SA download-URL key and from a `valid_dates` prose range; assert `None` when neither is present.
    - _Requirements: 5.1_

- [x] 2. Stage 1 — IFR-high (HI) extraction placement fix in `currency._extract_multidisk`
  - [x] 2.1 Add the `.HI. -> ChartData/HI/` routing branch
    - In `_extract_multidisk`, add an `elif ".HI." in filename: sub_dir = "HI"` branch parallel to the existing `.LO.`/`.SEC.` branches, so HI files land at `ChartData/HI/W###/<file>` (hardware-confirmed uppercase `HI` dir, verified 2026-10-27) instead of the flat `else: sub_dir = ""` ChartData root.
    - HI rides the scanned family / 56-day charts cycle; it is NOT a separate stream and is gated by `chart_types.ifr_high` (default off) via the existing `CHART_TYPE_MATCHERS` / `_enabled_chart_substrings` path — no family-model change.
    - _Requirements: 10.1, 10.2, 2.4, 2.5_

  - [x] 2.2 Write unit test for HI routing
    - Build a small MultiDiskImg-shaped zip containing `.HI.`, `.LO.`, and `.SEC.` members; extract via `_extract_multidisk`; assert HI members land under `ChartData/HI/W###/` and LO/SEC land under their existing dirs, with nothing dumped loose at the `ChartData/` root.
    - _Requirements: 10.1, 10.2_

- [x] 3. Stage 2 — Switch the transfer to manifest-driven `--files-from` and wire the cycle gate
  - [x] 3.1 Add the per-stream cycle gate to `drive_updater.check_drive_currency`
    - Add `StreamDecision` and `decide_streams(mount_point, local, card)`: charts (scanned) and plates -> `"skip-cycle"` when local cycle == card cycle else `"diff"`; nav -> always `"evaluate"` (checksum, no cycle). No card manifest -> every chart family diffs against an EMPTY card manifest (full write); nav evaluates normally.
    - Use the `CardManifest` `charts_cycle`/`plates_cycle` as authoritative; cross-check the volume label (`C<charts>P<plates>`) and log a WARNING on disagreement, manifest wins.
    - _Requirements: 3.1, 3.2, 3.5, 3.6, 6.1_

  - [x] 3.2 Write property test for cycle-skip safety
    - **Property 3: Cycle-skip safety**
    - Assert local-cycle == card-cycle implies `"skip-cycle"` and the family's card tree is left byte-identical, and that a cycle is only recorded on the card after full convergence (modeled: skip is only safe because the recorded cycle followed a converged write).
    - Tag: `Feature: chart-manifest-sync, Property 3: Cycle-skip safety`; min 100 iterations.
    - _Requirements: 3.1, 3.3, 3.4_

  - [x] 3.3 Write property test for independent streams
    - **Property 4: Independent streams**
    - Generate the 2x2x2 matrix of (charts rolled?, plates rolled?, nav changed?); assert each stream's decision/outcome is independent and nav always evaluates regardless of either cycle.
    - Tag: `Feature: chart-manifest-sync, Property 4: Independent streams`; min 100 iterations.
    - _Requirements: 2.2, 2.3_

  - [x] 3.4 Replace `--size-only` with manifest-driven `--files-from` in `sync_payload`
    - For a chart family selected for diff, feed `diff.files_from` (added + changed relpaths) to `rsync --files-from=-` into the FSKit `/Volumes` mount; explicitly `os.unlink` each `diff.removed` relpath and prune empty dirs. Remove the `--size-only` flag. KEEP the temp-file stdout draining and the dual-signal (stdout-growth + CPU) stall watchdog (`STALL_TIMEOUT_SECONDS`, `_proc_cpu_seconds`) and the existing metadata excludes / `COPYFILE_DISABLE=1`.
    - _Requirements: 1.1, 4.3, 4.5_

  - [x] 3.5 Write the card manifest and commit marker after a verified family sync
    - After a family converges (post `complete_family`), update `CardManifest` for that family to equal the local manifest and persist via `Manifest_Store.save_card` (atomic). Write the per-family commit marker then the card manifest; on a card-manifest write failure treat the family as NOT completed, do not relabel, log at ERROR.
    - _Requirements: 4.4, 7.2, 7.3, 7.4, 7.5_

  - [x] 3.6 Preserve the nav files job unchanged
    - Confirm `_run_files_job` / `_file_checksum` continue to size+SHA-256 compare, copy-and-fsync changed nav files, deep-checksum verify, then `complete_family`, with no cycle gate and no manifest.
    - _Requirements: 8.1, 8.2, 8.3, 2.1_

  - [x] 3.7 Write property test for nav checksum correctness
    - **Property 7: nav checksum correctness (preserved)**
    - For any nav file whose content differs from the card copy, assert it is rewritten and the family is verified by deep checksum before completion.
    - Tag: `Feature: chart-manifest-sync, Property 7: nav checksum correctness`; min 100 iterations.
    - _Requirements: 8.1, 8.2, 8.3_

  - [x] 3.8 Write unit tests for first-sync and corrupt-manifest fallback
    - No card manifest -> full write of each enabled chart family, nav evaluated normally; corrupt/unparseable card manifest -> `load_card` returns `None`, treated as absent, full write, WARNING logged.
    - _Requirements: 6.1, 6.2_

- [x] 4. Stage 3 — Collapse the write dispatch in `app.py` to the single FSKit `/Volumes` mount
  - [x] 4.1 Remove the write-path branching and call the FSKit-mount write directly
    - In `_run_drive_update`, remove the `_perform_write` / `_write_via_image` / `_write_via_mount_swap` dispatch and the `msdos_mount` wrapping; run the sync directly against the FSKit `/Volumes` mount. KEEP `_device_chart_capable` / `CHART_FAMILIES` and the idempotent no-op path byte-for-byte.
    - _Requirements: 4.5, 9.1, 9.2_

  - [x] 4.2 Write property test for the chart-capability gate
    - **Property 8: Chart-capability gate (preserved)**
    - For any device positively resolved as not chart-capable, assert no scanned/plates payload is written (nav only); any resolution failure fails safe to chart-capable.
    - Tag: `Feature: chart-manifest-sync, Property 8: Chart-capability gate`; min 100 iterations.
    - _Requirements: 9.1, 9.2_

- [x] 5. Stage 5 — Relocate the label helpers, THEN retire the image-dd and mount-swap paths
  - [x] 5.1 Relocate `build_volume_label`, `_cycles_from_metadata`, and their cycle helpers out of `image_composer.py`
    - Move `build_volume_label`, `_cycles_from_metadata`, `extract_cycle`, `_parse_start_date`, `_cycle_for_start_date`, `_sanitize_label`, and the `_CYCLE_ANCHOR`/`_CYCLE_DAYS`/`_MONTHS`/label-regex constants into `manifest.py` (or a small `labels.py`); update imports in `app.py` / `drive_updater.py` to the new home. This MUST land before the image-dd deletion so the post-sync relabel keeps them.
    - _Requirements: 6.4_

  - [x] 5.2 Write unit tests for `build_volume_label` after relocation
    - Cover `C2611P2612` (both cycles), single-cycle degradation (`C2611` / `P2612`), and the neither-known `EFIS<YYMMDD>` fallback, importing from the new module location.
    - _Requirements: 6.4_

  - [x] 5.3 Retire mount-swap (only after Stage 3 is proven)
    - Delete `src/efis_data_manager/mount_swap.py` and its imports/wiring: the `drive_updater` `from ... mount_swap import msdos_mount, MountSwapError`, and any `poller`/`usb_monitor` quiesce hooks that nothing else uses.
    - _Requirements: 4.5_

  - [x] 5.4 Retire image-dd
    - Delete `src/efis_data_manager/image_writer.py`, remove the `image_composer` compose/write entry points, and remove the `image_write_enabled` config key from `config.DEFAULT_CONFIG` and its readers in `app.py`.
    - _Requirements: 4.5_

- [x] 6. Stage 6 — Post-sync relabel, flush, and Prepare-time Spotlight marker
  - [x] 6.1 Relabel the volume after a successful sync
    - After the card manifest write, relabel the volume to `C<charts>P<plates>` via the relocated `build_volume_label` (human-facing mirror; not trusted for the gate).
    - _Requirements: 3.6, 6.4_

  - [x] 6.2 Flush on done: `os.sync` + unmount inside the timed bracket
    - On successful sync, `os.sync` then unmount the card before reporting "done", so the bytes and card manifest are durable.
    - _Requirements: 7.1_

  - [x] 6.3 Write property test for flush-means-done
    - **Property 6: Flush-means-done**
    - For any successful sync, assert that when the operation reports "done" the written bytes and the card manifest are durable (`os.sync`'d + unmounted) so a subsequent read sees exactly what was written.
    - Tag: `Feature: chart-manifest-sync, Property 6: Flush-means-done`; min 100 iterations.
    - _Requirements: 7.1, 7.2_

  - [x] 6.4 Write the `.metadata_never_index` marker at Prepare time
    - Extend `prepare_drive` to write a `.metadata_never_index` marker at the volume root and set an initial volume label via the relocated `build_volume_label`.
    - _Requirements: 6.4_

  - [x] 6.5 Write unit tests for Prepare marker and flush
    - Assert `prepare_drive` writes `.metadata_never_index` at the volume root; assert a successful sync performs `os.sync` and unmounts the card at the end (against a temp dir standing in for the mount).
    - _Requirements: 6.4, 7.1_

- [x] 7. Integration tests for the end-to-end flow
  - [x] 7.1 Write end-to-end diff -> `--files-from` rsync -> marker -> card manifest -> relabel -> unmount integration test
    - Against a temp dir standing in for the mount, run the full chart-family flow and assert the card tree converges to the local manifest, the commit marker and card manifest are written, the label reflects the cycles, and the card is unmounted.
    - _Requirements: 4.3, 4.4, 7.1, 7.2_

  - [x] 7.2 Write nav-stream integration test
    - Assert `_run_files_job` end-to-end behavior for NAV.DB / NAV-proc.DB is unchanged (checksum compare, rewrite on difference, deep-checksum verify).
    - _Requirements: 8.1, 8.2, 8.3_

- [x] 8. Stage 7 — Remove dead tests/config and bump the menu-bar version
  - [x] 8.1 Remove dead tests and config for the retired paths
    - Delete tests referencing `mount_swap`, `image_writer`, `image_composer` compose/write, and `image_write_enabled`; confirm the suite passes via `./venv/bin/python -m pytest`.
    - _Requirements: 4.5_

  - [x] 8.2 Bump `MENUBAR_VERSION` per the versioning policy
    - Bump `MENUBAR_VERSION` in `src/efis_data_manager/__init__.py` (this is a menu-bar change; leave `DASHBOARD_VERSION` unchanged). The git tag + `__version__` + `pyproject.toml` version are synced at release time, not in this task.
    - _Requirements: 4.5_

- [x] 9. Final checkpoint
  - Ensure all tests pass via `./venv/bin/python -m pytest`, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional (test sub-tasks) and can be skipped for a
  faster path, but the 8 correctness properties and the HI/label/flush unit
  tests are the design's stated acceptance evidence.
- Each task references specific requirement sub-clauses for traceability; all 10
  requirements are covered.
- The staged order mirrors the design's Retirement plan: each stage leaves a
  working sync, and the old path is deleted only after the new path is proven.
- Real-hardware full-cycle writes (~2.5 h, longer with HI enabled) are a manual
  acceptance step performed between stages, not an automated task.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "5.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "2.2", "5.2"] },
    { "id": 2, "tasks": ["1.4", "1.5"] },
    { "id": 3, "tasks": ["1.6", "1.7"] },
    { "id": 4, "tasks": ["1.8", "3.1"] },
    { "id": 5, "tasks": ["3.2", "3.3", "3.4"] },
    { "id": 6, "tasks": ["3.5", "3.6"] },
    { "id": 7, "tasks": ["3.7", "3.8", "4.1"] },
    { "id": 8, "tasks": ["4.2", "5.3", "5.4"] },
    { "id": 9, "tasks": ["6.1", "6.2", "6.4"] },
    { "id": 10, "tasks": ["6.3", "6.5", "7.1", "7.2"] },
    { "id": 11, "tasks": ["8.1", "8.2"] }
  ]
}
```
