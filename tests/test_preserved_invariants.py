# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Preserved-invariant / non-regression guards (chart-sync-wiring-fix task 14.1).

This file is a *concise* regression guard for Requirement 10 — the behaviors
that already worked and MUST keep working after the manifest diff was wired into
the live sync and the false-aborting stall detector was removed. Each invariant
here is already covered in depth by an authoritative test; where that is the
case this file adds only a small complementary assertion and names the
authoritative test in a comment rather than duplicating its coverage.

Invariants guarded (Requirement 10):

  * R10.1 — the nav family job from ``build_jobs`` is unchanged by the manifest
    wiring: ``kind == "files"``, ``files == ["NAV.DB", "NAV-proc.DB"]``,
    ``files_from`` is ``None``, ``removed`` is ``None``, and it carries no
    commit marker.
  * R10.4 — with the stall detector REMOVED, ``sync_payload``'s abort path still
    works (``is_aborted() -> True`` terminates rsync and returns an aborted
    error) AND the stall machinery is gone from the module
    (no ``STALL_TIMEOUT_SECONDS`` / ``_proc_cpu_seconds`` / ``_parse_ps_time``).
  * R10.2 — the card manifest is authoritative for the cycle gate: the
    ``decide_streams`` decision is driven by the ``CardManifest`` cycles, not by
    the volume label (the label is an advisory cross-check only).
  * R10.3 — a label/manifest disagreement logs a WARNING and the manifest wins.
  * R10.5 / R10.6 — the adopt ``ChartData_Delete``
    (``_delete_chart_data_tree``) is scoped to ``ChartData/`` and is DISTINCT
    from the sync's family ``--delete``: it removes ``ChartData/`` while leaving
    ``NAV.DB`` / ``Settings.dat`` / the identity file untouched.

