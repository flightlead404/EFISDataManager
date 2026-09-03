# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the mount-presence watchdog and abort path (Task 7).

Covers design.md "Mount-presence watchdog" and Testing strategy item 6
(interruption): while a job runs a background thread polls
``os.path.ismount(mount_point)``; when the mount vanishes the watchdog latches
an abort, the is_aborted() predicate it exposes goes True, and run_sync_job
aborts — leaving the commit marker unwritten, the interrupted-sync state in
place, and an ERROR logged.

Requirements: 7.2, 7.3, 8.1

rsync runs against real temp dirs; mount presence is driven by an injected
ismount function so the disappearance is deterministic (no physical USB).
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
    """A local image with the scanned family and an empty drive to sync into."""
    local = tmp_path / "image"
    drive = tmp_path / "drive"
    base_mtime = 1_000_000

    _write(str(local / "ChartData" / "LO" / "a.png"), b"aaaa")
    _write(str(local / "ChartData" / "SEC" / "b.png"), b"bbbb")
    _write(str(local / "ChartData" / "ScannedCharts.sqlite"), b"scanned-db", mtime=base_mtime)

    drive.mkdir()

    monkeypatch.setattr(du, "load_config", lambda: {"usb_image_path": str(local)})

    return {"local": local, "drive": drive, "mount": str(drive)}


def _job(env, name):
    return next(j for j in du.build_jobs(env["mount"]) if j.name == name)


# --- watchdog unit behaviour ------------------------------------------------


def test_watchdog_latches_when_mount_disappears():
    """The poll thread latches an abort the moment ismount flips to False."""
    present = {"v": True}
    wd = du.MountWatchdog(
        "/Volumes/EFIS",
        poll_interval=0.01,
        ismount=lambda p: present["v"],
        job_name="scanned",
    )
    wd.start()
    try:
        assert wd.is_aborted() is False
        present["v"] = False
        assert wd.wait_for_abort(timeout=2.0) is True
        assert wd.is_aborted() is True
        assert wd.aborted is True
        assert wd.abort_reason == "mount removed"
    finally:
        wd.stop()


def test_watchdog_at_risk_flag_aborts_without_mount_loss():
    """Sleep (at-risk) aborts even while the mount is still present (Req 7.4)."""
    wd = du.MountWatchdog(
        "/Volumes/EFIS",
        poll_interval=0.01,
        ismount=lambda p: True,  # mount never disappears
        job_name="scanned",
    )
    wd.start()
    try:
        assert wd.is_aborted() is False
        wd.mark_at_risk("system sleeping")
        assert wd.wait_for_abort(timeout=2.0) is True
        assert wd.is_aborted() is True
        assert wd.abort_reason == "system sleeping"
    finally:
        wd.stop()


# --- abort path through run_sync_job ----------------------------------------


def test_mount_removed_midjob_aborts_and_leaves_state(env, monkeypatch, caplog):
    """ismount flips False mid-job: abort, marker unwritten, state kept, ERROR."""
    # Preflight runs ismount once at the start; keep it True there, then let the
    # watchdog's injected ismount flip to False so the abort happens mid-job.
    monkeypatch.setattr(du.os.path, "ismount", lambda p: True)

    present = {"v": True}

    def fake_ismount(_path):
        # First call (preflight happens before start via du.os.path.ismount,
        # which is patched True above); this drives ONLY the watchdog thread.
        return present["v"]

    scanned = _job(env, "scanned")

    wd = du.MountWatchdog(
        env["mount"], poll_interval=0.01, ismount=fake_ismount, job_name="scanned"
    )
    wd.start()
    try:
        # Flip the mount away almost immediately so the running rsync is aborted.
        present["v"] = False
        wd.wait_for_abort(timeout=2.0)

        with caplog.at_level(logging.WARNING, logger=du.logger.name):
            result = du.run_sync_job(scanned, env["mount"], is_aborted=wd.is_aborted)
    finally:
        wd.stop()

    # Aborted result.
    assert result.status == "aborted"
    assert result.verified is False
    assert result.errors

    # Commit marker must be absent.
    assert not (env["drive"] / "ChartData" / "ScannedCharts.sqlite").exists()

    # Interrupted-sync state retained for the family.
    assert "scanned" in du.pending_families(env["mount"])

    # Quick check reports the family stale (interrupted forces not-current).
    currency = du.check_drive_currency(env["mount"])
    assert currency["families"]["scanned"]["current"] is False

    # Req 8.1: an ERROR was logged (watchdog logs the removal; the job logs the
    # abort). At least one ERROR-level record must be present.
    assert any(rec.levelno >= logging.ERROR for rec in caplog.records)


