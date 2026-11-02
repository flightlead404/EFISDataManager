# Implementation Plan: manifest-only-chart-sync

## Overview

Retire the legacy `rsync --size-only --delete` whole-tree job as a chart-sync
mechanism so every chart-family transfer (scanned, plates) runs the SHA-256
manifest `--files-from` content-hash diff — on normal insert, adopt populate,
and interrupted-sync repair. Build any missing/corrupt manifest instead of
falling back to size-only. For repair, diff the local manifest against a fresh
content-hash rescan of the card (not the possibly-stale card manifest file).

Work proceeds test-first and bottom-up: write the exploration test that pins the
current defect (repair yields a size-only job), then the preservation tests, then
the `_chart_family_job` build-if-missing rework, the repair rescan, the
`sync_payload` legacy-branch removal, and finally a full-suite checkpoint.

Language: Python against `efis_data_manager`. Property tests use Hypothesis
(>= 100 iterations) with the FAT32-safe uppercase-alphanumeric path alphabet.
Run tests via `./venv/bin/python -m pytest` using `./temp/cmd_output.txt`.

Requirements and acceptance criteria are authoritative in `bugfix.md`; the
implementation design is in `design.md`.

## Tasks

- [x] 1. Exploration test — pin the current defect (bugfix 1.3, 1.4)
  - Write `tests/test_manifest_only_chart_sync.py` with a test that builds a
    discrepant chart family (drive missing/altered files) and calls
    `verify_drive(mount, repair=True)`, asserting the EXPECTED post-fix behavior:
    the repair job(s) carry `files_from is not None` (manifest-driven) and the
    transferred set equals `diff(local_manifest, fresh_card_rescan)`.
  - Run on UNFIXED code: this test MUST FAIL (today repair forces
    `_legacy_tree_job` with `files_from=None`). Capture the failure as the
    counterexample that confirms the defect.
  - Also add a same-size-content-change exploration case: a tile edited to an
    identical byte size must appear in the change set — assert it does (fails
    today on the repair/size-only path).
  - Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.4

- [x] 2. Build-if-missing manifest helpers (bugfix 2.2, 2.3)
  - [x] 2.1 Add `_build_local_manifest(name, local_root)` (or reuse
    `currency._build_family_manifest`) that hashes the SSD family tree and
    applies the scanned Plates/ prune, returning a `FamilyManifest`. Resolve the
    drive_updater <-> currency import direction (lazy import if needed to avoid a
    cycle).
  - [x] 2.2 Unit-test it: scanned excludes `Plates/` and the marker; plates
    rooted at `ChartData/Plates`; entries match `Manifest_Builder.build` +
    prune; cycle best-effort/null tolerated.
  - Requirements: 2.2, 2.3

- [x] 3. Rework `_chart_family_job` to be manifest-only (bugfix 2.1, 2.2, 2.3)
  - [x] 3.1 Extract the roots/excludes/markers portion of `_legacy_tree_job`
    into `_chart_tree_job_skeleton(name, mount_point, local_root)` (same roots,
    excludes, markers; `files_from` left None, always populated before return).
  - [x] 3.2 Rewrite `_chart_family_job(name, mount_point, local_root, card,
    card_ground_truth=None)` per design Section A: ensure the local manifest
    (build + `save_local` if missing/corrupt); keep the `decide_streams`
    skip-cycle gate (return None); choose `card_entries` from
    `card_ground_truth[name]` when provided, else `{}` when `card is None`, else
    the card family's entries; set `files_from`/`removed` from
    `diff_family(local_fam, card_entries)`. NEVER return a `files_from=None` job.
  - [x] 3.3 Update `build_jobs` callsite if the signature changed (default
    `card_ground_truth=None` keeps the normal path identical).
  - Requirements: 2.1, 2.2, 2.3, 3.5

- [x] 4. Interrupted-sync repair via fresh card rescan (bugfix 2.4)
  - [x] 4.1 In `verify_drive(..., repair=True)`, replace the forced
    `_legacy_tree_job` for discrepant chart families with: build
    `card_truth = _card_manifest_from_card(mount_point)` once, then build each
    repair job via `_chart_family_job(name, mount_point, local_root, card=None,
    card_ground_truth=card_truth.families)` and run it through `run_sync_job`.
    Nav repair unchanged.
  - [x] 4.2 Verify task-1 exploration test now PASSES (repair yields a
    manifest job; same-size change transferred).
  - Requirements: 2.4, 3.1, 3.4

- [x] 5. Remove the size-only branch from `sync_payload` (bugfix 2.1, 2.5)
  - [x] 5.1 Delete the legacy `else` branch (`rsync -r --delete
    --delete-excluded --size-only ...`) from `sync_payload`; all chart tree jobs
    are now manifest-driven. Keep the `--files-from` path, metadata excludes,
    `COPYFILE_DISABLE=1`, the temp-file stdout/stderr draining, and the
    `_prune_removed` call for `job.removed`. Add a defensive guard (raise) if a
    tree job arrives with `files_from is None`.
  - [x] 5.2 Retire `_legacy_tree_job` for charts (remove or fold into
    `_chart_tree_job_skeleton`); confirm no remaining chart caller emits a
    size-only job. `_nav_files_job` untouched.
  - Requirements: 2.1, 2.5, 3.2

- [x] 6. Preservation tests (bugfix 3.1-3.5)
  - Assert: `_prune_removed` still prunes `diff.removed`; the
    commit-marker -> card-manifest -> `complete_family` order is intact on a
    clean converge; nav sync behavior unchanged; the cycle gate still permits a
    skip when local cycle == card cycle; and a clean, already-current insert
    performs NO card rescan (assert `_card_manifest_from_card` is not called on
    the clean path, per design Section D).
  - Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 5

- [x] 7. Error-path tests (design Error handling)
  - Local manifest build failure and card rescan failure each leave the family
    pending (interrupted marker intact), surface an error, and NEVER fall back to
    a size-only transfer.
  - Requirements: 2.1, 2.4

- [x] 8. Checkpoint — full suite green
  - Run `./venv/bin/python -m pytest`. Confirm the task-1 exploration test
    passes, all preservation/error tests pass, and no regressions. Bump
    `__version__` / `pyproject.toml` and `MENUBAR_VERSION` per the versioning
    policy, update `CHANGELOG.md`, and commit.
  - Requirements: all
