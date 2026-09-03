# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for verify_family() payload verification in drive_updater.

Covers Task 5 of the drive-sync-integrity spec:
  - build {relpath: size} maps for local and drive (excluding marker)
  - detect missing (on drive), extra (on drive), and size-mismatch
  - a clean tree returns no discrepancies

Requirements: 5.1, 5.2, 5.4, 6.1
"""

import os

import pytest

from efis_data_manager import drive_updater as du


def _write(path, content=b"x"):
    """Create a file with the given bytes, making parent dirs as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)


@pytest.fixture
def family_env(tmp_path, monkeypatch):
    """Build a local image root + drive mount and pin config to the local root.

    Returns (local_root, mount) as strings.
    """
    local_root = tmp_path / "local"
    mount = tmp_path / "mount"
    local_root.mkdir()
    mount.mkdir()
    monkeypatch.setattr(
        du, "load_config", lambda: {"usb_image_path": str(local_root)}
    )
    return str(local_root), str(mount)


# --- scanned (tree) family, clean ------------------------------------------


def _populate_scanned(local_root, mount, *, drive=True):
    """Populate a small ChartData tree on local (and optionally the drive).

    Includes the ScannedCharts.sqlite marker and a Plates/ subtree so the test
    can confirm both are excluded from the scanned payload comparison.
    """
    layout = {
        os.path.join("ChartData", "SEC", "a.tif"): b"aaaa",
        os.path.join("ChartData", "LO", "b.tif"): b"bbbbbb",
        os.path.join("ChartData", "HI", "c.tif"): b"cc",
    }
    for rel, content in layout.items():
        _write(os.path.join(local_root, rel), content)
        if drive:
            _write(os.path.join(mount, rel), content)

    # Marker present on both — must be excluded from payload compare.
    _write(os.path.join(local_root, "ChartData", "ScannedCharts.sqlite"), b"MARKER-LOCAL")
    if drive:
        _write(os.path.join(mount, "ChartData", "ScannedCharts.sqlite"), b"marker-drive-different-size")

    # Plates subtree present on both — owned by plates family, excluded here.
    _write(os.path.join(local_root, "ChartData", "Plates", "p.pdf"), b"plate")
    if drive:
        _write(os.path.join(mount, "ChartData", "Plates", "p.pdf"), b"DIFFERENT")


def test_clean_scanned_tree_has_no_discrepancies(family_env):
    local_root, mount = family_env
    _populate_scanned(local_root, mount, drive=True)

    scanned = next(j for j in du.build_jobs(mount) if j.name == "scanned")
    result = du.verify_family(scanned, mount)

    assert result == {"missing": [], "extra": [], "size_mismatch": []}


def test_scanned_excludes_marker_and_plates(family_env):
    """A marker/Plates size difference must NOT show up as a discrepancy."""
    local_root, mount = family_env
    _populate_scanned(local_root, mount, drive=True)
    # Marker and Plates differ in size across sides by construction above; the
    # scanned payload compare must ignore both.
    scanned = next(j for j in du.build_jobs(mount) if j.name == "scanned")
    result = du.verify_family(scanned, mount)
    assert result["size_mismatch"] == []
    # The marker relpath must never appear.
    assert "ScannedCharts.sqlite" not in result["size_mismatch"]


# --- missing / extra / size mismatch ---------------------------------------


def test_detects_missing_on_drive(family_env):
    local_root, mount = family_env
    _populate_scanned(local_root, mount, drive=True)
    # Remove one file from the drive.
    os.remove(os.path.join(mount, "ChartData", "LO", "b.tif"))

    scanned = next(j for j in du.build_jobs(mount) if j.name == "scanned")
    result = du.verify_family(scanned, mount)

    assert result["missing"] == [os.path.join("LO", "b.tif")]
    assert result["extra"] == []
    assert result["size_mismatch"] == []


def test_detects_extra_on_drive(family_env):
    local_root, mount = family_env
    _populate_scanned(local_root, mount, drive=True)
    # Add a file only on the drive.
    _write(os.path.join(mount, "ChartData", "SEC", "orphan.tif"), b"z")

    scanned = next(j for j in du.build_jobs(mount) if j.name == "scanned")
    result = du.verify_family(scanned, mount)

    assert result["extra"] == [os.path.join("SEC", "orphan.tif")]
    assert result["missing"] == []
    assert result["size_mismatch"] == []


