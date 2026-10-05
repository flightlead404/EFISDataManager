# FDL Operation Segmentation Bugfix Design

## Overview

The FDL importer currently equates one FDL file with one operation. It parses a
file with `parse_fdl_file` into an `FDLFile`, whose `start_time`/`end_time` are
simply `records[0].timestamp`/`records[-1].timestamp`, and inserts a single
`operations` row per file via `import_fdl_file`. This is wrong because GRT writes
FDL files in a way that breaks the one-to-one mapping: a file can hold multiple
engine start/stop sessions, one flight can be rotated across multiple numbered
files, and filenames cycle/reuse across dates.

The fix replaces file-based operation boundaries with **gap-based
segmentation**. Across the candidate set of FDL files, records are concatenated
and ordered by timestamp, then cut into operations at every point where the gap
between consecutive records exceeds a threshold (3 minutes, user-confirmed). A
single rule handles both failure modes: an internal gap splits a multi-session
file into separate operations, and the absence of a large gap across a file
boundary merges a rotated flight into one operation.

Because segmentation changes which records belong to which operation, the
file-based identity key `UNIQUE(source_filename, start_time)` no longer works
(filenames start at 0000 and cycle on every fresh USB, so the same name recurs
for different data; one operation can span files; one file can yield many
operations). Dedup is moved to the RECORD level — each record keyed by
(timestamp, content_hash) — and operation identity is derived from the stable
contiguous run — its timestamp span and record count — so re-import is
idempotent.

The fix includes a one-time cleanup migration that re-segments the already
imported `fdl_data` rows into correct operations, removes the bad
mega-operations (18, 19), preserves the healthy operations (3, 8, 14, 15, 16,
17), and recomputes all summaries. The `fdl_data` rows are the source of truth
and are already in the database, so the migration operates on existing data and
does not require the original CSV files.

The scope is the FDL import path only: `fdl_parser.py`, `database.py`, and the
`archiver.py` import entry point, plus a migration. The dashboard is unaffected
in code; it benefits automatically from corrected operation rows.

## Glossary

- **Operation**: One maximal contiguous run of FDL records whose consecutive
  timestamp gaps are all at most the gap threshold. Represents one real engine
  start/stop cycle, independent of file boundaries.
- **Gap**: The time difference between two consecutive records, after the full
  candidate record set is ordered by timestamp.
- **GAP_THRESHOLD**: 3 minutes (180 seconds). A gap greater than this starts a
  new operation. (Sampling is nominally 1 Hz; in flight there are no legitimate
  multi-minute gaps, so 3 minutes cleanly separates distinct sessions while
  leaving contiguous short ground operations intact.)
- **Bug_Condition (C)**: The record set triggers the bug when gap-based
  segmentation differs from file-based segmentation — either a file contains an
  internal gap greater than the threshold (needs splitting) or records continue
  across a file boundary within the threshold (needs merging).
- **Property (P)**: The desired behavior — the record set is cut into operations
  exactly at gaps greater than the threshold, with each operation's duration
  equal to its own first-to-last span and no internal gap exceeding the
  threshold.
- **Preservation**: Record sets that are already a single contiguous run (no
  internal large gap, nothing to merge) must produce exactly the same single
  operation as the current importer, including legitimate short ground
  operations.
- **F**: The current importer — one operation per FDL file.
- **F'**: The fixed importer — gap-based segmentation over the concatenated,
  timestamp-ordered candidate record set.
- **Candidate set**: The batch of FDL files the archiver already collects in
  `fdl_archived_paths` and passes to `_import_fdl_to_database`. The entry point
  therefore already has the full set of files available for cross-file merging.
- **`parse_fdl_file`**: Parser in `src/efis_data_manager/fdl_parser.py` that
  reads one CSV into an `FDLFile` with `FDLRecord`s (each has `timestamp` and
  `hourmeter`).
- **`import_fdl_file`**: Current DB import in `src/efis_data_manager/database.py`
  that inserts one operation per `FDLFile`.
