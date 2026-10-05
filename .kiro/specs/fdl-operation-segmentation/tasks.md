# Implementation Plan

- [x] 1. Write bug condition exploration test (gap-based segmentation)
  - **Property 1: Bug Condition** - Gap-Based Segmentation
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected segmentation - it validates the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate the mega-operation (split) and the split-flight (merge) failures
  - **Scoped PBT Approach**: Scope the property to concrete, reproducible failing cases that mirror the real data, then generalize with Hypothesis
  - Build in-memory `FDLRecord` streams (crafted timestamps, 1 Hz) for these shapes from the Bug Condition / Examples in design:
    - Op-19 shape: 6993 contiguous records from 2026-09-28 20:12:17, a ~5-day gap, then 180 contiguous records on 2026-10-03 -> expect 2 operations (~6990 s and ~179 s)
    - Op-18 shape: 56 records on 2026-05-13, ~4.5-month gap, 4 records on 2026-09-28 -> expect 2 operations
    - Rotated-flight shape: File A ends 12:00:00, File B begins 12:00:01 (1 s gap) across a file boundary -> expect 1 merged operation spanning both files
    - Boundary: gap of exactly 180 s stays one operation; 181 s splits
  - Assertions match Property 1: operations are cut exactly at gaps > 180 s; each operation has no internal gap > 180 s; each `duration_seconds` = its own end - start
  - Run on UNFIXED code (current one-operation-per-file behavior)
  - **EXPECTED OUTCOME**: Test FAILS (op-19 shape yields one 421275 s operation; rotated flight yields two operations) - this proves the bug exists
  - Document counterexamples found to confirm the root cause (file-equals-operation assumption; per-file import loop)
  - Mark task complete when the test is written, run, and the failure is documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Single Contiguous Run Unchanged
  - **IMPORTANT**: Follow observation-first methodology
  - Observe behavior on UNFIXED code for contiguous, single-session streams and the healthy short ground operations:
    - Observe: a contiguous 1 Hz stream of N records -> one operation, start = records[0].timestamp, end = records[-1].timestamp, duration = N-1 s, record_count = N
    - Observe: op-14 shape (180 contiguous records / 179 s) -> one operation, not split
    - Observe: op-17 shape (120 contiguous records / 119 s) -> one operation, not split
    - Observe: `_compute_operation_summary` outputs (`has_flight`, `airborne_seconds`, max/min values, cruise averages, fuel, hourmeter start/end) for a contiguous stream
  - Write property-based tests (Hypothesis) capturing these observations from the Preservation Requirements in design:
    - For all contiguous streams (no gap > 180 s), segmentation yields exactly one operation with unchanged span, count, and summary
    - Short ground ops are never split
  - Verify tests PASS on UNFIXED code (this is the baseline to preserve)
  - **EXPECTED OUTCOME**: Tests PASS on unfixed code
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.4, 3.5_

- [x] 3. Write idempotency exploration/preservation test (identity key)
  - **Property 3: Idempotent Record-Level Dedup** - timestamp+content, filename-agnostic
  - Observe on UNFIXED code how `(source_filename, start_time)` dedup behaves for a cycled filename carrying different data, and for a cross-file operation (document the unsound file-based dedup)
  - Write a test that imports a candidate set once, then again with overlap/duplicates (incl. the same records under a DIFFERENT filename), and asserts the record set and operation set are identical (no duplicate records/operations, healthy ops unchanged) under record-level `(timestamp, content_hash)` dedup
  - On UNFIXED code this test demonstrates the dedup weakness - document it
  - _Requirements: 2.6, 2.7, 3.3_

