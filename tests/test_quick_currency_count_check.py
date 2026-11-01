# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the cheap count cross-check in the quick drive-currency check.

Covers the quick-currency-count-size-check bugfix: for the chart families
(scanned, plates) the quick currency check compares the drive's actual
manifest-eligible file count against the LOCAL manifest's entry count and forces
not-current on any mismatch, BEFORE trusting the commit marker.

  - Core regression (2.1/2.2): a family with a matching commit marker but fewer
    payload files than the local manifest is reported NOT current (fast).
  - Count-match passes through (2.3): an equal count (with a .DS_Store and the
    commit marker also on the drive, both excluded) does NOT by itself reject —
    the result comes from the marker logic.
  - Local manifest missing (2.4): fail safe -> family not-current.
  - Nav unaffected (3.2): the nav family's checksum logic is untouched.

Requirements: 2.1, 2.1a, 2.2, 2.3, 2.4, 3.2, 3.4
"""

import os

import pytest

from efis_data_manager import drive_updater as du
from efis_data_manager import manifest as mf


def _write(path, data=b"x", mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _entry(rel, size=1):
    return mf.ManifestEntry(relpath=rel, sha256="0" * 64, size=size)


def _save_local(family, relpaths):
    """Persist a local manifest for ``family`` with one entry per relpath."""
    mf.Manifest_Store.save_local(
        mf.FamilyManifest(
            family=family,
            cycle=None,
            entries={rel: _entry(rel) for rel in relpaths},
        )
    )


@pytest.fixture
def state_paths(tmp_path, monkeypatch):
    """Redirect sync-state paths so pending_families is isolated per test."""
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
    """A local image + drive with matching chart commit markers.

    The marker tier passes for scanned and plates; individual tests set up the
    local manifests and drive payloads to exercise the count tier. The local
    manifest store is redirected to a tmp dir so nothing touches the real
    app-support manifests.
    """
    local = tmp_path / "image"
    drive = tmp_path / "drive"
    base_mtime = 1_000_000

    # scanned structure dirs (LO/SEC) + matching marker on both sides.
    _write(str(local / "ChartData" / "LO" / "a.png"))
    _write(str(local / "ChartData" / "SEC" / "b.png"))
    _write(str(drive / "ChartData" / "LO" / "a.png"))
    _write(str(drive / "ChartData" / "SEC" / "b.png"))
    _write(
        str(local / "ChartData" / "ScannedCharts.sqlite"), b"scanned-db", mtime=base_mtime
    )
    _write(
        str(drive / "ChartData" / "ScannedCharts.sqlite"), b"scanned-db", mtime=base_mtime
    )

    # plates payload + matching marker on both sides.
    _write(str(local / "ChartData" / "Plates" / "p1.pdf"))
    _write(str(drive / "ChartData" / "Plates" / "p1.pdf"))
    _write(
        str(local / "ChartData" / "Plates" / "Plates.sqlite"),
        b"plates-db",
        mtime=base_mtime,
    )
    _write(
        str(drive / "ChartData" / "Plates" / "Plates.sqlite"),
        b"plates-db",
        mtime=base_mtime,
    )

    # nav: identical on both sides.
    _write(str(local / "NAV.DB"), b"nav-content")
    _write(str(drive / "NAV.DB"), b"nav-content")
    _write(str(local / "NAV-proc.DB"), b"navproc-content")
    _write(str(drive / "NAV-proc.DB"), b"navproc-content")

    monkeypatch.setattr(du, "load_config", lambda: {"usb_image_path": str(local)})

    manifest_dir = tmp_path / "manifests"
    monkeypatch.setattr(mf, "LOCAL_MANIFEST_DIR", manifest_dir)

    return {
        "local": local,
        "drive": drive,
        "mount": str(drive),
        "base_mtime": base_mtime,
        "manifest_dir": manifest_dir,
    }


# --- 2.1/2.2: core regression -----------------------------------------------


def test_scanned_short_count_with_matching_marker_is_not_current(env):
    # Local manifest expects 3 scanned files, drive only has 2 payload files,
    # but the commit marker matches. Old behavior trusted the marker and
    # reported current; the count tier must force not-current.
    _save_local("scanned", ["LO/a.png", "SEC/b.png", "SEC/c.png"])
    # plates kept legitimately current so only scanned trips.
    _save_local("plates", ["p1.pdf"])

    result = du.check_drive_currency(env["mount"])

    assert result["families"]["scanned"]["current"] is False
    assert "count mismatch" in result["families"]["scanned"]["reason"]
    assert "drive=2" in result["families"]["scanned"]["reason"]
    assert "expected=3" in result["families"]["scanned"]["reason"]
    assert result["is_current"] is False


def test_plates_short_count_with_matching_marker_is_not_current(env):
    # The real-world defect: plates marker matches but the drive is missing
    # thousands of tiles. Here the manifest expects 3 plate files; the drive has 1.
    _save_local("scanned", ["LO/a.png", "SEC/b.png"])
    _save_local("plates", ["p1.pdf", "p2.pdf", "p3.pdf"])

    result = du.check_drive_currency(env["mount"])

    assert result["families"]["plates"]["current"] is False
    assert "count mismatch" in result["families"]["plates"]["reason"]
    assert "drive=1" in result["families"]["plates"]["reason"]
    assert "expected=3" in result["families"]["plates"]["reason"]
    # scanned legitimately current -> count tier leaves marker result standing.
    assert result["families"]["scanned"]["current"] is True


# --- 2.1a / 2.3: count-match passes through, excludes applied ---------------


def test_count_match_with_extra_metadata_and_marker_passes_through(env):
    # Drive payload count equals the local manifest, and the drive ALSO carries
    # a .DS_Store and the commit marker. Both must be excluded from the count so
    # the comparison is exact, and an equal count must NOT by itself reject: the
    # family stays current from the marker logic (2.3).
    _save_local("scanned", ["LO/a.png", "SEC/b.png"])
    _save_local("plates", ["p1.pdf"])
    # Excluded noise on the drive that a naive walk would miscount.
    _write(str(env["drive"] / "ChartData" / ".DS_Store"), b"junk")
    _write(str(env["drive"] / "ChartData" / "LO" / ".DS_Store"), b"junk")
    _write(str(env["drive"] / "ChartData" / "Plates" / ".DS_Store"), b"junk")
    _write(str(env["drive"] / "ChartData" / "._a.png"), b"applesidecar")

    result = du.check_drive_currency(env["mount"])

    assert result["families"]["scanned"]["current"] is True
    assert result["families"]["plates"]["current"] is True
    assert result["is_current"] is True


def test_scanned_count_ignores_plates_subtree(env):
    # scanned is rooted at ChartData but must exclude the Plates/ subtree. Adding
    # more plate files must NOT change the scanned count. Give plates a matching
    # manifest so only the scanned exclusion is under test.
    _save_local("scanned", ["LO/a.png", "SEC/b.png"])
    _write(str(env["drive"] / "ChartData" / "Plates" / "p2.pdf"))
    _write(str(env["drive"] / "ChartData" / "Plates" / "p3.pdf"))
    _save_local("plates", ["p1.pdf", "p2.pdf", "p3.pdf"])

    result = du.check_drive_currency(env["mount"])

    # scanned still sees exactly 2 (Plates/ excluded) -> count matches.
    assert result["families"]["scanned"]["current"] is True
    assert result["families"]["plates"]["current"] is True


# --- 2.4: local manifest missing -> fail safe -------------------------------


def test_missing_local_manifest_fails_safe_not_current(env):
    # No local manifest for scanned (plates has one and is current). The scanned
    # family must be routed to sync rather than trusting the marker.
    _save_local("plates", ["p1.pdf"])

    result = du.check_drive_currency(env["mount"])

    assert result["families"]["scanned"]["current"] is False
    assert "local manifest missing" in result["families"]["scanned"]["reason"]
    assert result["families"]["plates"]["current"] is True
    assert result["is_current"] is False


# --- 3.2: nav family unaffected by the count tier ---------------------------


def test_nav_unaffected_by_count_check(env):
    # Chart families current; nav currency still decided purely by its checksum
    # logic. A nav checksum mismatch trips nav, not the chart count tier.
    _save_local("scanned", ["LO/a.png", "SEC/b.png"])
    _save_local("plates", ["p1.pdf"])
    _write(str(env["drive"] / "NAV.DB"), b"NAV-content")  # same size, diff bytes

    result = du.check_drive_currency(env["mount"])

    assert result["families"]["scanned"]["current"] is True
    assert result["families"]["plates"]["current"] is True
    assert result["families"]["nav"]["current"] is False
    assert "checksum" in result["families"]["nav"]["reason"]


# --- direct helper coverage -------------------------------------------------


def test_helper_mismatch_and_match_and_missing(env):
    _save_local("scanned", ["LO/a.png", "SEC/b.png"])
    matches, reason = du._chart_family_count_matches_local(env["mount"], "scanned")
    assert matches is True
    assert reason is None

    _save_local("scanned", ["LO/a.png", "SEC/b.png", "SEC/c.png"])
    matches, reason = du._chart_family_count_matches_local(env["mount"], "scanned")
    assert matches is False
    assert "count mismatch" in reason

    # Remove the manifest -> fail safe.
    os.remove(str(env["manifest_dir"] / "scanned.json"))
    matches, reason = du._chart_family_count_matches_local(env["mount"], "scanned")
    assert matches is False
    assert "local manifest missing" in reason