- **`_compute_operation_summary`**: Existing routine that computes per-operation
  stats (max/min engine values, cruise averages, fuel, hourmeter start/end,
  `has_flight`, `airborne_seconds`). Reused unchanged, applied per operation.
- **Operation_Key**: The redesigned stable identity for an operation used for
  idempotent dedup (see Fix Implementation).

## Bug Details

### Bug Condition

The bug manifests whenever the correct (gap-based) segmentation of the candidate
record set differs from the current (file-based) segmentation. The current
importer either lumps multiple sessions from one file into a single operation
(internal gap greater than threshold) or splits a single rotated flight into
multiple operations (cross-file continuation within threshold).

**Formal Specification:**

```
FUNCTION isBugCondition(records)
  INPUT: records = candidate FDL record set, each record tagged with its
         source file, sorted ascending by timestamp
  OUTPUT: boolean

  // GAP_THRESHOLD = 180 seconds (3 minutes)

  hasInternalLargeGap :=
    EXISTS i IN [0 .. len(records)-2] WHERE
        source_file(records[i]) == source_file(records[i+1])
        AND (records[i+1].timestamp - records[i].timestamp) > GAP_THRESHOLD

  hasCrossFileContinuation :=
    EXISTS i IN [0 .. len(records)-2] WHERE
        source_file(records[i]) != source_file(records[i+1])
        AND (records[i+1].timestamp - records[i].timestamp) <= GAP_THRESHOLD

  RETURN hasInternalLargeGap OR hasCrossFileContinuation
END FUNCTION
```

### Examples

- **Op 19 (SPLIT, real)**: `GRT FDL 0001.CSV` holds a ~1.9h flight on 2026-09-28
  (20:12:17 → 22:08:47, 6993 records) and a ~3-minute ground session on
  2026-10-03 (17:10:33 → 17:13:32, 180 records). The ~5-day gap exceeds the
  threshold. Expected: 2 operations (flight ~1.9h; ground ~3 min). Current:
  1 operation, duration 421275 s.
- **Op 18 (SPLIT, real)**: `GRT FDL 0000.CSV` holds 56 records on 2026-05-13 and
  4 records on 2026-09-28 (~4.5-month gap). Expected: 2 operations. Current:
  1 operation, duration 11992874 s.
- **Rotated flight (MERGE, representative)**: `GRT FDL 0002.CSV` ends at
  12:00:00 and `GRT FDL 0003.CSV` begins at 12:00:01, one continuous flight the
  EFIS rotated at max file size. Gap of 1 s is within threshold. Expected:
  1 operation spanning both files. Current: 2 operations.
- **Healthy contiguous (edge case, must not change)**: op 14, 180 contiguous
  records / 179 s of ground running; op 17, 120 records / 119 s. No internal gap
  exceeds the threshold. Expected and current: 1 operation each.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**

- A single-session, single-file, contiguous FDL file must continue to produce
  exactly one operation with the same start time, end time, `duration_seconds`,
  and `record_count` as the current importer.
- Legitimate short contiguous ground operations (op 14: 180 records / 179 s;
  op 17: 120 records / 119 s) must remain a single operation and must not be
  split.
- Per-operation summary computation (`_compute_operation_summary`) must continue
  to produce the same `has_flight`, `airborne_seconds`, max/min engine values,
  cruise averages, fuel used, and hourmeter start/end for the records of a given
  operation.
- Logbook enrichment by hourmeter range must continue to match legs to the
  correct operation.
- The six healthy operations (3, 8, 14, 15, 16, 17) must survive the cleanup
  migration with identical record membership and identical computed summaries.

**Scope:**

Any record set that is already a single contiguous run — no internal gap greater
than the threshold, and nothing to merge across a file boundary — must be
completely unaffected by the fix. This includes:

- Single-session files sampled contiguously at 1 Hz.
- Short ground operations with no multi-minute gaps.
- Any already-correct operation re-processed by the migration.

The expected correct behavior for buggy inputs is defined in Property 1 of the
Correctness Properties section.

