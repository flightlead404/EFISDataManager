# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the quick per-family currency check in drive_updater.

Covers Task 3 of the drive-sync-integrity spec:
  - per-family marker-based check for scanned + plates (mtime + size +
    structural non-emptiness)
  - checksum-based check for nav
  - a pending (interrupted) family is forced not-current

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5
"""

import os

import pytest

from efis_data_manager import drive_updater as du


# ---------------------------------------------------------------------------
# Fixtures: build a realistic local image + drive under temp dirs, both
# fully current, then let individual tests perturb one family at a time.
# ---------------------------------------------------------------------------


def _write(path, data=b"x", mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def state_paths(tmp_path, monkeypatch):
    """Redirect sync-state paths so pending_families is isolated per test."""
    state_dir = tmp_path / "DataManagerLogs"
    state_dir.mkdir()
    monkeypatch.setattr(du, "SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setattr(du, "SYNC_STATE_PATH", str(state_dir / ".sync_state.json"))
    monkeypatch.setattr(du, "LEGACY_SYNC_MARKER_PATH", str(state_dir / ".sync_in_progress"))
    return state_dir


@pytest.fixture
def env(tmp_path, monkeypatch, state_paths):
    """A local image and a drive that are fully current across all families."""
    local = tmp_path / "image"
    drive = tmp_path / "drive"

    base_mtime = 1_000_000

    # scanned family: ChartData/{LO,SEC}/tile + marker
    _write(str(local / "ChartData" / "LO" / "a.png"))
    _write(str(local / "ChartData" / "SEC" / "b.png"))
    _write(str(drive / "ChartData" / "LO" / "a.png"))
    _write(str(drive / "ChartData" / "SEC" / "b.png"))
    _write(str(local / "ChartData" / "ScannedCharts.sqlite"), b"scanned-db", mtime=base_mtime)
    _write(str(drive / "ChartData" / "ScannedCharts.sqlite"), b"scanned-db", mtime=base_mtime)

    # plates family: ChartData/Plates/plate + marker
    _write(str(local / "ChartData" / "Plates" / "p1.pdf"))
    _write(str(drive / "ChartData" / "Plates" / "p1.pdf"))
    _write(str(local / "ChartData" / "Plates" / "Plates.sqlite"), b"plates-db", mtime=base_mtime)
    _write(str(drive / "ChartData" / "Plates" / "Plates.sqlite"), b"plates-db", mtime=base_mtime)

    # nav family: NAV.DB (+ NAV-proc.DB) identical content
    _write(str(local / "NAV.DB"), b"nav-content")
    _write(str(drive / "NAV.DB"), b"nav-content")
    _write(str(local / "NAV-proc.DB"), b"navproc-content")
    _write(str(drive / "NAV-proc.DB"), b"navproc-content")

    monkeypatch.setattr(du, "load_config", lambda: {"usb_image_path": str(local)})

    return {
        "local": local,
        "drive": drive,
        "mount": str(drive),
        "base_mtime": base_mtime,
    }


# --- baseline: everything current -------------------------------------------


def test_all_families_current(env):
    result = du.check_drive_currency(env["mount"])

    assert result["is_current"] is True
    assert result["stale_items"] == []
    assert set(result["families"]) == {"scanned", "plates", "nav"}
    assert all(f["current"] for f in result["families"].values())


# --- Req 3.1/3.3: scanned current while plates stale ------------------------


def test_scanned_current_while_plates_stale(env):
    # Make the local plates marker newer than the drive's -> plates stale,
    # scanned untouched -> scanned still current.
    newer = env["base_mtime"] + 10_000
    os.utime(str(env["local"] / "ChartData" / "Plates" / "Plates.sqlite"), (newer, newer))

    result = du.check_drive_currency(env["mount"])

    assert result["is_current"] is False
    assert result["families"]["scanned"]["current"] is True
    assert result["families"]["plates"]["current"] is False
    assert result["families"]["nav"]["current"] is True
    assert any("plates" in s for s in result["stale_items"])


def test_scanned_marker_size_mismatch_marks_scanned_stale(env):
    # Same mtime but different size -> stale on size.
    _write(
        str(env["drive"] / "ChartData" / "ScannedCharts.sqlite"),
        b"scanned-db-CHANGED-SIZE",
        mtime=env["base_mtime"],
    )
    result = du.check_drive_currency(env["mount"])
    assert result["families"]["scanned"]["current"] is False
    assert "size" in result["families"]["scanned"]["reason"]


def test_scanned_missing_marker_marks_scanned_stale(env):
    os.remove(str(env["drive"] / "ChartData" / "ScannedCharts.sqlite"))
    result = du.check_drive_currency(env["mount"])
    assert result["families"]["scanned"]["current"] is False
    assert "missing" in result["families"]["scanned"]["reason"]


def test_scanned_empty_structure_marks_stale(env):
    # Marker matches but a required structure dir (SEC/) is empty -> stale.
    os.remove(str(env["drive"] / "ChartData" / "SEC" / "b.png"))
    result = du.check_drive_currency(env["mount"])
    assert result["families"]["scanned"]["current"] is False
    assert "SEC" in result["families"]["scanned"]["reason"]


def test_drive_marker_newer_is_still_current(env):
    # Drive marker newer than local (within/over tolerance) -> current: we only
    # care that the drive is not OLDER than local.
    newer = env["base_mtime"] + 10_000
    os.utime(str(env["drive"] / "ChartData" / "ScannedCharts.sqlite"), (newer, newer))
    result = du.check_drive_currency(env["mount"])
    assert result["families"]["scanned"]["current"] is True


def test_modify_window_absorbs_small_drift(env):
    # Drive marker 1s older than local -> within the FAT tolerance -> current.
    slightly_older = env["base_mtime"] - 1
    os.utime(
        str(env["drive"] / "ChartData" / "ScannedCharts.sqlite"),
        (slightly_older, slightly_older),
    )
    result = du.check_drive_currency(env["mount"])
    assert result["families"]["scanned"]["current"] is True


# --- Req 3.2: nav checksum ---------------------------------------------------


def test_nav_checksum_mismatch_marks_nav_stale(env):
    # Same size, different content -> checksum mismatch.
    _write(str(env["drive"] / "NAV.DB"), b"NAV-content")  # same length, diff bytes
    assert os.path.getsize(str(env["drive"] / "NAV.DB")) == os.path.getsize(
        str(env["local"] / "NAV.DB")
    )
    result = du.check_drive_currency(env["mount"])
    assert result["families"]["nav"]["current"] is False
    assert "checksum" in result["families"]["nav"]["reason"]


def test_nav_missing_on_drive_marks_nav_stale(env):
    os.remove(str(env["drive"] / "NAV.DB"))
    result = du.check_drive_currency(env["mount"])
    assert result["families"]["nav"]["current"] is False
    assert "NAV.DB" in result["families"]["nav"]["reason"]


def test_nav_proc_optional_when_absent_locally(env):
    # If NAV-proc.DB is absent locally, its absence on the drive is irrelevant.
    os.remove(str(env["local"] / "NAV-proc.DB"))
    os.remove(str(env["drive"] / "NAV-proc.DB"))
    result = du.check_drive_currency(env["mount"])
    assert result["families"]["nav"]["current"] is True


# --- Req 3.5: interrupted marker forces not-current -------------------------


def test_interrupted_marker_forces_not_current(env, monkeypatch):
    # Everything is physically current, but a recorded interrupted sync for the
    # scanned family must force it not-current regardless of marker state.
    # The sync-state is keyed by the resolved drive id (task 15), so pin
    # resolve_drive_id to a stable id and record the interrupted family under it.
    monkeypatch.setattr(du, "resolve_drive_id", lambda mp: "id-THIS")
    du.begin_family("id-THIS", "scanned")

    result = du.check_drive_currency(env["mount"])

    assert result["is_current"] is False
    assert result["families"]["scanned"]["current"] is False
    assert "interrupted" in result["families"]["scanned"]["reason"]
    # Untouched families remain current.
    assert result["families"]["plates"]["current"] is True
    assert result["families"]["nav"]["current"] is True


def test_interrupted_marker_for_other_drive_does_not_affect(env, monkeypatch):
    # An interrupted record for a DIFFERENT drive id must never bleed into this
    # drive's currency (Property 6). This drive resolves to id-THIS; the record
    # is under id-OTHER.
    monkeypatch.setattr(du, "resolve_drive_id", lambda mp: "id-THIS")
    du.begin_family("id-OTHER", "scanned")
    result = du.check_drive_currency(env["mount"])
    assert result["is_current"] is True
    assert result["families"]["scanned"]["current"] is True


def test_unresolved_drive_id_applies_no_interrupted_state(env, monkeypatch):
    # Fail-safe (Req 10.7): when the drive id cannot be resolved, NO interrupted
    # state is applied to this drive — currency falls back to the marker check
    # (everything here is physically current, so the drive reads current).
    monkeypatch.setattr(du, "resolve_drive_id", lambda mp: None)
    du.begin_family("id-SOMETHING", "scanned")
    result = du.check_drive_currency(env["mount"])
    assert result["is_current"] is True
    assert result["families"]["scanned"]["current"] is True
