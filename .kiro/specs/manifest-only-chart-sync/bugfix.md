# Bugfix Requirements Document

## Introduction

Chart-family sync (scanned, plates) must be CONTENT-correct: a drive carried to the EFIS
must not hold a wrong chart tile. The SHA-256 manifest machinery exists precisely to
guarantee this — it detects a changed tile even when the new tile has the same byte size
as the old one. But the current code keeps a legacy `rsync --size-only --delete`
whole-tree job as a live sync mechanism and falls back to it in several situations. The
legacy job compares by SIZE ONLY, so a same-byte-size content change is SILENTLY SKIPPED.
For aviation charts this is unacceptable: the failure mode is an undetected wrong tile.

The legacy path is reached today whenever the manifest diff "cannot" run:

- `_chart_family_job` returns the legacy job when the local manifest is missing/corrupt
  (`Manifest_Store.load_local(...) is None`) or the card manifest is absent.
- `verify_drive(repair=True)` UNCONDITIONALLY forces the legacy whole-tree job
  (`_legacy_tree_job`, `files_from=None`) for any discrepant chart family, bypassing the
  manifest diff entirely — so recovery from ANY interrupted sync is size-only, which is
  exactly when content-correctness matters most.

Observed 2026-10-31: a drive (`C2609P2610`) whose local charts were not downloaded by DM
had no local manifest, so every sync used legacy `--size-only`. A separately-confirmed
defect left 11,345–18,262 plate tiles missing while the drive had been reported "current"
earlier. The legacy mechanism is both the silent-wrong-tile hazard and the mechanism in
play when the drive was mis-synced.

### Terminology (authoritative for this spec)

- **local manifest** — the SSD-side SHA-256 manifest, built by DM when it downloads and
  extracts a new chart/plates cycle. Path:
  `~/Library/Application Support/EFISDataManager/manifests/{scanned,plates}.json`
  (code: `Manifest_Store.load_local` / `save_local`). Authoritative for "what the current
  cycle's content SHOULD be".
- **card manifest** / **disk manifest** (synonyms) — the SHA-256 manifest on the removable
  USB media at the volume root (`EFIS_MANIFEST.json`), built at adoption / sync / verify
  (code: `Manifest_Store.load_card` / `save_card`, `CardManifest`). A record of what the
  physical drive claims to hold; it can be stale or partial after an interruption.

### Design position (user decision)

1. The legacy `--size-only --delete` whole-tree sync is RETIRED as a sync mechanism. It is
   never content-correct and must not be reachable on any chart-sync path (normal insert
   sync, adopt populate, or interrupted-sync repair).
2. The manifest `--files-from` content-hash diff is the ONLY chart-sync mechanism.
3. If a required manifest is missing or corrupt, the system BUILDS it, then runs the
   manifest diff — it never falls back to legacy.
   - local manifest missing/corrupt -> rebuild from the SSD source tree.
   - card manifest missing/corrupt -> rebuild from the card's actual contents.
4. Interrupted-sync repair also uses the manifest diff. Because the card manifest FILE can
   be stale/partial after an interruption, repair computes its change set as
   **local manifest vs a FRESH content-hash rescan of the card's actual bytes** (not vs the
   possibly-stale card manifest file). This is content-correct and transfers only the
   differing files.
5. Accepted cost: the repair-time card rescan hashes the card contents (minutes on slow
   USB). Correctness is chosen over speed for chart data.

Note: the local-manifest-absent condition observed on 2026-10-31 was a one-time artifact
of the manifest feature being developed AFTER the cycle was already downloaded; it is NOT
a standalone defect. It is subsumed by rule 3 ("if missing, build it").

### Bug Condition

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type ChartSyncJob         // a scanned/plates sync about to run
  OUTPUT: boolean

  // A chart-family sync is about to transfer using the size-only legacy
  // mechanism (files_from is NULL), i.e. not the content-hash manifest diff.
  RETURN X.family IN {"scanned","plates"}
         AND X.transfer_mode = "legacy_size_only"   // job.files_from is None
END FUNCTION
```

### Property Specification (Fix Checking)

```pascal
// Property: Fix Checking - chart syncs are always manifest-driven
FOR ALL X WHERE isBugCondition(X) DO
  result <- F'(X)
  ASSERT result.transfer_mode = "manifest_files_from"   // job.files_from is a list
  ASSERT result.change_set = diff(local_manifest, ground_truth_of_card)