## Hypothesized Root Cause

The defect is a design assumption, not a subtle code fault. Confirmed by reading
the source:

1. **File-equals-operation assumption in the parser.** In `fdl_parser.py`,
   `FDLFile.start_time`/`end_time` are `records[0]`/`records[-1].timestamp` and
   `duration_seconds` is their difference. The dataclass docstring literally
   states "one operation" per file. There is no gap analysis anywhere.

2. **One-insert-per-file in the importer.** In `database.py`,
   `import_fdl_file(fdl)` inserts exactly one `operations` row for the whole
   `FDLFile` and calls `_insert_fdl_data` / `_compute_operation_summary` once
   over all its records.

3. **File-based, cycling identity key.** The `operations` table has
   `UNIQUE(source_filename, start_time)` and `import_fdl_file` dedups on that
   pair. Because filenames cycle and one operation can span files, this key is
   neither unique nor stable across the real data.

4. **Per-file import loop at the entry point.** In `archiver.py`,
   `_import_fdl_to_database` loops over `fdl_paths`, parsing and importing each
   file independently — so even though the full file set is available, records
   are never concatenated across files for segmentation.

The root cause is therefore the absence of a segmentation step: records must be
pooled across the candidate files, ordered by timestamp, and cut at gaps, with a
stable identity for the resulting operations.

## Correctness Properties

Property 1: Bug Condition - Gap-Based Segmentation

_For any_ candidate FDL record set where the bug condition holds (isBugCondition
returns true — the set contains an internal gap greater than the threshold
and/or a cross-file continuation within the threshold), the fixed importer SHALL
cut the timestamp-ordered record set into operations exactly at the points where
the consecutive-record gap exceeds the threshold, such that every resulting
operation is a maximal contiguous run with no internal gap greater than the
threshold and with `duration_seconds` equal to its own last-record timestamp
minus its first-record timestamp.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**

Property 2: Preservation - Single Contiguous Run Unchanged

_For any_ candidate FDL record set where the bug condition does NOT hold
(isBugCondition returns false — a single contiguous run with no internal gap
greater than the threshold and nothing to merge), the fixed importer SHALL
produce the same single operation as the current importer, preserving the start
time, end time, `duration_seconds`, `record_count`, and all computed summary
statistics, including for legitimate short ground operations.

**Validates: Requirements 3.1, 3.2, 3.4, 3.5**

Property 3: Idempotent Operation Identity

_For any_ candidate FDL record set, importing it once and then importing an
overlapping or identical set again SHALL produce the same set of operations as a
single import — no duplicate operations and no altered healthy operations —
because operation identity is keyed on the stable contiguous-run signature
rather than on `source_filename`.

**Validates: Requirements 2.6, 2.7, 3.3**

## Fix Implementation

### Changes Required

Assuming the root-cause analysis is correct, the fix adds a segmentation layer,
redesigns operation identity, updates the import entry point to pool files, and
adds a cleanup migration.

**1. Segmentation function (new) — `src/efis_data_manager/fdl_parser.py`**

Add a pure function that segments a timestamp-ordered record list into
contiguous runs:

```
FUNCTION segment_records(records, gap_threshold_seconds = 180)
  INPUT: records sorted ascending by timestamp
  OUTPUT: list of record-runs (each run a non-empty list of FDLRecord)

  IF records is empty: RETURN []
  runs := []
  current := [records[0]]
  FOR i IN [1 .. len(records)-1]:
    gap := records[i].timestamp - records[i-1].timestamp
    IF gap > gap_threshold_seconds:
      runs.append(current)
      current := [records[i]]
    ELSE:
      current.append(records[i])
  runs.append(current)
  RETURN runs
END FUNCTION
```

Introduce an `Operation` view (or reuse `FDLFile`-like shape) carrying
`records`, `start_time`, `end_time`, `duration_seconds`, `record_count`,
`has_flight`, `date` for a single run, so downstream code computes per-run spans
rather than per-file spans. Keep `GAP_THRESHOLD_SECONDS = 180` as a named
constant. The existing `FDLFile` span properties remain for backward
compatibility but are no longer the unit of import.

