# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for run_sync_job (commit-marker write + per-job driver).

Covers Task 6 of the drive-sync-integrity spec and design.md Testing strategy
items 1 (commit-marker atomicity) and 2 (per-family independence):

  - a tree job syncs payload, verifies, then writes the commit marker last and
    clears the interrupted state (Property 1)
  - an abort BEFORE the marker leaves the drive marker absent, leaves the
    interrupted state in place, and the quick check reports the family stale
    (Property 1 / Property 2)
  - a payload/verify failure in one family (plates) does NOT touch another
    family's already-written marker; the healthy family stays current
    (Property 4)
  - the nav (files) job copies + verifies by checksum with no separate marker
  - errors are surfaced on the JobResult and logged at >= WARNING

Requirements: 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4, 5.3

rsync runs against real temp dirs; the mount presence/writability preflight is
mocked (temp dirs are not real mount points). No physical USB is needed.
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
    base_mtime = 1_000_000

    # scanned family payload + marker
    _write(str(local / "ChartData" / "LO" / "a.png"), b"aaaa")
    _write(str(local / "ChartData" / "SEC" / "b.png"), b"bbbb")
    _write(str(local / "ChartData" / "ScannedCharts.sqlite"), b"scanned-db", mtime=base_mtime)

    # plates family payload + marker
    _write(str(local / "ChartData" / "Plates" / "p1.pdf"), b"plate")
    _write(str(local / "ChartData" / "Plates" / "Plates.sqlite"), b"plates-db", mtime=base_mtime)

    # nav family files
    _write(str(local / "NAV.DB"), b"nav-content")
    _write(str(local / "NAV-proc.DB"), b"navproc-content")

    drive.mkdir()

    monkeypatch.setattr(du, "load_config", lambda: {"usb_image_path": str(local)})

    return {
        "local": local,
        "drive": drive,
        "mount": str(drive),
        "base_mtime": base_mtime,
    }


def _job(env, name):
    return next(j for j in du.build_jobs(env["mount"]) if j.name == name)


# --- happy path: tree job writes marker LAST + clears interrupted state -----


def test_tree_job_success_writes_marker_and_clears_state(env):
    scanned = _job(env, "scanned")
    result = du.run_sync_job(scanned, env["mount"])

    assert result.status == "updated"
    assert result.verified is True
    assert result.errors == []

    # Payload copied.
    assert (env["drive"] / "ChartData" / "LO" / "a.png").exists()
    assert (env["drive"] / "ChartData" / "SEC" / "b.png").exists()
    # Commit marker written last.
    assert (env["drive"] / "ChartData" / "ScannedCharts.sqlite").exists()
    assert (env["drive"] / "ChartData" / "ScannedCharts.sqlite").read_bytes() == b"scanned-db"

    # Interrupted state cleared (family completed).
    assert du.pending_families(env["mount"]) == []
    # And the quick check now reports scanned current.
    currency = du.check_drive_currency(env["mount"])
    assert currency["families"]["scanned"]["current"] is True


def test_second_run_reports_current_no_files(env):
    scanned = _job(env, "scanned")
    du.run_sync_job(scanned, env["mount"])
    # Re-run: nothing changed -> no files updated, status current.
    result = du.run_sync_job(scanned, env["mount"])
    assert result.status == "current"
    assert result.files_updated == 0
    assert result.verified is True


# --- Property 1 / 2: abort BEFORE marker -> marker absent, family stale ------


def test_abort_before_marker_leaves_marker_absent_and_family_stale(env, caplog):
    scanned = _job(env, "scanned")

    # Abort immediately: payload copy is terminated before it completes, so the
    # marker must never be written.
    with caplog.at_level(logging.WARNING, logger=du.logger.name):
        result = du.run_sync_job(scanned, env["mount"], is_aborted=lambda: True)

    assert result.status == "aborted"
    assert result.verified is False
    assert result.errors  # an abort error is recorded
    # The commit marker MUST NOT exist on the drive.
    assert not (env["drive"] / "ChartData" / "ScannedCharts.sqlite").exists()

    # Interrupted state persists (family NOT completed).
    assert "scanned" in du.pending_families(env["mount"])

    # Quick check reports the family stale (interrupted forces not-current).
    currency = du.check_drive_currency(env["mount"])
    assert currency["families"]["scanned"]["current"] is False

    # Req 8.1: the abort was logged at >= WARNING.
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records)


