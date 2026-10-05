# Bugfix Requirements Document

## Introduction

The EFIS Data Manager imports GRT FDL (Flight Data Logger) CSV files into a
SQLite database. The current importer treats one FDL file as exactly one
"operation" (one engine start/stop cycle): it reads a file with
`parse_fdl_file`, takes the operation span as the first record's timestamp
through the last record's timestamp, and inserts a single `operations` row per
file via `import_fdl_file`.

That ONE-FILE == ONE-OPERATION assumption is wrong for how the GRT EFIS actually
writes FDL files:

1. A single FDL file can contain MULTIPLE separate sessions (engine start/stop
   cycles) appended over days or months. Whether the EFIS starts a new file per
   session appears to be a GRT setting that may be off, so the importer must
   handle multi-session files regardless of that setting.
2. One flight can SPAN MULTIPLE files when the EFIS rotates to a new numbered
   file at a maximum file size, so an operation can cross file boundaries.
3. FDL filenames CYCLE and are REUSED (`GRT FDL 0000.CSV`, `0001`, `0002`,
   `0003`, then back to `0000`) across different dates, so `source_filename` is
   not a stable key for identifying an operation.

Confirmed via database inspection on 2026-10-04:

- Operation 19 (`GRT FDL 0001.CSV`): 7173 records lumping together a real ~1.9h
  flight on 2026-09-28 (20:12:17 → 22:08:47, 6993 records) PLUS a separate
  ~3-minute ground session on 2026-10-03 (17:10:33 → 17:13:32, 180 records),
  roughly 5 days apart, into ONE operation. Reported duration is 421275 seconds
  (~7021 minutes), and dashboard plots compress to the left with traces running
  far to the right.
- Operation 18 (`GRT FDL 0000.CSV`): 56 records on 2026-05-13 plus 4 records on
  2026-09-28, ~4.5 months apart in one file, producing one bogus operation
  spanning 11992874 seconds.
- Operations 3, 8, 14, 15, 16, 17 are HEALTHY: `duration_seconds` ≈
  `record_count` (contiguous 1 Hz sampling). Short contiguous ground operations
  (op 14: 180 records / 179 s; op 17: 120 records / 119 s) are LEGITIMATE and
  must NOT be split.

Impact: wrong per-flight durations, broken and compressed dashboard plots, and
corrupted downstream analysis (oil consumption per hour, per-flight statistics,
and trends).

The fix is to segment operations by timestamp gaps rather than by file. An
operation is a maximal contiguous run of records whose consecutive-timestamp gap
stays within a threshold, independent of file boundaries. The user confirmed a
gap threshold of 3 minutes. The same single rule both SPLITS multi-session files
into multiple operations and MERGES a flight that was rotated across consecutive
files. The fix also includes a re-import/cleanup migration that re-segments the
already-imported data into correct operations.

## Bug Analysis

### Current Behavior (Defect)

What currently happens when the bug is triggered:

1.1 WHEN a single FDL file contains two or more record sessions separated by a
large timestamp gap (for example 5 days or 4.5 months) THEN the system creates
ONE operation spanning the entire file, with `duration_seconds` computed as
last-record-timestamp minus first-record-timestamp.

1.2 WHEN an operation contains sessions separated by a large gap THEN the system
reports a `duration_seconds` far larger than the sum of the actual contiguous
record spans (for example 421275 s for op 19 and 11992874 s for op 18).

1.3 WHEN the dashboard plots the time-series data for such an operation THEN the
traces compress to the left and run far to the right because the plotted time
axis spans the full bogus duration.

1.4 WHEN a single flight was rotated by the EFIS across two or more consecutive
numbered files THEN the system imports each file as a separate operation,
splitting one real flight into multiple operations.

1.5 WHEN FDL filenames cycle (they start at 0000 on every fresh USB and are
reused across unrelated data) THEN the system's `(source_filename, start_time)`
dedup key does not reliably identify data: the same name recurs for different
flights, and one operation can span multiple files — so file/name-based dedup is
fundamentally unsound.

1.6 WHEN downstream analysis (oil consumption per hour, per-flight statistics,
trends) consumes an operation produced from a multi-session file THEN the
results are corrupted because the operation's duration and record span do not
represent a single real engine cycle.

### Expected Behavior (Correct)

What should happen instead:

2.1 WHEN a single FDL file contains two or more record sessions separated by a
consecutive-record timestamp gap greater than 3 minutes THEN the system SHALL
split the records into one operation per contiguous run, so each operation
spans exactly one maximal run of records whose consecutive gaps are at most
3 minutes.

2.2 WHEN an operation is formed from a contiguous run THEN the system SHALL
compute `duration_seconds` as that run's last-record timestamp minus its
first-record timestamp, so the duration reflects the real engine cycle (for
example op 19's flight becomes ~1.9h and its ground session becomes ~3 minutes,
as separate operations).

2.3 WHEN a single flight was rotated by the EFIS across two or more consecutive
numbered files and the records continue across the file boundary with a gap of
at most 3 minutes THEN the system SHALL merge those records into one operation
spanning the file boundary.

2.4 WHEN the importer groups candidate FDL files for segmentation THEN the
system SHALL order records by timestamp (not by filename) before segmenting,
because filenames cycle and are not monotonic with time.

