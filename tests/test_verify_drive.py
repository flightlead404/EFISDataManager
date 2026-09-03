# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for verify_drive() exhaustive verification + repair.

Covers Task 8 of the drive-sync-integrity spec and design.md Testing strategy
item 4 (verify count+size, then repair):

  - verify_drive aggregates verify_family per family and reports discrepancies
    (missing / extra / size-mismatch)
  - a clean drive reports clean=True with empty per-family discrepancies
  - repair=True re-runs the discrepant family (idempotent rsync convergence),
    re-verifies clean, and clears the family's interrupted-sync record
  - a family that was already clean is not listed in "repaired"
  - deep=True surfaces a same-size content change

Requirements: 6.1, 6.2, 6.4, 6.5

rsync runs against real temp dirs; the mount presence/writability preflight is
mocked (temp dirs are not real mount points). No physical USB is needed.
"""

import os
import shutil

import pytest

from efis_data_manager import drive_updater as du


pytestmark = pytest.mark.skipif(
    shutil.which("rsync") is None, reason="rsync not available on PATH"
)


def _write(path, content=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
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
    monkeypatch.setattr(
        du, "LEGACY_SYNC_MARKER_PATH", str(state_dir / ".sync_in_progress")
    )
    return state_dir


@pytest.fixture
def env(tmp_path, monkeypatch, state_paths):
    """A local image with all three families and a drive pre-synced clean."""
    local = tmp_path / "image"
    drive = tmp_path / "drive"
    base_mtime = 1_000_000

    # scanned family payload + marker
    _write(str(local / "ChartData" / "LO" / "a.png"), b"aaaa")
    _write(str(local / "ChartData" / "SEC" / "b.png"), b"bbbb")
    _write(str(local / "ChartData" / "ScannedCharts.sqlite"), b"scanned-db")
    os.utime(str(local / "ChartData" / "ScannedCharts.sqlite"), (base_mtime, base_mtime))

    # plates family payload + marker
    _write(str(local / "ChartData" / "Plates" / "p1.pdf"), b"plate")
    _write(str(local / "ChartData" / "Plates" / "Plates.sqlite"), b"plates-db")
    os.utime(
        str(local / "ChartData" / "Plates" / "Plates.sqlite"), (base_mtime, base_mtime)
    )

    # nav family files
    _write(str(local / "NAV.DB"), b"nav-content")

    drive.mkdir()

    monkeypatch.setattr(du, "load_config", lambda: {"usb_image_path": str(local)})

    return {"local": local, "drive": drive, "mount": str(drive)}


def _sync_all(env):
    """Run every family job so the drive starts fully synced and current."""
    for job in du.build_jobs(env["mount"]):
        result = du.run_sync_job(job, env["mount"])
        assert result.status in ("updated", "current")


# --- clean drive -------------------------------------------------------------


def test_clean_drive_reports_no_discrepancies(env):
    _sync_all(env)

    report = du.verify_drive(env["mount"])

    assert report["clean"] is True
    assert report["repaired"] == []
    assert report["errors"] == []
    for name in ("scanned", "plates", "nav"):
        assert report["families"][name] == {
            "missing": [],
            "extra": [],
            "size_mismatch": [],
        }


# --- discrepancy detection (missing / extra / size mismatch) ----------------


def test_detects_missing_extra_and_size_mismatch(env):
    _sync_all(env)

    # Introduce one of each discrepancy in the scanned family on the drive.
    os.remove(str(env["drive"] / "ChartData" / "LO" / "a.png"))  # missing
    _write(str(env["drive"] / "ChartData" / "SEC" / "orphan.png"), b"z")  # extra
    _write(str(env["drive"] / "ChartData" / "SEC" / "b.png"), b"bbbbbbbb")  # size

    report = du.verify_drive(env["mount"])

    assert report["clean"] is False
    scanned = report["families"]["scanned"]
    assert scanned["missing"] == [os.path.join("LO", "a.png")]
    assert scanned["extra"] == [os.path.join("SEC", "orphan.png")]
    assert scanned["size_mismatch"] == [os.path.join("SEC", "b.png")]
    # Other families untouched -> clean.
    assert report["families"]["plates"] == {
        "missing": [],
        "extra": [],
        "size_mismatch": [],
    }


# --- repair: re-run converges the drive and clears interrupted state --------


def test_repair_fixes_discrepancies_and_reports_clean(env):
    _sync_all(env)

    # Corrupt the scanned family three ways.
    os.remove(str(env["drive"] / "ChartData" / "LO" / "a.png"))  # missing
    _write(str(env["drive"] / "ChartData" / "SEC" / "orphan.png"), b"z")  # extra
    _write(str(env["drive"] / "ChartData" / "SEC" / "b.png"), b"bbbbbbbb")  # size

    report = du.verify_drive(env["mount"], repair=True)

    assert report["clean"] is True
    assert "scanned" in report["repaired"]
    assert report["errors"] == []

    # Post-repair the drive tree matches the local image exactly.
    assert (env["drive"] / "ChartData" / "LO" / "a.png").read_bytes() == b"aaaa"
    assert (env["drive"] / "ChartData" / "SEC" / "b.png").read_bytes() == b"bbbb"
    assert not (env["drive"] / "ChartData" / "SEC" / "orphan.png").exists()

    # Re-verify authoritatively reported clean for the repaired family.
    assert report["families"]["scanned"] == {
        "missing": [],
        "extra": [],
        "size_mismatch": [],
    }


def test_repair_clears_interrupted_sync_record(env):
    _sync_all(env)

    # Simulate an interrupted scanned sync: an interrupted-sync record exists
    # for this mount and the drive is missing a payload file.
    du.begin_family(env["mount"], "scanned")
    os.remove(str(env["drive"] / "ChartData" / "LO" / "a.png"))
    assert "scanned" in du.pending_families(env["mount"])

    report = du.verify_drive(env["mount"], repair=True)

    assert report["clean"] is True
    # Req 6.5: a clean repair clears the interrupted-sync record.
    assert du.pending_families(env["mount"]) == []
    # And the quick check now trusts the drive.
    currency = du.check_drive_currency(env["mount"])
    assert currency["families"]["scanned"]["current"] is True


def test_repair_skips_already_clean_families(env):
    _sync_all(env)

    # Only break plates; scanned and nav stay clean.
    os.remove(str(env["drive"] / "ChartData" / "Plates" / "p1.pdf"))

    report = du.verify_drive(env["mount"], repair=True)

    assert report["clean"] is True
    assert report["repaired"] == ["plates"]
    # p1.pdf restored by the re-run.
    assert (env["drive"] / "ChartData" / "Plates" / "p1.pdf").read_bytes() == b"plate"


# --- subset selection --------------------------------------------------------


def test_families_subset_only_verifies_requested(env):
    _sync_all(env)
    os.remove(str(env["drive"] / "ChartData" / "LO" / "a.png"))  # break scanned

    # Verify only nav -> scanned breakage is out of scope, so report is clean.
    report = du.verify_drive(env["mount"], families=["nav"])

    assert set(report["families"]) == {"nav"}
    assert report["clean"] is True


# --- deep verify -------------------------------------------------------------


def test_deep_verify_detects_same_size_content_change(env):
    _sync_all(env)
    # Same-size overwrite: invisible to count+size, caught only by deep hash.
    _write(str(env["drive"] / "ChartData" / "LO" / "a.png"), b"AAAA")

    shallow = du.verify_drive(env["mount"], deep=False)
    assert shallow["families"]["scanned"]["size_mismatch"] == []
    assert shallow["clean"] is True

    deep = du.verify_drive(env["mount"], deep=True)
    assert deep["families"]["scanned"]["size_mismatch"] == [os.path.join("LO", "a.png")]
    assert deep["clean"] is False