**2. Cross-file concatenation and ordering — `src/efis_data_manager/archiver.py`
and/or `database.py`**

Replace the per-file import loop. The importer must:
- Parse each candidate file into records, tagging each record with its source
  filename (needed only for provenance, not for ordering).
- Concatenate all records from the candidate set.
- Sort by timestamp ascending (filenames cycle, so ordering must be by
  timestamp, never by filename).
- Run `segment_records` to produce operations.
- Import each operation.

Ordering decision: sort by `timestamp`. For ties (duplicate timestamps across
rotated files at the boundary), use a stable sort and de-duplicate exact record
duplicates (same timestamp and identical field values) so a record present in
both the tail of one file and the head of the next is not counted twice.

**3. Operation identity / dedup redesign — `src/efis_data_manager/database.py`**

DEDUP IS RECORD-LEVEL, NOT FILE-LEVEL (user-confirmed). Filename is NOT a usable
key: GRT FDL filenames start at `0000` and cycle every time a USB without
pre-existing FDL files is inserted, so the same filename recurs for completely
different data, and one real operation can span several files. The identity of
the data is in the RECORDS themselves. So dedup is removed from the file layer
entirely and done at the record layer:

**Record identity = (timestamp, content_hash)** where content_hash is a hash of
the record's field values (everything except the operation_id/row-id). Two
records are "the same sample" iff they have the same timestamp AND identical
content. This makes the whole pipeline idempotent and filename-agnostic. Note: FDL files
are MOVED off the USB on import (archiver `_move_file`), so routine re-insertion
does not re-present old data; the dedup is therefore a correctness/idempotency
SAFETY NET for the cases that remain, rather than a primary de-duplicator:
- Boundary-overlap records duplicated across two rotated files on ONE drive
  (EFIS max-file-size rotation mid-session) -> de-duplicated.
- An interrupted or re-run import (or an FDL manually copied back) -> no new rows
  (every record already present by (timestamp, content_hash)).
- FUTURE multi-device support: two displays on the inter-display link can log the
  same flight to two drives; identical records across them collapse to one.
  (Caveat for the multi-device spec: if each display writes marginally different
  sensor values at the same timestamp, the hashes differ and both import — a
  genuine two-source case to handle THERE, e.g. import FDL only from the primary
  or tag records by source device. Out of scope for this bugfix.)
This bug (op 19) was a single-drive, multi-session FILE — no cross-drive scenario
is assumed anywhere; swapping drives in flight is not a case we design for.

The database enforces this with a unique index on the record signature, e.g.
`UNIQUE(timestamp, content_hash)` on `fdl_data` (store a `content_hash` column
per record, computed at insert). Insertion uses INSERT-OR-IGNORE on that key so
re-import is a no-op per record.

OPERATION identity is then DERIVED from its records, never from filenames: an
operation is the maximal contiguous run, and its convenience key
`(start_time, end_time, record_count)` is a pure function of the deduped record
set. Because records are already deduped, the operation key is automatically
stable and idempotent; it is used to find/update an operation's summary row, not
as the primary dedup gate (the record-level dedup is).

`source_filename` is DROPPED from operation identity. The `operations` row keeps
a nullable, informational `source_filename` label (contributing filename(s),
e.g. a joined or "first..last" label) purely for human reference; it is NOT part
of any uniqueness constraint.

Schema migration note: SQLite cannot drop the old `UNIQUE(source_filename,
start_time)` constraint in place. `_ensure_schema` adds the new per-record
`content_hash` column + `UNIQUE(timestamp, content_hash)` index on `fdl_data`,
and an index on `operations(start_time, end_time, record_count)` for the summary
upsert; the legacy operations-table constraint is made irrelevant once the
cleanup migration re-segments rows (operations are now created from the deduped
record stream, so no two carry the same derived key). If the legacy table
constraint actively conflicts, rebuild the `operations` table (create-copy-swap).