2.5 WHEN segmenting records into operations THEN the system SHALL depend only on
the record timestamps the parser already reads, and SHALL NOT depend on any GRT
setting or on FDL documentation.

2.6 WHEN records are imported THEN the system SHALL de-duplicate at the RECORD
level by (timestamp, content_hash) — NOT by filename — so that rotated-file
boundary overlap on one drive, and any interrupted/re-run import, are idempotent
and create no duplicate records or operations. (FDL files are moved off the USB
on import, so this is a safety/idempotency net; it also positions the system for
future multi-device collision handling.) Filename SHALL NOT be part of any dedup
or identity key (GRT FDL filenames start at 0000 and cycle on every fresh USB).

2.7 WHEN the fix is deployed THEN the system SHALL re-process the already
imported data (including the bad operations 18 and 19) into correctly segmented
operations, de-duplicating correctly and not double-importing the healthy
operations.

2.8 WHEN downstream analysis consumes a correctly segmented operation THEN the
system SHALL produce correct per-flight durations, `has_flight`,
`airborne_seconds`, hourmeter spans, and oil/analysis statistics.

### Unchanged Behavior (Regression Prevention)

Existing behavior that must be preserved:

3.1 WHEN an FDL file contains a single contiguous run of records with no
consecutive gap greater than 3 minutes THEN the system SHALL CONTINUE TO produce
exactly one operation for that run with the same start time, end time, duration,
and record count as before.

3.2 WHEN a short contiguous ground operation is imported (for example op 14 with
180 records / 179 s, or op 17 with 120 records / 119 s) THEN the system SHALL
CONTINUE TO keep it as a single operation and SHALL NOT split it.

3.3 WHEN the already-healthy operations (ids 3, 8, 14, 15, 16, 17) are
re-processed by the cleanup migration THEN the system SHALL CONTINUE TO
represent them as the same operations (same contiguous record membership, same
computed summary values) and SHALL NOT duplicate or alter them.

3.4 WHEN an operation's summary statistics are computed THEN the system SHALL
CONTINUE TO compute `has_flight`, `airborne_seconds`, max/min engine values,
cruise averages, fuel used, and hourmeter start/end using the existing
`_compute_operation_summary` logic, applied to the records of each correctly
segmented operation.

3.5 WHEN the archiver imports a batch of FDL files that are all single-session,
single-file, contiguous operations THEN the system SHALL CONTINUE TO import them
with no change to the resulting operation rows versus the current behavior.

3.6 WHEN logbook enrichment matches legs to operations by hourmeter range THEN
the system SHALL CONTINUE TO work against the correctly segmented operations
(the hourmeter ranges remain valid per contiguous run).

---

## Deriving the Bug Condition

The bug condition identifies the record streams that the current importer
mis-segments. `F` is the current importer (one operation per file); `F'` is the
fixed importer (gap-based segmentation across the concatenated, timestamp-ordered
record stream).

**Bug Condition Function** — identifies inputs the current importer gets wrong:

```pascal
FUNCTION isBugCondition(records)
  INPUT: records = time-ordered list of FDLRecord drawn from the candidate
         FDL file set (concatenated across files, sorted by timestamp)
  OUTPUT: boolean

  // GAP_THRESHOLD = 3 minutes (180 seconds)
  // The bug triggers whenever the "true" segmentation (by gap) differs from
  // the current segmentation (by file). Two concrete forms:

  // (a) SPLIT case: a single file's records contain a gap > threshold
  //     -> current code makes 1 operation; correct is >1.
  hasInternalLargeGap :=
    EXISTS i WHERE records[i] and records[i+1] come from the SAME file
             AND (records[i+1].timestamp - records[i].timestamp) > GAP_THRESHOLD

  // (b) MERGE case: a flight rotated across files, records continue across the
  //     file boundary within threshold -> current code makes >1 operation;
  //     correct is 1.
  hasCrossFileContinuation :=
    EXISTS i WHERE records[i] and records[i+1] come from DIFFERENT files
             AND (records[i+1].timestamp - records[i].timestamp) <= GAP_THRESHOLD

  RETURN hasInternalLargeGap OR hasCrossFileContinuation
END FUNCTION
```

**Property Specification** — correct behavior for buggy inputs (Fix Checking):

```pascal
// Property: Fix Checking - gap-based segmentation
FOR ALL records WHERE isBugCondition(records) DO
  operations := F'(records)   // fixed segmentation
  // Each operation is a maximal contiguous run split exactly at gaps > threshold
  ASSERT segments_split_exactly_at_gaps_over(operations, GAP_THRESHOLD)
  ASSERT FOR EACH op IN operations:
           op.duration_seconds = op.last.timestamp - op.first.timestamp
           AND no_internal_gap_exceeds(op, GAP_THRESHOLD)
END FOR
```

**Preservation Goal** — unchanged for non-buggy inputs:

```pascal
// Property: Preservation Checking
FOR ALL records WHERE NOT isBugCondition(records) DO
  // A single contiguous run (no internal large gap, no cross-file continuation
  // to merge) must segment to exactly the same single operation as before.
  ASSERT F(records) = F'(records)
END FOR
```

This guarantees that single-session, single-file, contiguous operations
(including legitimate short ground operations) are segmented identically to the
current behavior, while multi-session files are split and rotated flights are
merged.