def test_detects_size_mismatch(family_env):
    local_root, mount = family_env
    _populate_scanned(local_root, mount, drive=True)
    # Rewrite a drive file with a different size.
    _write(os.path.join(mount, "ChartData", "HI", "c.tif"), b"cccccccc")

    scanned = next(j for j in du.build_jobs(mount) if j.name == "scanned")
    result = du.verify_family(scanned, mount)

    assert result["size_mismatch"] == [os.path.join("HI", "c.tif")]
    assert result["missing"] == []
    assert result["extra"] == []


def test_detects_combination(family_env):
    local_root, mount = family_env
    _populate_scanned(local_root, mount, drive=True)
    os.remove(os.path.join(mount, "ChartData", "LO", "b.tif"))  # missing
    _write(os.path.join(mount, "ChartData", "extra.tif"), b"e")  # extra
    _write(os.path.join(mount, "ChartData", "HI", "c.tif"), b"cccc")  # size mismatch

    scanned = next(j for j in du.build_jobs(mount) if j.name == "scanned")
    result = du.verify_family(scanned, mount)

    assert result["missing"] == [os.path.join("LO", "b.tif")]
    assert result["extra"] == ["extra.tif"]
    assert result["size_mismatch"] == [os.path.join("HI", "c.tif")]


def test_missing_drive_root_reports_all_local_as_missing(family_env):
    """If the drive family root is absent entirely, all local files are missing."""
    local_root, mount = family_env
    _populate_scanned(local_root, mount, drive=False)  # local only

    scanned = next(j for j in du.build_jobs(mount) if j.name == "scanned")
    result = du.verify_family(scanned, mount)

    assert set(result["missing"]) == {
        os.path.join("SEC", "a.tif"),
        os.path.join("LO", "b.tif"),
        os.path.join("HI", "c.tif"),
    }
    assert result["extra"] == []
    assert result["size_mismatch"] == []


# --- plates family ----------------------------------------------------------


def test_plates_family_clean(family_env):
    local_root, mount = family_env
    for side in (local_root, mount):
        _write(os.path.join(side, "ChartData", "Plates", "KABC.pdf"), b"abc")
        _write(os.path.join(side, "ChartData", "Plates", "KXYZ.pdf"), b"xyz")
    # Marker present, differing size — must be excluded.
    _write(os.path.join(local_root, "ChartData", "Plates", "Plates.sqlite"), b"L")
    _write(os.path.join(mount, "ChartData", "Plates", "Plates.sqlite"), b"DDDD")

    plates = next(j for j in du.build_jobs(mount) if j.name == "plates")
    result = du.verify_family(plates, mount)

    assert result == {"missing": [], "extra": [], "size_mismatch": []}


# --- nav (files) family -----------------------------------------------------


def test_nav_family_size_mismatch_and_missing(family_env):
    local_root, mount = family_env
    _write(os.path.join(local_root, "NAV.DB"), b"navdata-1234")
    _write(os.path.join(mount, "NAV.DB"), b"navdata-DIFFERENT-SIZE")
    # NAV-proc.DB present locally only -> missing on drive.
    _write(os.path.join(local_root, "NAV-proc.DB"), b"proc")

    nav = next(j for j in du.build_jobs(mount) if j.name == "nav")
    result = du.verify_family(nav, mount)

    assert result["size_mismatch"] == ["NAV.DB"]
    assert result["missing"] == ["NAV-proc.DB"]
    assert result["extra"] == []


def test_nav_family_clean_ignores_absent_optional_file(family_env):
    """NAV-proc.DB absent on both sides is fine; NAV.DB matches -> clean."""
    local_root, mount = family_env
    _write(os.path.join(local_root, "NAV.DB"), b"navdata")
    _write(os.path.join(mount, "NAV.DB"), b"navdata")

    nav = next(j for j in du.build_jobs(mount) if j.name == "nav")
    result = du.verify_family(nav, mount)

    assert result == {"missing": [], "extra": [], "size_mismatch": []}


# --- deep verify (opt-in content hashing) -----------------------------------


def test_deep_verify_detects_same_size_content_change(family_env):
    """deep=True surfaces a same-size content change as a size_mismatch."""
    local_root, mount = family_env
    _populate_scanned(local_root, mount, drive=True)
    # Overwrite a drive file with same-size but different content.
    _write(os.path.join(mount, "ChartData", "SEC", "a.tif"), b"AAAA")  # same len as b"aaaa"

    scanned = next(j for j in du.build_jobs(mount) if j.name == "scanned")

    shallow = du.verify_family(scanned, mount, deep=False)
    assert shallow == {"missing": [], "extra": [], "size_mismatch": []}

    deep = du.verify_family(scanned, mount, deep=True)
    assert deep["size_mismatch"] == [os.path.join("SEC", "a.tif")]
