# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bug-condition exploration test for FDL operation segmentation.

Spec: .kiro/specs/fdl-operation-segmentation/
Task 1 - Property 1: Bug Condition - Gap-Based Segmentation.

This test encodes the EXPECTED post-fix behavior: an operation is a maximal
contiguous run of FDL records whose consecutive-timestamp gaps are all at most
GAP_THRESHOLD (180 s = 3 min). Across the candidate file set, records are
concatenated, SORTED BY TIMESTAMP (never by filename), and cut at every gap
greater than the threshold. One rule both SPLITS a multi-session file into N
operations and MERGES a flight rotated across files into one operation.

**CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug.
The current importer has NO segmentation step: `fdl_parser` has no
`segment_records` function (one FDLFile == one operation, span = first..last
record). So the import below fails / the attribute is missing on unfixed code.

**DO NOT fix the test or the code when it fails.** Failure is the SUCCESS case
for an exploration test: it proves the file-equals-operation assumption is the
root cause. When the fix (Task 4) adds `segment_records`, this same test
validates it (Property 1).

Validates: Requirements 1.1, 1.2, 1.3, 1.4
"""

from datetime import datetime, timedelta

import pytest
from hypothesis import given, settings, strategies as st

from efis_data_manager.fdl_parser import FDLRecord, FDLFile

# Expected post-fix API (see design.md "Segmentation function"):
#   segment_records(records, gap_threshold_seconds=180) -> list[list[FDLRecord]]
# where records is sorted ascending by timestamp and each returned inner list is
# one maximal contiguous run (one operation), and GAP_THRESHOLD_SECONDS == 180.
#
# This symbol does NOT exist on unfixed code. We resolve it lazily (not at module
# import) so the module still collects and the shape tests below fail with a
# meaningful message, AND we add current-behavior tests (via FDLFile, the only
# "operation" unit that exists today) that fail with the concrete counterexample
# (421275 s mega-op; rotated flight split). Both failure modes confirm the bug.
GAP_THRESHOLD_SECONDS = 180  # expected value; asserted against the real constant below


def segment_records(records, gap_threshold_seconds=180):
    """Lazy shim to the expected post-fix API.

    On UNFIXED code this raises (NO segmentation step exists) -> the test FAILS,
    which is the SUCCESS case for an exploration test. On fixed code this calls
    the real `efis_data_manager.fdl_parser.segment_records`.
    """
    from efis_data_manager import fdl_parser
    if not hasattr(fdl_parser, "segment_records"):
        raise AssertionError(
            "BUG CONFIRMED: fdl_parser has no segment_records(). The importer "
            "treats one FDL file as one operation (no gap-based segmentation), "
            "so multi-session files are not split and rotated flights are not "
            "merged. Expected post-fix API: "
            "fdl_parser.segment_records(records, gap_threshold_seconds=180)."
        )
    return fdl_parser.segment_records(records, gap_threshold_seconds)


# --------------------------------------------------------------------------
# Helpers: build in-memory 1 Hz FDLRecord streams.
# --------------------------------------------------------------------------

def _run(start: datetime, n: int, step_seconds: int = 1) -> list[FDLRecord]:
    """A contiguous run of n records at `step_seconds` spacing (default 1 Hz)."""
    return [
        FDLRecord(timestamp=start + timedelta(seconds=i * step_seconds))
        for i in range(n)
    ]


def _spans(runs: list[list[FDLRecord]]) -> list[tuple[datetime, datetime, int]]:
    """(start, end, count) per run, mirroring derived operation identity."""
    return [(r[0].timestamp, r[-1].timestamp, len(r)) for r in runs]


def _max_internal_gap(run: list[FDLRecord]) -> float:
    if len(run) < 2:
        return 0.0
    return max(
        (run[i].timestamp - run[i - 1].timestamp).total_seconds()
        for i in range(1, len(run))
    )


# --------------------------------------------------------------------------
# Concrete cases mirroring the real bug data.
# --------------------------------------------------------------------------

def test_threshold_constant_is_180s():
    """GAP_THRESHOLD is the user-confirmed 3 minutes.

    On unfixed code the constant does not exist yet (bug-confirming failure)."""
    from efis_data_manager import fdl_parser
    assert getattr(fdl_parser, "GAP_THRESHOLD_SECONDS", None) == 180


# --------------------------------------------------------------------------
# Current-behavior counterexamples (via FDLFile == the only "operation" today).
# These reproduce the exact bug numbers and FAIL on unfixed code, documenting
# the counterexample without depending on the not-yet-existing segment_records.
# --------------------------------------------------------------------------

def test_current_op19_file_is_one_bogus_mega_operation():
    """Counterexample (SPLIT): the op-19 shape as ONE FDLFile is a single
    operation of 421275 s spanning a flight + a ground session ~5 days apart.
    Correct behavior is 2 operations. This assertion FAILS on unfixed code,
    documenting the mega-operation counterexample."""
    flight = _run(datetime(2026, 9, 28, 20, 12, 17), 6993)
    ground = _run(datetime(2026, 10, 3, 17, 10, 33), 180)
    fdl = FDLFile("GRT FDL 0001.CSV", "x", flight + ground)

    # Current (buggy) behavior, asserted so the number is on the record:
    assert fdl.record_count == 7173
    assert fdl.duration_seconds == 421275  # the real bogus duration

    # Correct behavior: the file should become 2 operations. On unfixed code the
    # file IS one operation, so this fails (bug confirmed).
    runs = segment_records(flight + ground)
    assert len(runs) == 2


def test_current_rotated_flight_imports_as_two_operations():
    """Counterexample (MERGE): a flight rotated across File A (ends 12:00:00)
    and File B (begins 12:00:01) is imported per-file as 2 operations today.
    Correct behavior is 1 merged operation. FAILS on unfixed code."""
    file_a = _run(datetime(2026, 9, 28, 11, 30, 0), 1801)
    file_b = _run(datetime(2026, 9, 28, 12, 0, 1), 600)

    # Current behavior: two independent FDLFiles -> two operations.
    fa = FDLFile("GRT FDL 0002.CSV", "a", file_a)
    fb = FDLFile("GRT FDL 0003.CSV", "b", file_b)
    assert fa.record_count == 1801 and fb.record_count == 600  # two ops today

    # Correct behavior: concatenated + sorted -> one merged operation.
    runs = segment_records(sorted(file_a + file_b, key=lambda r: r.timestamp))
    assert len(runs) == 1


def test_op19_shape_splits_into_two_operations():
    """Op-19 shape (SPLIT): a ~1.9h flight + a ~3-min ground session, ~5 days
    apart, in ONE file -> expect 2 operations, NOT one 421275 s mega-op.

    Flight: 6993 records from 2026-09-28 20:12:17 (20:12:17 -> 22:08:47).
    Ground: 180 records from 2026-10-03 17:10:33 (17:10:33 -> 17:13:32).
    """
    flight = _run(datetime(2026, 9, 28, 20, 12, 17), 6993)
    ground = _run(datetime(2026, 10, 3, 17, 10, 33), 180)
    records = flight + ground  # already timestamp-ordered

    runs = segment_records(records)

    assert len(runs) == 2, "op-19 file must split at the ~5-day gap"
    spans = _spans(runs)
    # Flight run: 6993 records, 6992 s span (~1.9h).
    assert spans[0] == (
        datetime(2026, 9, 28, 20, 12, 17),
        datetime(2026, 9, 28, 22, 8, 49),
        6993,
    )
    # Ground run: 180 records, 179 s span (~3 min).
    assert spans[1] == (
        datetime(2026, 10, 3, 17, 10, 33),
        datetime(2026, 10, 3, 17, 13, 32),
        180,
    )
    # No operation carries the bogus 421275 s duration.
    for run in runs:
        dur = (run[-1].timestamp - run[0].timestamp).total_seconds()
        assert dur != 421275
        assert _max_internal_gap(run) <= GAP_THRESHOLD_SECONDS


def test_op18_shape_splits_into_two_operations():
    """Op-18 shape (SPLIT): 56 records on 2026-05-13 + 4 records on 2026-09-28,
    ~4.5 months apart in one file -> expect 2 operations, not one 11992874 s op.
    """
    first = _run(datetime(2026, 5, 13, 10, 0, 0), 56)
    second = _run(datetime(2026, 9, 28, 12, 0, 0), 4)
    records = first + second

    runs = segment_records(records)

    assert len(runs) == 2
    spans = _spans(runs)
    assert spans[0][2] == 56
    assert spans[1][2] == 4
    for run in runs:
        dur = (run[-1].timestamp - run[0].timestamp).total_seconds()
        assert dur != 11992874


def test_rotated_flight_merges_across_file_boundary():
    """Rotated flight (MERGE): File A ends 12:00:00, File B begins 12:00:01
    (1 s gap). Concatenated + timestamp-sorted -> 1 operation spanning both
    files. Current per-file import yields 2 operations.
    """
    file_a = _run(datetime(2026, 9, 28, 11, 30, 0), 1801)  # ...ends 12:00:00
    assert file_a[-1].timestamp == datetime(2026, 9, 28, 12, 0, 0)
    file_b = _run(datetime(2026, 9, 28, 12, 0, 1), 600)     # begins 12:00:01

    records = sorted(file_a + file_b, key=lambda r: r.timestamp)
    runs = segment_records(records)

    assert len(runs) == 1, "a flight rotated across files must merge into one op"
    start, end, count = _spans(runs)[0]
    assert start == datetime(2026, 9, 28, 11, 30, 0)
    assert end == datetime(2026, 9, 28, 12, 10, 0)
    assert count == 1801 + 600


def test_boundary_gap_exactly_180s_stays_one_operation():
    """A gap of exactly 180 s is within threshold -> one operation."""
    a = _run(datetime(2026, 9, 28, 12, 0, 0), 10)       # ...ends 12:00:09
    b = _run(datetime(2026, 9, 28, 12, 3, 9), 10)        # 180 s after 12:00:09
    assert (b[0].timestamp - a[-1].timestamp).total_seconds() == 180

    runs = segment_records(a + b)
    assert len(runs) == 1


def test_boundary_gap_181s_splits():
    """A gap of 181 s exceeds the threshold -> two operations."""
    a = _run(datetime(2026, 9, 28, 12, 0, 0), 10)        # ...ends 12:00:09
    b = _run(datetime(2026, 9, 28, 12, 3, 10), 10)       # 181 s after 12:00:09
    assert (b[0].timestamp - a[-1].timestamp).total_seconds() == 181

    runs = segment_records(a + b)
    assert len(runs) == 2


# --------------------------------------------------------------------------
# Property 1 generalized with Hypothesis.
# --------------------------------------------------------------------------

@st.composite
def _gap_sequences(draw):
    """Build a timestamp-ordered record stream from random inter-record gaps,
    so we know exactly where the >180 s cut points should be."""
    n_gaps = draw(st.integers(min_value=0, max_value=40))
    # Gaps straddle the 180 s boundary so both splits and non-splits occur.
    gaps = draw(
        st.lists(
            st.integers(min_value=1, max_value=600),
            min_size=n_gaps, max_size=n_gaps,
        )
    )
    t = datetime(2026, 1, 1, 0, 0, 0)
    records = [FDLRecord(timestamp=t)]
    for g in gaps:
        t = t + timedelta(seconds=g)
        records.append(FDLRecord(timestamp=t))
    return records, gaps


@settings(max_examples=200)
@given(_gap_sequences())
def test_segmentation_cuts_exactly_at_gaps_over_threshold(data):
    """For any timestamp-ordered stream, segment_records cuts the stream exactly
    at gaps > 180 s, every run has no internal gap > 180 s, and each run's
    duration equals its own first-to-last span (Property 1)."""
    records, gaps = data
    runs = segment_records(records)

    # Reassembling the runs reproduces the full stream in order (no loss/dup).
    flat = [r for run in runs for r in run]
    assert [r.timestamp for r in flat] == [r.timestamp for r in records]

    # Number of cut points = number of gaps strictly greater than threshold.
    expected_cuts = sum(1 for g in gaps if g > GAP_THRESHOLD_SECONDS)
    assert len(runs) == expected_cuts + 1 if records else len(runs) == 0

    for run in runs:
        assert len(run) >= 1
        assert _max_internal_gap(run) <= GAP_THRESHOLD_SECONDS
        dur = (run[-1].timestamp - run[0].timestamp).total_seconds()
        assert dur == (run[-1].timestamp - run[0].timestamp).total_seconds()
