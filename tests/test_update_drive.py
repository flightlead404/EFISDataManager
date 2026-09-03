# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for update_drive aggregation and prepare_drive reporting.

Covers Task 9 of the drive-sync-integrity spec:

  - update_drive builds jobs for the requested families and runs each via the
    watchdog-wrapped per-job driver, aggregating results into
    {"jobs": {name: JobResult}, "errors": [...], "aborted": bool}
  - a mixed result (one family updated, one failed) aggregates correctly:
    both JobResults land in "jobs", the failed family's errors land in
    "errors", and "aborted" reflects any aborted job
  - an aborted job sets aborted=True and surfaces its error
  - update_drive runs exactly the families it is given (default None = all)
  - prepare_drive derives success/message from the aggregated jobs/errors and
    reports per-family results (Req 9.4)

Requirements: 1.1, 8.2, 9.3, 9.4

The per-job driver (run_sync_job / run_sync_job_watched) is exercised
end-to-end in test_run_sync_job.py; here it is stubbed so the aggregation
logic is tested deterministically without a real rsync/mount.
"""

import pytest

from efis_data_manager import drive_updater as du
from efis_data_manager.drive_updater import JobResult


@pytest.fixture(autouse=True)
def _fake_config(monkeypatch, tmp_path):
    """build_jobs calls load_config; give it a stable local image root."""
    monkeypatch.setattr(du, "load_config", lambda: {"usb_image_path": str(tmp_path / "image")})


def _stub_jobs_and_driver(monkeypatch, results_by_name):
    """Wire build_jobs -> the named families and run_sync_job_watched -> canned
    JobResults keyed by family name. Records the order/args of driver calls.

    Returns the list capturing (job_name, mount) tuples for assertions.
    """
    calls = []

    def fake_build_jobs(mount_point, families=None):
        names = list(results_by_name) if families is None else list(families)
        # Minimal SyncJob stand-ins: only .name is used by update_drive.
        return [du.SyncJob(name=n, kind="tree") for n in names]

    def fake_watched(job, mount_point, sync_state=None, progress_callback=None,
                     poll_interval=du.WATCHDOG_POLL_INTERVAL, ismount=None):
        calls.append((job.name, mount_point))
        return results_by_name[job.name]

    monkeypatch.setattr(du, "build_jobs", fake_build_jobs)
    monkeypatch.setattr(du, "run_sync_job_watched", fake_watched)
    return calls


# --- mixed result: one updated, one failed ----------------------------------


def test_mixed_result_aggregates_correctly(monkeypatch):
    results_by_name = {
        "scanned": JobResult(
            name="scanned", status="updated", files_updated=12, verified=True
        ),
        "plates": JobResult(
            name="plates",
            status="failed",
            files_updated=0,
            errors=["plates verification failed: 3 missing, 0 extra, 0 size-mismatch"],
            verified=False,
        ),
    }
    _stub_jobs_and_driver(monkeypatch, results_by_name)

    out = du.update_drive("/Volumes/EFIS")

    # Both JobResults present, keyed by family name.
    assert set(out["jobs"]) == {"scanned", "plates"}
    assert out["jobs"]["scanned"].status == "updated"
    assert out["jobs"]["plates"].status == "failed"

    # The failed family's errors are aggregated into the top-level errors list.
    assert out["errors"] == [
        "plates verification failed: 3 missing, 0 extra, 0 size-mismatch"
    ]

    # No aborted job -> aborted is False.
    assert out["aborted"] is False


# --- aborted case -----------------------------------------------------------


def test_aborted_job_sets_aborted_flag(monkeypatch):
    results_by_name = {
        "scanned": JobResult(
            name="scanned", status="updated", files_updated=5, verified=True
        ),
        "plates": JobResult(
            name="plates",
            status="aborted",
            files_updated=0,
            errors=["plates sync aborted (drive removed or system sleeping)"],
            verified=False,
        ),
    }
    _stub_jobs_and_driver(monkeypatch, results_by_name)

    out = du.update_drive("/Volumes/EFIS")

    assert out["aborted"] is True
    assert out["jobs"]["plates"].status == "aborted"
    assert "aborted" in out["errors"][0].lower()
    # The healthy family's result is still aggregated.
    assert out["jobs"]["scanned"].status == "updated"


# --- all-clean case: no errors, not aborted ---------------------------------


def test_all_current_no_errors(monkeypatch):
    results_by_name = {
        "scanned": JobResult(name="scanned", status="current", verified=True),
        "plates": JobResult(name="plates", status="current", verified=True),
        "nav": JobResult(name="nav", status="current", verified=True),
    }
    _stub_jobs_and_driver(monkeypatch, results_by_name)

    out = du.update_drive("/Volumes/EFIS")

    assert out["errors"] == []
    assert out["aborted"] is False
    assert set(out["jobs"]) == {"scanned", "plates", "nav"}


# --- update_drive runs exactly the families it is given ----------------------


def test_runs_only_requested_families(monkeypatch):
    results_by_name = {
        "scanned": JobResult(name="scanned", status="updated", verified=True),
        "plates": JobResult(name="plates", status="updated", verified=True),
        "nav": JobResult(name="nav", status="updated", verified=True),
    }
    calls = _stub_jobs_and_driver(monkeypatch, results_by_name)

    out = du.update_drive("/Volumes/EFIS", families=["plates"])

    # Only the requested family was run and aggregated.
    assert [name for name, _ in calls] == ["plates"]
    assert set(out["jobs"]) == {"plates"}


def test_default_runs_all_families(monkeypatch):
    results_by_name = {
        "scanned": JobResult(name="scanned", status="updated", verified=True),
        "plates": JobResult(name="plates", status="updated", verified=True),
        "nav": JobResult(name="nav", status="updated", verified=True),
    }
    calls = _stub_jobs_and_driver(monkeypatch, results_by_name)

    du.update_drive("/Volumes/EFIS")

    assert {name for name, _ in calls} == {"scanned", "plates", "nav"}


# --- is_aborted injection (task 11: app-owned watchdog) ----------------------


def test_default_uses_per_job_watchdog(monkeypatch):
    """With no is_aborted, update_drive uses run_sync_job_watched per job and
    does NOT call the bare run_sync_job driver (backward-compatible path)."""
    results_by_name = {
        "scanned": JobResult(name="scanned", status="updated", verified=True),
    }
    _stub_jobs_and_driver(monkeypatch, results_by_name)

    bare_calls = []

    def fake_run_sync_job(job, mount_point, sync_state=None,
                          progress_callback=None, is_aborted=None):
        bare_calls.append(job.name)
        return results_by_name[job.name]

    monkeypatch.setattr(du, "run_sync_job", fake_run_sync_job)

    du.update_drive("/Volumes/EFIS")

    # Bare driver not used when no external predicate is supplied.
    assert bare_calls == []


def test_is_aborted_injected_into_run_sync_job(monkeypatch):
    """When the app supplies an is_aborted predicate (its owned watchdog),
    update_drive injects it into run_sync_job directly and skips the per-job
    watchdog wrapper (task 11)."""
    results_by_name = {
        "scanned": JobResult(name="scanned", status="updated", verified=True),
        "plates": JobResult(name="plates", status="updated", verified=True),
    }

    def fake_build_jobs(mount_point, families=None):
        names = list(results_by_name) if families is None else list(families)
        return [du.SyncJob(name=n, kind="tree") for n in names]

    monkeypatch.setattr(du, "build_jobs", fake_build_jobs)

    watched_calls = []

    def fake_watched(job, mount_point, sync_state=None, progress_callback=None,
                     poll_interval=du.WATCHDOG_POLL_INTERVAL, ismount=None):
        watched_calls.append(job.name)
        return results_by_name[job.name]

    monkeypatch.setattr(du, "run_sync_job_watched", fake_watched)

    bare_calls = []
    sentinel = object()

    def fake_run_sync_job(job, mount_point, sync_state=None,
                          progress_callback=None, is_aborted=None):
        # The exact predicate object we passed must be threaded through.
        assert is_aborted is sentinel
        bare_calls.append(job.name)
        return results_by_name[job.name]

    monkeypatch.setattr(du, "run_sync_job", fake_run_sync_job)

    # update_drive only checks `is_aborted is not None` and threads the object
    # through to run_sync_job; it never calls it here, so identity is enough.
    out = du.update_drive("/Volumes/EFIS", is_aborted=sentinel)

    # Every job went through the bare driver with the injected predicate;
    # the per-job watchdog wrapper was bypassed.
    assert set(bare_calls) == {"scanned", "plates"}
    assert watched_calls == []
    assert set(out["jobs"]) == {"scanned", "plates"}


# --- prepare_drive derives success/message from the aggregation --------------


def _prepare_to_populate(monkeypatch, tmp_path):
    """Stub prepare_drive's format/remount steps so it reaches update_drive.

    Returns the mount dir used as the freshly-"formatted" drive.
    """
    import plistlib
    import subprocess

    mount = tmp_path / "EFIS"
    mount.mkdir()

    class _FakeProc:
        returncode = 0
        stdout = plistlib.dumps({"DeviceIdentifier": "disk9"})
        stderr = ""

    def fake_run(cmd, *a, **k):
        return _FakeProc()

    monkeypatch.setattr(subprocess, "run", fake_run)

    # The format step now runs eraseDisk via Popen (with an elapsed-time
    # heartbeat). Stub Popen with an immediately-finished fake process so the
    # heartbeat loop exits at once.
    class _FakePopen:
        def __init__(self, *a, **k):
            self.returncode = 0
        def poll(self):
            return 0
        def wait(self, timeout=None):
            return 0
        def communicate(self):
            return ("", "")
        def terminate(self):
            pass
        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    # prepare_drive polls for "/Volumes/EFIS"; redirect isdir to our temp mount.
    real_isdir = du.os.path.isdir

    def fake_isdir(p):
        if p == "/Volumes/EFIS":
            return True
        return real_isdir(p)

    monkeypatch.setattr(du.os.path, "isdir", fake_isdir)

    # Make GRTCHARTS creation and the eventual mount target land in our temp dir
    # by pointing "/Volumes/EFIS" operations at `mount` via makedirs passthrough.
    real_makedirs = du.os.makedirs

    def fake_makedirs(path, *a, **k):
        if path == du.os.path.join("/Volumes/EFIS", "GRTCHARTS"):
            return real_makedirs(str(mount / "GRTCHARTS"), *a, **k)
        return real_makedirs(path, *a, **k)

    monkeypatch.setattr(du.os, "makedirs", fake_makedirs)
    return mount


def test_prepare_drive_success_reports_per_family(monkeypatch, tmp_path):
    _prepare_to_populate(monkeypatch, tmp_path)

    def fake_update_drive(mount_point, families=None, progress_callback=None):
        return {
            "jobs": {
                "scanned": JobResult(name="scanned", status="updated", verified=True),
                "plates": JobResult(name="plates", status="updated", verified=True),
                "nav": JobResult(name="nav", status="updated", verified=True),
            },
            "errors": [],
            "aborted": False,
        }

    monkeypatch.setattr(du, "update_drive", fake_update_drive)

    out = du.prepare_drive("/Volumes/UNTITLED")

    assert out["success"] is True
    # Per-family results surfaced in the message (Req 9.4).
    assert "scanned: updated" in out["message"]
    assert "plates: updated" in out["message"]
    assert "nav: updated" in out["message"]


def test_prepare_drive_failure_reports_failed_family(monkeypatch, tmp_path):
    _prepare_to_populate(monkeypatch, tmp_path)

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

    out = du.prepare_drive("/Volumes/UNTITLED")

    assert out["success"] is False
    assert "plates" in out["message"]


# --- prepare_drive must erase the WHOLE disk, not a partition slice ----------
# Regression: a mounted volume's DeviceIdentifier is typically a slice
# (e.g. "disk4s1"), but `diskutil eraseDisk` requires the whole-disk id
# (e.g. "disk4"). prepare_drive must resolve ParentWholeDisk and pass THAT to
# eraseDisk, otherwise the format silently no-ops against the slice.


def test_prepare_drive_erases_whole_disk_not_slice(monkeypatch, tmp_path):
    import plistlib
    import subprocess

    mount = tmp_path / "EFIS"
    mount.mkdir()

    captured = {"erase_cmd": None}

    def fake_run(cmd, *a, **k):
        class _P:
            returncode = 0
            stderr = ""
            stdout = b""

        p = _P()
        if cmd[:2] == ["diskutil", "info"]:
            # Volume is mounted on a SLICE; parent whole-disk is disk4.
            p.stdout = plistlib.dumps({
                "DeviceIdentifier": "disk4s1",
                "ParentWholeDisk": "disk4",
            })
        return p

    monkeypatch.setattr(subprocess, "run", fake_run)

    # eraseDisk now runs via Popen (heartbeat loop); capture its argv here.
    class _FakePopen:
        def __init__(self, cmd, *a, **k):
            if cmd[:2] == ["diskutil", "eraseDisk"]:
                captured["erase_cmd"] = list(cmd)
            self.returncode = 0
        def poll(self):
            return 0
        def wait(self, timeout=None):
            return 0
        def communicate(self):
            return ("", "")
        def terminate(self):
            pass
        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    real_isdir = du.os.path.isdir
    monkeypatch.setattr(
        du.os.path, "isdir",
        lambda p: True if p == "/Volumes/EFIS" else real_isdir(p),
    )
    real_makedirs = du.os.makedirs

    def fake_makedirs(path, *a, **k):
        if path == du.os.path.join("/Volumes/EFIS", "GRTCHARTS"):
            return real_makedirs(str(mount / "GRTCHARTS"), *a, **k)
        return real_makedirs(path, *a, **k)

    monkeypatch.setattr(du.os, "makedirs", fake_makedirs)
    monkeypatch.setattr(
        du, "update_drive",
        lambda *a, **k: {
            "jobs": {"scanned": JobResult(name="scanned", status="updated", verified=True)},
            "errors": [],
            "aborted": False,
        },
    )

    # Default label "EFIS" so the remount wait resolves against the stubbed
    # "/Volumes/EFIS". The whole-disk assertion is independent of the label.
    out = du.prepare_drive("/Volumes/EFIS_1")

    assert out["success"] is True
    assert captured["erase_cmd"] is not None, "eraseDisk was never invoked"
    # The device argument must be the WHOLE disk, never the slice.
    assert "/dev/disk4" in captured["erase_cmd"]
    assert "/dev/disk4s1" not in captured["erase_cmd"]


def test_prepare_drive_wholedisk_falls_back_to_device_id(monkeypatch, tmp_path):
    # When ParentWholeDisk is absent (volume already IS a whole disk), fall
    # back to DeviceIdentifier so eraseDisk still gets a usable target.
    import plistlib
    import subprocess

    mount = tmp_path / "EFIS"
    mount.mkdir()
    captured = {"erase_cmd": None}

    def fake_run(cmd, *a, **k):
        class _P:
            returncode = 0
            stderr = ""
            stdout = b""

        p = _P()
        if cmd[:2] == ["diskutil", "info"]:
            p.stdout = plistlib.dumps({"DeviceIdentifier": "disk9"})
        return p

    monkeypatch.setattr(subprocess, "run", fake_run)

    class _FakePopen:
        def __init__(self, cmd, *a, **k):
            if cmd[:2] == ["diskutil", "eraseDisk"]:
                captured["erase_cmd"] = list(cmd)
            self.returncode = 0
        def poll(self):
            return 0
        def wait(self, timeout=None):
            return 0
        def communicate(self):
            return ("", "")
        def terminate(self):
            pass
        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    real_isdir = du.os.path.isdir
    monkeypatch.setattr(
        du.os.path, "isdir",
        lambda p: True if p == "/Volumes/EFIS" else real_isdir(p),
    )
    real_makedirs = du.os.makedirs

    def fake_makedirs(path, *a, **k):
        if path == du.os.path.join("/Volumes/EFIS", "GRTCHARTS"):
            return real_makedirs(str(mount / "GRTCHARTS"), *a, **k)
        return real_makedirs(path, *a, **k)

    monkeypatch.setattr(du.os, "makedirs", fake_makedirs)
    monkeypatch.setattr(
        du, "update_drive",
        lambda *a, **k: {
            "jobs": {"scanned": JobResult(name="scanned", status="updated", verified=True)},
            "errors": [],
            "aborted": False,
        },
    )

    out = du.prepare_drive("/Volumes/UNTITLED")
    assert out["success"] is True
    assert "/dev/disk9" in captured["erase_cmd"]
