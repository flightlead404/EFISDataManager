# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for build_jobs() per-family job definitions in drive_updater.

Covers Task 2 of the drive-sync-integrity spec:
  - job specs contain the right payload roots, excludes, and markers per family

Requirements: 1.1, 1.5
"""

import os

import pytest

from efis_data_manager import drive_updater as du


LOCAL_ROOT = "/local/EFIS_USBImage"
MOUNT = "/Volumes/EFIS_1"


@pytest.fixture
def fake_local_root(monkeypatch):
    """Pin the local image root so tests don't depend on real user config."""
    monkeypatch.setattr(
        du, "load_config", lambda: {"usb_image_path": LOCAL_ROOT}
    )
    return LOCAL_ROOT


def _by_name(jobs):
    return {job.name: job for job in jobs}


# --- default set ------------------------------------------------------------


def test_build_jobs_returns_all_three_in_order(fake_local_root):
    jobs = du.build_jobs(MOUNT)
    assert [j.name for j in jobs] == ["scanned", "plates", "nav"]


def test_build_jobs_filters_to_requested_families(fake_local_root):
    jobs = du.build_jobs(MOUNT, families=["plates"])
    assert [j.name for j in jobs] == ["plates"]

    jobs = du.build_jobs(MOUNT, families=["nav", "scanned"])
    # Requested order is preserved.
    assert [j.name for j in jobs] == ["nav", "scanned"]


def test_build_jobs_ignores_unknown_family(fake_local_root):
    jobs = du.build_jobs(MOUNT, families=["scanned", "bogus"])
    assert [j.name for j in jobs] == ["scanned"]


# --- scanned family ---------------------------------------------------------


def test_scanned_job_roots_and_marker(fake_local_root):
    scanned = _by_name(du.build_jobs(MOUNT))["scanned"]

    assert scanned.kind == "tree"
    # Payload roots point at ChartData/ with a trailing separator (rsync
    # "contents of").
    assert scanned.payload_root_local == os.path.join(LOCAL_ROOT, "ChartData") + os.sep
    assert scanned.payload_root_drive == os.path.join(MOUNT, "ChartData") + os.sep

    # Commit marker is ChartData/ScannedCharts.sqlite on both sides.
    assert scanned.marker_src == os.path.join(LOCAL_ROOT, "ChartData", "ScannedCharts.sqlite")
    assert scanned.marker_dst == os.path.join(MOUNT, "ChartData", "ScannedCharts.sqlite")

    # No standalone files for a tree job.
    assert scanned.files == []


def test_scanned_job_excludes_plates_and_own_marker(fake_local_root):
    scanned = _by_name(du.build_jobs(MOUNT))["scanned"]

    # The scanned payload is ChartData minus Plates/ minus its own marker.
    assert "Plates/" in scanned.excludes
    assert "ScannedCharts.sqlite" in scanned.excludes
    # Common metadata excludes are carried through.
    for pat in du.COMMON_TREE_EXCLUDES:
        assert pat in scanned.excludes


# --- plates family ----------------------------------------------------------


def test_plates_job_roots_and_marker(fake_local_root):
    plates = _by_name(du.build_jobs(MOUNT))["plates"]

    assert plates.kind == "tree"
    assert plates.payload_root_local == os.path.join(LOCAL_ROOT, "ChartData", "Plates") + os.sep
    assert plates.payload_root_drive == os.path.join(MOUNT, "ChartData", "Plates") + os.sep

    assert plates.marker_src == os.path.join(LOCAL_ROOT, "ChartData", "Plates", "Plates.sqlite")
    assert plates.marker_dst == os.path.join(MOUNT, "ChartData", "Plates", "Plates.sqlite")
    assert plates.files == []


def test_plates_job_excludes_own_marker(fake_local_root):
    plates = _by_name(du.build_jobs(MOUNT))["plates"]

    assert "Plates.sqlite" in plates.excludes
    for pat in du.COMMON_TREE_EXCLUDES:
        assert pat in plates.excludes
    # The plates job must NOT exclude Plates/ (that is its own payload root).
    assert "Plates/" not in plates.excludes


# --- nav family -------------------------------------------------------------


def test_nav_job_is_files_kind_with_no_marker(fake_local_root):
    nav = _by_name(du.build_jobs(MOUNT))["nav"]

    assert nav.kind == "files"
    # NAV.DB and NAV-proc.DB are the standalone files; verified by checksum.
    assert nav.files == ["NAV.DB", "NAV-proc.DB"]
    # No directory commit marker for nav — the files themselves are the marker.
    assert nav.marker_src is None
    assert nav.marker_dst is None
    assert nav.payload_root_local == ""
    assert nav.payload_root_drive == ""