END FOR
```

Where **F** reaches the legacy size-only job and **F'** ensures both manifests exist
(building any missing/corrupt one) and emits a manifest `--files-from` job. For the repair
path, `ground_truth_of_card` is a fresh content-hash rescan of the card, not the card
manifest file.

### Preservation Goal (Preservation Checking)

```pascal
// Property: Preservation Checking - correct syncs and invariants unchanged
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
END FOR
```

Specifically preserved: the explicit `_prune_removed` of `diff.removed`; the
commit-marker -> card-manifest -> `complete_family` ordering; metadata excludes +
`COPYFILE_DISABLE=1`; the mount-presence watchdog abort; nav family handling (no manifest,
per-file checksum) is unchanged.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN `_chart_family_job` finds the local manifest missing or corrupt THEN it returns the legacy `--size-only --delete` whole-tree job instead of building the manifest and diffing
1.2 WHEN `_chart_family_job` finds the card manifest absent THEN it returns the legacy job rather than building a card manifest from the card and diffing
1.3 WHEN `verify_drive(repair=True)` repairs a discrepant chart family THEN it forces the legacy whole-tree job (`_legacy_tree_job`, `files_from=None`), bypassing the manifest diff entirely
1.4 WHEN the legacy `--size-only` job runs THEN a chart tile whose content changed but whose byte size is unchanged is silently NOT transferred, so the drive can retain a stale/wrong tile with no detection

### Expected Behavior (Correct)

2.1 WHEN any chart-family sync runs (normal insert, adopt populate, or interrupted-sync repair) THEN it SHALL use the manifest `--files-from` content-hash diff and SHALL NOT use the legacy `--size-only` mechanism
2.2 WHEN the local manifest is missing or corrupt at sync time THEN the system SHALL rebuild it from the SSD source tree and then run the manifest diff
2.3 WHEN the card manifest is missing or corrupt at sync time THEN the system SHALL rebuild it from the card's actual contents and then run the manifest diff
2.4 WHEN an interrupted-sync repair runs THEN the system SHALL compute the change set as the local manifest diffed against a FRESH content-hash rescan of the card's actual bytes (not against the stored card manifest file), and transfer exactly the differing files
2.5 WHEN the manifest diff yields removals THEN the system SHALL CONTINUE TO prune them explicitly via `_prune_removed` (no reliance on `--delete`)

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a chart family converges cleanly THEN the system SHALL CONTINUE TO write the commit marker, then the card manifest, then call `complete_family`, in that order
3.2 WHEN macOS metadata (`._*`, `.DS_Store`) or structural excludes are involved THEN the system SHALL CONTINUE TO exclude them and keep `COPYFILE_DISABLE=1`
3.3 WHEN a drive is removed or the system sleeps mid-sync THEN the mount watchdog SHALL CONTINUE TO abort the transfer and leave the interrupted-sync marker
3.4 WHEN the nav family syncs THEN its per-file checksum behavior (no manifest, no cycle gate) SHALL remain unchanged
3.5 WHEN the cycle gate legitimately determines a family is unchanged (local cycle equals card cycle, content equal by construction) THEN a skip SHALL still be permitted — the retirement is of the size-only TRANSFER mechanism, not of the cycle gate

## Notes (non-authoritative — for design phase)

- Touch points: `_chart_family_job` (returns `_legacy_tree_job` base when `card is None`
  or `load_local() is None`); `_legacy_tree_job` (the size-only job builder);
  `sync_payload` (the `manifest_driven = job.files_from is not None` branch and the legacy
  `--size-only --delete` else-branch); `verify_drive` repair loop (forces
  `_legacy_tree_job` for scanned/plates).
- The repair-time "fresh content-hash rescan of the card" likely reuses the same
  `Manifest_Builder.build` machinery pointed at the card's family root
  (`_card_manifest_from_card` already hashes the card for adopt A1 — the repair can build
  an ephemeral card-side manifest the same way and diff the local manifest against it).
- Decide in design whether `sync_payload`'s legacy branch is deleted outright or retained
  only for the nav/non-chart callers (nav is a `files` job, not a tree job, so it does not
  use the `--size-only` chart path — likely the legacy chart branch can be removed
  entirely).
- Validation: property tests (local manifest missing -> built then manifest job, never
  legacy; card manifest missing/corrupt -> built then manifest job; repair -> manifest job
  against a card rescan, never `_legacy_tree_job`); a same-size content-change fixture that
  legacy would miss and the manifest path catches; preserve `_prune_removed` and the
  commit ordering. Keep an exploration test that FAILS on current code (repair yields a
  `files_from=None` legacy job) to confirm the defect before the fix.
- Related specs: quick-currency-count-size-check (cheap non-currency reject);
  chart-sync-wiring-fix (original manifest wiring); chart-manifest-sync (engine).
- Backlog item: captured 2026-10-31 during chart-capable adopt testing. Not yet scheduled.
