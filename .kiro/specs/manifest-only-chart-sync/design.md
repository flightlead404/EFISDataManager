# Design: manifest-only-chart-sync

## Overview

Retire the legacy `rsync --size-only --delete` whole-tree job as a chart-sync
mechanism. After this change, every chart-family transfer (scanned, plates) —
on a normal insert sync, an adopt populate, and an interrupted-sync repair —
runs the SHA-256 manifest `--files-from` content-hash diff. If a required
manifest is missing or corrupt, the system builds it and then diffs; it never
falls back to the size-only path. The size-only transfer branch is removed from
`sync_payload`, and `_legacy_tree_job` is retired for chart families.

This closes a flight-safety gap: `--size-only` cannot detect a chart tile whose
content changed while its byte size stayed the same, so a stale/wrong tile could
survive a sync undetected. The manifest diff compares by SHA-256 and catches it.

The requirements, bug condition, and acceptance criteria are authoritative in
`bugfix.md`; this document is the implementation design. Terminology follows
`bugfix.md`: **local manifest** = SSD-side (`Manifest_Store.load_local` /
`save_local`); **card manifest** = removable-media side (`Manifest_Store.load_card`
/ `save_card`, `CardManifest`), also called the **disk manifest**.

Language: Python against the existing `efis_data_manager` package. Property tests
use Hypothesis (>= 100 iterations). Tests run via `./venv/bin/python -m pytest`.

## Current behavior (what we are changing)

Three leaks route chart syncs to the legacy size-only job:

1. `_chart_family_job(name, mount_point, local_root, card)` (drive_updater.py
   ~1050) returns `base = _legacy_tree_job(...)` (which has `files_from=None`)
   when:
   - `card is None` (absent/corrupt card manifest), or
   - `Manifest_Store.load_local(name) is None` (absent/corrupt local manifest).
   Otherwise it computes `diff_family(local_fam, card_entries)` and sets
   `base.files_from` / `base.removed`.

2. `verify_drive(mount_point, ..., repair=True)` (drive_updater.py ~2082) builds
   diff-aware jobs for the READ (verify) pass, but for the WRITE (repair) pass it
   UNCONDITIONALLY rebuilds each discrepant chart family via
   `_legacy_tree_job(name, mount_point, local_root)` (`files_from=None`) — so
   every interrupted-sync recovery is size-only.

3. `sync_payload(job, ...)` (drive_updater.py ~1150) branches on
   `manifest_driven = job.files_from is not None`. The `else` branch runs
   `rsync -r --delete --delete-excluded --size-only --modify-window=2 ...`.

`diff_family(local, card_family)` (manifest.py ~411) diffs by `relpath -> sha256`
and does NOT re-hash the card — it trusts `card_family` (the card manifest's
stored entries) as the faithful record of what was last written. That trust is
the reason repair currently avoids the manifest path: after an interruption the
card manifest file can be stale/partial, so a diff against it could transfer
only a partial delta.

## Design

### Principle

The manifest `--files-from` diff is the only chart-transfer mechanism. The
change set is always `diff(local_manifest, ground_truth_of_card)`:

- **Normal insert / adopt populate:** `ground_truth_of_card` is the stored card
  manifest's family entries (the existing behavior when both manifests exist).
  The card manifest is trustworthy here because it is only written after a clean,
  verified convergence (commit-marker ordering, preserved).