def test_run_sync_job_watched_aborts_on_missing_mount(env, monkeypatch, caplog):
    """The convenience wrapper wires the watchdog and aborts when unmounted."""
    # Preflight must pass (temp dir is not a real mount): patch module ismount.
    monkeypatch.setattr(du.os.path, "ismount", lambda p: True)

    scanned = _job(env, "scanned")

    with caplog.at_level(logging.ERROR, logger=du.logger.name):
        # The watchdog's injected ismount reports the drive gone from the start.
        result = du.run_sync_job_watched(
            scanned,
            env["mount"],
            poll_interval=0.01,
            ismount=lambda p: False,
        )

    assert result.status == "aborted"
    assert not (env["drive"] / "ChartData" / "ScannedCharts.sqlite").exists()
    assert "scanned" in du.pending_families(env["mount"])
    assert any(rec.levelno >= logging.ERROR for rec in caplog.records)


def test_watchdog_stays_current_when_mount_present(env, monkeypatch):
    """A job with a healthy (present) mount completes normally, no abort."""
    monkeypatch.setattr(du.os.path, "ismount", lambda p: True)

    scanned = _job(env, "scanned")
    result = du.run_sync_job_watched(
        scanned, env["mount"], poll_interval=0.01, ismount=lambda p: True
    )

    assert result.status == "updated"
    assert result.verified is True
    assert (env["drive"] / "ChartData" / "ScannedCharts.sqlite").exists()
    assert du.pending_families(env["mount"]) == []


# --- mount-readiness race hardening (bug fixes) -----------------------------
#
# Two bugs surfaced by a real re-mount:
#   1. the watchdog latched on the FIRST ismount()==False, so a volume still
#      settling (a one-poll blip) triggered a false "mount removed" abort;
#   2. a single latched watchdog was reused across verify+repair AND the
#      update, so a blip during verify poisoned the later update.
# The watchdog now debounces removal over settle_polls consecutive absent
# polls, and preflight waits for the mount to become ready.


def test_watchdog_ignores_single_poll_blip():
    """A one-poll absence (settling volume) must NOT latch an abort."""
    # present sequence: True, False (blip), True, True, ...
    seq = iter([True, False, True, True, True, True, True, True])
    import threading

    lock = threading.Lock()

    def ismount(_p):
        with lock:
            try:
                return next(seq)
            except StopIteration:
                return True

    wd = du.MountWatchdog(
        "/Volumes/EFIS",
        poll_interval=0.01,
        ismount=ismount,
        job_name="scanned",
        settle_polls=2,  # require 2 CONSECUTIVE absent polls
    )
    wd.start()
    try:
        # Give the poll loop time to pass the single False and several Trues.
        assert wd.wait_for_abort(timeout=0.3) is False
        assert wd.is_aborted() is False
    finally:
        wd.stop()


def test_watchdog_latches_on_persistent_absence():
    """A persistent (>= settle_polls) absence latches 'mount removed'."""
    present = {"v": True}
    wd = du.MountWatchdog(
        "/Volumes/EFIS",
        poll_interval=0.01,
        ismount=lambda p: present["v"],
        job_name="scanned",
        settle_polls=3,
    )
    wd.start()
    try:
        assert wd.is_aborted() is False
        present["v"] = False  # stays gone -> must latch after ~3 polls
        assert wd.wait_for_abort(timeout=2.0) is True
        assert wd.abort_reason == "mount removed"
    finally:
        wd.stop()


def test_wait_for_mount_ready_true_when_ready(tmp_path, monkeypatch):
    """Returns True immediately when the mount is a writable, listable mount."""
    monkeypatch.setattr(du.os.path, "ismount", lambda p: True)
    ok = du.wait_for_mount_ready(str(tmp_path), timeout=1.0, poll=0.01)
    assert ok is True


def test_wait_for_mount_ready_false_on_timeout(tmp_path, monkeypatch):
    """Returns False when the volume never becomes a mount within the budget."""
    monkeypatch.setattr(du.os.path, "ismount", lambda p: False)
    slept = []
    ok = du.wait_for_mount_ready(
        str(tmp_path), timeout=0.05, poll=0.01, sleep=lambda s: slept.append(s)
    )
    assert ok is False
    assert slept  # it polled at least once before giving up


def test_wait_for_mount_ready_tolerates_delayed_ready(tmp_path, monkeypatch):
    """A volume that becomes ready after a couple polls is accepted."""
    calls = {"n": 0}

    def ismount(_p):
        calls["n"] += 1
        return calls["n"] >= 3  # not a mount for the first two checks

    monkeypatch.setattr(du.os.path, "ismount", ismount)
    ok = du.wait_for_mount_ready(
        str(tmp_path), timeout=1.0, poll=0.001, sleep=lambda s: None
    )
    assert ok is True
    assert calls["n"] >= 3