def test_verification_mismatch_does_not_write_marker(env, monkeypatch, caplog):
    scanned = _job(env, "scanned")

    # Force verify_family to report a discrepancy after a clean rsync so we
    # exercise the "verified failed -> no marker" branch deterministically.
    monkeypatch.setattr(
        du,
        "verify_family",
        lambda job, mount, deep=False: {"missing": ["LO/a.png"], "extra": [], "size_mismatch": []},
    )

    with caplog.at_level(logging.WARNING, logger=du.logger.name):
        result = du.run_sync_job(scanned, env["mount"])

    assert result.status == "failed"
    assert result.verified is False
    assert not (env["drive"] / "ChartData" / "ScannedCharts.sqlite").exists()
    assert "scanned" in du.pending_families(env["mount"])
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records)


# --- Property 4: a plates failure leaves the scanned marker valid -----------


def test_plates_failure_leaves_scanned_marker_valid(env, monkeypatch):
    # First, sync scanned successfully so its marker is valid + current.
    scanned = _job(env, "scanned")
    scanned_result = du.run_sync_job(scanned, env["mount"])
    assert scanned_result.status == "updated"
    assert (env["drive"] / "ChartData" / "ScannedCharts.sqlite").exists()

    # Now fail the plates job via a forced verification mismatch.
    plates = _job(env, "plates")
    monkeypatch.setattr(
        du,
        "verify_family",
        lambda job, mount, deep=False: {"missing": ["p1.pdf"], "extra": [], "size_mismatch": []},
    )
    plates_result = du.run_sync_job(plates, env["mount"])

    assert plates_result.status == "failed"
    # Plates marker NOT written.
    assert not (env["drive"] / "ChartData" / "Plates" / "Plates.sqlite").exists()

    # The scanned marker is untouched and scanned is still reported current.
    assert (env["drive"] / "ChartData" / "ScannedCharts.sqlite").read_bytes() == b"scanned-db"
    currency = du.check_drive_currency(env["mount"])
    assert currency["families"]["scanned"]["current"] is True
    # Plates is stale (interrupted + no marker).
    assert currency["families"]["plates"]["current"] is False


# --- nav (files) job: checksum copy + verify, no separate marker ------------


def test_nav_job_copies_and_verifies(env):
    nav = _job(env, "nav")
    result = du.run_sync_job(nav, env["mount"])

    assert result.status == "updated"
    assert result.verified is True
    assert result.errors == []
    assert (env["drive"] / "NAV.DB").read_bytes() == b"nav-content"
    assert (env["drive"] / "NAV-proc.DB").read_bytes() == b"navproc-content"
    assert du.pending_families(env["mount"]) == []

    # Re-run is a no-op (already current by checksum).
    again = du.run_sync_job(nav, env["mount"])
    assert again.status == "current"
    assert again.files_updated == 0


# --- preflight failure -------------------------------------------------------


def test_preflight_failure_when_mount_absent(env, monkeypatch, caplog):
    scanned = _job(env, "scanned")
    monkeypatch.setattr(du.os.path, "ismount", lambda p: False)

    with caplog.at_level(logging.ERROR, logger=du.logger.name):
        result = du.run_sync_job(scanned, env["mount"])

    assert result.status == "failed"
    assert result.errors
    assert not (env["drive"] / "ChartData" / "ScannedCharts.sqlite").exists()
    assert any(rec.levelno >= logging.ERROR for rec in caplog.records)


# --- stall detection: wedged-but-mounted drive ------------------------------
# A drive that stops accepting writes but stays mounted is NOT caught by the
# mount-removal watchdog. The stall watchdog uses TWO liveness signals and
# aborts ONLY when BOTH go flat for STALL_TIMEOUT_SECONDS:
#   (1) rsync stdout-log growth (--info=progress2 streams transfer progress),
#   (2) rsync CPU-time advance (proves liveness during the silent file-list /
#       --delete scan on a large existing tree).
# The scan phase (no stdout growth) previously tripped a stdout-only watchdog
# and falsely aborted a healthy incremental refresh; the CPU signal fixes that
# while preserving detection of a genuine wedge (no output AND no CPU).


class _FakeProc:
    """A fake rsync process driven by the tests.

    ``poll()`` returns None ``alive_polls`` times, then 0 (clean exit) unless
    the watchdog terminated it first. ``pid`` is present so the CPU probe path
    can be exercised/patched.
    """

    def __init__(self, alive_polls=1000, exit_code=0):
        import subprocess as _sp
        self._sp = _sp
        self._terminated = False
        self._alive_polls = alive_polls
        self._exit_code = exit_code
        self.returncode = None
        self.pid = 4242

    def poll(self):
        if self._terminated:
            return self.returncode
        if self._alive_polls > 0:
            self._alive_polls -= 1
            return None
        self.returncode = self._exit_code
        return self.returncode

    def wait(self, timeout=None):
        if self._terminated or self.returncode is not None:
            return self.returncode
        raise self._sp.TimeoutExpired(cmd="rsync", timeout=timeout)

    def terminate(self):
        self._terminated = True
        self.returncode = -15

    def kill(self):
        self._terminated = True
        self.returncode = -9