- [x] 4. Fix for FDL operation segmentation (gap-based, cross-file, idempotent)

  - [x] 4.1 Add `segment_records` and the per-run operation view
    - Add `GAP_THRESHOLD_SECONDS = 180` constant and `segment_records(records, gap_threshold_seconds=180)` to `src/efis_data_manager/fdl_parser.py` (pure function; cuts a timestamp-ordered list at gaps > threshold)
    - Provide an operation view carrying per-run `start_time`, `end_time`, `duration_seconds`, `record_count`, `has_flight`, `date`
    - _Bug_Condition: isBugCondition(records) from design (internal gap > 180 s OR cross-file continuation <= 180 s)_
    - _Expected_Behavior: Property 1 - cut exactly at gaps > 180 s; per-run duration = end - start_
    - _Requirements: 2.1, 2.2, 2.5_

  - [x] 4.2 Pool and timestamp-order the candidate file set
    - Update the import entry point (`archiver._import_fdl_to_database` and/or a new `database` function) to parse all candidate FDL files, tag records with source filename, concatenate, sort by timestamp (never by filename), and de-duplicate exact boundary-duplicate records
    - _Bug_Condition: cross-file continuation and cycling filenames from design_
    - _Expected_Behavior: Property 1 (merge across file boundary); ordering by timestamp_
    - _Requirements: 2.3, 2.4_

  - [x] 4.3 Record-level dedup + derived operation identity; import a single operation
    - Add a per-record `content_hash` column to `fdl_data` and a `UNIQUE(timestamp, content_hash)` index in `_ensure_schema`; insert records with INSERT-OR-IGNORE on that key (record-level dedup). Drop filename from identity entirely; keep `source_filename` only as a nullable informational label on `operations`
    - Add an index on `operations(start_time, end_time, record_count)` for upserting the operation summary row (derived key, a pure function of the deduped records)
    - Refactor `import_fdl_file` into `import_operation(records, source_label)`: insert records (dedup by timestamp+content), upsert the operation on its derived key, call `_compute_operation_summary` over the run (summary logic unchanged)
    - _Bug_Condition: isBugCondition(records) from design_
    - _Expected_Behavior: Property 3 - idempotent identity_
    - _Preservation: Preservation Requirements - summary computation unchanged per run_
    - _Requirements: 2.6, 2.7, 3.4_

  - [x] 4.4 Add the cleanup / re-import migration (idempotent, transactional)
    - In `database.py`, add a one-time migration guarded by a `schema_info` flag: read all `fdl_data` ordered by timestamp (source of truth), de-duplicate exact duplicate records, run `segment_records`, compute `Operation_Key` for each correct operation, re-point `fdl_data.operation_id` to the correct operation, create missing operations, delete the bad mega-operations (18, 19) and any superseded/empty operations only after their records are re-pointed, recompute summaries
    - Preserve healthy operations (3, 8, 14, 15, 16, 17) unchanged (same key -> left untouched); never delete `fdl_data` rows; run inside a transaction; safe to re-run
    - _Bug_Condition: existing imported ops 18/19 satisfy isBugCondition_
    - _Expected_Behavior: Properties 1 and 3 applied to existing data_
    - _Preservation: healthy ops and short ground ops unchanged_
    - _Requirements: 2.7, 2.8, 3.3_

  - [x] 4.5 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Gap-Based Segmentation
    - **IMPORTANT**: Re-run the SAME test from task 1 - do NOT write a new test
    - Run the segmentation exploration test from step 1
    - **EXPECTED OUTCOME**: Test PASSES (op-19 shape -> 2 operations with correct durations; rotated flight -> 1 merged operation)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

  - [x] 4.6 Verify preservation and idempotency tests still pass
    - **Property 2: Preservation** - Single Contiguous Run Unchanged
    - **IMPORTANT**: Re-run the SAME tests from tasks 2 and 3 - do NOT write new tests
    - Run the preservation property tests (task 2) and the idempotency test (task 3)
    - **EXPECTED OUTCOME**: Tests PASS (contiguous runs and short ground ops unchanged; re-import idempotent; healthy ops preserved)
    - Confirm no regressions after the fix and after the migration
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

- [x] 5. Checkpoint - Ensure all tests pass and release labeling is correct
  - Run the full test suite with `./venv/bin/python -m pytest`
  - Confirm downstream values are correct for the re-segmented flight and ground operations (duration, `has_flight`, `airborne_seconds`, hourmeter spans, oil/analysis)
  - Apply versioning policy on release (bump `__version__` and `pyproject.toml`, tag the repo; confirm `MENUBAR_VERSION` vs `DASHBOARD_VERSION` label with the user)
  - Ensure all tests pass; ask the user if questions arise
  - _Requirements: 2.8_
