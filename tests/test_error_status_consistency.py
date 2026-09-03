# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property 5 (error/status agreement) for the drive-sync-integrity spec.

Design.md Correctness Property 5 and Testing strategy item 7:

    Every error surfaced in a JobResult's errors[] has a matching log record
    at level >= WARNING.

The other test modules assert that *some* WARNING/ERROR record exists on a
given failure path. This module asserts the stronger invariant the design
actually states: for EACH entry in errors[], there is a log record whose
message matches it at level >= WARNING. It drives several distinct failure
paths (preflight failure, verification mismatch, abort, rsync failure) and
checks the correspondence uniformly.

Validates: Requirements 8.1, 8.3 (design Property 5)

rsync runs against real temp dirs; the mount presence/writability preflight is
mocked where a job must otherwise reach payload sync. No physical USB is needed.
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


@pytest.fixture
def state_paths(tmp_path, monkeypatch):
    """Isolate the durable sync-state file under a temp dir."""
    state_dir = tmp_path / "DataManagerLogs"
    state_dir.mkdir()
    monkeypatch.setattr(du, "SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setattr(du, "SYNC_STATE_PATH", str(state_dir / ".sync_state.json"))
    monkeypatch.setattr(du, "LEGACY_SYNC_MARKER_PATH", str(state_dir / ".sync_in_progress"))
    return state_dir


@pytest.fixture
def env(tmp_path, monkeypatch, state_paths):
    """A local image with all three families and an empty drive to sync into."""
    local = tmp_path / "image"
    drive = tmp_path / "drive"

    _write(str(local / "ChartData" / "LO" / "a.png"), b"aaaa")
    _write(str(local / "ChartData" / "SEC" / "b.png"), b"bbbb")
    _write(str(local / "ChartData" / "ScannedCharts.sqlite"), b"scanned-db", mtime=1_000_000)
    _write(str(local / "ChartData" / "Plates" / "p1.pdf"), b"plate")
    _write(str(local / "ChartData" / "Plates" / "Plates.sqlite"), b"plates-db", mtime=1_000_000)
    _write(str(local / "NAV.DB"), b"nav-content")

    drive.mkdir()
    monkeypatch.setattr(du, "load_config", lambda: {"usb_image_path": str(local)})
    return {"local": local, "drive": drive, "mount": str(drive)}


def _job(env, name):
    return next(j for j in du.build_jobs(env["mount"]) if j.name == name)


def _assert_every_error_logged(result, caplog):
    """Property 5: each errors[] entry has a matching log record >= WARNING.

    Match by message: a record at >= WARNING whose rendered message shares a
    substantial fragment with the error string. We use the family-qualified
    prefix of the error (up to the first colon/paren) so wording differences
    between the surfaced error and the log call (e.g. the rsync log splits the
    detail into args) don't cause false negatives.
    """
    warn_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert result.errors, "expected at least one surfaced error for this path"
    for err in result.errors:
        # A stable anchor present in both the surfaced error and the log line.
        anchor = err.split("(")[0].split(":")[0].strip()
        assert anchor, f"could not derive an anchor from error: {err!r}"
        matched = any(anchor in r.getMessage() for r in warn_records)
        assert matched, (
            f"error {err!r} (anchor {anchor!r}) has no matching "
            f">= WARNING log record; records="
            f"{[(r.levelname, r.getMessage()) for r in warn_records]}"
        )


def test_preflight_failure_errors_are_all_logged(env, monkeypatch, caplog):
    """Preflight failure: the surfaced error is logged at >= WARNING."""
    scanned = _job(env, "scanned")
    monkeypatch.setattr(du.os.path, "ismount", lambda p: False)

    with caplog.at_level(logging.WARNING, logger=du.logger.name):
        result = du.run_sync_job(scanned, env["mount"])

    assert result.status == "failed"
    _assert_every_error_logged(result, caplog)


def test_verification_mismatch_errors_are_all_logged(env, monkeypatch, caplog):
    """Verify mismatch: the surfaced error is logged at >= WARNING."""
    monkeypatch.setattr(du.os.path, "ismount", lambda p: True)
    scanned = _job(env, "scanned")

    # Force a verification discrepancy so the marker is never written.
    monkeypatch.setattr(
        du,
        "verify_family",
        lambda job, mount, deep=False: {
            "missing": ["ChartData/LO/a.png"],
            "extra": [],
            "size_mismatch": [],
        },
    )

    with caplog.at_level(logging.WARNING, logger=du.logger.name):
        result = du.run_sync_job(scanned, env["mount"])

    assert result.status == "failed"
    _assert_every_error_logged(result, caplog)


def test_abort_errors_are_all_logged(env, monkeypatch, caplog):
    """Abort: the surfaced abort error is logged at >= WARNING."""
    monkeypatch.setattr(du.os.path, "ismount", lambda p: True)
    scanned = _job(env, "scanned")

    with caplog.at_level(logging.WARNING, logger=du.logger.name):
        result = du.run_sync_job(scanned, env["mount"], is_aborted=lambda: True)

    assert result.status == "aborted"
    _assert_every_error_logged(result, caplog)


def test_rsync_failure_errors_are_all_logged(tmp_path, caplog, monkeypatch):
    """sync_payload rsync failure: each surfaced error is logged at >= WARNING."""
    src = tmp_path / "missing"  # nonexistent source -> rsync exits non-zero
    dst = tmp_path / "drive"
    dst.mkdir()

    job = du.SyncJob(
        name="scanned",
        kind="tree",
        payload_root_local=str(src),
        payload_root_drive=str(dst),
        excludes=[],
        marker_src=None,
        marker_dst=None,
    )

    with caplog.at_level(logging.WARNING, logger=du.logger.name):
        files_updated, errors = du.sync_payload(job)

    assert files_updated == 0
    assert errors

    warn_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    for err in errors:
        anchor = err.split("(")[0].split(":")[0].strip()
        assert any(anchor in r.getMessage() for r in warn_records), (
            f"rsync error {err!r} has no matching >= WARNING log record"
        )