def test_sync_payload_stall_detected(env, monkeypatch, caplog):
    """GENUINE WEDGE: no stdout growth AND flat CPU -> stalled and aborted."""
    import subprocess

    scanned = _job(env, "scanned")

    def fake_popen(cmd, stdout=None, stderr=None, **k):
        # stdout/stderr are open file handles (temp files); leave them empty so
        # the byte-size liveness proxy never advances.
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    # Flat CPU: the probe always returns the same value -> never "advances".
    monkeypatch.setattr(du, "_proc_cpu_seconds", lambda pid: 5.0)
    # Tiny timeout + a fast monotonic clock so the flat window elapses on the
    # next tick deterministically (no reliance on wall-clock spinning).
    monkeypatch.setattr(du, "STALL_TIMEOUT_SECONDS", 1)
    _no_real_sleep(monkeypatch)

    with caplog.at_level(logging.ERROR, logger=du.logger.name):
        files, errors = du.sync_payload(scanned)

    assert files == 0
    assert any("stalled" in e for e in errors), errors
    assert any("stalled" in rec.getMessage() for rec in caplog.records)


def test_sync_payload_scan_phase_cpu_advancing_not_stalled(env, monkeypatch):
    """SCAN PHASE (the bug being fixed): stdout is silent but CPU keeps
    advancing -> NOT a stall; rsync is allowed to complete cleanly.

    Simulates a large incremental refresh whose file-list build / --delete
    reconciliation scan emits nothing on stdout for longer than the timeout.
    """
    import subprocess

    scanned = _job(env, "scanned")

    # rsync stays "alive" for several polls, longer than the (tiny) timeout
    # would allow if only the silent stdout were considered, then exits 0.
    def fake_popen(cmd, stdout=None, stderr=None, **k):
        return _FakeProc(alive_polls=6, exit_code=0)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    # CPU advances on every probe -> liveness even though stdout never grows.
    cpu = {"t": 0.0}

    def advancing_cpu(pid):
        cpu["t"] += 1.0
        return cpu["t"]

    monkeypatch.setattr(du, "_proc_cpu_seconds", advancing_cpu)
    # Never let the poll actually sleep a real second.
    monkeypatch.setattr(du, "STALL_TIMEOUT_SECONDS", 1)
    _no_real_sleep(monkeypatch)

    files, errors = du.sync_payload(scanned)

    # No stall error: the sync completed (exit 0) despite the silent scan.
    assert not any("stalled" in e for e in errors), errors
    assert files == 0  # empty stdout -> nothing counted, but importantly no stall


def test_sync_payload_transfer_growing_stdout_not_stalled(env, monkeypatch, tmp_path):
    """Normal transfer: stdout log grows -> not stalled even with flat CPU."""
    import subprocess

    scanned = _job(env, "scanned")

    growing = {"n": 0}

    def fake_popen(cmd, stdout=None, stderr=None, **k):
        return _FakeProc(alive_polls=6, exit_code=0)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    # Flat CPU probe: if the watchdog relied on CPU it would stall; it must not.
    monkeypatch.setattr(du, "_proc_cpu_seconds", lambda pid: 5.0)

    # os.path.getsize reports an ever-growing log -> stdout-growth liveness.
    real_getsize = du.os.path.getsize

    def growing_getsize(path):
        growing["n"] += 100
        return growing["n"]

    monkeypatch.setattr(du.os.path, "getsize", growing_getsize)
    monkeypatch.setattr(du, "STALL_TIMEOUT_SECONDS", 1)
    _no_real_sleep(monkeypatch)

    files, errors = du.sync_payload(scanned)

    assert not any("stalled" in e for e in errors), errors


def _no_real_sleep(monkeypatch):
    """Make proc.wait(timeout=...) a no-op so the poll loop spins fast.

    The loop calls proc.wait(timeout=1) each tick; our _FakeProc raises
    TimeoutExpired for that. Combined with a monotonic clock that jumps past
    the (patched, tiny) STALL_TIMEOUT_SECONDS, the loop advances quickly while
    still exercising the real liveness decision.
    """
    # Advance monotonic by 2s per read so (now - last_progress) can exceed a
    # 1s timeout on the very next flat tick.
    clock = {"t": 0.0}
    real_monotonic = du.time.monotonic

    def fast_monotonic():
        clock["t"] += 2.0
        return clock["t"]

    monkeypatch.setattr(du.time, "monotonic", fast_monotonic)