NOTE: the ARCHIVE still keeps every raw FDL file, date-bucketed under
`FDL/<today>/` (user: raw files may be sent to GRT for troubleshooting — that is
what GRT uses). Archive retention is independent of import dedup: we keep the
raw files for provenance but no longer use filename/size as any import key.

**4. Import of a single operation — `src/efis_data_manager/database.py`**

Refactor `import_fdl_file` into an operation-oriented path, for example
`import_operation(records, source_label)`:
- Insert records with INSERT-OR-IGNORE on `(timestamp, content_hash)` so
  re-import is a per-record no-op (record-level dedup). The operation summary
  row is upserted on its derived key `(start_time, end_time, record_count)`.
- Insert the `operations` row using the run's own span and count.
- Call `_insert_fdl_data` with the run's records.
- Call `_compute_operation_summary` over the run's records (unchanged logic).

Keep a thin `import_fdl_file` wrapper or update callers; the archiver entry point
switches to the pooled/segmented path.

**5. Cleanup / re-import migration (new) — `src/efis_data_manager/database.py`
(invoked once, idempotent)**

The `fdl_data` rows are the source of truth and already in the DB, so the
migration does not need CSV files:
- Read all `fdl_data` rows ordered by timestamp (across all operations).
- De-duplicate records by `(timestamp, content_hash)` (handles old per-file
  double-imports, cross-drive duplicates, and rotated-file boundary overlap).
- Run `segment_records` to compute the correct set of operations.
- For each correct operation, compute its derived key
  `(start_time, end_time, record_count)`. If an operation with that key already
  exists with the correct record membership, leave it untouched (preserves
  healthy ops 3, 8, 14, 15, 16, 17). Otherwise
  create it.
- Re-point `fdl_data.operation_id` to the correct operation for each record.
- Delete the bad mega-operations (18, 19) and any other operation left with no
  records or superseded by re-segmentation, using the existing
  `delete_operation` path semantics (delete `fdl_data` then the row; here the
  rows are re-pointed first so no data is lost).
- Recompute summaries for every (re)created operation via
  `_compute_operation_summary`.
- Guard the migration with a schema-version/flag in `schema_info` so it runs
  once and is safe to re-run (idempotent).

Safety: the migration must run inside a transaction and must preserve every
`fdl_data` row (re-point, never delete the time-series). Deleting an operation
row only happens after its records are re-pointed to a correct operation.

**6. Named constant and versioning**

- Add `GAP_THRESHOLD_SECONDS = 180` as a documented module constant.
- Per the versioning policy, this changes import/analysis behavior consumed by
  the dashboard's data; bump `__version__` and `pyproject.toml` on release and
  tag the repo. The dashboard renders corrected data without code change, so
  `DASHBOARD_VERSION` need not change unless dashboard code is touched; the
  import belongs to the menu-bar/data path, so `MENUBAR_VERSION` is the display
  label to bump. Confirm the exact label with the user at release time.

## Testing Strategy

### Validation Approach

Two phases: first surface counterexamples that demonstrate the bug on the
UNFIXED code (both the split and merge forms, plus a reproduction of the op-19
mega-operation), then verify the fix segments correctly and preserves existing
behavior for contiguous runs. Property-based testing (Hypothesis, already a
project dependency) is used for both the segmentation property and preservation.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing
the fix, and confirm the root-cause analysis. If refuted, re-hypothesize.

**Test Plan**: Build in-memory record streams (lists of `FDLRecord` with crafted
timestamps) that reproduce the real data shapes, and assert the correct
segmentation. Run against the current importer/parser behavior (one span per
file) to observe failure.

**Test Cases**:
1. **Multi-session split (op 19 shape)**: One file's records = 6993 contiguous
   1 Hz records starting 2026-09-28 20:12:17, then a 5-day gap, then 180
   contiguous records on 2026-10-03. Assert correct segmentation yields 2
   operations with durations ~6990 s and ~179 s. On unfixed code the file maps
   to 1 operation of 421275 s — FAIL (confirms bug).