- **Interrupted-sync repair:** the stored card manifest may be stale/partial, so
  `ground_truth_of_card` is a FRESH content-hash rescan of the card's actual
  bytes (hash the card's family tree now), NOT the card manifest file.

In all cases, if the local manifest is missing/corrupt it is rebuilt from the SSD
source first; the size-only job is never emitted.

### A. A single manifest-job builder with build-if-missing (R2.1, R2.2, R2.3)

Rework `_chart_family_job` so it never returns a `files_from=None` job for a
chart family. New shape:

```
FUNCTION _chart_family_job(name, mount_point, local_root, card, card_ground_truth=None)
  base <- _chart_tree_job_skeleton(name, mount_point, local_root)   # roots/excludes/markers only
  # 1. Ensure the LOCAL manifest exists (build from SSD if missing/corrupt).
  local_fam <- Manifest_Store.load_local(name)
  IF local_fam is NULL THEN
    local_fam <- _build_local_manifest(name, local_root)   # hashes the SSD family tree
    Manifest_Store.save_local(local_fam)                   # persist for reuse
  END IF
  # 2. Cycle gate (unchanged): equal local/card cycle => skip-cycle => return None.
  IF decide_streams(...).action == "skip-cycle" THEN RETURN NULL
  # 3. Determine the card-side ground truth entries to diff against.
  IF card_ground_truth is not NULL THEN
    card_entries <- card_ground_truth[name]        # repair: fresh card rescan
  ELSE IF card is NULL THEN
    card_entries <- {}                             # no card manifest => full write (diff vs empty)
  ELSE
    card_entries <- card.families[name].entries if present else {}
  END IF
  diff <- diff_family(local_fam, card_entries)
  base.files_from <- diff.files_from
  base.removed    <- diff.removed
  RETURN base
END FUNCTION
```

Notes:
- `_chart_tree_job_skeleton` is the roots/excludes/markers part of today's
  `_legacy_tree_job` with `files_from` left None by construction (it is always
  populated before return). `_legacy_tree_job` itself is retired (see E).
- `card is None` yields a diff against an empty card family — a full content
  write (every local relpath in `files_from`, `removed` empty). This replaces
  today's "card is None -> legacy" leak with "card is None -> full manifest
  write", which is content-correct.
- `_build_local_manifest(name, local_root)` reuses
  `currency._build_family_manifest` (which already builds + applies the scanned
  Plates/ prune) — or an equivalent in drive_updater to avoid a layering issue;
  decide in task 2. The cycle stamped is best-effort from
  `currency._harvest_cycle` / existing cycle metadata (null is acceptable; the
  diff does not depend on the cycle).

### B. Interrupted-sync repair via fresh card rescan (R2.4)

In `verify_drive(..., repair=True)`, replace the forced `_legacy_tree_job` for
discrepant chart families with a manifest job built against a FRESH rescan of the
card:

```
# Build ground truth by hashing the card's actual contents now.
card_truth <- _card_manifest_from_card(mount_point)          # already exists (adopt A1)
FOR each discrepant chart family `name`:
  repair_job <- _chart_family_job(name, mount_point, local_root, card=None,
                                  card_ground_truth=card_truth.families)
  run_sync_job(repair_job, mount_point, ...)                 # files_from diff, never legacy
```

`_card_manifest_from_card(mount_point)` (drive_updater.py ~497) already hashes
`ChartData/` (scanned, Plates pruned) and `ChartData/Plates/` (plates) into a
`CardManifest` — reuse it verbatim as the rescan. Passing its `families` as
`card_ground_truth` makes the repair diff `local_manifest vs rescanned-card`,
which is content-correct and transfers exactly the differing files. `run_sync_job`
is unchanged; `_run_tree_job` then writes the marker, the (real) card manifest,
and `complete_family` on a clean pass (preserved ordering, R3.1).

Nav repair is unchanged (nav is a `files` job, no manifest, no cycle gate — R3.4).

### C. sync_payload: remove the size-only branch (R2.1)

`sync_payload` currently has two branches keyed on `manifest_driven =
job.files_from is not None`. After A and B, every chart tree job carries a
`files_from` list (possibly empty for a no-op). Remove the `else` (legacy
`--size-only --delete`) branch entirely. An empty `files_from` yields an empty
rsync payload (transfers nothing), which is the correct no-op. `_prune_removed`
for `job.removed` is preserved and still runs on a clean transfer (R2.5).

Decide in task 4 whether to keep a guarded `raise`/assert if a tree job ever
arrives with `files_from is None` (defensive: it should be impossible after A/B).

### D. Cost containment on the normal path (design decision)

The expensive step introduced is the card rescan in repair (B) and the
build-if-missing local manifest (A). Neither should run on a clean,
already-current insert:

- The quick currency check (`check_drive_currency`, with the shipped
  `quick-currency-count-size-check` cheap count tier) still gates: a clean,
  current drive reports current and no sync job is built, so no rescan/hash runs.
- `_build_local_manifest` only runs when the local manifest is genuinely missing
  (a one-time cost per cycle; normally DM's download already built it).
- The repair rescan only runs on the interrupted-sync path (a family actually
  flagged pending). This is the accepted cost (R5 / bugfix item 5).

So the normal "insert a current drive" path adds no hashing. This must be
asserted by a test (task 5).

### E. Retire `_legacy_tree_job` for charts

Remove `_legacy_tree_job` (or reduce it to the private skeleton builder used by
A, renamed `_chart_tree_job_skeleton`). Confirm no remaining caller emits a
`files_from=None` chart job: after A/B the only callers are `_chart_family_job`
and the repair loop, both of which populate `files_from`. `_nav_files_job` is
untouched (nav is `kind="files"`, never size-only tree).

## Data flow (after)

```
insert / adopt populate:
  build_jobs -> _chart_family_job(..., card=load_card)
     -> ensure local manifest (build if missing)
     -> diff(local, card.entries)  -> files_from/removed
  run_sync_job -> sync_payload(files_from)  [manifest only]

interrupted-sync repair (verify_drive repair=True):
  _card_manifest_from_card(mount)          [fresh card hash = ground truth]
  _chart_family_job(..., card_ground_truth=rescan.families)
     -> ensure local manifest (build if missing)
     -> diff(local, rescanned_card.entries) -> files_from/removed
  run_sync_job -> sync_payload(files_from)  [manifest only]
```

## Error handling

- Local manifest build failure (SSD unreadable): the sync cannot proceed
  content-correctly; fail the job (status "failed", leave interrupted marker) and
  surface the error — do NOT silently fall back to size-only. (This is stricter
  than today and is the point of the spec.)
- Card rescan failure during repair: same — fail the repair job, leave the
  interrupted marker; the next insert retries. Never size-only.
- `_prune_removed` best-effort semantics unchanged.

## Testing strategy

Validation approach: the sync/verify helpers mutate real temp trees and shell out
to rsync; follow the existing `test_run_sync_job.py` / `test_verify_drive.py`
patterns (real temp dirs, mocked mount preflight, `conftest.py` isolates
`LOCAL_MANIFEST_DIR`). Property tests use Hypothesis (>= 100 iterations) with the
FAT32-safe uppercase-alphanumeric path alphabet established in
`test_card_manifest_from_card_property.py`.

Key tests:
- Exploration (must FAIL on current code): `verify_drive(repair=True)` on a
  discrepant chart family yields a `files_from is None` legacy job. After the fix
  it yields a manifest `--files-from` job.
- `_chart_family_job` with the local manifest absent -> builds it, returns a
  manifest job (never legacy); with the card manifest absent -> full manifest
  write (diff vs empty), never legacy.
- Same-size content change fixture: a tile edited to identical byte size is in
  `files_from` (legacy would miss it) and is transferred.
- Repair diffs against a fresh card rescan: seed the card with content that
  disagrees with a stale card manifest; assert the repair transfers the bytes
  that actually differ from the card (not what the stale manifest claims).
- Preservation: `_prune_removed` still prunes `removed`; commit-marker ->
  card-manifest -> `complete_family` ordering intact; nav unchanged; the normal
  current-drive insert performs NO card rescan (assert `_card_manifest_from_card`
  is not called on the clean path).

## Requirements traceability

- R2.1 (manifest-only transfer) -> A, C, E
- R2.2 (build local if missing) -> A
- R2.3 (build card if missing / diff vs empty) -> A
- R2.4 (repair diffs vs fresh card rescan) -> B
- R2.5 (prune removed explicitly) -> C (preserved)
- R3.1-R3.5 (preservation) -> A/B/C keep ordering, excludes, watchdog, nav, cycle gate
- R5 (accepted rescan cost, not on clean path) -> D
