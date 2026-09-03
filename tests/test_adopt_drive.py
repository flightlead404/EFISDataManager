# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for Task 20: Prepare Drive adopt-vs-clean + flight-data safety.

Covers the pure/headless drive_updater pieces that back the app.py Prepare Drive
flow (the interactive rumps dialogs themselves are not unit-tested):

  - adopt_drive: NON-destructive adoption of an existing GRT chart drive. Writes
    an identity file (drive becomes managed), runs an incremental update, returns
    a {"success", "message"} dict, never reformats, and leaves files outside the
    family roots untouched.
  - has_unarchived_flight_data: True when any FDL/DEMO/SNAP/Logbook/Settings-bak
    file is present; False for a chart-only or blank drive.
  - _summarize_update: the shared prepare/adopt summary helper — clean -> success,
    a failed family -> not success and names it.

Requirements: 10.9, 10.10, 10.11, 10.14, 10.16
"""

import os

import pytest

from efis_data_manager import drive_updater as du
from efis_data_manager.drive_updater import JobResult


@pytest.fixture(autouse=True)
def _no_diskutil(monkeypatch):
    """No real diskutil in tests; identity captures no VolumeUUID/name."""
    monkeypatch.setattr(du, "_volume_uuid", lambda mp: None)
    monkeypatch.setattr(du, "_volume_name", lambda mp: None)


# --- _summarize_update (shared prepare/adopt helper) -------------------------


def test_summarize_update_clean_is_success():
    results = {
        "jobs": {
            "scanned": JobResult(name="scanned", status="updated", verified=True),
            "plates": JobResult(name="plates", status="current", verified=True),
            "nav": JobResult(name="nav", status="updated", verified=True),
        },
        "errors": [],
        "aborted": False,
    }
    success, summary = du._summarize_update(results)
    assert success is True
    assert "scanned: updated" in summary
    assert "plates: current" in summary
    assert "nav: updated" in summary


def test_summarize_update_failed_family_not_success_and_named():
    results = {
        "jobs": {
            "scanned": JobResult(name="scanned", status="updated", verified=True),
            "plates": JobResult(
                name="plates",
                status="failed",
                errors=["plates verification failed"],
                verified=False,
            ),
        },
        "errors": ["plates verification failed"],
        "aborted": False,
    }
    success, summary = du._summarize_update(results)
    assert success is False
    assert "plates" in summary


def test_summarize_update_aborted_not_success_and_named():
    results = {
        "jobs": {
            "nav": JobResult(name="nav", status="aborted", verified=False),
        },
        "errors": ["nav sync aborted"],
        "aborted": True,
    }
    success, summary = du._summarize_update(results)
    assert success is False
    assert "nav" in summary


# --- has_unarchived_flight_data ----------------------------------------------


@pytest.mark.parametrize(
    "filename",
    [
        "GRT FDL 2026-01-01.csv",
        "DEMO-0001.LOG",
        "SNAP0001.PNG",
        "Logbook.csv",
        "Settings.bak",
        "State.bak",
        "WP.bak",
        "Plan.bak",
    ],
)
def test_has_flight_data_true_for_each_pattern(tmp_path, filename):
    (tmp_path / filename).write_text("x")
    assert du.has_unarchived_flight_data(str(tmp_path)) is True


def test_has_flight_data_case_insensitive(tmp_path):
    # GRT writes uppercase extensions; a lowercased name must still match.
    (tmp_path / "demo-0002.log").write_text("x")
    assert du.has_unarchived_flight_data(str(tmp_path)) is True


def test_has_flight_data_false_for_chart_only_drive(tmp_path):
    (tmp_path / "GRTCHARTS").mkdir()
    (tmp_path / "ChartData").mkdir()
    (tmp_path / "ChartData" / "ScannedCharts.sqlite").write_text("marker")
    assert du.has_unarchived_flight_data(str(tmp_path)) is False


def test_has_flight_data_false_for_blank_drive(tmp_path):
    assert du.has_unarchived_flight_data(str(tmp_path)) is False


def test_has_flight_data_false_on_unreadable_mount(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert du.has_unarchived_flight_data(str(missing)) is False


def test_has_flight_data_ignores_matching_directory(tmp_path):
    # A directory named like a pattern is not flight data (read-only, isfile).
    (tmp_path / "SNAPSHOTS.PNG").mkdir()
    assert du.has_unarchived_flight_data(str(tmp_path)) is False


# --- adopt_drive (non-destructive) -------------------------------------------


def _make_local_image(tmp_path):
    """Build a minimal local USB image with all three family markers + payload."""
    image = tmp_path / "image"
    chart = image / "ChartData"
    plates = chart / "Plates"
    plates.mkdir(parents=True)
    # scanned payload + marker
    (chart / "SEC").mkdir()
    (chart / "SEC" / "tile1.png").write_text("sec-tile")
    (chart / "ScannedCharts.sqlite").write_text("scanned-marker")
    # plates payload + marker
    (plates / "plate1.pdf").write_text("plate")
    (plates / "Plates.sqlite").write_text("plates-marker")
    # nav files
    (image / "NAV.DB").write_text("nav-db")
    (image / "NAV-proc.DB").write_text("nav-proc")
    return image


@pytest.fixture
def _local_image(tmp_path, monkeypatch):
    image = _make_local_image(tmp_path)
    monkeypatch.setattr(du, "load_config", lambda: {"usb_image_path": str(image)})
    # Temp "drives" aren't real OS mount points; make preflight/watchdog treat
    # them as present, writable mounts so the real rsync-based update runs.
    monkeypatch.setattr(du.os.path, "ismount", lambda p: True)
    return image


def test_adopt_drive_writes_identity_runs_update_no_format(
    tmp_path, _local_image, monkeypatch
):
    # A pre-existing GRT drive: has GRTCHARTS/ + ChartData/ but no identity.
    drive = tmp_path / "drive"
    (drive / "GRTCHARTS").mkdir(parents=True)
    (drive / "ChartData").mkdir()

    # Guard: adopt must NEVER call diskutil (no format/erase). Cosmetic cleanup
    # like dot_clean (AppleDouble sweep) IS allowed — it is non-destructive to
    # chart data — so only diskutil invocations trip the guard.
    import subprocess

    _real_run = subprocess.run

    def _no_diskutil(cmd, *a, **k):
        argv0 = cmd[0] if isinstance(cmd, (list, tuple)) and cmd else cmd
        if isinstance(argv0, str) and "diskutil" in argv0:
            raise AssertionError("adopt_drive must not shell out to diskutil (no format)")
        # Allow dot_clean etc.; make it a harmless no-op.
        class _P:
            returncode = 0
            stdout = b""
            stderr = b""
        return _P()

    monkeypatch.setattr(subprocess, "run", _no_diskutil)

    # A pre-existing extra file outside any family root must survive adoption.
    (drive / "keep_me.txt").write_text("do not delete")

    assert du.read_identity(str(drive)) is None  # not managed yet

    result = du.adopt_drive(str(drive))

    assert result["success"] is True, result["message"]

    # Identity written -> drive is now managed.
    identity = du.read_identity(str(drive))
    assert identity is not None
    assert identity["kind"] == du.IDENTITY_KIND
    assert identity["id"]

    # update ran: markers + payload copied to the drive.
    assert (drive / "ChartData" / "ScannedCharts.sqlite").exists()
    assert (drive / "ChartData" / "SEC" / "tile1.png").exists()
    assert (drive / "ChartData" / "Plates" / "Plates.sqlite").exists()
    assert (drive / "NAV.DB").exists()

    # Non-destructive: the out-of-family extra file is untouched.
    assert (drive / "keep_me.txt").read_text() == "do not delete"


def test_adopt_drive_preserves_existing_identity(
    tmp_path, _local_image, monkeypatch
):
    drive = tmp_path / "drive2"
    (drive / "ChartData").mkdir(parents=True)

    monkeypatch.setattr(du, "wait_for_mount_ready", lambda *a, **k: True)
    first_id = du._ensure_identity(str(drive))

    result = du.adopt_drive(str(drive))
    assert result["success"] is True
    # Adoption of an already-managed drive keeps its established id.
    assert du.read_identity(str(drive))["id"] == first_id


def test_adopt_drive_failure_reports_failed_family(
    tmp_path, _local_image, monkeypatch
):
    drive = tmp_path / "drive3"
    (drive / "ChartData").mkdir(parents=True)

    def fake_update_drive(mount_point, families=None, progress_callback=None):
        return {
            "jobs": {
                "scanned": JobResult(name="scanned", status="updated", verified=True),
                "plates": JobResult(
                    name="plates",
                    status="failed",
                    errors=["plates verification failed"],
                    verified=False,
                ),
            },
            "errors": ["plates verification failed"],
            "aborted": False,
        }

    monkeypatch.setattr(du, "update_drive", fake_update_drive)

    result = du.adopt_drive(str(drive))
    assert result["success"] is False
    assert "plates" in result["message"]
    # Identity was still written even though the update reported an error.
    assert du.read_identity(str(drive)) is not None
