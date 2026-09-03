# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for identity provenance updates around the commit-marker step.

Covers Task 16 of the drive-sync-integrity spec:
  - a clean, verified run against a drive WITH an identity file records
    last_sync_completed, last_sync_result == "clean", bumps sync_count, adds the
    family to last_sync_families, and (best-effort) sets data_cycle
  - a forced verify mismatch / abort records last_sync_result "failed"/"aborted"
    WITHOUT bumping sync_count and WITHOUT a completion timestamp
  - provenance no-ops safely when the drive has no identity file (the job still
    works via the mount-path fallback key; no identity file is created)
  - _current_data_cycle is tolerant of missing metadata (returns None, no raise)

No physical USB is required: rsync runs against real temp dirs; the mount
presence/writability preflight is mocked. app.py is NOT imported (rumps).

Requirements: 10.7, 10.8, 10.10
"""

import logging
import os
import shutil

import pytest

from efis_data_manager import drive_updater as du


pytestmark = pytest.mark.skipif(
    shutil.which("rsync") is None, reason="rsync not available on PATH"
)


def _write(path, content=b"x", mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture(autouse=True)
def _mount_ok(monkeypatch):
    """Make preflight pass for temp dirs: pretend they are writable mounts."""
    monkeypatch.setattr(du.os.path, "ismount", lambda p: True)


@pytest.fixture
def _no_data_cycle(monkeypatch):
    """Neutralise data_cycle so provenance assertions are deterministic.

    Applied by the ``env`` fixture (the sync-job tests). The standalone
    ``_current_data_cycle`` tests do NOT use it so they exercise the real
    function. Individual sync tests that assert data_cycle override this.
    """
    monkeypatch.setattr(du, "_current_data_cycle", lambda: None)


@pytest.fixture(autouse=True)
def _fast_mount_ready(monkeypatch):
    """resolve_drive_id waits for mount readiness; short-circuit for temp dirs."""
    monkeypatch.setattr(du, "wait_for_mount_ready", lambda *a, **k: True)


@pytest.fixture
def state_paths(tmp_path, monkeypatch):
    """Isolate the durable sync-state file under a temp dir."""
    state_dir = tmp_path / "DataManagerLogs"
    state_dir.mkdir()
    monkeypatch.setattr(du, "SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setattr(du, "SYNC_STATE_PATH", str(state_dir / ".sync_state.json"))
    monkeypatch.setattr(
        du, "LEGACY_SYNC_MARKER_PATH", str(state_dir / ".sync_in_progress")
    )
    return state_dir


@pytest.fixture
def env(tmp_path, monkeypatch, state_paths, _no_data_cycle):
    """A local image with all three families and an empty drive to sync into."""
    local = tmp_path / "image"
    drive = tmp_path / "drive"
    base_mtime = 1_000_000

    _write(str(local / "ChartData" / "LO" / "a.png"), b"aaaa")
    _write(str(local / "ChartData" / "SEC" / "b.png"), b"bbbb")
    _write(
        str(local / "ChartData" / "ScannedCharts.sqlite"),
        b"scanned-db",
        mtime=base_mtime,
    )

    _write(str(local / "ChartData" / "Plates" / "p1.pdf"), b"plate")
    _write(
        str(local / "ChartData" / "Plates" / "Plates.sqlite"),
        b"plates-db",
        mtime=base_mtime,
    )

    _write(str(local / "NAV.DB"), b"nav-content")
    _write(str(local / "NAV-proc.DB"), b"navproc-content")

    drive.mkdir()

    monkeypatch.setattr(du, "load_config", lambda: {"usb_image_path": str(local)})

    return {"local": local, "drive": drive, "mount": str(drive)}


def _job(env, name):
    return next(j for j in du.build_jobs(env["mount"]) if j.name == name)


def _adopt(env, monkeypatch):
    """Write an identity file to the drive so provenance has something to update.

    Returns the drive id. Uses resolve_drive_id's adoption path with
    is_efis_drive forced True so an EFIS_DRIVE_ID.json is created at the root.
    """
    monkeypatch.setattr(
        "efis_data_manager.usb_monitor.is_efis_drive", lambda mp: True
    )
    monkeypatch.setattr(du, "_volume_uuid", lambda mp: None)
    monkeypatch.setattr(du, "_volume_name", lambda mp: None)
    drive_id = du.resolve_drive_id(env["mount"])
    assert drive_id is not None
    assert du.read_identity(env["mount"]) is not None
    return drive_id


# --- clean run records provenance -------------------------------------------


def test_clean_run_records_completed_clean_and_bumps_count(env, monkeypatch):
    _adopt(env, monkeypatch)
    before = du.read_identity(env["mount"])
    assert before["sync_count"] == 0
    assert before["last_sync_completed"] is None

    result = du.run_sync_job(_job(env, "scanned"), env["mount"])
    assert result.status == "updated"
    assert result.verified is True

    ident = du.read_identity(env["mount"])
    assert ident["last_sync_result"] == "clean"
    assert ident["last_sync_completed"] is not None
    assert ident["last_sync_started"] is not None
    assert ident["sync_count"] == 1
    assert "scanned" in ident["last_sync_families"]


def test_clean_run_accumulates_families_and_count(env, monkeypatch):
    _adopt(env, monkeypatch)

    du.run_sync_job(_job(env, "scanned"), env["mount"])
    du.run_sync_job(_job(env, "plates"), env["mount"])

    ident = du.read_identity(env["mount"])
    # sync_count bumped once per successful family.
    assert ident["sync_count"] == 2
    # last_sync_families accumulates both.
    assert set(ident["last_sync_families"]) == {"scanned", "plates"}
    assert ident["last_sync_result"] == "clean"


def test_clean_run_sets_data_cycle_from_nav_valid_date(env, monkeypatch):
    _adopt(env, monkeypatch)
    monkeypatch.setattr(du, "_current_data_cycle", lambda: "2025-01-23")

    du.run_sync_job(_job(env, "nav"), env["mount"])

    ident = du.read_identity(env["mount"])
    assert ident["data_cycle"] == "2025-01-23"
    assert ident["last_sync_result"] == "clean"
    assert ident["sync_count"] == 1


# --- failure / abort record result without bumping count/completed ----------


def test_verify_mismatch_records_failed_no_count_no_completed(env, monkeypatch):
    _adopt(env, monkeypatch)
    monkeypatch.setattr(
        du,
        "verify_family",
        lambda job, mount, deep=False: {
            "missing": ["LO/a.png"],
            "extra": [],
            "size_mismatch": [],
        },
    )

    result = du.run_sync_job(_job(env, "scanned"), env["mount"])
    assert result.status == "failed"

    ident = du.read_identity(env["mount"])
    assert ident["last_sync_result"] == "failed"
    assert ident["sync_count"] == 0
    assert ident["last_sync_completed"] is None
    # started was still stamped at job-begin.
    assert ident["last_sync_started"] is not None
    assert ident["last_sync_families"] == []


def test_abort_records_aborted_no_count_no_completed(env, monkeypatch):
    _adopt(env, monkeypatch)

    result = du.run_sync_job(
        _job(env, "scanned"), env["mount"], is_aborted=lambda: True
    )
    assert result.status == "aborted"

    ident = du.read_identity(env["mount"])
    assert ident["last_sync_result"] == "aborted"
    assert ident["sync_count"] == 0
    assert ident["last_sync_completed"] is None
    assert ident["last_sync_started"] is not None


# --- no identity file: provenance no-ops safely, job still works ------------


def test_no_identity_file_provenance_noops_job_still_syncs(env, monkeypatch):
    # Plain temp dir: not a recognized EFIS drive, so resolve_drive_id returns
    # None and run_sync_job keys sync-state by the mount path fallback.
    monkeypatch.setattr(
        "efis_data_manager.usb_monitor.is_efis_drive", lambda mp: False
    )
    monkeypatch.setattr(du, "_volume_uuid", lambda mp: None)
    monkeypatch.setattr(du, "_volume_name", lambda mp: None)

    assert du.read_identity(env["mount"]) is None

    result = du.run_sync_job(_job(env, "scanned"), env["mount"])
    assert result.status == "updated"
    assert result.verified is True
    # Payload + marker synced via the fallback key.
    assert (env["drive"] / "ChartData" / "ScannedCharts.sqlite").exists()

    # Provenance created NO identity file.
    assert du.read_identity(env["mount"]) is None
    assert not os.path.exists(
        os.path.join(env["mount"], du.IDENTITY_FILENAME)
    )


# --- _current_data_cycle tolerance ------------------------------------------


def test_current_data_cycle_returns_none_when_metadata_missing(monkeypatch):
    # currency._load_grt_metadata returns an empty dict when no metadata file.
    from efis_data_manager import currency

    monkeypatch.setattr(currency, "_load_grt_metadata", lambda: {})
    assert du._current_data_cycle() is None


def test_current_data_cycle_returns_value_when_present(monkeypatch):
    from efis_data_manager import currency

    monkeypatch.setattr(
        currency, "_load_grt_metadata", lambda: {"nav_db_valid_date": "2025-02-20"}
    )
    assert du._current_data_cycle() == "2025-02-20"


def test_current_data_cycle_tolerant_of_raising_metadata(monkeypatch):
    from efis_data_manager import currency

    def _boom():
        raise RuntimeError("metadata unreadable")

    monkeypatch.setattr(currency, "_load_grt_metadata", _boom)
    # Must not raise into the caller.
    assert du._current_data_cycle() is None


def test_safe_update_provenance_never_raises(monkeypatch, tmp_path, caplog):
    mount = str(tmp_path)
    du.write_identity(mount, du._new_identity(mount))

    def _boom(*a, **k):
        raise RuntimeError("write failed")

    monkeypatch.setattr(du, "update_identity_provenance", _boom)
    with caplog.at_level(logging.WARNING, logger=du.logger.name):
        du._safe_update_provenance(mount, sync_count=99)
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records)