2. **Multi-session split (op 18 shape)**: 56 records on 2026-05-13, 4.5-month
   gap, 4 records on 2026-09-28. Assert 2 operations. Unfixed: 1 operation of
   11992874 s — FAIL.
3. **Cross-file merge (rotated flight)**: File A ends 12:00:00, File B begins
   12:00:01 (1 s gap). Assert the concatenated, timestamp-ordered stream yields
   1 operation spanning both files. Unfixed per-file import yields 2 operations
   — FAIL.
4. **Boundary edge**: a gap of exactly 180 s stays in one operation; a gap of
   181 s splits. Confirms the threshold is inclusive at 180 s.

**Expected Counterexamples**:
- A multi-session file produces one operation with a duration far exceeding the
  sum of its contiguous runs.
- A rotated flight produces two operations instead of one.

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed
importer produces the expected segmentation.

**Pseudocode:**
```
FOR ALL records WHERE isBugCondition(records) DO
  operations := segment_records(sort_by_timestamp(records))
  ASSERT no operation has an internal gap > GAP_THRESHOLD
  ASSERT every gap > GAP_THRESHOLD in the input is a boundary between operations
  ASSERT FOR EACH op: op.duration_seconds = op.end - op.start
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the
fixed importer produces the same single operation as the current importer.

**Pseudocode:**
```
FOR ALL records WHERE NOT isBugCondition(records) DO
  ASSERT segment_records(records) has exactly 1 run
  ASSERT run.start = records[0].timestamp
  ASSERT run.end = records[-1].timestamp
  ASSERT run.record_count = len(records)
  ASSERT summary(run) = summary_current(records)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation
because it is a universal statement over all contiguous record sets. Generate
contiguous 1 Hz streams (and short ground-op-shaped streams) with Hypothesis and
assert single-operation segmentation and unchanged summaries. Observe current
behavior first for the healthy operations, then encode it.

**Test Cases**:
1. **Contiguous single operation**: a 1 Hz stream of N records with no gap > 300
   s segments to exactly 1 operation; assert span and count match current.
2. **Short ground op (op 14 shape)**: 180 contiguous records / 179 s stays 1
   operation.
3. **Short ground op (op 17 shape)**: 120 contiguous records / 119 s stays 1
   operation.
4. **Healthy op round-trip in migration**: re-segmenting the records of a
   healthy op reproduces the same operation (same key, same summary).

### Unit Tests

- `segment_records`: empty input, single record, all-contiguous, single large
  gap, multiple gaps, exact-threshold boundary (180 vs 181 s).
- Cross-file concatenation: records from files tagged out of filename order are
  ordered by timestamp; exact-duplicate boundary records de-duplicated.
- Record-level dedup: re-importing the same records (same timestamp+content)
  is a no-op; a derived operation key collision is impossible for distinct data.
- Migration: re-points `fdl_data` without losing rows; removes bad ops; keeps
  healthy ops; is idempotent on a second run.

### Property-Based Tests

- Segmentation property: for randomly generated timestamp sequences, every
  produced operation has no internal gap > threshold and the cut points are
  exactly the input gaps > threshold (Property 1).
- Preservation property: for randomly generated contiguous streams, segmentation
  yields exactly one operation with unchanged span, count, and summary
  (Property 2).
- Idempotency property: importing a generated candidate set once vs. twice (with
  overlap) yields the same operation set (Property 3).

### Integration Tests

- End-to-end import of a crafted candidate file set (multi-session file +
  rotated-flight pair + healthy single file) through the archiver entry point,
  asserting the resulting `operations` rows and `fdl_data.operation_id`
  assignments.
- Migration against a fixture DB seeded with the op-18/op-19 mega-operations and
  the six healthy operations: assert the bad ops are replaced by correctly
  segmented operations, the healthy ops are unchanged, no `fdl_data` rows are
  lost, and a second migration run changes nothing.
- Downstream check: durations, `has_flight`, `airborne_seconds`, and hourmeter
  spans are correct for the re-segmented flight and ground operations.
