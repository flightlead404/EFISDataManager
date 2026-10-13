# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the MANUAL image-write hardware harness PURE logic (task 12).

The harness (:mod:`efis_data_manager.image_write_harness`) performs a REAL,
DESTRUCTIVE ``dd`` on physical hardware and is deliberately excluded from CI.
These tests cover ONLY its pure, device-free surface — the acceptance gate,
the throughput/speedup metrics, the from-timings mapping, and the report
formatter — using synthetic ``ComposeResult`` / ``WriteResult`` stand-ins.

NOTHING here touches a device, needs root, or reaches the destructive stages:
the real stages (``inspect_target`` / ``unmount_slice`` / ``confirm_and_write``)
are only reached via the explicit ``--write --i-understand-this-erases-the-drive``
CLI flags, which these tests never pass. This is the same CI-safe boundary the
harness's own ``--self-test`` uses.

Requirements: 5.1, 6.1, 6.3, 2.3
"""

from dataclasses import dataclass
from typing import Optional

from efis_data_manager import image_write_harness as h


# --- synthetic stand-ins for the real dataclasses (no device) --------------


@dataclass
class _FakeCompose:
    image_path: str
    image_sha256: str
    byte_size: int
    families_included: list
    composed_at: str


@dataclass
class _FakeWrite:
    device_node: str
    bytes_written: int
    verified: bool
    status: str
    error: Optional[str] = None


def _compose_timing(elapsed=120.0):
    return h.ComposeTiming(
        result=_FakeCompose(
            image_path="/tmp/EFIS_USB.img",
            image_sha256="deadbeef",
            byte_size=30_000_000_000,
            families_included=["charts", "nav", "identity", "marker"],
            composed_at="2026-10-03T00:00:00+00:00",
        ),
        elapsed_seconds=elapsed,
    )


def _write_timing(status="complete", verified=True, bytes_written=30_000_000_000,
                  error=None, elapsed=300.0, refused=False):
    return h.WriteTiming(
        result=_FakeWrite(
            device_node="/dev/disk9s1",
            bytes_written=bytes_written,
            verified=verified,
            status=status,
            error=error,
        ),
        elapsed_seconds=elapsed,
        refused=refused,
    )


# --- compute_metrics -------------------------------------------------------


def test_metrics_total_and_throughput():
    m = h.compute_metrics(
        h.AcceptanceInputs(
            guard_passed=True, dd_completed=True, status="complete",
            verified=True, bytes_written=30_000_000_000,
            compose_seconds=120.0, write_seconds=300.0,
        )
    )
    assert m["total_seconds"] == 420.0
    assert abs(m["dd_bytes_per_second"] - 100_000_000.0) < 1.0


def test_metrics_no_speedup_without_baseline():
    m = h.compute_metrics(
        h.AcceptanceInputs(
            guard_passed=True, dd_completed=True, status="complete",
            verified=True, bytes_written=1, compose_seconds=1.0,
            write_seconds=1.0, baseline_seconds=None,
        )
    )
    assert "speedup_ratio" not in m
    assert "baseline_seconds" not in m


def test_metrics_speedup_with_baseline():
    m = h.compute_metrics(
        h.AcceptanceInputs(
            guard_passed=True, dd_completed=True, status="complete",
            verified=True, bytes_written=1, compose_seconds=120.0,
            write_seconds=300.0, baseline_seconds=1260.0,
        )
    )
    assert abs(m["speedup_ratio"] - 3.0) < 1e-6


def test_metrics_zero_write_time_is_safe():
    m = h.compute_metrics(
        h.AcceptanceInputs(
            guard_passed=True, dd_completed=False, status="incomplete",
            verified=False, bytes_written=0, compose_seconds=0.0,
            write_seconds=0.0,
        )
    )
    assert m["dd_bytes_per_second"] == 0.0
    assert m["total_seconds"] == 0.0


# --- evaluate_acceptance ---------------------------------------------------


def test_gate_clean_passes():
    r = h.evaluate_acceptance(
        h.AcceptanceInputs(
            guard_passed=True, dd_completed=True, status="complete",
            verified=True, bytes_written=1, compose_seconds=1.0,
            write_seconds=1.0,
        )
    )
    assert r.passed


def test_gate_guard_refusal_fails():
    r = h.evaluate_acceptance(
        h.AcceptanceInputs(False, True, "complete", True, 1, 1.0, 1.0)
    )
    assert not r.passed
    assert not r.guard_passed


def test_gate_no_dd_fails():
    r = h.evaluate_acceptance(
        h.AcceptanceInputs(True, False, "complete", True, 0, 1.0, 1.0)
    )
    assert not r.passed
    assert not r.dd_completed


def test_gate_non_complete_status_fails():
    r = h.evaluate_acceptance(
        h.AcceptanceInputs(True, True, "incomplete", True, 1, 1.0, 1.0)
    )
    assert not r.passed
    assert not r.status_complete


def test_gate_unverified_fails():
    r = h.evaluate_acceptance(
        h.AcceptanceInputs(True, True, "complete", False, 1, 1.0, 1.0)
    )
    assert not r.passed
    assert not r.verified


# --- acceptance_from_timings ----------------------------------------------


def test_from_timings_complete_verified_passes():
    r = h.acceptance_from_timings(
        _compose_timing(), _write_timing(), baseline_seconds=1260.0
    )
    assert r.passed
    assert abs(r.metrics["speedup_ratio"] - 3.0) < 1e-6


def test_from_timings_harness_refusal_fails():
    r = h.acceptance_from_timings(
        _compose_timing(),
        _write_timing(
            status="incomplete", verified=False, bytes_written=0,
            error="refused: explicit confirmation (confirmed=True) required",
            elapsed=0.0, refused=True,
        ),
    )
    assert not r.passed
    assert not r.guard_passed


def test_from_timings_writer_guard_refusal_fails():
    r = h.acceptance_from_timings(
        _compose_timing(),
        _write_timing(
            status="incomplete", verified=False, bytes_written=0,
            error="device guard refused target: slice size mismatch",
            elapsed=0.0,
        ),
    )
    assert not r.passed
    assert not r.guard_passed


def test_from_timings_verify_failure_dd_ran_but_fails():
    # dd wrote bytes (guard passed) but readback mismatched -> incomplete.
    r = h.acceptance_from_timings(
        _compose_timing(),
        _write_timing(
            status="incomplete", verified=False,
            bytes_written=30_000_000_000, error="readback mismatch",
        ),
    )
    assert not r.passed
    assert r.guard_passed  # the guard DID pass; verification is what failed
    assert r.dd_completed
    assert not r.verified


# --- format_report ---------------------------------------------------------


def test_report_leads_with_verdict_and_includes_sections():
    r = h.acceptance_from_timings(
        _compose_timing(), _write_timing(), baseline_seconds=1260.0
    )
    md = h.format_report(
        r, device_node="/dev/disk9s1", image_sha256="deadbeef",
        families_included=["charts", "nav"],
    )
    assert "Result:** PASS" in md
    assert "dd throughput" in md
    assert "speedup" in md
    assert "/dev/disk9s1" in md


def test_report_without_baseline_notes_timings_only():
    r = h.acceptance_from_timings(_compose_timing(), _write_timing())
    md = h.format_report(r, device_node="/dev/disk9s1")
    assert "no baseline supplied" in md