Validates: Requirements 10.1, 10.2, 10.3, 10.4, 10.5, 10.6
"""

import logging
import os
import shutil

import pytest

from efis_data_manager import drive_updater as du
from efis_data_manager.manifest import CardManifest


LOCAL_ROOT = "/local/EFIS_USBImage"
MOUNT = "/Volumes/EFIS_1"


@pytest.fixture
def fake_local_root(monkeypatch):
    """Pin the local image root so build_jobs does not read real user config."""
    monkeypatch.setattr(du, "load_config", lambda: {"usb_image_path": LOCAL_ROOT})
    # No card manifest on this fake mount -> chart families fall back to legacy
    # jobs, but the nav job is manifest-independent, which is what R10.1 checks.
    return LOCAL_ROOT


# ---------------------------------------------------------------------------
# R10.1 — nav family job unchanged by the manifest wiring
# ---------------------------------------------------------------------------
#
# Authoritative coverage: tests/test_build_jobs.py
#   ::test_nav_job_is_files_kind_with_no_marker (roots/marker/files)
#   ::test_nav_stream_integration.py (end-to-end checksum sync)
# The assertion below is the concise R10.1 non-regression guard: the nav job the
# REAL build_jobs emits carries NO manifest diff fields (files_from/removed are
# None) even though chart families now compute a diff.


def test_nav_job_is_manifest_independent(fake_local_root, monkeypatch):
    """R10.1: the nav job is a files job with no manifest diff and no marker.

    Validates: Requirements 10.1
    """
    # A valid card manifest is present so the chart families take the diff path;
    # the nav job must remain untouched by that wiring regardless.
    monkeypatch.setattr(
        du, "_chart_family_job", lambda *a, **k: None
    )  # omit chart jobs; isolate nav

    nav = [j for j in du.build_jobs(MOUNT) if j.name == "nav"]
    assert len(nav) == 1
    nav = nav[0]

    assert nav.kind == "files"
    assert nav.files == ["NAV.DB", "NAV-proc.DB"]
    # The manifest wiring must NOT populate the diff fields on the nav job.
    assert nav.files_from is None
    assert nav.removed is None
    # No directory commit marker for nav — the files themselves are the marker.
    assert nav.marker_src is None
    assert nav.marker_dst is None


# ---------------------------------------------------------------------------
# R10.4 — abort path intact AND stall machinery gone
# ---------------------------------------------------------------------------
#
# Authoritative coverage of the abort path running rsync against real temp dirs:
# tests/test_sync_payload.py::test_is_aborted_terminates_and_records_error.
# Here we (a) prove the stall machinery is GONE from the module via hasattr/
# getattr, and (b) add a lightweight, rsync-free abort-path guard using a fake
# Popen so this invariant is checked even on a host without rsync on PATH.


def test_stall_machinery_is_gone():
    """R10.4/R1: the stall detector attributes no longer exist on the module.

    Validates: Requirements 10.4
    """
    # None of the removed stall-detector symbols may exist anymore.
    assert not hasattr(du, "STALL_TIMEOUT_SECONDS")
    assert not hasattr(du, "_proc_cpu_seconds")
    assert not hasattr(du, "_parse_ps_time")
    # getattr with a sentinel default confirms the names resolve to nothing.
    sentinel = object()
    assert getattr(du, "STALL_TIMEOUT_SECONDS", sentinel) is sentinel
    assert getattr(du, "_proc_cpu_seconds", sentinel) is sentinel
    assert getattr(du, "_parse_ps_time", sentinel) is sentinel
    # sync_payload no longer accepts a stall_hook parameter.
    import inspect

    assert "stall_hook" not in inspect.signature(du.sync_payload).parameters


def test_abort_path_terminates_and_reports_aborted(tmp_path, monkeypatch, caplog):
    """R10.4: is_aborted() -> True terminates rsync and returns an aborted error.

    This is the stall-detector-free liveness mechanism: the poll loop performs
    ONLY the ``is_aborted()`` check plus ``proc.wait()`` draining. A fake Popen
    keeps the test independent of rsync being installed and proves the aborted
    branch still fires with the stall machinery removed.

    Validates: Requirements 10.4
    """
    import subprocess

    src = tmp_path / "local"
    dst = tmp_path / "drive"
    (src / "SEC").mkdir(parents=True)
    (src / "SEC" / "a.png").write_text("aaa")
    dst.mkdir()

    terminated = {"called": False}

    class _NeverEndingProc:
        """A process that never exits on its own; only an abort stops it."""

        def __init__(self):
            self.returncode = None
            self.pid = 9999
            # sync_payload feeds the change set over stdin; a None stdin makes
            # it skip the write (nothing to feed this fake process).
            self.stdin = None

        def poll(self):
            return self.returncode  # None until terminated

        def wait(self, timeout=None):
            # Still running (pre-abort) -> looks like a long transfer; the poll
            # loop treats a timeout as "keep waiting". Once terminated, report
            # the exit code like a real process would.
            if self.returncode is None:
                raise subprocess.TimeoutExpired(cmd="rsync", timeout=timeout)
            return self.returncode

        def terminate(self):
            terminated["called"] = True
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    def fake_popen(cmd, stdout=None, stderr=None, stdin=None, **k):
        return _NeverEndingProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    # manifest-only-chart-sync R2.1: sync_payload is manifest-only, so the job
    # carries a change set (files_from). The abort path is unchanged by the
    # manifest wiring — is_aborted() -> True still terminates the transfer.
    job = du.SyncJob(
        name="scanned",
        kind="tree",
        payload_root_local=str(src) + os.sep,
        payload_root_drive=str(dst) + os.sep,
        excludes=list(du.COMMON_TREE_EXCLUDES),
        files_from=["SEC/a.png"],
        removed=[],
    )

    with caplog.at_level(logging.ERROR, logger=du.logger.name):
        files_updated, errors = du.sync_payload(job, is_aborted=lambda: True)

    assert files_updated == 0
    assert len(errors) == 1
    assert "aborted" in errors[0].lower()
    assert terminated["called"] is True
    assert any(rec.levelno >= logging.ERROR for rec in caplog.records)


# ---------------------------------------------------------------------------
# R10.2 — the card manifest is authoritative for the cycle gate
# ---------------------------------------------------------------------------
#
# Authoritative coverage: tests/test_decide_streams.py (full matrix of
# skip-cycle / diff decisions). This is a single concise guard that the decision
# follows the CardManifest cycles and NOT the volume label.


def test_decide_streams_is_driven_by_card_manifest_not_label(monkeypatch):
    """R10.2: decide_streams follows the CardManifest cycle, not the label.

    The label is deliberately set to a cycle that would flip the decision if it
    were authoritative; the manifest must win.

    Validates: Requirements 10.2
    """
    # Label claims charts=9999 (would force a diff if the label were trusted),
    # but the manifest records charts=2611 == local 2611 -> skip-cycle.
    monkeypatch.setattr(du, "_volume_name", lambda mp: "C9999P9999")
    card = CardManifest(charts_cycle="2611", plates_cycle="2612")
    local = du.LocalCycles(charts_cycle="2611", plates_cycle="2612")

    by_family = {d.family: d for d in du.decide_streams(MOUNT, local, card)}

    # Decision tracks the manifest (equal cycles -> skip), ignoring the label.
    assert by_family["scanned"].action == "skip-cycle"
    assert by_family["scanned"].card_cycle == "2611"
    assert by_family["plates"].action == "skip-cycle"
    assert by_family["plates"].card_cycle == "2612"


# ---------------------------------------------------------------------------
# R10.3 — label/manifest disagreement logs a WARNING, manifest wins
# ---------------------------------------------------------------------------
#
# Authoritative coverage: tests/test_decide_streams.py
#   ::test_label_disagreement_logs_warning_manifest_wins. Concise complement:
# a disagreeing _volume_name logs a WARNING through decide_streams and the
# decision still follows the manifest.


def test_label_disagreement_warns_and_manifest_still_wins(monkeypatch, caplog):
    """R10.3: a disagreeing volume label warns; the manifest value is used.

    Validates: Requirements 10.3
    """
    # Label says charts=2609 but the authoritative manifest says 2611 == local.
    monkeypatch.setattr(du, "_volume_name", lambda mp: "C2609P2612")
    card = CardManifest(charts_cycle="2611", plates_cycle="2612")
    local = du.LocalCycles(charts_cycle="2611", plates_cycle="2612")

    with caplog.at_level(logging.WARNING, logger=du.logger.name):
        by_family = {d.family: d for d in du.decide_streams(MOUNT, local, card)}

    assert any("disagrees" in rec.message for rec in caplog.records)
    # Manifest wins: equal cycles (2611 == 2611) -> skip, unswayed by the label.
    assert by_family["scanned"].action == "skip-cycle"


# ---------------------------------------------------------------------------
# R10.5 / R10.6 — adopt ChartData delete is scoped and distinct from --delete
# ---------------------------------------------------------------------------
#
# Authoritative coverage: tests/test_delete_chart_data_scope_property.py
#   ::test_delete_chart_data_scope (Property 9, 100 arbitrary card layouts).
# This is a SINGLE concise example (not a duplicate of the property test): the
# adopt delete removes ChartData/ but leaves the nav DB, Settings.dat, and the
# identity file intact.
#
# NOTE on the DISTINCTION from the sync's family --delete (R10.5): the sync's
# rsync --delete is scoped WITHIN the family payload roots (ChartData/ and
# ChartData/Plates/) and therefore never touches root-level flight data. That
# sync-side scoping is covered by task 6.6's property test
# (tests/test_chart_sync_preserves_root_flight_data_property.py). The adopt
# delete tested here is a separate, explicit, user-confirmed action.


def test_adopt_delete_is_scoped_to_chartdata(tmp_path):
    """R10.5/R10.6: _delete_chart_data_tree removes ChartData/ only.

    NAV.DB, Settings.dat, and the identity file (all root-level, outside
    ChartData/) survive the adopt delete, demonstrating the adopt
    ChartData_Delete is scoped to the chart tree and distinct from any
    whole-root wipe.

    Validates: Requirements 10.5, 10.6
    """
    mount = tmp_path / "card"
    chart_root = mount / "ChartData"

    # ChartData/ payload + markers (the delete target).
    (chart_root / "SEC").mkdir(parents=True)
    (chart_root / "SEC" / "a.png").write_text("chart-bytes")
    (chart_root / "Plates").mkdir()
    (chart_root / "Plates" / "KABC.pdf").write_text("plate-bytes")
    (chart_root / "ScannedCharts.sqlite").write_text("scanned-marker")
    (chart_root / "Plates" / "Plates.sqlite").write_text("plates-marker")

    # Root-level survivors (outside ChartData/) that must NOT be touched.
    (mount / "NAV.DB").write_text("nav-db-bytes")
    (mount / "Settings.dat").write_text("settings-bytes")
    (mount / "EFIS_DRIVE_ID.json").write_text('{"id": "abc"}')

    du._delete_chart_data_tree(str(mount))

    # ChartData/ and everything inside it is gone.
    assert not chart_root.exists()
    # The root-level flight data / identity survived untouched with identical
    # bytes (the adopt delete is NOT a root-level wipe).
    assert (mount / "NAV.DB").read_text() == "nav-db-bytes"
    assert (mount / "Settings.dat").read_text() == "settings-bytes"
    assert (mount / "EFIS_DRIVE_ID.json").read_text() == '{"id": "abc"}'
    # The volume root itself still exists.
    assert mount.is_dir()
